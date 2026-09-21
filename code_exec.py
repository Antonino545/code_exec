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
    _adjust_indentation,
    _find_closest_match,
    _find_high_similarity_match,
    _get_leading_indent,
    _match_line_spans,
    _normalize_jsx_line,
    _strip_symbols_and_emojis,
    apply_unified_patch,
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


# ============================================================
# Operation execution
# ============================================================

def execute(op: Operation, fs) -> str | None:
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
            match = find_unique(doc, with_newlines(op.data, newline), "SEARCH", args[0])
            replacement = with_newlines(op.extra, newline)

            # Auto-align indentation if matching occurred under indentation drift
            if match.fuzzy and match.line_range is not None:
                replacement = _adjust_indentation(doc, match.line_range, op.data, replacement, newline)

            new = doc[: match.start] + replacement + doc[match.end :]
            note = match.note
            done = "Edited"

        elif command in {"INSERT_BEFORE", "INSERT_AFTER"}:
            match = find_unique(doc, with_newlines(op.data, newline), "MARKER", args[0])
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
            new = bom + content + doc[len(bom) :]
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
        count = doc.count(search)
        if count == 0:
            raise OpError(f"ERR|SEARCH_NOT_FOUND|{args[0]} - pattern not found for REPLACE_ALL")
        new = doc.replace(search, replace)
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
        src = safe_path(args[0], follow_leaf=(command == "COPY"))
        dst = safe_path(args[1], follow_leaf=False)
        if not fs.lexists(src):
            raise OpError(f"Source does not exist: {args[0]}")
        if fs.lexists(dst):
            raise OpError(f"Destination already exists: {args[1]}")
        if src in dst.parents:
            raise OpError(f"Cannot {command.lower()} {args[0]} into itself")
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

    if command == "RUN":
        fs.run(args[0])
        return None

    if command == "COMMIT":
        return None

    raise OpError(f"Unsupported command: {command}")


def check_paths(op: Operation) -> None:
    if op.command in {"RUN", "COMMIT"}:
        return
    for value in op.args:
        safe_path(value, follow_leaf=False)


def generate_plan_diff(operations: list[Operation]) -> str:
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
                execute(op, vfs)
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
                execute(op, vfs)
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
                execute(op, vfs)
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
                execute(op, vfs)
            except Exception:
                pass
        elif cmd in {"MOVE", "RENAME"}:
            diff_lines.append(f"--- a/{op.args[0]}\n+++ b/{op.args[1]}\n@@ move/rename @@\n")
            try:
                execute(op, vfs)
            except Exception:
                pass
    return "".join(diff_lines)


