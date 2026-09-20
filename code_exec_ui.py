"""
code_exec_ui.py - Modern, minimal terminal UI inspired by Claude Code.
Provides clean ANSI typography, muted slate/coral palettes, and streamlined feedback.
"""

from __future__ import annotations

import os
import re
import shutil
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
        self.themes = self.THEMES
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

    def set_theme(self, name: str) -> None:
        if name in self.themes:
            self.current_theme = name
            theme_file = Path.home() / ".config" / "code_exec" / "theme"
            try:
                theme_file.parent.mkdir(parents=True, exist_ok=True)
                theme_file.write_text(name, "utf-8")
            except Exception:
                pass


_ANSI_STRIP_RE = re.compile(r"\033\[[0-9;]*m")


def _vlen(text: str) -> int:
    """Calculates visible character length without counting ANSI escape bytes."""
    return len(_ANSI_STRIP_RE.sub("", text))


class TerminalUI:
    """Claude Code inspired TUI with responsive boxed cards and status panels."""

    def __init__(self):
        self.palette = TerminalPalette()

    def _width(self, max_cols: int = 120) -> int:
        cols = shutil.get_terminal_size((80, 24)).columns
        return min(max(cols - 4, 58), max_cols)

    @staticmethod
    def _line_count(text: str | None) -> int:
        return 0 if not text else text.count("\n") + 1

    def _badge(self, cmd: str) -> str:
        c = self.palette
        colors = {
            "CREATE": c.GREEN,
            "EDIT": c.AMBER,
            "DELETE": c.RED,
            "MOVE": c.CYAN,
            "COPY": c.CYAN,
            "RENAME": c.CYAN,
            "RUN": c.CORAL,
            "COMMIT": c.CYAN,
        }
        color = colors.get(cmd, c.SLATE)
        return c.paint(f" {cmd:<6} ", color, bold=True)

    def describe_op(self, op: Operation) -> str:
        cmd, args = op.command, op.args
        c = self.palette
        badge = self._badge(cmd)

        if cmd in {"MOVE", "COPY", "RENAME"}:
            arrow = c.paint("→", c.SLATE)
            return f"{badge} {args[0]} {arrow} {args[1]}"

        if cmd == "CREATE":
            lines = self._line_count(op.data)
            diff = c.paint(f"(+{lines} lines)", c.GREEN)
            return f"{badge} {args[0]} {diff}"

        if cmd == "EDIT":
            del_lines = self._line_count(op.data)
            add_lines = self._line_count(op.extra)
            diff = c.paint(f"(-{del_lines} / +{add_lines})", c.AMBER)
            return f"{badge} {args[0]} {diff}"

        if cmd in {"INSERT_BEFORE", "INSERT_AFTER", "APPEND", "PREPEND"}:
            content = op.extra if cmd.startswith("INSERT") else op.data
            lines = self._line_count(content)
            diff = c.paint(f"(+{lines} lines)", c.GREEN)
            return f"{badge} {args[0]} {diff}"

        if cmd == "COMMIT":
            msg = c.paint(f'"{args[0]}"', c.WHITE)
            return f"{badge} {msg}"

        return f"{badge} {args[0]}"

    def show_guide(self) -> None:
        c = self.palette
        w = self._width()
        inner_w = w - 4

        title = c.paint(" code-exec · Quick Guide ", c.CORAL, bold=True)
        top = f"╭─{title}{'─' * max(0, inner_w - _vlen(title) + 1)}╮"
        bot = f"╰{'─' * (w - 2)}╯"

        def _row(text: str = "") -> None:
            pad = max(0, inner_w - _vlen(text))
            print(f"│ {text}{' ' * pad} │")

        print(f"\n{c.paint(top, c.SLATE)}")
        _row()
        _row(c.paint("Workflow:", c.WHITE, bold=True))
        _row(f"  {c.paint('1.', c.CORAL)} Run {c.paint('code-exec -p', c.CYAN)} to copy AI system instructions to clipboard.")
        _row(f"  {c.paint('2.', c.CORAL)} Ask your AI for a change; it generates an executable plan block.")
        _row(f"  {c.paint('3.', c.CORAL)} Copy the plan ({c.paint('⌘C', c.AMBER)}) and run {c.paint('code-exec', c.GREEN, bold=True)} (or {c.paint('code-exec apply', c.GREEN)}).")
        _row()
        _row(c.paint("Commands & Syntax:", c.WHITE, bold=True))
        _row(f"  {c.paint('CREATE', c.GREEN, bold=True):<18} {c.paint('path', c.SLATE)} <<< content >>>")
        _row(f"  {c.paint('EDIT', c.AMBER, bold=True):<18} {c.paint('path', c.SLATE)} SEARCH <<<...>>> REPLACE <<<...>>>")
        _row(f"  {c.paint('DELETE', c.RED, bold=True):<18} {c.paint('path', c.SLATE)}")
        _row(f"  {c.paint('MOVE', c.CYAN, bold=True):<18} {c.paint('src -> dst', c.SLATE)}")
        _row(f"  {c.paint('RUN', c.CORAL, bold=True):<18} {c.paint('shell command', c.SLATE)}")
        _row(f"  {c.paint('COMMIT', c.CYAN, bold=True):<18} {c.paint('commit message', c.SLATE)} (stages only modified files)")
        _row(f"  {c.paint('UPDATE', c.CYAN, bold=True):<18} {c.paint('code-exec update', c.SLATE)} (pulls latest commit from GitHub main)")
        _row()
        _row(f"  {c.paint('THEME', c.CYAN, bold=True):<18} {c.paint('code-exec theme <name>', c.SLATE)} (claude, catppuccin, tokyo-night)")
        _row()
        _row(c.paint("CLI Options & Commands:", c.WHITE, bold=True))
        _row(f"  {c.paint('code-exec', c.WHITE):<18} Open interactive launcher menu")
        _row(f"  {c.paint('code-exec apply', c.WHITE):<18} Apply plan directly from clipboard")
        _row(f"  {c.paint('code-exec undo', c.WHITE):<18} Revert changes made by the last plan")
        _row(f"  {c.paint('code-exec update', c.WHITE):<18} Check and install latest version from GitHub")
        _row(f"  {c.paint('code-exec theme <name>', c.WHITE):<18} Change terminal color theme")
        _row(f"  {c.paint('--diff', c.CYAN):<18} Display unified diff before applying changes")
        _row(f"  {c.paint('--prompt, -p', c.CYAN):<18} Copy AI instructions prompt to clipboard")
        _row(f"  {c.paint('--dry-run', c.CYAN):<18} Validate operations without modifying files")
        _row(f"  {c.paint('--yes', c.CYAN):<18} Apply changes directly without confirmation")
        _row(f"  {c.paint('--no-commit', c.CYAN):<18} Skip git commit prompt even if COMMIT is present")
        _row(f"  {c.paint('--file <path>', c.CYAN):<18} Read instructions from a file ('-' for stdin)")
        _row(f"  {c.paint('--help, -h', c.CYAN):<18} Show this guide")
        _row()
        print(f"{c.paint(bot, c.SLATE)}\n")

    def banner(self) -> None:
        c = self.palette
        # Claude Code inspired clean block font
        art = [
            r"  ____ ___  ____  _____   _______  _______ ____ ",
            r" / ___/ _ \|  _ \| ____| | ____\ \/ / ____/ ___|",
            r"| |  | | | | | | |  _|   |  _|  \  /|  _| | |   ",
            r"| |__| |_| | |_| | |___  | |___ /  \| |___| |___",
            r" \____\___/|____/|_____| |_____/_/\_\_____|\____|",
        ]
        print()
        for line in art:
            print(c.paint(line, c.CORAL, bold=True))
        print(f"  {c.paint('Local Deterministic Coding Agent', c.SLATE)}  {c.paint('v1.3', c.CYAN)}")
        print()

    def header(self, root: Path) -> None:
        c = self.palette
        w = self._width()
        inner_w = w - 4

        self.banner()

        raw_path = f"📁 {root}"
        if _vlen(raw_path) > inner_w - 4:
            raw_path = f"📁 ...{raw_path[-(inner_w - 10):]}"

        top = f"╭{'─' * (w - 2)}╮"
        bot = f"╰{'─' * (w - 2)}╯"
        path_line = f"│  {c.paint(raw_path, c.WHITE)}{' ' * max(0, inner_w - _vlen(raw_path) - 1)}│"

        print(c.paint(top, c.SLATE))
        print(path_line)
        print(c.paint(bot, c.SLATE))
        print()

    def project_root(self, root: Path) -> None:
        self.header(root)

    def show_plan(
        self,
        operations: list[Operation],
        deferred_reason: str | None = None,
        deferred_count: int = 0,
    ) -> None:
        c = self.palette
        w = self._width()
        inner_w = w - 4
        total = len(operations)

        title = c.paint(f" Planned Operations ({total}) ", c.WHITE, bold=True)
        top = f"╭─{title}{'─' * max(0, inner_w - _vlen(title) + 1)}╮"
        bot = f"╰{'─' * (w - 2)}╯"

        print(c.paint(top, c.SLATE))
        print(f"│{' ' * (inner_w + 2)}│")

        for idx, op in enumerate(operations, 1):
            num = c.paint(f"{idx:>2}.", c.SLATE)
            op_text = self.describe_op(op)
            line_str = f"  {num} {op_text}"
            pad = max(0, inner_w - _vlen(line_str))
            print(f"│ {line_str}{' ' * pad} │")

        if deferred_count:
            warn_msg = f"  ▲ {deferred_count} op(s) after {deferred_reason} verified at runtime"
            pad = max(0, inner_w - _vlen(warn_msg))
            print(f"│{' ' * (inner_w + 2)}│")
            print(f"│ {c.paint(warn_msg, c.AMBER)}{' ' * pad} │")

        print(f"│{' ' * (inner_w + 2)}│")
        print(f"{c.paint(bot, c.SLATE)}\n")

    def start_apply(self, total: int) -> None:
        c = self.palette
        w = self._width()
        inner_w = w - 4
        title = c.paint(f" Executing ({total}) ", c.WHITE, bold=True)
        top = f"╭─{title}{'─' * max(0, inner_w - _vlen(title) + 1)}╮"
        print(c.paint(top, c.SLATE))

    def step_done(self, idx: int, total: int, message: str) -> None:
        c = self.palette
        w = self._width()
        inner_w = w - 4

        icon = c.paint("✔", c.GREEN)
        step = c.paint(f"[{idx}/{total}]", c.SLATE)
        content = f"  {icon} {step} {message}"
        pad = max(0, inner_w - _vlen(content))
        print(f"│ {content}{' ' * pad} │")

    def command(self, cmd: str) -> None:
        c = self.palette
        w = self._width()
        inner_w = w - 4

        prompt = c.paint("❯", c.CYAN, bold=True)
        content = f"  {prompt} {c.paint(cmd, c.WHITE)}"
        pad = max(0, inner_w - _vlen(content))
        print(f"│ {content}{' ' * pad} │", flush=True)

    def confirm(self, diff_text: str | None = None) -> bool:
        c = self.palette
        while True:
            hint = " [y/N/v] (v: view diff): " if diff_text else " [y/N]: "
            prompt = c.paint(f"  Apply these changes?{hint}", c.CORAL, bold=True)
            try:
                answer = input(prompt).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return False
            if answer in {"v", "view"} and diff_text:
                self.show_diff(diff_text)
                continue
            return answer in {"y", "yes"}

    def show_diff(self, diff_text: str) -> None:
        if not diff_text.strip():
            print(self.palette.paint("\n    No diff detected for planned operations.\n", self.palette.SLATE))
            return
        c = self.palette
        cols = shutil.get_terminal_size((80, 24)).columns
        w = self._width(max_cols=max(cols - 2, 80))
        inner_w = w - 4
        title = c.paint(" Proposed Plan Diff ", c.CYAN, bold=True)
        top = f"\n {title}{' ' * max(0, inner_w - _vlen(title) + 1)}"
        bot = f" {' ' * (w - 2)}"
        print(c.paint(top, c.SLATE))
        for line in diff_text.splitlines():
            stripped = line.rstrip()
            if stripped.startswith("---") or stripped.startswith("+++"):
                colored = c.paint(stripped, c.CYAN, bold=True)
            elif stripped.startswith("@@"):
                colored = c.paint(stripped, c.AMBER)
            elif stripped.startswith("+"):
                colored = c.paint(stripped, c.GREEN)
            elif stripped.startswith("-"):
                colored = c.paint(stripped, c.RED)
            else:
                colored = c.paint(stripped, c.SLATE)
            pad = max(0, inner_w - _vlen(colored))
            print(f"  {colored}{' ' * pad} ")
        print(f"{c.paint(bot, c.SLATE)}\n")

    def cancelled(self) -> None:
        c = self.palette
        print(c.paint("Cancelled. No files were modified.\n", c.SLATE))

    def instructions_copied(self, path: Path) -> None:
        c = self.palette
        w = self._width()
        inner_w = w - 4

        title = c.paint(" Prompt Copied ", c.GREEN, bold=True)
        top = f"\n╭─{title}{'─' * max(0, inner_w - _vlen(title) + 1)}╮"
        bot = f"╰{'─' * (w - 2)}╯"

        msg1 = "  ✔ Instructions copied to your clipboard!"
        msg2 = f"  📋 Source: {path.name}"
        msg3 = "  💡 Tip: Paste (⌘V) into your AI chat to generate plan blocks."

        pad1 = max(0, inner_w - _vlen(msg1))
        pad2 = max(0, inner_w - _vlen(msg2))
        pad3 = max(0, inner_w - _vlen(msg3))

        print(c.paint(top, c.SLATE))
        print(f"│ {c.paint(msg1, c.WHITE, bold=True)}{' ' * pad1} │")
        print(f"│ {c.paint(msg2, c.SLATE)}{' ' * pad2} │")
        print(f"│ {c.paint(msg3, c.AMBER)}{' ' * pad3} │")
        print(f"{c.paint(bot, c.SLATE)}\n")

    def interactive_menu(self, root: Path) -> str:
        c = self.palette
        w = self._width()
        inner_w = w - 4

        self.header(root)

        title = c.paint(" Menu ", c.CORAL, bold=True)
        top = f"╭─{title}{'─' * max(0, inner_w - _vlen(title) + 1)}╮"
        bot = f"╰{'─' * (w - 2)}╯"

        def _row(text: str = "") -> None:
            pad = max(0, inner_w - _vlen(text))
            print(f"│ {text}{' ' * pad} │")

        print(c.paint(top, c.SLATE))
        _row()
        _row(f"  {c.paint('1.', c.CORAL, bold=True)} {c.paint('Apply plan', c.WHITE, bold=True)} from clipboard")
        _row(f"  {c.paint('2.', c.CORAL, bold=True)} {c.paint('Dry-run', c.WHITE, bold=True)} validate plan from clipboard")
        _row(f"  {c.paint('3.', c.CORAL, bold=True)} {c.paint('Copy AI prompt', c.WHITE, bold=True)} instructions to clipboard ({c.paint('-p', c.CYAN)})")
        _row(f"  {c.paint('4.', c.CORAL, bold=True)} {c.paint('Quick guide', c.WHITE, bold=True)} & syntax reference ({c.paint('-h', c.CYAN)})")
        _row(f"  {c.paint('5.', c.CORAL, bold=True)} {c.paint('Check for updates', c.WHITE, bold=True)} from GitHub ({c.paint('update', c.CYAN)})")
        _row(f"  {c.paint('6.', c.CORAL, bold=True)} {c.paint('Undo last plan', c.WHITE, bold=True)} revert file changes ({c.paint('undo', c.CYAN)})")
        _row(f"  {c.paint('q.', c.SLATE, bold=True)} Exit")
        _row()
        print(f"{c.paint(bot, c.SLATE)}\n")

        prompt = c.paint("  Choose an option [1/2/3/4/5/6/q]: ", c.CORAL, bold=True)
        try:
            return input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return "q"

    def prompt_commit(self, default_msg: str) -> bool:
        c = self.palette
        w = self._width()
        inner_w = w - 4

        title = c.paint(" Git Commit ", c.CYAN, bold=True)
        top = f"\n╭─{title}{'─' * max(0, inner_w - _vlen(title) + 1)}╮"
        bot = f"╰{'─' * (w - 2)}╯"

        msg_line = f"  Message: {c.paint(default_msg, c.WHITE, bold=True)}"
        pad = max(0, inner_w - _vlen(msg_line))

        print(c.paint(top, c.SLATE))
        print(f"│ {msg_line}{' ' * pad} │")
        print(f"{c.paint(bot, c.SLATE)}")

        prompt = c.paint("❯ Commit these changes to git? [y/N]: ", c.CORAL, bold=True)
        try:
            answer = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return answer in {"y", "yes"}

    def commit_success(self, commit_output: str, commit_msg: str) -> None:
        c = self.palette
        w = self._width()
        inner_w = w - 4

        title = c.paint(" Committed ", c.GREEN, bold=True)
        top = f"╭─{title}{'─' * max(0, inner_w - _vlen(title) + 1)}╮"
        bot = f"╰{'─' * (w - 2)}╯"

        msg1 = f"  ✔ {commit_msg}"
        pad1 = max(0, inner_w - _vlen(msg1))

        print(c.paint(top, c.SLATE))
        print(f"│ {c.paint(msg1, c.WHITE, bold=True)}{' ' * pad1} │")
        first_line = next((ln.strip() for ln in commit_output.split("\n") if ln.strip()), "")
        if first_line:
            out_line = f"  {first_line}"
            pad2 = max(0, inner_w - _vlen(out_line))
            print(f"│ {c.paint(out_line, c.SLATE)}{' ' * pad2} │")
        print(f"{c.paint(bot, c.SLATE)}\n")

    def commit_failed(self, error: str) -> None:
        c = self.palette
        w = self._width()
        inner_w = w - 4

        title = c.paint(" Commit Skipped ", c.AMBER, bold=True)
        top = f"╭─{title}{'─' * max(0, inner_w - _vlen(title) + 1)}╮"
        bot = f"╰{'─' * (w - 2)}╯"

        err_line = f"  ▲ {error}"
        pad = max(0, inner_w - _vlen(err_line))

        print(c.paint(top, c.SLATE))
        print(f"│ {c.paint(err_line, c.AMBER)}{' ' * pad} │")
        print(f"{c.paint(bot, c.SLATE)}\n")

    def dry_run(self) -> None:
        c = self.palette
        w = self._width()
        inner_w = w - 4

        title = c.paint(" Dry Run Validation ", c.GREEN, bold=True)
        top = f"╭─{title}{'─' * max(0, inner_w - _vlen(title) + 1)}╮"
        msg = f"  ✔ All checks passed. Plan is completely valid!"
        pad = max(0, inner_w - _vlen(msg))
        bot = f"╰{'─' * (w - 2)}╯"

        print(c.paint(top, c.SLATE))
        print(f"│ {c.paint(msg, c.WHITE)}{' ' * pad} │")
        print(f"{c.paint(bot, c.SLATE)}\n")

    def done(self, has_git: bool = False) -> None:
        c = self.palette
        w = self._width()
        inner_w = w - 4
        bot = f"╰{'─' * (w - 2)}╯"
        print(c.paint(bot, c.SLATE))

        title = c.paint(" Success ", c.GREEN, bold=True)
        top = f"\n╭─{title}{'─' * max(0, inner_w - _vlen(title) + 1)}╮"
        msg1 = "  ✔ All operations applied cleanly."
        pad1 = max(0, inner_w - _vlen(msg1))

        print(c.paint(top, c.SLATE))
        print(f"│ {c.paint(msg1, c.WHITE, bold=True)}{' ' * pad1} │")

        if has_git:
            msg2 = f"  💡 Review changes: {c.paint('git diff', c.CYAN)}"
            pad2 = max(0, inner_w - _vlen(msg2))
            print(f"│ {msg2}{' ' * pad2} │")

        print(f"╰{'─' * (w - 2)}╯\n")

    def warn(self, message: str) -> None:
        c = self.palette
        print(c.paint(f"  ▲ {message}", c.AMBER, bold=True))

    def error(self, message: str) -> None:
        c = self.palette
        cols = shutil.get_terminal_size((80, 24)).columns
        w = self._width(max_cols=max(cols - 2, 80))
        inner_w = w - 4

        title = c.paint(" Execution / Validation Error ", c.RED, bold=True)
        top = f"\n╭─{title}{'─' * max(0, inner_w - _vlen(title) + 1)}╮"
        bot = f"╰{'─' * (w - 2)}╯"

        print(c.paint(top, c.RED), file=sys.stderr)

        lines = message.split("\n")
        in_diff = False

        for line in lines:
            stripped = line.strip()

            # Empty spacer line
            if not stripped:
                print(f"│{' ' * (inner_w + 2)}│", file=sys.stderr)
                continue

            # Diff boundaries
            if set(stripped) == {"-"}:
                in_diff = True
                divider = f"  {'─' * (inner_w - 4)}"
                pad = max(0, inner_w - _vlen(divider))
                print(f"│ {c.paint(divider, c.SLATE)}{' ' * pad} │", file=sys.stderr)
                continue

            # Diff lines colorization
            if in_diff:
                if stripped.startswith("---") or stripped.startswith("+++"):
                    colored = f"  {c.paint(stripped, c.CYAN)}"
                elif stripped.startswith("@@"):
                    colored = f"  {c.paint(stripped, c.AMBER)}"
                elif stripped.startswith("-"):
                    colored = f"  {c.paint(stripped, c.RED)}"
                elif stripped.startswith("+"):
                    colored = f"  {c.paint(stripped, c.GREEN)}"
                else:
                    colored = f"  {c.paint(stripped, c.SLATE)}"
                pad = max(0, inner_w - _vlen(colored))
                print(f"│ {colored}{' ' * pad} │", file=sys.stderr)
                continue

            # Machine-readable error code tags (ERR|...)
            if "ERR|" in stripped:
                parts = stripped.split("ERR|", 1)
                prefix = parts[0]
                err_token = f"ERR|{parts[1]}"
                colored_err = c.paint(err_token, c.AMBER, bold=True)
                formatted = f"  ✖ {prefix}{colored_err}"
                pad = max(0, inner_w - _vlen(formatted))
                print(f"│ {formatted}{' ' * pad} │", file=sys.stderr)
            elif stripped.startswith("Closest candidate"):
                badge = c.paint(" SIMILARITY MATCH ", c.CYAN, bold=True)
                formatted = f"  ▲ {badge} {c.paint(stripped, c.WHITE)}"
                pad = max(0, inner_w - _vlen(formatted))
                print(f"│ {formatted}{' ' * pad} │", file=sys.stderr)
            else:
                formatted = f"  ✖ {c.paint(line, c.RED)}"
                pad = max(0, inner_w - _vlen(formatted))
                print(f"│ {formatted}{' ' * pad} │", file=sys.stderr)

        # Contextual recommendation banner
        hint = ""
        if "ERR|SEARCH_TOO_BIG" in message:
            hint = "Tip: SEARCH block is too large. Shrink your SEARCH anchor to 3-6 unique lines around the edit."
        elif "ERR|SEARCH_NOT_FOUND" in message:
            hint = "Tip: SEARCH block didn't match. Compare against the diff above and add unique lines."
        elif "ERR|SEARCH_AMBIGUOUS" in message:
            hint = "Tip: SEARCH target matches multiple locations. Include more surrounding lines for uniqueness."
        elif "ERR|CONFLICTING_OPERATIONS" in message:
            hint = "Tip: The plan performs contradictory operations on the same file. Separate or order your edits."
        elif "ERR|FORBIDDEN_COMMAND" in message:
            hint = "Tip: RUN command not permitted. Use whitelisted test runners or run manually."
        elif "ERR|FILE_PROTECTED" in message:
            hint = "Tip: Target is a sensitive file/key. Modify configuration files manually."
        elif "ERR|MULTIPLE_PLANS" in message:
            hint = "Tip: Multiple plan blocks found. Provide a single plan block per response."

        if hint:
            print(f"│{' ' * (inner_w + 2)}│", file=sys.stderr)
            hint_str = f"  💡 {c.paint(hint, c.AMBER)}"
            pad = max(0, inner_w - _vlen(hint_str))
            print(f"│ {hint_str}{' ' * pad} │", file=sys.stderr)

        print(f"│{' ' * (inner_w + 2)}│", file=sys.stderr)
        print(f"{c.paint(bot, c.RED)}\n", file=sys.stderr)

    def command_failed(self, error: str, backup_dir: Path | None) -> None:
        c = self.palette
        w = self._width()
        inner_w = w - 4
        bot = f"╰{'─' * (w - 2)}╯"
        print(c.paint(bot, c.SLATE))

        title = c.paint(" Command Failed ", c.RED, bold=True)
        top = f"\n╭─{title}{'─' * max(0, inner_w - _vlen(title) + 1)}╮"
        msg1 = f"  ✖ {error}"
        msg2 = "  ✋ Stopped. Earlier changes kept."
        pad1 = max(0, inner_w - _vlen(msg1))
        pad2 = max(0, inner_w - _vlen(msg2))

        print(c.paint(top, c.RED), file=sys.stderr)
        print(f"│ {c.paint(msg1, c.RED)}{' ' * pad1} │", file=sys.stderr)
        print(f"│ {c.paint(msg2, c.AMBER)}{' ' * pad2} │", file=sys.stderr)
        if backup_dir is not None:
            msg3 = f"  📦 Backups: {backup_dir}"
            pad3 = max(0, inner_w - _vlen(msg3))
            print(f"│ {c.paint(msg3, c.SLATE)}{' ' * pad3} │", file=sys.stderr)
        print(f"{c.paint(bot, c.RED)}\n", file=sys.stderr)

    def apply_interrupted(self, exc: Exception) -> None:
        c = self.palette
        w = self._width()
        inner_w = w - 4
        bot = f"╰{'─' * (w - 2)}╯"
        print(c.paint(bot, c.SLATE))

        title = c.paint(" Interrupted ", c.RED, bold=True)
        top = f"\n╭─{title}{'─' * max(0, inner_w - _vlen(title) + 1)}╮"
        msg = "  ✖ Execution interrupted by user." if isinstance(exc, KeyboardInterrupt) else f"  ✖ {exc}"
        pad = max(0, inner_w - _vlen(msg))

        print(c.paint(top, c.RED), file=sys.stderr)
        print(f"│ {c.paint(msg, c.RED)}{' ' * pad} │", file=sys.stderr)
        print(f"{c.paint(bot, c.RED)}\n", file=sys.stderr)

    def rollback_report(
        self,
        errors: list[str],
        count: int,
        backup_dir: Path | None,
        ran_commands: list[str],
    ) -> None:
        c = self.palette
        w = self._width()
        inner_w = w - 4

        if errors:
            title = c.paint(" Incomplete Rollback ", c.RED, bold=True)
            top = f"╭─{title}{'─' * max(0, inner_w - _vlen(title) + 1)}╮"
            print(c.paint(top, c.RED), file=sys.stderr)
            for err in errors:
                err_line = f"  ✖ {err}"
                pad = max(0, inner_w - _vlen(err_line))
                print(f"│ {c.paint(err_line, c.RED)}{' ' * pad} │", file=sys.stderr)
            print(f"╰{'─' * (w - 2)}╯\n", file=sys.stderr)
        else:
            title = c.paint(" Rollback Complete ", c.GREEN, bold=True)
            top = f"╭─{title}{'─' * max(0, inner_w - _vlen(title) + 1)}╮"
            msg = f"  ✔ Rolled back {count} change(s); directory restored."
            pad = max(0, inner_w - _vlen(msg))
            print(c.paint(top, c.SLATE), file=sys.stderr)
            print(f"│ {c.paint(msg, c.SLATE)}{' ' * pad} │", file=sys.stderr)
            print(f"╰{'─' * (w - 2)}╯\n", file=sys.stderr)

        if ran_commands:
            print(c.paint("  Commands executed prior to rollback:", c.SLATE))
            for cmd in ran_commands:
                print(c.paint(f"    ❯ {cmd}", c.SLATE))


ui = TerminalUI()
