"""
code_exec_watch.py - Real-time clipboard watcher for code_exec plans.
Monitors system clipboard for ```code_exec``` blocks and prompts for instant preview and execution.
"""

from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path
from typing import Any

from code_exec_parser import extract_plan, get_clipboard, parse_operations
from code_exec_types import OpError


def _clip_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def watch_clipboard(
    args: Any,
    root: Path,
    ui: Any,
    preflight_fn: Any,
    apply_plan_fn: Any,
    generate_diff_fn: Any,
    poll_interval: float = 0.5,
    max_loops: int | None = None,  # For unit testing
) -> int:
    """
    Watches system clipboard for new code_exec plans.
    Polls every `poll_interval` seconds.
    """
    ui.watch_started(root)

    try:
        initial_clip = get_clipboard()
        last_hash = _clip_hash(initial_clip) if initial_clip else ""
    except Exception:
        last_hash = ""

    loops = 0
    while True:
        if max_loops is not None and loops >= max_loops:
            break
        loops += 1

        try:
            time.sleep(poll_interval)
            current_clip = get_clipboard()
        except KeyboardInterrupt:
            print(ui.palette.paint("\n\n  Exited watch mode (Ctrl+C).\n", ui.palette.SLATE))
            return 0
        except Exception:
            continue

        if not current_clip:
            continue

        curr_h = _clip_hash(current_clip)
        if curr_h == last_hash:
            continue

        # Content changed: update hash
        last_hash = curr_h

        # Quick pre-filter: does it contain code_exec markers or common plan keywords?
        quick_candidates = ("code_exec", "<<<", "EDIT ", "CREATE ", "DELETE ", "MOVE ", "COPY ", "RUN ", "FETCH ")
        if not any(k in current_clip for k in quick_candidates):
            continue

        try:
            plan_text = extract_plan(current_clip)
        except OpError:
            continue

        if not plan_text or not plan_text.strip():
            continue

        try:
            operations = parse_operations(plan_text)
        except Exception as exc:
            ui.warn(f"Detected plan in clipboard could not be parsed: {exc}")
            continue

        if not operations:
            continue

        # Plan detected! Trigger audible notification and render detection card
        try:
            sys.stdout.write("\a")
            sys.stdout.flush()
        except Exception:
            pass

        unique_files = len(set(op.args[0] for op in operations if op.args and isinstance(op.args[0], str)))
        ui.watch_plan_detected(len(operations), unique_files)

        # Preflight validation
        try:
            reason, deferred, match_cache = preflight_fn(operations)
        except OpError as exc:
            ui.warn(f"Plan validation failed: {exc}\n  Fix the issue or regenerate plan from AI.")
            print(ui.palette.paint("\n  👀 Continuing to watch clipboard...\n", ui.palette.SLATE))
            continue

        ui.show_plan(operations, reason, deferred)
        try:
            diff_text = generate_diff_fn(operations, _match_cache=match_cache)
        except TypeError:
            diff_text = generate_diff_fn(operations)

        if getattr(args, "diff", False):
            ui.show_diff(diff_text)

        # Auto-apply if --yes is set
        if getattr(args, "yes", False):
            apply_plan_fn(
                operations,
                args.timeout,
                no_commit=args.no_commit,
                auto_commit=True,
                dry_run=args.dry_run,
                match_cache=match_cache,
                verify_cmd=getattr(args, "verify", None),
            )
            print(ui.palette.paint("\n  ✓ Plan applied automatically (--yes). Resuming watch...\n", ui.palette.GREEN, bold=True))
            continue

        # Interactive confirmation loop
        while True:
            try:
                choice = ui.prompt_choice(
                    "Action for detected plan? [y] Apply  [n] Skip  [d] Diff  [t] Tree  [q] Quit watch",
                    default="y",
                ).lower().strip()
            except (KeyboardInterrupt, EOFError):
                print(ui.palette.paint("\n\n  Exited watch mode.\n", ui.palette.SLATE))
                return 0

            if choice in {"y", "yes"}:
                apply_plan_fn(
                    operations,
                    args.timeout,
                    no_commit=args.no_commit,
                    auto_commit=False,
                    dry_run=args.dry_run,
                    match_cache=match_cache,
                    verify_cmd=getattr(args, "verify", None),
                )
                print(ui.palette.paint("\n  ✓ Resuming watch... Copy another AI reply to apply.\n", ui.palette.SLATE))
                break
            elif choice in {"n", "no", "skip"}:
                print(ui.palette.paint("\n  Skipped plan. Resuming watch...\n", ui.palette.SLATE))
                break
            elif choice in {"d", "diff"}:
                ui.show_diff(diff_text)
            elif choice in {"t", "tree"}:
                ui.show_tree(operations)
            elif choice in {"q", "quit", "exit"}:
                print(ui.palette.paint("\n  Exited watch mode.\n", ui.palette.SLATE))
                return 0
            else:
                ui.warn(f"Unknown choice '{choice}'. Enter 'y', 'n', 'd', 't', or 'q'.")

    return 0