def preflight(operations: list[Operation]) -> tuple[str | None, int]:
    vfs = VirtualFS()
    reason = None
    trigger = 0

    # Phase 1: Static Consistency & Conflict Verification
    file_history: dict[str, list[str]] = {}
    for number, op in enumerate(operations, 1):
        if op.command == "RUN":
            validate_run_command(op.args[0])
        elif op.command not in {"RUN", "COMMIT"}:
            for path_arg in op.args:
                path_str = str(safe_path(path_arg, follow_leaf=False))
                history = file_history.setdefault(path_str, [])
                if op.command == "CREATE" and "CREATE" in history:
                    raise OpError(f"Operation {number} ({describe_operation(op)}): ERR|CONFLICTING_OPERATIONS|{path_arg} - multiple CREATE commands for same file")
                if op.command in {"EDIT", "APPEND", "PREPEND", "INSERT_BEFORE", "INSERT_AFTER", "REPLACE_ALL", "CHMOD", "PATCH"}:
                    if "DELETE" in history:
                        raise OpError(f"Operation {number} ({describe_operation(op)}): ERR|CONFLICTING_OPERATIONS|{path_arg} - attempting to operate on a file that was DELETED in the same plan")
                history.append(op.command)

    # Phase 2: Virtual Simulation
    for number, op in enumerate(operations, 1):
        try:
            if reason is not None:
                check_paths(op)
            elif op.command == "RUN":
                reason, trigger = "a RUN command", number
            else:
                try:
                    execute(op, vfs)
                except Unverifiable as exc:
                    reason, trigger = str(exc), number
        except OpError as exc:
            raise OpError(
                f"Operation {number} ({describe_operation(op)}): {exc}"
            ) from None

    return reason, (len(operations) - trigger if reason else 0)


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
            "- **SEARCH_NOT_FOUND**: The target SEARCH snippet does not exist in the file. "
            "Inspect the 'Closest candidate' diff above: lines marked with '+' show the actual code currently in the file. "
            "Update your SEARCH block to match those real lines verbatim."
        )
    if "ERR|SEARCH_AMBIGUOUS" in error_msg:
        hints.append(
            "- **SEARCH_AMBIGUOUS**: The SEARCH snippet matched multiple locations. "
            "Include 1-3 lines of surrounding code before or after the target to form a unique local anchor."
        )
    if "ERR|SEARCH_TOO_BIG" in error_msg:
        hints.append(
            "- **SEARCH_TOO_BIG**: The SEARCH block is too large (max 60 lines / 4000 characters). "
            "Shrink the SEARCH block to a 3-6 line unique local anchor rather than whole functions or components."
        )
    if "ERR|CREATE_EXISTS" in error_msg:
        hints.append(
            "- **CREATE_EXISTS**: The file already exists. "
            "Use `EDIT <file>` or `REPLACE_ALL <file>` to modify existing files instead of CREATE."
        )
    if "ERR|FILE_NOT_FOUND" in error_msg:
        hints.append(
            "- **FILE_NOT_FOUND**: The file to edit or insert into does not exist. "
            "Verify the relative file path, or use `CREATE <file>` if you meant to create a new file."
        )
    if "ERR|CONFLICTING_OPERATIONS" in error_msg:
        hints.append(
            "- **CONFLICTING_OPERATIONS**: The plan contains contradictory operations on the same file. "
            "Combine edits sequentially and never EDIT or APPEND after a DELETE."
        )
    if "ERR|MULTIPLE_PLANS" in error_msg:
        hints.append(
            "- **MULTIPLE_PLANS**: More than one plan block was detected. "
            "Output exactly ONE single plan block in your response."
        )
    if "ERR|PLAN_NOT_FOUND" in error_msg:
        hints.append(
            "- **PLAN_NOT_FOUND**: No executable plan block was detected. "
            "Ensure your plan starts with an executable code_exec block."
        )
    if "ERR|FORBIDDEN_COMMAND" in error_msg:
        hints.append(
            "- **FORBIDDEN_COMMAND**: The RUN command is forbidden or destructive. "
            "Use standard test runners (e.g. pytest, python3 -m unittest, npm test, cargo test)."
        )
    if "ERR|UNKNOWN_COMMAND" in error_msg:
        hints.append(
            f"- **UNKNOWN_COMMAND**: An unrecognized command was used. Supported commands: {', '.join(sorted(COMMANDS))}. "
            "Check the suggested command hint in the error and update your instruction."
        )
    if "ERR|PATCH_FAILED" in error_msg:
        hints.append(
            "- **PATCH_FAILED**: A unified diff hunk could not be applied. "
            "Verify the context lines against current file contents, or use EDIT with SEARCH/REPLACE instead."
        )
    if not hints:
        hints.append("- Review the error details above and fix the problematic command or target block.")
    return "\n".join(hints)


