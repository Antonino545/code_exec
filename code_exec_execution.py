"""Plan validation and execution workflow for the code-exec CLI.

The public command-line entry point remains in :mod:`code_exec`; this module
owns operation execution, preflight/diff generation, error guidance, context
fetching, and post-apply verification.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
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


def get_clipboard() -> str:
    """Resolve through the public CLI module to preserve its patch points."""
    import code_exec

    return code_exec.get_clipboard()


def set_clipboard(text: str) -> None:
    """Resolve through the public CLI module to preserve its patch points."""
    import code_exec

    code_exec.set_clipboard(text)


def set_clipboard_file(path: Path) -> bool:
    """Resolve through the public CLI module to preserve its patch points."""
    import code_exec

    return code_exec.set_clipboard_file(path)

def _project_root() -> Path:
    """Read the active root from the public CLI module for compatibility."""
    import code_exec

    return code_exec.ROOT


# Audit log of fuzzy match resolutions applied in the current run
AUDIT_FUZZY_RESOLUTIONS: list[tuple[str, tuple[int, int] | None, float]] = []


def validate_fuzzy_replacement_safety(
    target: str,
    old_doc: str,
    new_doc: str,
    match: MatchResult,
) -> None:
    """Temporary compatibility shim.

    The project currently treats fuzzy syntax validation as non-fatal to avoid
    blocking valid edits with ERR|FUZZY_SYNTAX_ERROR. Keep the hook in place for
    future re-enablement, but do not reject the operation here.
    """
    # Intentionally no-op for now: fuzzy matches are allowed to proceed without
    # being rejected as invalid syntax. This avoids false positives while the
    # engine is still being tuned.
    return


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
        doc = (_project_root() / target).read_text(encoding="utf-8")
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

def _cached_match(op: Operation, doc: str, needle: str, label: str, cache: dict | None) -> MatchResult:
    fingerprint = hashlib.sha256(doc.encode("utf-8")).hexdigest()
    match = cache.get(id(op)) if cache is not None else None
    if match is not None and match.source_fingerprint is not None:
        if match.source_fingerprint != fingerprint:
            raise OpError(
                f"ERR|STALE_FILE|{op.args[0]} - File changed since validation; "
                "FETCH current content and rebuild the plan before applying it"
            )
        return match
    # Older callers may supply an unguarded MatchResult. Recompute it rather
    # than trusting offsets without knowing which content they refer to.
    match = find_unique(doc, needle, label, op.args[0])
    match.source_fingerprint = fingerprint
    if cache is not None:
        cache[id(op)] = match
    return match


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

    if command == "EDIT_RANGE":
        path = safe_path(args[0])
        if not fs.is_file(path):
            raise OpError(f"ERR|FILE_NOT_FOUND|{args[0]}")
        start, end = int(args[1]), int(args[2])
        doc = fs.read(path)
        lines = doc.splitlines()
        if start < 1 or end < start or end > len(lines):
            raise OpError(
                f"ERR|STALE_RANGE|{args[0]}|lines {start}-{end} are outside the current "
                f"file range (1-{len(lines)}); FETCH the current file and rebuild this edit"
            )
        expected = op.data.replace("\r\n", "\n").replace("\r", "\n")
        actual = "\n".join(lines[start - 1:end])
        if actual != expected:
            raise OpError(
                f"ERR|STALE_RANGE|{args[0]}|current lines {start}-{end} differ from EXPECT; "
                "FETCH the current range and rebuild this edit"
            )
        newline = newline_style(doc)
        replacement = op.extra.replace("\r\n", "\n").replace("\r", "\n")
        replacement_lines = replacement.splitlines() if replacement else []
        updated = lines[:start - 1] + replacement_lines + lines[end:]
        new_doc = newline.join(updated)
        if doc.endswith(("\n", "\r")) and updated:
            new_doc += newline
        fs.write(path, new_doc)
        return f"Edited exact lines {start}-{end} in {args[0]}"

    if command in {"EDIT", "INSERT_BEFORE", "INSERT_AFTER", "APPEND", "PREPEND"}:
        path = safe_path(args[0])
        if not fs.is_file(path):
            raise OpError(f"ERR|FILE_NOT_FOUND|{args[0]}")

        doc = fs.read(path)
        newline = newline_style(doc)

        if command == "EDIT":
            match = _cached_match(op, doc, with_newlines(op.data, newline), "SEARCH", _match_cache)
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
            match = _cached_match(op, doc, with_newlines(op.data, newline), "MARKER", _match_cache)
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
                parent_dir = _project_root()
                for p in parent_dir.rglob("*"):
                    rel_p = rel(p)
                    if rx.search(rel_p) and not any(part in PROTECTED_NAMES for part in p.parts):
                        matched_paths.append(p)
            else:
                for p in _project_root().glob(pattern):
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
            if "/" in path_str or (_project_root() / path_str).is_dir():
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
        if op.command in {"FETCH", "CHMOD", "EDIT_RANGE"} and idx > 0:
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
        elif cmd in {"EDIT", "EDIT_RANGE", "INSERT_BEFORE", "INSERT_AFTER", "APPEND", "PREPEND", "REPLACE_ALL", "PATCH"}:
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
            except OpError as exc:
                if "ERR|STALE_FILE|" in str(exc):
                    raise
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
    match_cache maps id(op) -> MatchResult with a source fingerprint for
    EDIT/INSERT ops. Preview and apply reuse offsets only for that content.
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
                if op.command in {"FETCH", "CHMOD", "EDIT_RANGE"} and idx > 0:
                    continue
                if idx == 0 and op.command in {"MOVE", "COPY"} and (any(ch in path_arg for ch in ("*", "?", "[")) or path_arg.startswith("regex:")):
                    continue
                args_to_check.append(path_arg)
            for path_arg in args_to_check:
                path_str = str(safe_path(path_arg, follow_leaf=False))
                history = file_history.setdefault(path_str, [])
                if op.command == "CREATE" and "CREATE" in history:
                    raise OpError(f"Operation {number} ({describe_operation(op)}){_loc(op)}: ERR|CONFLICTING_OPERATIONS|{path_arg} - multiple CREATE commands for same file")
                if op.command in {"EDIT", "EDIT_RANGE", "APPEND", "PREPEND", "INSERT_BEFORE", "INSERT_AFTER", "REPLACE_ALL", "CHMOD", "PATCH"}:
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
            "  ✅ **Do**: Make your SEARCH block more precise by including unique surrounding context.\n"
            "  ❌ **Do NOT**: Try to guess which match is correct. Read the structured DUPLICATE_MATCH error "
            "details to decide how to proceed safely."
        )
    if "ERR|SEARCH_TOO_BIG" in error_msg:
        hints.append(
            "- **SEARCH_TOO_BIG**: The SEARCH block exceeds the 60-line / 4000-char limit.\n"
            "  ✅ **Do**: Shrink the SEARCH block to a 3–6 line unique anchor. "
            "For large regions, FETCH the current numbered lines and use EDIT_RANGE with exact EXPECT text.\n"
            "  ❌ **Do NOT**: Include entire functions, classes, or components in a SEARCH block."
        )
    if "ERR|STALE_RANGE" in error_msg:
        hints.append(
            "- **STALE_RANGE**: The numbered lines no longer contain the exact EXPECT text, or "
            "the range is outside the current file. Do not broaden the match or move the edit "
            "automatically. Request the current lines with `FETCH path:start-end`, then rebuild "
            "EDIT_RANGE using the returned line numbers and exact text."
        )
    if "ERR|STALE_FILE" in error_msg:
        hints.append(
            "- **STALE_FILE**: File content changed after validation or preview. "
            "FETCH the current file and rebuild the plan; do not reuse cached positions "
            "or broaden the match to force the old plan through."
        )
    if "ERR|CREATE_EXISTS" in error_msg:
        hints.append(
            "- **CREATE_EXISTS**: The file already exists — CREATE only works for new files.\n"
            "  ✅ **Do**: Use `EDIT <file>` with SEARCH/REPLACE to modify it, "
            "or `REPLACE_ALL <file>` to replace every occurrence of a pattern.\n"
            "  ❌ **Do NOT**: Use CREATE on a file that already exists."
        )
    if "ERR|FILE_NOT_FOUND" in error_msg:
        nearby: list[str] = []
        parent_dir = ""
        m = re.search(r"ERR\|FILE_NOT_FOUND\|([^\s|]+)", error_msg)
        req_file = m.group(1) if m else '?'
        if m:
            try:
                missing_path = Path(req_file)
                parent = (_project_root() / missing_path.parent) if missing_path.parent != Path(".") else _project_root()
                if parent.is_dir():
                    parent_dir = missing_path.parent.as_posix() + "/"
                    candidates = sorted(
                        p.relative_to(_project_root()).as_posix()
                        for p in parent.iterdir()
                        if not p.name.startswith(".")
                    )[:10]
                    if candidates:
                        nearby = candidates
            except Exception:
                pass

        possible_files = "\n".join(f"- {Path(p).name}" for p in nearby) if nearby else "No files found."

        struct_err = (
            "CODE_EXEC_ERROR\n"
            "status: error\n"
            "type: FILE_NOT_FOUND\n"
            "operation: EDIT\n"
            f"requested_file: {req_file}\n\n"
            "The requested file does not exist.\n\n"
            f"Existing parent folder:\n{parent_dir or 'N/A'}\n\n"
            f"Possible files/folders:\n{possible_files}\n\n"
            "Confirm which file should be used.\n"
            "  ✅ **Do**: Verify the exact relative path from the project root.\n"
            "  ❌ **Do NOT**: Guess paths. Only use paths that appear in the file list provided in context."
        )
        hints.append(struct_err)
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
    if "ERR|FUZZY_SYNTAX_ERROR" in error_msg:
        hints.append(
            "- **FUZZY_SYNTAX_ERROR**: The replacement caused syntax errors or broken bracket balance in the file.\n"
            "  ✅ **Do**: Look at the current file content provided below. Write an exact, verbatim SEARCH block "
            "with balanced opening and closing braces/brackets.\n"
            "  ❌ **Do NOT**: Rely on fuzzy matching for large code blocks. Never omit closing brackets."
        )
    if "ERR|PATCH_FAILED" in error_msg:
        hints.append(
            "- **PATCH_FAILED**: A unified diff hunk could not be applied.\n"
            "  ✅ **Do**: Verify the context lines match current file contents exactly. "
            "Alternatively, switch to `EDIT` with SEARCH/REPLACE instead of PATCH.\n"
            "  ❌ **Do NOT**: Generate a unified diff from memory — always base it on the actual file."
        )
    # Parser syntax errors are ValueErrors rather than ERR| codes. Match their
    # stable message fragments so the copied retry prompt can explain the
    # exact syntax expected instead of falling back to generic advice.
    parser_hints = []
    if "requires SEARCH" in error_msg or "requires MARKER" in error_msg:
        parser_hints.append(
            "- **Missing search/marker block**: EDIT and REPLACE_ALL require SEARCH then REPLACE; "
            "INSERT_BEFORE and INSERT_AFTER require MARKER then CONTENT. Put each keyword on its "
            "own line and close each block with `>>>`, or use the `<<<< / ==== / >>>>` style."
        )
    if "requires EXPECT" in error_msg:
        parser_hints.append(
            "- **EDIT_RANGE block syntax**: After `EDIT_RANGE path start-end`, provide an `EXPECT` "
            "block containing the exact current text for those inclusive 1-based lines, followed "
            "by a `REPLACE` block. Use a recent FETCH for both the line numbers and expected text."
        )
    if "requires REPLACE" in error_msg or "requires CONTENT" in error_msg:
        parser_hints.append(
            "- **Missing replacement/content block**: After the first closed SEARCH or MARKER block, "
            "include the matching REPLACE or CONTENT block. Do not mix keyword blocks with `====` "
            "conflict-marker blocks."
        )
    if any(fragment in error_msg for fragment in ("Missing >>>", "missing >>>>", "missing ====")):
        parser_hints.append(
            "- **Unclosed block**: Close every `<<<` block with a line containing `>>>`. For "
            "conflict-marker style, use `<<<<`, `====`, and `>>>>` in that order. Check the "
            "reported line number and keep code content between the delimiters."
        )
    if "requires 'source -> destination'" in error_msg:
        parser_hints.append(
            "- **MOVE/COPY/RENAME syntax**: Use `MOVE source -> destination` (likewise COPY or RENAME) "
            "with both paths on the command line."
        )
    if "CHMOD requires 'path mode'" in error_msg:
        parser_hints.append(
            "- **CHMOD syntax**: Include both the relative path and mode, for example "
            "`CHMOD scripts/run.sh +x`."
        )
    if "requires an argument" in error_msg:
        parser_hints.append(
            "- **Missing command argument**: Add the required path or value after the command. "
            "For example, use `DELETE path/to/file` or `MKDIR path/to/folder`."
        )
    if parser_hints:
        hints.extend(parser_hints)
        hints.append(
            "- **Before retrying**: Correct only the reported syntax issue, preserve the intended "
            "operation, and do not invent file contents or paths. The parser's line number and "
            "error text are the source of truth."
        )
    if not hints:
        hints.append(
            "- Review the error details above and fix the problematic command or target block. "
            "Use its reported line number; do not guess file contents or paths."
        )
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


def _recovery_guidance(state: str) -> str:
    messages = {
        "no_changes": "No changes applied by journaled file operations. Correct the error and rebuild the full intended plan using current context.",
        "rolled_back": "Complete rollback. Earlier file operations were reverted. Correct the error and rebuild the full intended plan, including the reverted operations.",
        "retained": "Changes retained. FETCH current content and resume only unfinished work; do not repeat file operations whose changes remain. Use code-exec undo to revert journaled file changes if needed.",
        "incomplete_rollback": "Incomplete rollback. Recover the files using the retained backup information before another executable plan. Inspect current context; do not assume earlier operations remain or were reverted.",
        "unknown": "Recovery outcome unknown. FETCH current content and establish which changes remain before emitting another executable plan.",
    }
    return messages[state] + " Command side effects are not automatically reversed."


def copy_error_to_clipboard(error_msg: str, *, recovery_state: str = "unknown", recovery_details: str = "") -> None:
    clean_err = error_msg.strip()
    guidance = _get_error_guidance(clean_err)
    fence = chr(96) * 3
    op_context = _extract_operation_context(clean_err)
    candidate_diff = _extract_candidate_diff(clean_err)

    op_section = f"\n{op_context}\n" if op_context else ""

    # ---- Target file resolution & context attachment ----
    file_section = ""
    affected_file: str | None = None
    attempt = 1

    err_code = "UNKNOWN"
    err_code_m = re.search(r"ERR\|(\w+)", clean_err)
    if err_code_m:
        err_code = err_code_m.group(1)

    # 1. Look for Operation N (CMD path)
    op_m = re.search(
        r"Operation\s+\d+\s+\(\s*(?:EDIT|EDIT_RANGE|CREATE|APPEND|PREPEND|INSERT_\w+|REPLACE_ALL|PATCH)\s+([^\s)]+)",
        clean_err,
    )
    if op_m:
        cand = op_m.group(1).split(":")[0].strip()
        if (_project_root() / cand).is_file():
            affected_file = cand

    # 2. Look for ERR|CODE|filepath
    if not affected_file:
        raw_m = re.search(r"ERR\|\w+\|([^\s|]+)", clean_err)
        if raw_m:
            cand = raw_m.group(1).split(":")[0].strip()
            if (_project_root() / cand).is_file():
                affected_file = cand

    # 3. Look for 'in <filepath>'
    if not affected_file:
        in_m = re.search(r"\bin\s+([a-zA-Z0-9_\-./\\]+\.[a-zA-Z0-9_]+)", clean_err)
        if in_m:
            cand = in_m.group(1).strip()
            if (_project_root() / cand).is_file():
                affected_file = cand

    if affected_file:
        key = (affected_file, err_code)
        _error_history[key] = _error_history.get(key, 0) + 1
        attempt = _error_history[key]

    if affected_file and attempt >= 2:
        try:
            file_path = _project_root() / affected_file
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
        "### Recovery outcome:\n\n"
        f"{_recovery_guidance(recovery_state)}\n{recovery_details}\n\n"
        "### Troubleshooting Guidance:\n\n"
        f"{guidance}\n\n"
        "### Instructions for the fix:\n"
        "- Follow the recovery outcome above when deciding which operations to include. Preserve the intended change and use current file content.\n"
        "- You may explain your analysis outside the code block.\n"
        f"- Then output a single revised {fence}code_exec ...{fence} block that corrects the issue."
        f"{file_section}"
    )
    try:
        set_clipboard(prompt)
    except Exception:
        pass


def fail(message: str) -> int:
    copy_error_to_clipboard(message, recovery_state="no_changes")
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

    if not (_project_root() / ".git").exists():
        raise OpError("ERR|NOT_GIT_REPO|Current directory is not a git repository (.git missing)")
    diff_res = subprocess.run(["git", "diff", "HEAD"], cwd=_project_root(), capture_output=True, text=True)
    diff_text = diff_res.stdout.strip()
    if not diff_text:
        diff_cached = subprocess.run(["git", "diff", "--cached"], cwd=_project_root(), capture_output=True, text=True)
        diff_unstaged = subprocess.run(["git", "diff"], cwd=_project_root(), capture_output=True, text=True)
        diff_text = f"{diff_cached.stdout}\n{diff_unstaged.stdout}".strip()
    status_res = subprocess.run(["git", "status", "--porcelain"], cwd=_project_root(), capture_output=True, text=True)
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

    out_folder = _project_root() / "context"
    dump_file = None

    if tokens > MAX_CLIPBOARD_TOKENS or len(prompt_body.encode("utf-8")) > MAX_CLIPBOARD_BYTES:
        out_folder.mkdir(parents=True, exist_ok=True)
        dump_path = out_folder / "COMMIT_DIFF.md"
        dump_path.write_text(prompt_body, encoding="utf-8")
        copied_file = set_clipboard_file(dump_path)
        if not copied_file:
            set_clipboard(prompt_body)
        dump_file = dump_path.relative_to(_project_root())
    else:
        set_clipboard(prompt_body)

    return prompt_body, tokens, dump_file


def perform_git_commit(message: str, paths: list[str]) -> tuple[bool, str]:
    try:
        if not paths:
            status_res = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=_project_root(), capture_output=True, text=True, check=True
            )
            if not status_res.stdout.strip():
                return False, "No changes detected in git repository to commit."
            subprocess.run(["git", "add", "-A"], cwd=_project_root(), check=True)
            diff_cached = subprocess.run(
                ["git", "diff", "--cached", "--name-only"],
                cwd=_project_root(), capture_output=True, text=True, check=True
            )
            if not diff_cached.stdout.strip():
                return False, "No staged changes detected in repository."
            res = subprocess.run(
                ["git", "commit", "-m", message],
                cwd=_project_root(), capture_output=True, text=True, check=True
            )
            return True, res.stdout.strip()

        subprocess.run(["git", "add", "--", *paths], cwd=_project_root(), check=True)
        diff_cached = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--", *paths],
            cwd=_project_root(), capture_output=True, text=True, check=True
        )
        if not diff_cached.stdout.strip():
            return False, "No staged changes detected for modified files."
        res = subprocess.run(
            ["git", "commit", "-m", message, "--", *paths],
            cwd=_project_root(), capture_output=True, text=True, check=True
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

    out_folder = _project_root() / "context"
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
    if (_project_root() / ".code-exec-verify").is_file():
        cmd = (_project_root() / ".code-exec-verify").read_text(encoding="utf-8").strip()
        if cmd:
            return cmd

    if (_project_root() / "package.json").is_file():
        if (_project_root() / "tsconfig.json").is_file():
            return "npx tsc --noEmit"
        return "npm test"
    elif (_project_root() / "Cargo.toml").is_file():
        return "cargo test"
    elif (_project_root() / "pyproject.toml").is_file() or (_project_root() / "setup.py").is_file() or any(_project_root().glob("test*.py")):
        if shutil.which("ruff"):
            return "ruff check ."
        elif shutil.which("pytest") and (_project_root() / "tests").is_dir():
            return "pytest"
        return "python3 -m unittest"
    return None


def copy_verification_error_to_clipboard(cmd: str, returncode: int, output: str, modified_files: list[str], *, recovery_details: str = "") -> None:
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
        f"### Recovery outcome:\n\n{_recovery_guidance('retained')}\n{recovery_details}\n\n"
        f"### Instructions for the fix:\n"
        f"- Analyze the compiler, linter, or test failures shown above.\n"
        f"- FETCH current content before editing; the applied file changes remain and no commit was made by this plan.\n"
        f"- Fix only the errors in {file_list}.\n"
        f"- Provide an updated {fence}code_exec ...{fence} plan block with the necessary EDITs."
    )
    try:
        set_clipboard(prompt)
    except Exception:
        pass


def _save_recovery(fs: RealFS, *, recovery_required: bool = False) -> tuple[bool, str]:
    if not fs.journal:
        return True, "No journaled file changes to undo."
    try:
        fs.save_backup_manifest(recovery_required=recovery_required)
    except Exception as exc:
        details = (
            f"ERR|RECOVERY_SAVE_FAILED|{exc}. Recovery manifest was not saved. "
            + (f"Backup data retained at {fs.backup_dir}; inspect it before retrying."
               if fs.backup_dir is not None else "No backup directory could be created; inspect current files before retrying.")
        )
        ui.warn(details)
        return False, details
    return True, f"Recovery manifest saved at {fs.backup_dir / 'manifest.json'}. Use code-exec undo to restore journaled file changes."


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
                if op.command in {"CREATE", "EDIT", "EDIT_RANGE", "DELETE", "APPEND", "PREPEND", "INSERT_BEFORE", "INSERT_AFTER", "REPLACE_ALL", "TOUCH", "CHMOD", "PATCH", "MKDIR"}:
                    modified_paths.append(op.args[0])
                elif op.command in {"MOVE", "COPY", "RENAME"}:
                    src_arg = op.args[0]
                    if not (any(ch in src_arg for ch in ("*", "?", "[")) or src_arg.startswith("regex:")):
                        modified_paths.append(src_arg)
                    modified_paths.append(op.args[1])

    except CommandFailed as exc:
        saved, details = _save_recovery(fs)
        state = "retained" if fs.journal else "no_changes"
        ui.command_failed(str(exc), fs.backup_dir, changes_kept=bool(fs.journal), recovery_saved=saved)
        copy_error_to_clipboard(str(exc), recovery_state=state, recovery_details=details)
        return 1
    except (Exception, KeyboardInterrupt) as exc:
        ui.apply_interrupted(exc)
        state, details = "no_changes", ""
        if hasattr(fs, "journal"):
            count = len(fs.journal)
            errors = fs.rollback()
            ui.rollback_report(errors, count, fs.backup_dir, fs.ran_commands)
            if not errors:
                fs.cleanup()
                state = "rolled_back" if count else "no_changes"
            else:
                state = "incomplete_rollback"
                _, details = _save_recovery(fs, recovery_required=True)
                details = "\n".join(errors) + "\n" + details
        copy_error_to_clipboard(str(exc) or "Execution interrupted by user.", recovery_state=state, recovery_details=details)
        return 1

    if dry_run:
        ui.dry_run()
        return 0

    saved, recovery_details = _save_recovery(fs)
    if not saved:
        copy_error_to_clipboard(recovery_details, recovery_state="retained", recovery_details=recovery_details)
        return 1
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
                    cwd=str(_project_root()),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=timeout or 60,
                )
                if res.returncode != 0:
                    ui.error(f"Verification failed ({actual_verify_cmd}): exit code {res.returncode}")
                    print(res.stdout)
                    unique_modified = list(dict.fromkeys(modified_paths))
                    copy_verification_error_to_clipboard(actual_verify_cmd, res.returncode, res.stdout, unique_modified, recovery_details=recovery_details)
                    print(ui.palette.paint("  📋 Verification error and fix prompt copied to clipboard!\n", ui.palette.AMBER, bold=True))
                    return res.returncode
                else:
                    print(ui.palette.paint("  ✓ Verification passed cleanly!\n", ui.palette.GREEN, bold=True))
            except subprocess.TimeoutExpired:
                message = f"Verification timed out after {timeout or 60} seconds."
                ui.error(message)
                copy_error_to_clipboard(message, recovery_state="retained", recovery_details=recovery_details)
                return 1
            except Exception as exc:
                message = f"Failed to execute verification hook: {exc}"
                ui.error(message)
                copy_error_to_clipboard(message, recovery_state="retained", recovery_details=recovery_details)
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
