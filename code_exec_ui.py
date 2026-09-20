"""
code_exec_ui.py - Modern, minimal terminal UI inspired by Claude Code.
Provides clean ANSI typography, muted slate/coral palettes, and streamlined feedback.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from code_exec import Operation


class TerminalPalette:
    """ANSI 24-bit / 256-color palette with theme switching."""

    THEMES = {
        "claude": {
            "CORAL": "\033[38;2;217;119;87m",   # Anthropic Terracotta #D97757
            "AMBER": "\033[38;2;230;149;94m",   # Warm edit amber
            "GREEN": "\033[38;2;120;169;137m",  # Soft sage green
            "CYAN":  "\033[38;2;110;175;210m",  # Command sky blue
            "SLATE": "\033[38;2;135;140;148m",  # Neutral slate
            "RED":   "\033[38;2;225;98;89m",    # Muted crimson
            "WHITE": "\033[38;2;240;240;240m",
        },
        "catppuccin": {
            "CORAL": "\033[38;2;203;166;247m",  # Mauve
            "AMBER": "\033[38;2;250;179;135m",  # Peach
            "GREEN": "\033[38;2;166;227;161m",  # Green
            "CYAN":  "\033[38;2;137;180;250m",  # Blue
            "SLATE": "\033[38;2;108;112;134m",  # Overlay
            "RED":   "\033[38;2;243;139;168m",  # Red
            "WHITE": "\033[38;2;205;214;244m",  # Text
        },
        "tokyo-night": {
            "CORAL": "\033[38;2;187;154;247m",  # Violet
            "AMBER": "\033[38;2;255;158;100m",  # Orange
            "GREEN": "\033[38;2;158;206;106m",  # Lime
            "CYAN":  "\033[38;2;122;162;247m",  # Blue
            "SLATE": "\033[38;2;86;95;137m",    # Dark slate
            "RED":   "\033[38;2;247;118;142m",  # Red
            "WHITE": "\033[38;2;192;202;245m",
        },
    }

    def __init__(self):
        is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
        no_color = bool(os.environ.get("NO_COLOR")) or os.environ.get("TERM") == "dumb"
        self.enabled = is_tty and not no_color

        self.RESET = "\033[0m" if self.enabled else ""
        self.BOLD = "\033[1m" if self.enabled else ""
        self.DIM = "\033[2m" if self.enabled else ""

        # Determine active theme (env var -> ~/.config/code_exec/theme -> default "claude")
        theme_name = os.environ.get("CODE_EXEC_THEME", "").strip().lower()
        if not theme_name:
            cfg = Path.home() / ".config" / "code_exec" / "theme"
            if cfg.is_file():
                theme_name = cfg.read_text(encoding="utf-8").strip().lower()

        colors = self.THEMES.get(theme_name, self.THEMES["claude"])

        self.CORAL = colors["CORAL"] if self.enabled else ""
        self.AMBER = colors["AMBER"] if self.enabled else ""
        self.GREEN = colors["GREEN"] if self.enabled else ""
        self.CYAN = colors["CYAN"] if self.enabled else ""
        self.SLATE = colors["SLATE"] if self.enabled else ""
        self.RED = colors["RED"] if self.enabled else ""
        self.WHITE = colors["WHITE"] if self.enabled else ""

    def paint(self, text: str, color: str, bold: bool = False) -> str:
        if not self.enabled:
            return text
        prefix = f"{self.BOLD}{color}" if bold else color
        return f"{prefix}{text}{self.RESET}"


class TerminalUI:
    """Handles terminal formatting, progress reporting, and user prompts."""

    def __init__(self):
        self.palette = TerminalPalette()

    @staticmethod
    def _line_count(text: str | None) -> int:
        return 0 if not text else text.count("\n") + 1

    def describe_op(self, op: Operation) -> str:
        cmd, args = op.command, op.args
        c = self.palette

        tag = c.paint(f"{cmd:<13}", c.CORAL, bold=True)

        if cmd in {"MOVE", "COPY", "RENAME"}:
            arrow = c.paint("→", c.SLATE)
            return f"{tag} {args[0]} {arrow} {args[1]}"

        if cmd == "CREATE":
            lines = self._line_count(op.data)
            diff = c.paint(f"(+{lines} lines)", c.GREEN)
            return f"{tag} {args[0]} {diff}"

        if cmd == "EDIT":
            del_lines = self._line_count(op.data)
            add_lines = self._line_count(op.extra)
            diff = c.paint(f"(-{del_lines} / +{add_lines} lines)", c.AMBER)
            return f"{tag} {args[0]} {diff}"

        if cmd in {"INSERT_BEFORE", "INSERT_AFTER", "APPEND", "PREPEND"}:
            content = op.extra if cmd.startswith("INSERT") else op.data
            lines = self._line_count(content)
            diff = c.paint(f"(+{lines} lines)", c.GREEN)
            return f"{tag} {args[0]} {diff}"

        return f"{tag} {args[0]}"

    def header(self, root: Path) -> None:
        c = self.palette
        badge = c.paint("●", c.CORAL)
        title = c.paint("code-exec", c.WHITE, bold=True)
        sep = c.paint("·", c.SLATE)
        path_str = c.paint(str(root), c.SLATE)
        print(f"\n{badge} {title} {sep} {path_str}\n")

    def banner(self) -> None:
        pass

    def project_root(self, root: Path) -> None:
        self.header(root)

    def show_plan(
        self,
        operations: list[Operation],
        deferred_reason: str | None = None,
        deferred_count: int = 0,
    ) -> None:
        c = self.palette
        total = len(operations)
        print(c.paint(f"Planned operations ({total}):", c.WHITE, bold=True))

        for idx, op in enumerate(operations, 1):
            bullet = c.paint("  ●", c.SLATE)
            num = c.paint(f"{idx:>2}.", c.SLATE)
            print(f"{bullet} {num} {self.describe_op(op)}")

        if deferred_count:
            warn_bullet = c.paint("  ▲", c.AMBER)
            note = c.paint(
                f"{deferred_count} operation(s) after {deferred_reason} will be verified during execution.",
                c.SLATE,
            )
            print(f"\n{warn_bullet} {note}")
        print()

    def start_apply(self, total: int) -> None:
        c = self.palette
        noun = "operation" if total == 1 else "operations"
        print(c.paint(f"Applying {total} {noun}...\n", c.WHITE, bold=True))

    def step_done(self, idx: int, total: int, message: str) -> None:
        c = self.palette
        icon = c.paint("✔", c.GREEN)
        step = c.paint(f"[{idx}/{total}]", c.SLATE)
        print(f"  {icon} {step} {message}")

    def command(self, cmd: str) -> None:
        c = self.palette
        prompt = c.paint("  ❯", c.CYAN, bold=True)
        print(f"{prompt} {c.paint(cmd, c.WHITE)}", flush=True)

    def confirm(self) -> bool:
        c = self.palette
        prompt = c.paint("❯ Apply these changes? [y/N]: ", c.CORAL, bold=True)
        try:
            answer = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return answer in {"y", "yes"}

    def cancelled(self) -> None:
        c = self.palette
        print(c.paint("Cancelled. No files were modified.\n", c.SLATE))

    def dry_run(self) -> None:
        c = self.palette
        icon = c.paint("✔", c.GREEN)
        print(f"{icon} {c.paint('Dry run complete: plan is valid, no changes applied.', c.WHITE)}\n")

    def done(self, has_git: bool = False) -> None:
        c = self.palette
        icon = c.paint("✔", c.GREEN)
        title = c.paint("Done. All operations applied successfully.", c.WHITE, bold=True)
        print(f"\n{icon} {title}")
        if has_git:
            tip = c.paint("  Review changes with:", c.SLATE)
            cmd = c.paint("git diff", c.CYAN)
            print(f"{tip} {cmd}\n")

    def error(self, message: str) -> None:
        c = self.palette
        icon = c.paint("✖", c.RED, bold=True)
        msg = c.paint(f"Error: {message}", c.RED)
        print(f"\n{icon} {msg}\n", file=sys.stderr)

    def command_failed(self, error: str, backup_dir: Path | None) -> None:
        c = self.palette
        icon = c.paint("✖", c.RED, bold=True)
        print(f"\n{icon} {c.paint(f'Command failed: {error}', c.RED)}", file=sys.stderr)
        print(c.paint("  Execution stopped. Preceding file changes were kept.", c.SLATE), file=sys.stderr)
        if backup_dir is not None:
            print(c.paint(f"  Backups preserved in: {backup_dir}", c.SLATE), file=sys.stderr)

    def apply_interrupted(self, exc: Exception) -> None:
        c = self.palette
        icon = c.paint("✖", c.RED, bold=True)
        if isinstance(exc, KeyboardInterrupt):
            print(f"\n{icon} {c.paint('Execution interrupted by user.', c.RED)}", file=sys.stderr)
        else:
            print(f"\n{icon} {c.paint(f'Error applying changes: {exc}', c.RED)}", file=sys.stderr)

    def rollback_report(
        self,
        errors: list[str],
        count: int,
        backup_dir: Path | None,
        ran_commands: list[str],
    ) -> None:
        c = self.palette
        if errors:
            icon = c.paint("✖", c.RED, bold=True)
            print(f"\n{icon} {c.paint('Rollback was incomplete:', c.RED)}", file=sys.stderr)
            for err in errors:
                print(c.paint(f"    - {err}", c.RED), file=sys.stderr)
            if backup_dir is not None:
                print(c.paint(f"  Backups preserved in: {backup_dir}", c.SLATE), file=sys.stderr)
        else:
            icon = c.paint("✔", c.GREEN)
            msg = c.paint(f"Rolled back {count} change(s); no files were left modified.", c.SLATE)
            print(f"\n{icon} {msg}", file=sys.stderr)

        if ran_commands:
            print(c.paint("\n  Note: Commands already executed cannot be rolled back:", c.SLATE))
            for cmd in ran_commands:
                print(c.paint(f"    $ {cmd}", c.SLATE))


ui = TerminalUI()