def copy_error_to_clipboard(error_msg: str) -> None:
    clean_err = error_msg.strip()
    guidance = _get_error_guidance(clean_err)
    fence = chr(96) * 3
    prompt = (
        "The previous `code_exec` plan failed with the following error:\n\n"
        f"{fence}\n{clean_err}\n{fence}\n\n"
        "### Troubleshooting Guidance:\n"
        f"{guidance}\n\n"
        "You may think and explain your analysis outside the code block. "
        f"Then output a single revised {fence}code_exec ... {fence} block fixing the issue."
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


def generate_commit_prompt() -> str:
    """Collects git diff and untracked files into an AI prompt for commit generation."""
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
    return (
        "Generate a concise, scoped conventional commit message for the following repository changes.\n\n"
        f"{changes_body}\n\n"
        "Output ONLY a single executable `code_exec` block:\n"
        "```code_exec\n"
        "COMMIT type(scope): concise summary\n"
        "```"
    )


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


def apply_plan(operations: list[Operation], timeout: int, no_commit: bool = False, auto_commit: bool = False, dry_run: bool = False) -> int:
    fs = VirtualFS() if dry_run else RealFS(timeout)

    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, _raise_interrupt)
        except (ValueError, OSError):
            pass

    exec_ops = [op for op in operations if op.command != "COMMIT"]
    commit_op = next((op for op in operations if op.command == "COMMIT"), None)
    commit_msg = commit_op.args[0] if commit_op else None

    ui.start_apply(len(exec_ops))
    modified_paths: list[str] = []

    try:
        step = 0
        for op in operations:
            if op.command == "COMMIT":
                continue
            step += 1
            try:
                message = execute(op, fs)
            except OpError as exc:
                raise type(exc)(
                    f"Operation {step} ({describe_operation(op)}): {exc}"
                ) from None
            if message:
                ui.step_done(step, len(exec_ops), message)
                if op.command in {"CREATE", "EDIT", "DELETE", "APPEND", "PREPEND", "INSERT_BEFORE", "INSERT_AFTER", "REPLACE_ALL", "TOUCH", "CHMOD", "PATCH", "MKDIR"}:
                    modified_paths.append(op.args[0])
                elif op.command in {"MOVE", "COPY", "RENAME"}:
                    modified_paths.extend([op.args[0], op.args[1]])

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
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="Deterministic local code executor", add_help=False)
    parser.add_argument("action", nargs="?", default=None,
                        help="Direct action: 'apply', 'undo', 'theme', or 'update'")
    parser.add_argument("subarg", nargs="?", default=None,
                        help="Sub-argument for actions (e.g., theme name)")
    parser.add_argument("-h", "--help", action="store_true",
                        help="Show interactive usage guide and options")
    parser.add_argument("-c", "--commit-prompt", "--docommit", dest="commit_prompt", action="store_true",
                        help="Copy git diff prompt to clipboard for AI commit message generation")
    parser.add_argument("-p", "--prompt", "--copy-instructions", dest="prompt", action="store_true",
                        help="Copy code_exec_instructions.md to the clipboard for your AI prompt")
    parser.add_argument("--diff", action="store_true",
                        help="Display unified diff of file changes before applying")
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
    args = parser.parse_args(argv)

    if args.action in {"1", "apply"}:
        args.action = "apply"
    elif args.action == "2":
        args.dry_run = True
        args.action = "apply"
    elif args.action in {"3", "prompt", "-p"}:
        args.prompt = True
        args.action = None
    elif args.action in {"4", "help", "-h"}:
        args.help = True
        args.action = None
    elif args.action in {"5", "update"}:
        from code_exec_updater import update_code_exec
        return 0 if update_code_exec() else 1
    elif args.action in {"6", "undo"}:
        success, msg = undo_last_run()
        if success:
            print(ui.palette.paint(f"\n    {msg}\n", ui.palette.GREEN, bold=True))
            return 0
        return fail(msg)
    elif args.action in {"7", "commit-prompt", "docommit", "commit"}:
        args.commit_prompt = True
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
        args.clipboard,
        args.file,
        args.dry_run,
        args.yes,
        args.no_run,
        args.no_commit,
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
        else:
            ui.error(f"Invalid option: {choice}")
            return 1

    if args.help:
        ui.show_guide()
        return 0

    if args.commit_prompt:
        try:
            prompt_body = generate_commit_prompt()
            set_clipboard(prompt_body)
        except Exception as exc:
            return fail(f"Could not generate commit prompt: {exc}")
        ui.commit_prompt_copied(len(prompt_body))
        return 0

    if args.prompt:
        instructions_path = Path(__file__).resolve().parent / "code_exec_instructions.md"
        if not instructions_path.is_file():
            return fail(f"Instructions file not found: {instructions_path.name} (checked {instructions_path.parent})")
        try:
            content = instructions_path.read_text(encoding="utf-8")
            set_clipboard(content)
        except Exception as exc:
            return fail(f"Could not copy instructions: {exc}")
        ui.instructions_copied(instructions_path)
        return 0

    if args.timeout < 0:
        parser.error("--timeout must be >= 0")
    if args.file == "-" and not (args.yes or args.dry_run):
        return fail("Reading the plan from stdin requires --yes or --dry-run "
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

    if args.no_run and any(op.command == "RUN" for op in operations):
        return fail("Plan contains RUN but --no-run was given. No files were modified.")

    try:
        reason, deferred = preflight(operations)
    except OpError as exc:
        return fail(f"Validation failed: {exc}\n\nNo files were modified.")

    ui.header(ROOT)
    ui.show_plan(operations, reason, deferred)

    diff_text = generate_plan_diff(operations)
    if args.diff:
        ui.show_diff(diff_text)

    if args.dry_run:
        return apply_plan(operations, args.timeout, no_commit=True, auto_commit=True, dry_run=True)

    if not args.yes:
        if not ui.confirm(diff_text=diff_text):
            ui.cancelled()
            return 0

    return apply_plan(operations, args.timeout, no_commit=args.no_commit, auto_commit=args.yes)


if __name__ == "__main__":
    sys.exit(main())
