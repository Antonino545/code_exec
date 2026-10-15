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
import code_exec_execution as _execution

# Plan workflow lives in a separate module; these names remain imported here
# for backwards compatibility with callers that imported helpers from
# ``code_exec``.
from code_exec_execution import (
    AUDIT_FUZZY_RESOLUTIONS,
    apply_plan,
    check_paths,
    copy_error_to_clipboard,
    copy_verification_error_to_clipboard,
    describe_operation,
    detect_verification_command,
    execute,
    fail,
    generate_commit_prompt,
    generate_plan_diff,
    handle_fetch_operations,
    interactive_fuzzy_resolver,
    preflight,
    perform_git_commit,
    read_input,
    should_show_folder_tree,
    validate_fuzzy_replacement_safety,
    _extract_candidate_diff,
    _extract_operation_context,
    _find_symbol_range,
    _get_error_guidance,
    _lines,
    _raise_interrupt,
    _error_history,
)


def _main_impl(argv=None) -> int:
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
        _execution.ROOT = target_root
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

    try:
        diff_text = generate_plan_diff(operations, _match_cache=match_cache)
    except OpError as exc:
        return fail(f"Preview failed: {exc}\n\nNo files were modified.")
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


def main(argv=None) -> int:
    """Run the CLI and restore process-global project state when it returns.

    The installed command exits after one call, while tests and embedding
    applications may call ``main`` repeatedly in the same process.
    """
    global ROOT
    import code_exec_types
    import code_exec_fs

    original_cwd = Path.cwd()
    original_root = ROOT
    original_types_root = code_exec_types.ROOT
    original_fs_root = code_exec_fs.ROOT
    original_backup_root = code_exec_fs.BACKUP_ROOT
    original_execution_root = _execution.ROOT
    try:
        return _main_impl(argv)
    finally:
        os.chdir(original_cwd)
        ROOT = original_root
        code_exec_types.ROOT = original_types_root
        code_exec_fs.ROOT = original_fs_root
        code_exec_fs.BACKUP_ROOT = original_backup_root
        _execution.ROOT = original_execution_root


if __name__ == "__main__":
    sys.exit(main())
