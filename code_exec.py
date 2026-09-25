#!/usr/bin/env python3
"""
code_exec.py - apply a batch of file operations and shell commands, described
in a plain-text plan, to the current directory (the "project root").

Plan format (one instruction per line; content blocks follow the line):

    CREATE path                     <content block>
    EDIT path                       SEARCH <block>  REPLACE <block>
    INSERT_BEFORE|INSERT_AFTER path MARKER <block>  CONTENT <block>
    APPEND path                     <content block>
    PREPEND path                    <content block>
    DELETE path
    MKDIR path
    MOVE|COPY|RENAME src -> dst
    RUN shell command

A content block is either raw lines ended by a line `END_OF_FILE`, or
lines wrapped in `<<<` / `>>>`.
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from code_exec_ui import ui

# Re-export types, constants, and errors for backward compatibility
from code_exec_types import (
    ALLOWED_RUN_COMMAND_PREFIXES,
    COMMANDS,
    DEFAULT_RUN_TIMEOUT,
    FORBIDDEN_RUN_SUBSTRINGS,
    INTERACTIVE_ONLY_PREFIXES,
    PROTECTED_FILE_EXACT,
    PROTECTED_NAMES,
    PROTECTED_SUFFIXES,
    SHELL_CHAINING_OPERATORS,
    CommandFailed,
    FuzzyCandidate,
    MatchResult,
    OpError,
    Operation,
    ROOT,
    Unverifiable,
    clean_path,
)

# Re-export sandbox and validation
from code_exec_sandbox import (
    build_sandboxed_command,
    validate_run_command,
)

# Re-export matching engine
from code_exec_matcher import (
    CONFIDENT_THRESHOLD,
    RESOLVER_THRESHOLD,
    _adjust_indentation,
    _find_closest_match,
    _find_high_similarity_candidates,
    _find_high_similarity_match,
    _get_leading_indent,
    _match_line_spans,
    _normalize_jsx_line,
    _semantic_similarity,
    _strip_symbols_and_emojis,
    apply_unified_patch,
    find_all,
    find_unique,
    parse_unified_diff,
)

# Re-export parser and clipboard
from code_exec_parser import (
    expect_keyword,
    extract_plan,
    get_clipboard,
    parse_operations,
    read_block,
    set_clipboard,
    set_clipboard_file,
)

# Re-export filesystems and path/text helpers
from code_exec_fs import (
    BACKUP_ROOT,
    NEW_FILE_MODE,
    RealFS,
    VirtualFS,
    _remove,
    atomic_write,
    newline_style,
    read_text,
    rel,
    safe_path,
    undo_last_run,
    with_newlines,
)

# Audit log of fuzzy match resolutions applied in the current run
AUDIT_FUZZY_RESOLUTIONS: list[tuple[str, tuple[int, int] | None, float]] = []


def validate_fuzzy_replacement_safety(
    target: str,
    old_doc: str,
    new_doc: str,
    match: MatchResult,
) -> None:
    """
    🛡️ Guardrails Pre-Validation:
    Verifies that a fuzzy replacement does not corrupt file syntax or balance.
    Checks:
    - Python AST parsing for .py/.pyi files
    - JSON parsing for .json files
    - Bracket/brace balance for JS/TS/C/Go/Rust
    """
    target_lower = target.lower()

    # 1. Python AST verification
    if target_lower.endswith((".py", ".pyi")):
        import ast
        try:
            ast.parse(new_doc, filename=target)
        except SyntaxError as new_exc:
            is_old_valid = True
            try:
                ast.parse(old_doc, filename=target)
            except SyntaxError:
                is_old_valid = False
            if is_old_valid:
                line_info = f"lines {match.line_range[0] + 1}-{match.line_range[1] + 1}" if match.line_range else "span"
                raise OpError(
                    f"ERR|FUZZY_SYNTAX_ERROR|Fuzzy replacement in {target} ({line_info}) broke Python syntax: {new_exc.msg} (line {new_exc.lineno})"
                )

    # 2. JSON verification
    elif target_lower.endswith(".json"):
        import json
        try:
            json.loads(new_doc)
        except Exception as new_exc:
            is_old_valid = True
            try:
                json.loads(old_doc)
            except Exception:
                is_old_valid = False
            if is_old_valid:
                line_info = f"lines {match.line_range[0] + 1}-{match.line_range[1] + 1}" if match.line_range else "span"
                raise OpError(
                    f"ERR|FUZZY_SYNTAX_ERROR|Fuzzy replacement in {target} ({line_info}) broke JSON syntax: {new_exc}"
                )

    # 3. Bracket/brace balance verification
    elif target_lower.endswith((".js", ".jsx", ".ts", ".tsx", ".rs", ".go", ".c", ".cpp", ".java")):
        def _count_brackets(text: str) -> tuple[int, int, int]:
            s = re.sub(r"//.*$", "", text, flags=re.MULTILINE)
            s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)
            s = re.sub(r'"(?:\\.|[^"\\])*"', "", s)
            s = re.sub(r"'(?:\\.|[^'\\])*'", "", s)
            s = re.sub(r"`(?:\\.|[^`\\])*`", "", s)
            curly = s.count("{") - s.count("}")
            square = s.count("[") - s.count("]")
            paren = s.count("(") - s.count(")")
            return curly, square, paren

        old_b = _count_brackets(old_doc)
        new_b = _count_brackets(new_doc)
        if old_b == (0, 0, 0) and new_b != (0, 0, 0):
            diffs = []
            if new_b[0] != 0:
                diffs.append(f"curly brace {'unclosed' if new_b[0] > 0 else 'extra closing'} ({abs(new_b[0])})")
            if new_b[1] != 0:
                diffs.append(f"square bracket {'unclosed' if new_b[1] > 0 else 'extra closing'} ({abs(new_b[1])})")
            if new_b[2] != 0:
                diffs.append(f"parenthesis {'unclosed' if new_b[2] > 0 else 'extra closing'} ({abs(new_b[2])})")
            line_info = f"lines {match.line_range[0] + 1}-{match.line_range[1] + 1}" if match.line_range else "span"
            raise OpError(
                f"ERR|FUZZY_SYNTAX_ERROR|Fuzzy replacement in {target} ({line_info}) broke bracket balance: {', '.join(diffs)}"
            )


def interactive_fuzzy_resolver(target: str, needle: str, candidates: list[FuzzyCandidate]) -> MatchResult | None:
    """Resolver callback connected to code_exec_matcher.FUZZY_RESOLVER."""
    if not candidates:
        return None
    ui.stop_searching()
    if len(candidates) > 1:
        chosen = ui.resolve_fuzzy_ambiguity(target, candidates)
        if chosen is None:
            return None
        cand = chosen
    else:
        cand = candidates[0]

    try:
        doc = (ROOT / target).read_text(encoding="utf-8")
        doc_lines = doc.split("\n")
    except Exception:
        doc_lines = []

    accepted = ui.resolve_fuzzy_match(target, needle, doc_lines, cand)
    if accepted:
        pct = int(cand.similarity * 100)
        note = f" (interactively resolved fuzzy match with {pct}% similarity)"
        return MatchResult(
            cand.start,
            cand.end,
            note,
            fuzzy=True,
            line_range=(cand.start_line, cand.end_line),
            similarity=cand.similarity,
            candidates=candidates,
        )
    return None



# ============================================================
# Session-level error history — tracks (file, error_code) → failure count
# so repeated failures on the same file trigger full file attachment.
# ============================================================
_error_history: dict[tuple[str, str], int] = {}


# ============================================================
# Operation execution
# ============================================================

def execute(op: Operation, fs, _match_cache: dict | None = None) -> str | None:
    command = op.command
    args = op.args

    if command == "CREATE":
        path = safe_path(args[0], follow_leaf=False)
        if fs.lexists(path):
            raise OpError(f"ERR|CREATE_EXISTS|{args[0]}")
        text = op.data
        if text and not text.endswith("\n"):
            text += "\n"
        fs.write(path, text)
        return f"Created {args[0]}"

    if command == "PATCH":
        path = safe_path(args[0])
        if not fs.is_file(path):
            raise OpError(f"ERR|FILE_NOT_FOUND|{args[0]}")
        doc = fs.read(path)
        newline = newline_style(doc)
        new_doc, count = apply_unified_patch(doc, op.data or "", args[0], newline)
        fs.write(path, new_doc)
        return f"Patched {args[0]} ({count} hunk{'s' if count != 1 else ''})"

    if command in {"EDIT", "INSERT_BEFORE", "INSERT_AFTER", "APPEND", "PREPEND"}:
        path = safe_path(args[0])
        if not fs.is_file(path):
            raise OpError(f"ERR|FILE_NOT_FOUND|{args[0]}")

        doc = fs.read(path)
        newline = newline_style(doc)

        if command == "EDIT":
            # Reuse the MatchResult from preflight if available (avoids double scanning)
            cache_key = id(op)
            if _match_cache is not None and cache_key in _match_cache:
                match = _match_cache[cache_key]
            else:
                match = find_unique(doc, with_newlines(op.data, newline), "SEARCH", args[0])
                if _match_cache is not None:
                    _match_cache[cache_key] = match
            replacement = with_newlines(op.extra, newline)

            # Auto-align indentation if matching occurred under indentation drift
            if match.fuzzy and match.line_range is not None:
                replacement = _adjust_indentation(doc, match.line_range, op.data, replacement, newline)

            new = doc[: match.start] + replacement + doc[match.end :]
            if match.fuzzy:
                validate_fuzzy_replacement_safety(args[0], doc, new, match)
                if not isinstance(fs, VirtualFS):
                    AUDIT_FUZZY_RESOLUTIONS.append((args[0], match.line_range, getattr(match, "similarity", 1.0)))
            note = match.note
            done = "Edited"

        elif command in {"INSERT_BEFORE", "INSERT_AFTER"}:
            cache_key = id(op)
            if _match_cache is not None and cache_key in _match_cache:
                match = _match_cache[cache_key]
            else:
                match = find_unique(doc, with_newlines(op.data, newline), "MARKER", args[0])
                if _match_cache is not None:
                    _match_cache[cache_key] = match
            content = with_newlines(op.extra, newline)

            if match.fuzzy and match.line_range is not None:
                doc_lines = doc.split("\n")
                doc_indent = _get_leading_indent(doc_lines[match.line_range[0]])
                content_lines = content.split(newline)
                first_c = next((ln for ln in content_lines if ln.strip()), "")
                if doc_indent and not _get_leading_indent(first_c):
                    content = newline.join((doc_indent + ln if ln.strip() else ln) for ln in content_lines)

            content_clean = content[:-len(newline)] if content.endswith(newline) else content
            if command == "INSERT_BEFORE":
                new = doc[: match.start] + content_clean + newline + doc[match.start :]
                done = "Inserted before marker in"
            else:
                new = doc[: match.end] + newline + content_clean + doc[match.end :]
                done = "Inserted after marker in"
            if match.fuzzy:
                validate_fuzzy_replacement_safety(args[0], doc, new, match)
                if not isinstance(fs, VirtualFS):
                    AUDIT_FUZZY_RESOLUTIONS.append((args[0], match.line_range, getattr(match, "similarity", 1.0)))
            note = match.note

        elif command == "APPEND":
            base = doc if (not doc or doc.endswith("\n")) else doc + newline
            content = with_newlines(op.data, newline)
            if not content.endswith(newline):
                content += newline
            new = base + content
            note = ""
            done = "Appended to"

        else:
            bom = "\ufeff" if doc.startswith("\ufeff") else ""
            content = with_newlines(op.data, newline)
            if not content.endswith(newline):
                content += newline
            new = bom + content + doc[len(bom):]
            note = ""
            done = "Prepended to"

        fs.write(path, new)
        return f"{done} {args[0]}{note}"

    if command == "REPLACE_ALL":
        path = safe_path(args[0])
        if not fs.is_file(path):
            raise OpError(f"ERR|FILE_NOT_FOUND|{args[0]}")
        doc = fs.read(path)
        newline = newline_style(doc)
        search = with_newlines(op.data, newline)
        replace = with_newlines(op.extra, newline)
        if not search:
            raise OpError(f"SEARCH block is empty for REPLACE_ALL in {args[0]}")
        # Use the smart multi-tier matcher so trailing-whitespace / indent drift is tolerated
        spans = find_all(doc, search, args[0])  # returns spans in reverse (right-to-left) order
        count = len(spans)
        new = doc
        for start, end in spans:
            new = new[:start] + replace + new[end:]
        fs.write(path, new)
        return f"Replaced {count} occurrence(s) in {args[0]}"

    if command == "TOUCH":
        path = safe_path(args[0], follow_leaf=False)
        fs.touch(path)
        return f"Touched {args[0]}"
    if command == "CHMOD":
        path = safe_path(args[0])
        fs.chmod(path, args[1])
        return f"Changed mode of {args[0]} to {args[1]}"
    if command == "DELETE":
        path = safe_path(args[0], follow_leaf=False)
        if not fs.lexists(path):
            raise OpError(f"ERR|DELETE_NOT_FOUND|{args[0]}")
        fs.delete(path)
        return f"Deleted {args[0]}"

    if command in {"MOVE", "COPY", "RENAME"}:
        pattern = args[0]
        has_wildcard = any(ch in pattern for ch in ("*", "?", "[")) or pattern.startswith("regex:")

        # Handle wildcard / regex pattern move
        if command in {"MOVE", "COPY"} and has_wildcard:
            dst_dir = safe_path(args[1], follow_leaf=False)
            if fs.lexists(dst_dir) and not fs.is_dir(dst_dir):
                raise OpError(f"Target '{args[1]}' exists but is not a directory for pattern {command.lower()}")

            # Resolve matching files
            matched_paths: list[Path] = []
            if pattern.startswith("regex:"):
                rx = re.compile(pattern[6:])
                parent_dir = ROOT
                for p in parent_dir.rglob("*"):
                    rel_p = rel(p)
                    if rx.search(rel_p) and not any(part in PROTECTED_NAMES for part in p.parts):
                        matched_paths.append(p)
            else:
                for p in ROOT.glob(pattern):
                    if not any(part in PROTECTED_NAMES for part in p.parts):
                        matched_paths.append(p)

            if not matched_paths:
                raise OpError(f"ERR|FILE_NOT_FOUND|{args[0]} - No files matched pattern '{pattern}'")

            # Ensure destination directory exists
            if not fs.lexists(dst_dir):
                fs.mkdir(dst_dir)

            moved_count = 0
            for item in sorted(matched_paths):
                item_safe = safe_path(rel(item), follow_leaf=False)
                item_dest = dst_dir / item.name
                if item_safe.resolve() == item_dest.resolve():
                    continue
                if item_safe.resolve() in item_dest.resolve().parents:
                    continue
                if fs.lexists(item_dest):
                    continue
                if command == "COPY":
                    fs.copy(item_safe, item_dest)
                else:
                    fs.move(item_safe, item_dest)
                moved_count += 1

            verb = "Copied" if command == "COPY" else "Moved"
            return f"{verb} {moved_count} file(s) matching '{pattern}' -> {args[1]}"

        src = safe_path(args[0], follow_leaf=(command == "COPY"))
        dst = safe_path(args[1], follow_leaf=False)
        if not fs.lexists(src):
            raise OpError(f"Source does not exist: {args[0]}")

        # Check identity: prevent operating onto itself before checking dst existence
        src_res = src.resolve()
        dst_res = dst.resolve() if dst.exists() else dst.parent.resolve() / dst.name
        if src_res == dst_res:
            raise OpError(f"Cannot {command.lower()} {args[0]} onto itself")
        if src.exists() and dst.exists():
            try:
                if os.path.samefile(src, dst):
                    raise OpError(f"Cannot {command.lower()} {args[0]} onto itself")
            except OSError:
                pass

        # Check nesting: prevent copying/moving a directory into its own child tree
        if src_res in dst_res.parents or (fs.is_dir(src) and (src in dst.parents or src_res in dst_res.parents)):
            raise OpError(f"Cannot {command.lower()} {args[0]} into itself")

        if fs.lexists(dst):
            raise OpError(f"Destination already exists: {args[1]}")

        if command == "COPY":
            fs.copy(src, dst)
            return f"Copied {args[0]} -> {args[1]}"
        fs.move(src, dst)
        past = "Moved" if command == "MOVE" else "Renamed"
        return f"{past} {args[0]} -> {args[1]}"

    if command == "MKDIR":
        path = safe_path(args[0])
        if fs.lexists(path) and not fs.is_dir(path):
            raise OpError(f"Path exists but is not a directory: {args[0]}")
        fs.mkdir(path)
        return f"Created directory {args[0]}"
    if command == "FETCH":
        path = safe_path(args[0])
        if not fs.is_file(path):
            raise OpError(f"ERR|FILE_NOT_FOUND|{args[0]}")
        range_str = f" ({args[1]})" if len(args) > 1 and args[1] else ""
        return f"Fetched {args[0]}{range_str}"
    if command == "RUN":
        fs.run(args[0])
        return None
    if command == "COMMIT":
        return None
    raise OpError(f"Unsupported command: {command}")


def should_show_folder_tree(operations: list[Operation]) -> bool:
    for op in operations:
        cmd = op.command
        if cmd in {"MKDIR", "MOVE", "RENAME", "COPY"}:
            return True
        if cmd == "DELETE" and op.args:
            path_str = str(op.args[0])
            if "/" in path_str or (ROOT / path_str).is_dir():
                return True
        if cmd in {"CREATE", "TOUCH"} and op.args:
            if "/" in str(op.args[0]):
                return True
    return False


def check_paths(op: Operation) -> None:
    if op.command in {"RUN", "COMMIT"}:
        return
    for idx, value in enumerate(op.args):
        # Skip path normalization on source wildcard patterns
        if idx == 0 and op.command in {"MOVE", "COPY"} and any(ch in value for ch in ("*", "?", "[")):
            continue
        if idx == 0 and op.command in {"MOVE", "COPY"} and value.startswith("regex:"):
            continue
        if op.command in {"FETCH", "CHMOD"} and idx > 0:
            continue
        safe_path(value, follow_leaf=(op.command != "FETCH"))


def generate_plan_diff(operations: list[Operation], _match_cache: dict | None = None) -> str:
    """Generates unified diff text for all planned file modifications."""
    vfs = VirtualFS()
    diff_lines = []

    for op in operations:
        cmd = op.command
        if cmd == "CREATE":
            target = op.args[0]
            new_text = op.data or ""
            diff = difflib.unified_diff(
                [],
                new_text.splitlines(keepends=True),
                fromfile="/dev/null",
                tofile=f"b/{target}",
            )
            diff_lines.extend(diff)
            try:
                execute(op, vfs, _match_cache=_match_cache)
            except Exception:
                pass
        elif cmd in {"EDIT", "INSERT_BEFORE", "INSERT_AFTER", "APPEND", "PREPEND", "REPLACE_ALL", "PATCH"}:
            target = op.args[0]
            try:
                target_path = safe_path(target)
                old_text = ""
                if vfs.lexists(target_path):
                    old_text = vfs.read(target_path)
                elif target_path.is_file():
                    old_text = read_text(target_path)
                execute(op, vfs, _match_cache=_match_cache)
                new_text = vfs.read(target_path)
                diff = difflib.unified_diff(
                    old_text.splitlines(keepends=True),
                    new_text.splitlines(keepends=True),
                    fromfile=f"a/{target}",
                    tofile=f"b/{target}",
                )
                diff_lines.extend(diff)
            except Exception:
                pass
        elif cmd in {"TOUCH", "CHMOD", "MKDIR"}:
            try:
                execute(op, vfs, _match_cache=_match_cache)
            except Exception:
                pass
        elif cmd == "DELETE":
            target = op.args[0]
            try:
                target_path = safe_path(target)
                old_text = ""
                if target_path.is_file():
                    old_text = read_text(target_path)
                diff = difflib.unified_diff(
                    old_text.splitlines(keepends=True),
                    [],
                    fromfile=f"a/{target}",
                    tofile="/dev/null",
                )
                diff_lines.extend(diff)
                execute(op, vfs, _match_cache=_match_cache)
            except Exception:
                pass
        elif cmd in {"MOVE", "RENAME"}:
            diff_lines.append(f"--- a/{op.args[0]}\n+++ b/{op.args[1]}\n@@ move/rename @@\n")
            try:
                execute(op, vfs, _match_cache=_match_cache)
            except Exception:
                pass
        elif cmd == "COPY":
            diff_lines.append(f"--- /dev/null\n+++ b/{op.args[1]}\n@@ copy from {op.args[0]} @@\n")
            try:
                execute(op, vfs, _match_cache=_match_cache)
            except Exception:
                pass
    return "".join(diff_lines)


def preflight(operations: list[Operation]) -> tuple[str | None, int, dict]:
    """
    Validate and dry-run the operation list.

    Returns (deferred_reason, deferred_count, match_cache).
    match_cache maps id(op) -> MatchResult for EDIT/INSERT ops so apply_plan
    can skip the second find_unique() scan (avoids running the 7-tier matcher twice).
    """
    vfs = VirtualFS()
    reason = None
    trigger = 0
    match_cache: dict = {}

    def _loc(op: Operation) -> str:
        return f" [plan line {op.source_line}]" if op.source_line else ""

    # Phase 1: Static Consistency & Conflict Verification
    file_history: dict[str, list[str]] = {}
    for number, op in enumerate(operations, 1):
        if op.command == "RUN":
            validate_run_command(op.args[0])
        elif op.command not in {"RUN", "COMMIT"}:
            args_to_check = []
            for idx, path_arg in enumerate(op.args):
                if op.command in {"FETCH", "CHMOD"} and idx > 0:
                    continue
                if idx == 0 and op.command in {"MOVE", "COPY"} and (any(ch in path_arg for ch in ("*", "?", "[")) or path_arg.startswith("regex:")):
                    continue
                args_to_check.append(path_arg)
            for path_arg in args_to_check:
                path_str = str(safe_path(path_arg, follow_leaf=False))
                history = file_history.setdefault(path_str, [])
                if op.command == "CREATE" and "CREATE" in history:
                    raise OpError(f"Operation {number} ({describe_operation(op)}){_loc(op)}: ERR|CONFLICTING_OPERATIONS|{path_arg} - multiple CREATE commands for same file")
                if op.command in {"EDIT", "APPEND", "PREPEND", "INSERT_BEFORE", "INSERT_AFTER", "REPLACE_ALL", "CHMOD", "PATCH"}:
                    if "DELETE" in history:
                        raise OpError(f"Operation {number} ({describe_operation(op)}){_loc(op)}: ERR|CONFLICTING_OPERATIONS|{path_arg} - attempting to operate on a file that was DELETED in the same plan")
                history.append(op.command)

    # Phase 2: Virtual Simulation — also builds the match_cache
    for number, op in enumerate(operations, 1):
        try:
            if reason is not None:
                check_paths(op)
            elif op.command == "RUN":
                reason, trigger = "a RUN command", number
            else:
                try:
                    execute(op, vfs, _match_cache=match_cache)
                except Unverifiable as exc:
                    reason, trigger = str(exc), number
        except OpError as exc:
            raise OpError(
                f"Operation {number} ({describe_operation(op)}){_loc(op)}: {exc}"
            ) from None

    return reason, (len(operations) - trigger if reason else 0), match_cache


# ============================================================
# Display
# ============================================================

def _lines(text: str | None) -> int:
    return 0 if not text else text.count("\n") + 1


def describe_operation(op: Operation) -> str:
    return ui.describe_op(op)


# ============================================================
# Main
# ============================================================
def _get_error_guidance(error_msg: str) -> str:
    hints = []
    if "ERR|SEARCH_NOT_FOUND" in error_msg:
        hints.append(
            "- **SEARCH_NOT_FOUND**: The SEARCH block does not exist verbatim in the file.\n"
            "  ✅ **Do**: Copy the real lines from the file content provided in context "
            "(lines marked `+` in the 'Closest candidate' diff above show what is actually there). "
            "Update your SEARCH block to match those exact lines.\n"
            "  ❌ **Do NOT**: Write the SEARCH block from memory, paraphrase it, "
            "or copy it from a previous version of the file."
        )
    if "ERR|SEARCH_AMBIGUOUS" in error_msg:
        hints.append(
            "- **SEARCH_AMBIGUOUS**: The SEARCH snippet matched in multiple places.\n"
            "  ✅ **Do**: Add 1–3 unique surrounding lines (a function name, a class, "
            "a distinctive comment, or a unique variable) to make the anchor unambiguous.\n"
            "  ❌ **Do NOT**: Use generic lines like `pass`, `return`, `}`, or bare HTML tags "
            "as your SEARCH anchor — they appear everywhere."
        )
    if "ERR|SEARCH_TOO_BIG" in error_msg:
        hints.append(
            "- **SEARCH_TOO_BIG**: The SEARCH block exceeds the 60-line / 4000-char limit.\n"
            "  ✅ **Do**: Shrink the SEARCH block to a 3–6 line unique anchor. "
            "For large regions, use the boundary anchor shorthand: first 4 lines + last 4 lines only.\n"
            "  ❌ **Do NOT**: Include entire functions, classes, or components in a SEARCH block."
        )
    if "ERR|CREATE_EXISTS" in error_msg:
        hints.append(
            "- **CREATE_EXISTS**: The file already exists — CREATE only works for new files.\n"
            "  ✅ **Do**: Use `EDIT <file>` with SEARCH/REPLACE to modify it, "
            "or `REPLACE_ALL <file>` to replace every occurrence of a pattern.\n"
            "  ❌ **Do NOT**: Use CREATE on a file that already exists."
        )
    if "ERR|FILE_NOT_FOUND" in error_msg:
        # Try to surface files that are close to the missing path for context
        nearby: list[str] = []
        m = re.search(r"ERR\|FILE_NOT_FOUND\|([^\s|]+)", error_msg)
        if m:
            missing = m.group(1)
            try:
                missing_path = Path(missing)
                parent = (ROOT / missing_path.parent) if missing_path.parent != Path(".") else ROOT
                if parent.is_dir():
                    candidates = sorted(
                        p.relative_to(ROOT).as_posix()
                        for p in parent.iterdir()
                        if p.is_file() and not p.name.startswith(".")
                    )[:6]
                    if candidates:
                        nearby = candidates
            except Exception:
                pass
        nearby_hint = (
            f"\n  Nearby files in the same directory: {', '.join(nearby)}" if nearby else ""
        )
        hints.append(
            f"- **FILE_NOT_FOUND**: The path `{m.group(1) if m else '?'}` does not exist.{nearby_hint}\n"
            "  ✅ **Do**: Verify the exact relative path from the project root. "
            "Use `CREATE <file>` if you meant to create a new file.\n"
            "  ❌ **Do NOT**: Guess paths. Only use paths that appear in the file list provided in context."
        )
    if "ERR|CONFLICTING_OPERATIONS" in error_msg:
        hints.append(
            "- **CONFLICTING_OPERATIONS**: The plan has contradictory operations on the same file.\n"
            "  ✅ **Do**: Combine edits sequentially under a single EDIT block, "
            "or order operations so dependencies exist first.\n"
            "  ❌ **Do NOT**: EDIT or APPEND a file after DELETE, or CREATE a file twice."
        )
    if "ERR|MULTIPLE_PLANS" in error_msg:
        hints.append(
            "- **MULTIPLE_PLANS**: More than one `code_exec` block was detected.\n"
            "  ✅ **Do**: Output exactly ONE single `code_exec` block. "
            "Merge all operations into it.\n"
            "  ❌ **Do NOT**: Split operations across multiple code blocks or add a second block "
            "as a 'correction' — combine them into one."
        )
    if "ERR|PLAN_NOT_FOUND" in error_msg:
        hints.append(
            "- **PLAN_NOT_FOUND**: No executable plan block was detected (output may be cut off, "
            "or the block was never opened/closed).\n"
            "  ✅ **Do**: Wrap your plan in a fenced block with ````code_exec` as the language. "
            "Use 4+ backticks if your content itself contains triple backticks.\n"
            "  ❌ **Do NOT**: Place operations outside the fence, or start a block without closing it."
        )
    if "ERR|FORBIDDEN_COMMAND" in error_msg:
        hints.append(
            "- **FORBIDDEN_COMMAND**: The RUN command uses a forbidden or destructive pattern.\n"
            "  ✅ **Do**: Use standard safe runners: `pytest`, `python3 -m unittest`, "
            "`npm test`, `cargo test`, `ruff`, `black`.\n"
            "  ❌ **Do NOT**: Use `rm -rf`, `sudo`, `curl | sh`, or `wget`."
        )
    if "ERR|UNKNOWN_COMMAND" in error_msg:
        hints.append(
            f"- **UNKNOWN_COMMAND**: An unrecognized command was used. "
            f"Supported commands: {', '.join(sorted(COMMANDS))}.\n"
            "  ✅ **Do**: Check the suggested command hint in the error and update your instruction. "
            "Shell commands must be prefixed with `RUN`.\n"
            "  ❌ **Do NOT**: Place prose, comments, or explanations inside the `code_exec` block — "
            "put them outside it."
        )
    if "ERR|PATCH_FAILED" in error_msg:
        hints.append(
            "- **PATCH_FAILED**: A unified diff hunk could not be applied.\n"
            "  ✅ **Do**: Verify the context lines match current file contents exactly. "
            "Alternatively, switch to `EDIT` with SEARCH/REPLACE instead of PATCH.\n"
            "  ❌ **Do NOT**: Generate a unified diff from memory — always base it on the actual file."
        )
    if not hints:
        hints.append("- Review the error details above and fix the problematic command or target block.")
    return "\n\n".join(hints)


def _extract_operation_context(error_msg: str) -> str:
    """Extract the failing operation description from the error message for the retry prompt."""
    # Look for "Operation N (COMMAND path):" pattern
    m = re.search(r"Operation\s+(\d+)\s+\(([^)]+)\)", error_msg)
    if m:
        return f"The failing operation was **Operation {m.group(1)}** (`{m.group(2)}`)."
    return ""


def _extract_candidate_diff(error_msg: str) -> str:
    """Pull the closest-candidate diff block out of the error message if present."""
    # The diff is injected by _find_closest_match between dashed lines
    m = re.search(
        r"(Closest candidate found at lines .+?)\n-{40,}\n(.+?)\n-{40,}",
        error_msg,
        re.DOTALL,
    )
    if m:
        header = m.group(1).strip()
        diff_body = m.group(2).strip()
        return (
            f"\n\n### File content at the closest match ({header}):\n"
            "```diff\n"
            f"{diff_body}\n"
            "```\n"
            "> Lines starting with `+` are the **actual current file content**. "
            "Your SEARCH block must match those lines exactly."
        )
    return ""


def copy_error_to_clipboard(error_msg: str) -> None:
    clean_err = error_msg.strip()
    guidance = _get_error_guidance(clean_err)
    fence = chr(96) * 3
    op_context = _extract_operation_context(clean_err)
    candidate_diff = _extract_candidate_diff(clean_err)

    op_section = f"\n{op_context}\n" if op_context else ""

    # ---- Repeated-error detection: inject the full file on the 2nd+ failure ----
    file_section = ""
    affected_file: str | None = None
    attempt = 1

    # Extract the target filename and error code to key the history
    err_code_m = re.search(r"ERR\|(\w+)\|([^\s|]+)", clean_err)
    if err_code_m:
        err_code = err_code_m.group(1)
        raw_path = err_code_m.group(2)
        # Normalise: strip leading path fragments that look like operation labels
        candidate = raw_path.split(":")[0].strip()
        if candidate and not candidate.startswith("matched") and not candidate.startswith("line"):
            affected_file = candidate
            key = (affected_file, err_code)
            _error_history[key] = _error_history.get(key, 0) + 1
            attempt = _error_history[key]

    if affected_file and attempt >= 2:
        # Read the file and attach it so the LLM can't hallucinate SEARCH content
        try:
            file_path = ROOT / affected_file
            if file_path.is_file():
                raw = file_path.read_text(encoding="utf-8", errors="replace")
                line_count = raw.count("\n") + 1
                size_kb = round(len(raw.encode()) / 1024, 1)
                file_section = (
                    f"\n\n---\n"
                    f"### ⚠️ Repeated failure (attempt {attempt}) — full file attached\n\n"
                    f"The SEARCH block in `{affected_file}` has now failed **{attempt} times**. "
                    f"The complete current content of that file is shown below "
                    f"({line_count} lines, {size_kb} KB) so you can read the exact lines "
                    f"before writing any SEARCH block. Do **not** write from memory.\n\n"
                    f"```\n{raw}\n```\n"
                    f"\n> **Do NOT copy lines from the snippet above into a SEARCH block by retyping "
                    f"them.** Select and paste the verbatim lines exactly as they appear.\n"
                )
                ui.repeated_error_file_attached(affected_file, attempt, line_count, size_kb)
        except Exception:
            pass

    prompt = (
        "The previous `code_exec` plan failed with the following error:\n\n"
        f"{fence}\n{clean_err}\n{fence}\n"
        f"{op_section}"
        f"{candidate_diff}\n\n"
        "### Troubleshooting Guidance:\n\n"
        f"{guidance}\n\n"
        "### Instructions for the fix:\n"
        "- Fix **only** the failing operation. Do not re-emit operations that already succeeded.\n"
        "- You may explain your analysis outside the code block.\n"
        f"- Then output a single revised {fence}code_exec ...{fence} block that corrects the issue."
        f"{file_section}"
    )
    try:
        set_clipboard(prompt)
    except Exception:
        pass


def fail(message: str) -> int:
    copy_error_to_clipboard(message)
    ui.error(message)
    return 1


def read_input(args) -> str:
    if args.file == "-":
        return sys.stdin.read()
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    return get_clipboard()


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt


def generate_commit_prompt() -> tuple[str, int, Path | None]:
    """Collects git diff and untracked files into an AI prompt, saving to file if too large."""
    from code_exec_plan_export import estimate_tokens

    if not (ROOT / ".git").exists():
        raise OpError("ERR|NOT_GIT_REPO|Current directory is not a git repository (.git missing)")
    diff_res = subprocess.run(["git", "diff", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    diff_text = diff_res.stdout.strip()
    if not diff_text:
        diff_cached = subprocess.run(["git", "diff", "--cached"], cwd=ROOT, capture_output=True, text=True)
        diff_unstaged = subprocess.run(["git", "diff"], cwd=ROOT, capture_output=True, text=True)
        diff_text = f"{diff_cached.stdout}\n{diff_unstaged.stdout}".strip()
    status_res = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True)
    untracked = [line[3:].strip() for line in status_res.stdout.splitlines() if line.startswith("?? ")]
    if not diff_text and not untracked:
        raise OpError("No changes detected in git repository (working tree clean)")
    sections = []
    if diff_text:
        sections.append(f"```diff\n{diff_text}\n```")
    if untracked:
        untracked_list = "\n".join(f"- {p}" for p in untracked)
        sections.append(f"Untracked new files:\n{untracked_list}")
    changes_body = "\n\n".join(sections)
    prompt_body = (
        "Generate a concise, scoped conventional commit message for the following repository changes.\n\n"
        f"{changes_body}\n\n"
        "Output ONLY a single executable `code_exec` block:\n"
        "```code_exec\n"
        "COMMIT type(scope): concise summary\n"
        "```"
    )

    tokens = estimate_tokens(prompt_body)
    MAX_CLIPBOARD_TOKENS = 18000
    MAX_CLIPBOARD_BYTES = 75 * 1024

    out_folder = ROOT / "context"
    dump_file = None

    if tokens > MAX_CLIPBOARD_TOKENS or len(prompt_body.encode("utf-8")) > MAX_CLIPBOARD_BYTES:
        out_folder.mkdir(parents=True, exist_ok=True)
        dump_path = out_folder / "COMMIT_DIFF.md"
        dump_path.write_text(prompt_body, encoding="utf-8")
        copied_file = set_clipboard_file(dump_path)
        if not copied_file:
            set_clipboard(prompt_body)
        dump_file = dump_path.relative_to(ROOT)
    else:
        set_clipboard(prompt_body)

    return prompt_body, tokens, dump_file


def perform_git_commit(message: str, paths: list[str]) -> tuple[bool, str]:
    try:
        if not paths:
            status_res = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=ROOT, capture_output=True, text=True, check=True
            )
            if not status_res.stdout.strip():
                return False, "No changes detected in git repository to commit."
            subprocess.run(["git", "add", "-A"], cwd=ROOT, check=True)
            diff_cached = subprocess.run(
                ["git", "diff", "--cached", "--name-only"],
                cwd=ROOT, capture_output=True, text=True, check=True
            )
            if not diff_cached.stdout.strip():
                return False, "No staged changes detected in repository."
            res = subprocess.run(
                ["git", "commit", "-m", message],
                cwd=ROOT, capture_output=True, text=True, check=True
            )
            return True, res.stdout.strip()

        subprocess.run(["git", "add", "--", *paths], cwd=ROOT, check=True)
        diff_cached = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--", *paths],
            cwd=ROOT, capture_output=True, text=True, check=True
        )
        if not diff_cached.stdout.strip():
            return False, "No staged changes detected for modified files."
        res = subprocess.run(
            ["git", "commit", "-m", message, "--", *paths],
            cwd=ROOT, capture_output=True, text=True, check=True
        )
        return True, res.stdout.strip()
    except subprocess.CalledProcessError as exc:
        err = exc.stderr.strip() if exc.stderr else (exc.stdout.strip() if exc.stdout else str(exc))
        return False, err
    except OSError as exc:
        return False, str(exc)


def _find_symbol_range(target_path: Path, content: str, symbol: str) -> tuple[int, int, str] | None:
    """Finds start/end lines (1-indexed) and kind for a function or class symbol."""
    suffix = target_path.suffix.lower()
    clean_sym = symbol.strip()

    # 1. Python AST resolution
    if suffix == ".py":
        import ast
        try:
            tree = ast.parse(content)
            # Support Class.method or top-level symbol
            parts = clean_sym.split(".")
            if len(parts) == 1:
                target_name = parts[0]
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        if node.name == target_name:
                            kind = "class" if isinstance(node, ast.ClassDef) else "def"
                            end = getattr(node, "end_lineno", node.lineno)
                            return node.lineno, end, f"{kind} {node.name}"
            elif len(parts) == 2:
                cls_name, method_name = parts
                for node in ast.walk(tree):
                    if isinstance(node, ast.ClassDef) and node.name == cls_name:
                        for sub in node.body:
                            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name == method_name:
                                end = getattr(sub, "end_lineno", sub.lineno)
                                return sub.lineno, end, f"{cls_name}.{sub.name}"
        except Exception:
            pass

    # 2. General regex fallback for JS/TS/Python/etc.
    lines = content.splitlines()
    escaped = re.escape(clean_sym)
    pattern = re.compile(rf"(?:def|function|class|const|let|var)\s+{escaped}\b|[bB]?{escaped}\s*=\s*(?:function|\()|class\s+{escaped}\b")
    for idx, line in enumerate(lines):
        if pattern.search(line):
            start_lineno = idx + 1
            # Determine end by looking at indentation or matching braces
            base_indent = len(line) - len(line.lstrip())
            end_lineno = start_lineno
            brace_count = line.count("{") - line.count("}")
            seen_open = "{" in line
            for j in range(idx + 1, len(lines)):
                next_line = lines[j]
                if seen_open:
                    brace_count += next_line.count("{") - next_line.count("}")
                    end_lineno = j + 1
                    if brace_count <= 0:
                        break
                else:
                    if not next_line.strip():
                        continue
                    indent = len(next_line) - len(next_line.lstrip())
                    if indent <= base_indent:
                        break
                    end_lineno = j + 1
            return start_lineno, end_lineno, clean_sym

    return None


def handle_fetch_operations(fetch_ops: list[Operation]) -> tuple[str, int, int, int]:
    """Reads requested files/ranges/symbols, formats them for AI context, and copies to clipboard."""
    from code_exec_plan_export import estimate_tokens
    sections = [
        "## Requested File Context\n",
        "> The following file contents were requested via `FETCH` for accurate edits.\n",
    ]
    total_lines = 0
    for op in fetch_ops:
        path_str = op.args[0]
        spec_str = op.args[1] if len(op.args) > 1 and op.args[1] else None
        target_path = safe_path(path_str)
        if not target_path.is_file():
            raise OpError(f"ERR|FILE_NOT_FOUND|{path_str}")
        content = read_text(target_path)
        lines = content.splitlines()
        total_file_lines = len(lines)
        if spec_str:
            m = re.match(r"^(\d+)(?:-(\d+))?$", spec_str)
            if m:
                start = max(1, int(m.group(1)))
                end = min(total_file_lines, int(m.group(2))) if m.group(2) else total_file_lines
                if start > end:
                    start, end = end, start
                selected_lines = lines[start - 1 : end]
                header = f"### `{path_str}` (lines {start}-{end} of {total_file_lines})\n"
                total_lines += len(selected_lines)
                body = "\n".join(selected_lines)
            else:
                sym_match = _find_symbol_range(target_path, content, spec_str)
                if sym_match:
                    start, end, label = sym_match
                    selected_lines = lines[start - 1 : end]
                    header = f"### `{path_str}` ({label}, lines {start}-{end} of {total_file_lines})\n"
                    total_lines += len(selected_lines)
                    body = "\n".join(selected_lines)
                else:
                    header = f"### `{path_str}` (full file, symbol '{spec_str}' not located, {total_file_lines} lines)\n"
                    total_lines += total_file_lines
                    body = content
        else:
            header = f"### `{path_str}` (full file, {total_file_lines} lines)\n"
            total_lines += total_file_lines
            body = content
        ext = target_path.suffix.lstrip(".")
        sections.append(f"{header}```{ext}\n{body}\n```\n")

    sections.append("> You can now emit the executable ````code_exec```` plan block based on the exact lines above.")
    result_text = "\n".join(sections)
    tokens = estimate_tokens(result_text)

    # If payload is very large (> 18,000 tokens or > 75 KB), save to a file and notify clipboard
    MAX_CLIPBOARD_TOKENS = 18000
    MAX_CLIPBOARD_BYTES = 75 * 1024

    out_folder = ROOT / "context"
    out_folder.mkdir(parents=True, exist_ok=True)
    dump_path = out_folder / "FETCHED_CONTEXT.md"
    dump_path.write_text(result_text, encoding="utf-8")

    # If payload is large, copy the actual file to clipboard so Cmd+V / Ctrl+V attaches the file
    if tokens > MAX_CLIPBOARD_TOKENS or len(result_text.encode("utf-8")) > MAX_CLIPBOARD_BYTES:
        copied_file = set_clipboard_file(dump_path)
        if not copied_file:
            set_clipboard(result_text)
    else:
        set_clipboard(result_text)

    return result_text, len(fetch_ops), total_lines, tokens


def detect_verification_command() -> str | None:
    """Auto-detects a reasonable lint/typecheck/test command for the project."""
    if (ROOT / ".code-exec-verify").is_file():
        cmd = (ROOT / ".code-exec-verify").read_text(encoding="utf-8").strip()
        if cmd:
            return cmd

    if (ROOT / "package.json").is_file():
        if (ROOT / "tsconfig.json").is_file():
            return "npx tsc --noEmit"
        return "npm test"
    elif (ROOT / "Cargo.toml").is_file():
        return "cargo test"
    elif (ROOT / "pyproject.toml").is_file() or (ROOT / "setup.py").is_file() or any(ROOT.glob("test*.py")):
        if shutil.which("ruff"):
            return "ruff check ."
        elif shutil.which("pytest") and (ROOT / "tests").is_dir():
            return "pytest"
        return "python3 -m unittest"
    return None


def copy_verification_error_to_clipboard(cmd: str, returncode: int, output: str, modified_files: list[str]) -> None:
    """Formats verification failure into a structured AI retry prompt on the clipboard."""
    fence = chr(96) * 3
    file_list = ", ".join(f"`{f}`" for f in modified_files) if modified_files else "the modified files"
    prompt = (
        f"The previous `code_exec` changes were applied, but the verification hook failed.\n\n"
        f"**Command executed:** `{cmd}` (exit code {returncode})\n\n"
        f"**Error Output:**\n"
        f"{fence}\n"
        f"{output.strip()}\n"
        f"{fence}\n\n"
        f"### Instructions for the fix:\n"
        f"- Analyze the compiler, linter, or test failures shown above.\n"
        f"- Fix only the errors in {file_list}.\n"
        f"- Provide an updated {fence}code_exec ...{fence} plan block with the necessary EDITs."
    )
    set_clipboard(prompt)


def apply_plan(operations: list[Operation], timeout: int, no_commit: bool = False, auto_commit: bool = False, dry_run: bool = False, match_cache: dict | None = None, verify_cmd: str | None = None) -> int:
    AUDIT_FUZZY_RESOLUTIONS.clear()
    fetch_ops = [op for op in operations if op.command == "FETCH"]
    exec_ops = [op for op in operations if op.command not in {"COMMIT", "FETCH"}]

    if fetch_ops:
        try:
            _, count, lines_fetched, tokens = handle_fetch_operations(fetch_ops)
            ui.fetch_success(count, lines_fetched, tokens)
        except OpError as exc:
            return fail(str(exc))
        if not exec_ops:
            return 0

    fs = VirtualFS() if dry_run else RealFS(timeout)
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, _raise_interrupt)
        except (ValueError, OSError):
            pass
    commit_op = next((op for op in operations if op.command == "COMMIT"), None)
    commit_msg = commit_op.args[0] if commit_op else None
    ui.start_apply(len(exec_ops))
    modified_paths: list[str] = []

    def _loc(op: Operation) -> str:
        return f" [plan line {op.source_line}]" if op.source_line else ""

    try:
        step = 0
        for op in operations:
            if op.command == "COMMIT":
                continue
            step += 1
            try:
                message = execute(op, fs, _match_cache=match_cache)
            except OpError as exc:
                raise type(exc)(
                    f"Operation {step} ({describe_operation(op)}){_loc(op)}: {exc}"
                ) from None
            if message:
                ui.step_done(step, len(exec_ops), message)
                if op.command in {"CREATE", "EDIT", "DELETE", "APPEND", "PREPEND", "INSERT_BEFORE", "INSERT_AFTER", "REPLACE_ALL", "TOUCH", "CHMOD", "PATCH", "MKDIR"}:
                    modified_paths.append(op.args[0])
                elif op.command in {"MOVE", "COPY", "RENAME"}:
                    src_arg = op.args[0]
                    if not (any(ch in src_arg for ch in ("*", "?", "[")) or src_arg.startswith("regex:")):
                        modified_paths.append(src_arg)
                    modified_paths.append(op.args[1])

    except CommandFailed as exc:
        copy_error_to_clipboard(str(exc))
        ui.command_failed(str(exc), fs.backup_dir if hasattr(fs, "backup_dir") else None)
        return 1
    except (Exception, KeyboardInterrupt) as exc:
        if not isinstance(exc, KeyboardInterrupt):
            copy_error_to_clipboard(str(exc))
        ui.apply_interrupted(exc)
        if hasattr(fs, "journal"):
            count = len(fs.journal)
            errors = fs.rollback()
            ui.rollback_report(errors, count, fs.backup_dir, fs.ran_commands)
            if not errors:
                fs.cleanup()
        return 1

    if dry_run:
        ui.dry_run()
        return 0

    fs.save_backup_manifest()
    ui.done()
    if AUDIT_FUZZY_RESOLUTIONS:
        ui.fuzzy_audit_summary(AUDIT_FUZZY_RESOLUTIONS)

    # Post-apply verification hook (--verify)
    if verify_cmd:
        actual_verify_cmd = detect_verification_command() if verify_cmd == "auto" else verify_cmd
        if actual_verify_cmd:
            print(ui.palette.paint(f"\n  🔍 Running post-apply verification: {actual_verify_cmd} ...", ui.palette.CYAN, bold=True))
            try:
                res = subprocess.run(
                    actual_verify_cmd,
                    shell=True,
                    cwd=str(ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=timeout or 60,
                )
                if res.returncode != 0:
                    ui.error(f"Verification failed ({actual_verify_cmd}): exit code {res.returncode}")
                    print(res.stdout)
                    unique_modified = list(dict.fromkeys(modified_paths))
                    copy_verification_error_to_clipboard(actual_verify_cmd, res.returncode, res.stdout, unique_modified)
                    print(ui.palette.paint("  📋 Verification error and fix prompt copied to clipboard!\n", ui.palette.AMBER, bold=True))
                    return res.returncode
                else:
                    print(ui.palette.paint("  ✓ Verification passed cleanly!\n", ui.palette.GREEN, bold=True))
            except subprocess.TimeoutExpired:
                ui.error(f"Verification timed out after {timeout or 60} seconds.")
                return 1
            except Exception as exc:
                ui.error(f"Failed to execute verification hook: {exc}")
                return 1

    if commit_msg and not no_commit:
        should_commit = auto_commit or (not exec_ops) or ui.prompt_commit(commit_msg)
        if should_commit:
            unique_paths = list(dict.fromkeys(modified_paths))
            success, out = perform_git_commit(commit_msg, unique_paths)
            if success:
                ui.commit_success(out, commit_msg)
            else:
                ui.commit_failed(out)

    return 0


def main(argv=None) -> int:
    global ROOT
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    # Ensure .code_exec/ is always ignored by git in the active repository
    try:
        from code_exec_plan_export import ensure_gitignore_entry
        ensure_gitignore_entry(ROOT, ".code_exec")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="Deterministic local code executor", add_help=False)
    parser.add_argument("action", nargs="?", default=None,
                        help="Direct action: 'apply', 'undo', 'theme', 'update', or 'export-context'")
    parser.add_argument("subarg", nargs="?", default=None,
                        help="Sub-argument for actions (e.g., theme name)")
    parser.add_argument("-h", "--help", action="store_true",
                        help="Show interactive usage guide and options")
    parser.add_argument("-c", "--commit-prompt", "--docommit", dest="commit_prompt", action="store_true",
                        help="Copy git diff prompt to clipboard for AI commit message generation")
    parser.add_argument("-p", "--prompt", "--copy-instructions", dest="prompt", action="store_true",
                        help="Copy code_exec_instructions.md to the clipboard for your AI prompt")
    parser.add_argument("--export-context", "--export-concet", "--export-plan", "--context", dest="export_plan", action="store_true",
                        help="Export a clean project folder (context) excluding temp/caches with file and token counts")
    parser.add_argument("--ignore-file", default=".code-exec-ignore",
                        help="Path or name of custom ignore configuration file (default: .code-exec-ignore)")
    parser.add_argument("--target-dir", default="context",
                        help="Target output directory for clean plan export (default: context)")
    parser.add_argument("--compact", action="store_true",
                        help="Export compact skeleton/summaries for context to minimize tokens")
    parser.add_argument("--diff", action="store_true",
                        help="Display unified diff of file changes before applying")
    parser.add_argument("--tree", action="store_true",
                        help="Display the proposed directory tree structure")
    parser.add_argument("--check", action="store_true",
                        help="Debug mode: parse and validate syntax only without reading or checking files")
    parser.add_argument("--clipboard", action="store_true",
                        help="Read instructions from clipboard (the default)")
    parser.add_argument("--file", help="Read instructions from a file ('-' for stdin)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate and show operations without applying")
    parser.add_argument("--yes", action="store_true",
                        help="Apply without confirmation")
    parser.add_argument("--no-run", action="store_true",
                        help="Refuse plans that contain RUN commands")
    parser.add_argument("--no-commit", action="store_true",
                        help="Skip git commit prompt even if COMMIT is present")
    parser.add_argument("--timeout", type=int, default=DEFAULT_RUN_TIMEOUT,
                        help=f"Seconds allowed per RUN command, 0 = no limit "
                             f"(default {DEFAULT_RUN_TIMEOUT})")
    parser.add_argument("--with-tree", dest="with_tree", action="store_true",
                        help="With -p: append current project file tree to the copied instructions")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show which matching tier was used for each SEARCH block")
    parser.add_argument("--short", action="store_true",
                        help="With -p: copy the compact instructions (for small/local models)")
    parser.add_argument("-V", "--version", action="store_true",
                        help="Show program's version number and exit")
    parser.add_argument("-C", "--project-dir", dest="project_dir", default=None,
                        help="Run as if code-exec was started in <path> instead of the current working directory")
    parser.add_argument("-b", "--bundle", "--single-file", dest="bundle", action="store_true",
                        help="Export a single consolidated markdown bundle (PROJECT_CONTEXT.md) and copy to clipboard")
    parser.add_argument("-w", "--watch", dest="watch", action="store_true",
                        help="Watch clipboard in background and auto-prompt when AI code plans are detected")
    parser.add_argument("--install-completions", dest="install_completions", action="store_true",
                        help="Install shell autocompletions for active shell (bash, zsh, fish)")
    parser.add_argument("--full", action="store_true",
                        help="Export full file contents in bundle instead of default compact skeletons")
    parser.add_argument("--verify", nargs="?", const="auto", default=None, metavar="CMD",
                        help="Run a verification command after applying (e.g. 'pytest'). "
                             "Pass 'auto' or omit the value to auto-detect from .code-exec-verify "
                             "or project type (package.json → npm test, etc.)")
    parser.add_argument("--fuzzy", choices=["prompt", "strict", "auto"], default=None,
                        help="Fuzzy matching resolution policy: prompt (interactive), strict (fail if <90%%), auto (accept >=72%%)")
    parser.add_argument("--no-notify", dest="notify", action="store_false", default=True,
                        help="Disable desktop/system notifications in watch mode")
    args = parser.parse_args(argv)

    if args.version:
        print("code-exec 1.3.0")
        return 0

    if args.project_dir:
        target_root = Path(args.project_dir).resolve()
        if not target_root.is_dir():
            return fail(f"Specified project directory does not exist: {args.project_dir}")
        os.chdir(target_root)
        import code_exec_types
        import code_exec_fs
        code_exec_types.ROOT = target_root
        code_exec_fs.ROOT = target_root
        code_exec_fs.BACKUP_ROOT = target_root / ".code_exec" / "backups"
        ROOT = target_root

    if args.action in {"1", "apply"}:
        args.action = "apply"
    elif args.action in {"2"}:
        args.dry_run = True
        args.action = "apply"
    elif args.action in {"check", "--check"}:
        args.check = True
        args.action = None
    elif args.action in {"3", "prompt", "-p"}:
        args.prompt = True
        args.action = None
    elif args.action in {"4", "help", "-h"}:
        args.help = True
        args.action = None
    elif args.action in {"5", "update"}:
        from code_exec_updater import update_code_exec
        return 0 if update_code_exec() else 1
    elif args.action in {"d", "docs", "doc", "web-guide", "guide-online", "online-guide"}:
        ui.open_web_guide()
        return 0
    elif args.action in {"6", "undo"}:
        success, msg = undo_last_run()
        if success:
            print(ui.palette.paint(f"\n    {msg}\n", ui.palette.GREEN, bold=True))
            return 0
        return fail(msg)
    elif args.action in {"7", "commit-prompt", "docommit", "commit"}:
        args.commit_prompt = True
        args.action = None
    elif args.action in {
        "8",
        "bundle",
        "b",
        "pack",
        "outline",
        "digest",
        "export-bundle",
        "export-context",
        "export-concet",
        "context",
        "export-plan",
        "plan-export",
        "plan-only",
    }:
        if args.action in {"b", "bundle", "pack", "outline", "digest", "export-bundle"}:
            args.bundle = True
        args.export_plan = True
        args.action = None
    elif args.action in {"fetch", "get", "read"}:
        if not args.subarg:
            return fail("fetch requires a file path: code-exec fetch <path>[:start-end]")
        try:
            target = args.subarg
            parts = target.split(":", 1)
            raw_path = parts[0]
            range_str = parts[1] if len(parts) > 1 else ""
            op = Operation("FETCH", (clean_path(raw_path), range_str))
            _, count, lines_fetched, tokens = handle_fetch_operations([op])
            ui.fetch_success(count, lines_fetched, tokens)
            return 0
        except Exception as exc:
            return fail(f"Fetch failed: {exc}")
    elif args.action in {"9", "short-prompt"}:
        args.prompt = True
        args.short = True
        args.action = None
    elif args.action == "themes":
        themes = list(ui.palette.themes.keys())
        print(f"\n  🎨 Current theme: {ui.palette.current_theme}")
        print(f"  Available themes: {', '.join(themes)}")
        print("  Usage: code-exec theme <name>\n")
        return 0
    elif args.action == "theme":
        if args.subarg:
            if args.subarg in ui.palette.themes:
                ui.palette.set_theme(args.subarg)
                print(f"\n  ✨ Theme successfully switched to '{args.subarg}'!\n")
                return 0
            else:
                return fail(f"Unknown theme '{args.subarg}'. Available: {', '.join(ui.palette.themes.keys())}")
        else:
            print(f"\n  🎨 Current theme: {ui.palette.current_theme}")
            print(f"  Available themes: {', '.join(ui.palette.themes.keys())}")
            print("  Usage: code-exec theme <name>\n")
            return 0
    elif args.action in {"watch", "listen", "-w"}:
        args.watch = True
        args.action = None
    elif args.action in {"completions", "completion"}:
        from code_exec_completions import generate_bash_completions, generate_zsh_completions, generate_fish_completions
        sh = (args.subarg or "").lower()
        if sh == "bash":
            print(generate_bash_completions())
        elif sh == "fish":
            print(generate_fish_completions())
        elif sh in {"zsh", ""}:
            print(generate_zsh_completions())
        else:
            return fail(f"Unsupported shell '{sh}'. Choose: zsh, bash, fish")
        return 0
    elif args.action in {"install-completions", "install-completion"}:
        args.install_completions = True
        args.action = None
    elif args.action and not args.file and not Path(args.action).exists():
        return fail(f"Unknown command or file: '{args.action}'. Run 'code-exec' without arguments for the menu.")

    if args.clipboard:
        args.action = "apply"
    is_interactive = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
    has_flags = any([
        args.help,
        args.prompt,
        args.commit_prompt,
        args.diff,
        args.tree,
        args.check,
        args.clipboard,
        args.file,
        args.dry_run,
        args.yes,
        args.no_run,
        args.no_commit,
        args.export_plan,
        args.bundle,
        getattr(args, "watch", False),
        getattr(args, "install_completions", False),
    ])
    if args.action is None and not has_flags and is_interactive:
        choice = ui.interactive_menu(ROOT)
        if choice in {"q", "quit", "exit", ""}:
            return 0
        if choice == "1":
            args.action = "apply"
        elif choice == "2":
            args.dry_run = True
            args.action = "apply"
        elif choice == "3":
            args.prompt = True
        elif choice == "4":
            args.help = True
        elif choice in {"d", "docs", "doc", "web-guide", "online-guide"}:
            ui.open_web_guide()
            return 0
        elif choice in {"5", "u", "update"}:
            from code_exec_updater import update_code_exec
            return 0 if update_code_exec() else 1
        elif choice in {"6", "undo"}:
            success, msg = undo_last_run()
            if success:
                print(ui.palette.paint(f"\n    {msg}\n", ui.palette.GREEN, bold=True))
                return 0
            return fail(msg)
        elif choice in {"7", "c", "commit", "commit-prompt", "docommit"}:
            args.commit_prompt = True
        elif choice in {"8", "export-context", "export-concet", "context", "export-plan", "plan-export", "plan-only"}:
            args.export_plan = True
        elif choice in {"w", "watch"}:
            args.watch = True
        elif choice in {"b", "bundle", "pack", "outline", "digest"}:
            args.export_plan = True
            args.bundle = True
        elif choice in {"9", "short-prompt"}:
            args.prompt = True
            args.short = True
        elif choice in {"t", "theme", "themes"}:
            themes = list(ui.palette.themes.keys())
            curr = ui.palette.current_theme
            idx = (themes.index(curr) + 1) % len(themes)
            next_t = themes[idx]
            ui.palette.set_theme(next_t)
            print(f"\n  ✨ Theme switched to '{next_t}'!\n")
            return 0
        else:
            ui.error(f"Invalid option: {choice}")
            return 1

    if args.help:
        ui.show_guide()
        return 0

    if getattr(args, "install_completions", False):
        from code_exec_completions import install_completions
        ok, shell_name, msg = install_completions(args.subarg)
        if ok:
            ui.completions_installed(shell_name, msg)
            return 0
        return fail(f"Could not install completions: {msg}")

    if getattr(args, "watch", False):
        from code_exec_watch import watch_clipboard
        return watch_clipboard(
            args,
            ROOT,
            ui,
            preflight_fn=preflight,
            apply_plan_fn=apply_plan,
            generate_diff_fn=generate_plan_diff,
        )

    if args.export_plan or getattr(args, "bundle", False):
        from code_exec_plan_export import create_plan_folder, create_plan_bundle
        try:
            copied_clip = False
            bundle_file_path = None
            if getattr(args, "bundle", False):
                bundle_file = "PROJECT_CONTEXT.md"
                compact_mode = not getattr(args, "full", False)
                res = create_plan_bundle(
                    export_all=True,
                    output_file=bundle_file,
                    ignore_filename=args.ignore_file,
                    root=Path.cwd().resolve(),
                    compact=compact_mode,
                )
                try:
                    bundle_path = Path.cwd().resolve() / bundle_file
                    if bundle_path.is_file():
                        tokens = int(res.get("tokens", 0))
                        MAX_CLIPBOARD_TOKENS = 18000
                        MAX_CLIPBOARD_BYTES = 75 * 1024
                        file_bytes = bundle_path.stat().st_size
                        # If file is large (>75KB or >18k tokens), place file object in clipboard so Cmd+V in Gemini/Claude attaches file
                        if tokens > MAX_CLIPBOARD_TOKENS or file_bytes > MAX_CLIPBOARD_BYTES:
                            copied_file = set_clipboard_file(bundle_path)
                            if copied_file:
                                bundle_file_path = bundle_path
                                copied_clip = True
                            else:
                                set_clipboard(bundle_path.read_text(encoding="utf-8"))
                                copied_clip = True
                        else:
                            set_clipboard(bundle_path.read_text(encoding="utf-8"))
                            copied_clip = True
                except Exception:
                    pass
            else:
                target_out = args.target_dir or "context"
                res = create_plan_folder(
                    export_all=True,
                    output_dirname=target_out,
                    ignore_filename=args.ignore_file,
                    root=Path.cwd().resolve(),
                    compact=args.compact,
                )
            ui.plan_export_success(
                location=str(res["location"]),
                included=int(res["included"]),
                ignored=int(res["ignored"]),
                ignore_file=str(res["ignore_file"]),
                tokens=int(res.get("tokens", 0)),
                size_kb=float(res.get("size_kb", 0.0)),
                compact=bool(res.get("compact", False)),
                copied_to_clipboard=copied_clip,
                file_path=bundle_file_path,
            )
            return 0
        except Exception as exc:
            return fail(f"Could not export context: {exc}")

    if args.commit_prompt:
        try:
            prompt_body, tokens, dump_file = generate_commit_prompt()
        except Exception as exc:
            return fail(f"Could not generate commit prompt: {exc}")
        ui.commit_prompt_copied(len(prompt_body), tokens=tokens, file_path=dump_file)
        return 0

    if args.prompt:
        filename = "code_exec_instructions_short.md" if getattr(args, "short", False) else "code_exec_instructions.md"
        instructions_path = Path(__file__).resolve().parent / filename
        if not instructions_path.is_file():
            return fail(f"Instructions file not found: {instructions_path.name} (checked {instructions_path.parent})")
        try:
            content = instructions_path.read_text(encoding="utf-8")
            if getattr(args, "with_tree", False):
                # Build a compact project file tree and append it to the instructions
                tree_lines: list[str] = []
                try:
                    for root_dir, dirs, files in os.walk(ROOT):
                        dirs[:] = sorted(
                            d for d in dirs
                            if not d.startswith(".") and d not in {"__pycache__", "node_modules", ".venv", "venv", "context"}
                        )
                        rel_root = Path(root_dir).relative_to(ROOT)
                        depth = len(rel_root.parts)
                        prefix = "  " * depth
                        if depth > 0:
                            tree_lines.append(f"{prefix}{rel_root.name}/")
                        for fname in sorted(files):
                            if not fname.startswith(".") and not fname.endswith((".pyc",)):
                                tree_lines.append(f"{'  ' * (depth + 1)}{fname}")
                except Exception:
                    pass
                if tree_lines:
                    tree_block = "\n".join(tree_lines)
                    content += (
                        f"\n\n## Project file tree (as of now)\n\n```\n{ROOT.name}/\n{tree_block}\n```\n"
                        "\n> Use the paths above when writing file operations. Do NOT guess paths that aren't listed here.\n"
                    )
            set_clipboard(content)
        except Exception as exc:
            return fail(f"Could not copy instructions: {exc}")
        ui.instructions_copied(instructions_path)
        return 0

    if args.timeout < 0:
        parser.error("--timeout must be >= 0")
    if args.file == "-" and not (args.yes or args.dry_run or args.check):
        return fail("Reading the plan from stdin requires --yes, --dry-run, or --check "
                    "(stdin can't be used for the confirmation prompt).")

    try:
        text = read_input(args)
    except (OSError, UnicodeDecodeError, RuntimeError) as exc:
        return fail(f"Cannot read instructions: {exc}")

    if not text.strip():
        ui.show_guide()
        print("  💡 Tip: Copy a code_exec plan block to your clipboard first, or use --file <path>.\n")
        return 0

    try:
        plan_text = extract_plan(text)
    except OpError as exc:
        return fail(str(exc))

    try:
        operations = parse_operations(plan_text)
    except (ValueError, OpError) as exc:
        return fail(f"Could not parse instructions: {exc}")

    if not operations:
        return fail("No operations found")

    if args.check:
        ui.header(ROOT)
        for idx, op in enumerate(operations, 1):
            print(f"  {ui.palette.paint(str(idx), ui.palette.SLATE)}: {ui.describe_op(op)}")
        if args.tree or should_show_folder_tree(operations):
            ui.show_tree(operations)
        ui.parse_check_success(len(operations))
        return 0

    if args.no_run and any(op.command == "RUN" for op in operations):
        return fail("Plan contains RUN but --no-run was given. No files were modified.")

    # Wire --verbose into the matcher before plan execution
    import code_exec_matcher as _matcher_mod
    if getattr(args, "verbose", False):
        _matcher_mod.VERBOSE = True

    fuzzy_policy = getattr(args, "fuzzy", None) or os.environ.get("CODE_EXEC_FUZZY", "").strip().lower()
    if not fuzzy_policy:
        fuzzy_policy = "prompt" if is_interactive else "strict"
    _matcher_mod.FUZZY_POLICY = fuzzy_policy
    if fuzzy_policy == "prompt" and is_interactive:
        _matcher_mod.FUZZY_RESOLVER = interactive_fuzzy_resolver
    else:
        _matcher_mod.FUZZY_RESOLVER = None

    try:
        reason, deferred, match_cache = preflight(operations)
    except OpError as exc:
        return fail(f"Validation failed: {exc}\n\nNo files were modified.")

    ui.header(ROOT)
    ui.show_plan(operations, reason, deferred)

    if args.tree or should_show_folder_tree(operations):
        ui.show_tree(operations)

    diff_text = generate_plan_diff(operations, _match_cache=match_cache)
    if args.diff:
        ui.show_diff(diff_text)

    if args.dry_run:
        return apply_plan(operations, args.timeout, no_commit=True, auto_commit=True, dry_run=True, match_cache=match_cache)

    is_pure_fetch = all(op.command == "FETCH" for op in operations)
    if not args.yes and not is_pure_fetch:
        if not ui.confirm(diff_text=diff_text, on_view_tree=(lambda: ui.show_tree(operations)) if operations else None):
            ui.cancelled()
            return 0

    return apply_plan(operations, args.timeout, no_commit=args.no_commit, auto_commit=args.yes, match_cache=match_cache, verify_cmd=args.verify)


if __name__ == "__main__":
    sys.exit(main())
