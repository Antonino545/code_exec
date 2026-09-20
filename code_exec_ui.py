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
            "PATCH": c.AMBER,
            "REPLACE_ALL": c.AMBER,
            "DELETE": c.RED,
            "MOVE": c.CYAN,
            "COPY": c.CYAN,
            "RENAME": c.CYAN,
            "RUN": c.CORAL,
            "COMMIT": c.CYAN,
            "CHMOD": c.CYAN,
            "TOUCH": c.GREEN,
        }
        color = colors.get(cmd, c.SLATE)
        return c.paint(f" {cmd:<6} ", color, bold=True)

    def panel_top(self, title: str = "", border_color: str | None = None, max_cols: int = 120) -> str:
        c = self.palette
        w = self._width(max_cols)
        inner_w = w - 4
        b_color = border_color or c.SLATE
        left = c.paint("╭", b_color)
        right = c.paint("╮", b_color)
        horiz = c.paint("─", b_color)
        if title:
            dashes = max(0, inner_w - _vlen(title) + 1)
            return f"{left}{horiz}{title}{c.paint('─' * dashes, b_color)}{right}"
        return f"{left}{c.paint('─' * (w - 2), b_color)}{right}"

    def panel_row(self, content: str = "", border_color: str | None = None, max_cols: int = 120) -> str:
        c = self.palette
        w = self._width(max_cols)
        inner_w = w - 4
        b_color = border_color or c.SLATE
        left = c.paint("│", b_color)
        right = c.paint("│", b_color)
        pad = max(0, inner_w - _vlen(content))
        return f"{left} {content}{' ' * pad} {right}"

    def panel_bottom(self, border_color: str | None = None, max_cols: int = 120) -> str:
        c = self.palette
        w = self._width(max_cols)
        b_color = border_color or c.SLATE
        left = c.paint("╰", b_color)
        right = c.paint("╯", b_color)
        return f"{left}{c.paint('─' * (w - 2), b_color)}{right}"

    def render_panel(
        self,
        title: str = "",
        lines: list[str] | None = None,
        border_color: str | None = None,
        max_cols: int = 120,
        file=None,
    ) -> None:
        print(self.panel_top(title, border_color, max_cols), file=file)
        if lines:
            for line in lines:
                print(self.panel_row(line, border_color, max_cols), file=file)
        print(self.panel_bottom(border_color, max_cols), file=file)

    def prompt_choice(
        self,
        prompt_text: str,
        default: str = "",
    ) -> str:
        c = self.palette
        raw_prompt = prompt_text if prompt_text.endswith(" ") else f"{prompt_text}: "
        prompt_str = f"  {raw_prompt}" if not raw_prompt.startswith("  ") else raw_prompt
        try:
            val = input(c.paint(prompt_str, c.CORAL, bold=True)).strip()
            return val if val else default
        except (EOFError, KeyboardInterrupt):
            print()
            return default

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
        if cmd == "PATCH":
            lines = self._line_count(op.data)
            diff = c.paint(f"(patch {lines} lines)", c.AMBER)
            return f"{badge} {args[0]} {diff}"

        if cmd in {"INSERT_BEFORE", "INSERT_AFTER", "APPEND", "PREPEND"}:
            content = op.extra if cmd.startswith("INSERT") else op.data
            lines = self._line_count(content)
            diff = c.paint(f"(+{lines} lines)", c.GREEN)
            return f"{badge} {args[0]} {diff}"

        if cmd == "REPLACE_ALL":
            del_lines = self._line_count(op.data)
            add_lines = self._line_count(op.extra)
            diff = c.paint(f"(-{del_lines} / +{add_lines} all)", c.AMBER)
            return f"{badge} {args[0]} {diff}"
        if cmd == "CHMOD":
            mode = c.paint(args[1], c.WHITE)
            return f"{badge} {args[0]} {mode}"
        if cmd == "TOUCH":
            return f"{badge} {args[0]}"
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
        _row(f"  {c.paint('PATCH', c.AMBER, bold=True):<18} {c.paint('path', c.SLATE)} <<< unified diff >>>")
        _row(f"  {c.paint('REPLACE_ALL', c.AMBER, bold=True):<18} {c.paint('path', c.SLATE)} SEARCH <<<...>>> REPLACE <<<...>>>")
        _row(f"  {c.paint('DELETE', c.RED, bold=True):<18} {c.paint('path', c.SLATE)}")
        _row(f"  {c.paint('CHMOD', c.CYAN, bold=True):<18} {c.paint('path mode', c.SLATE)} (+x, 755, 644)")
        _row(f"  {c.paint('TOUCH', c.GREEN, bold=True):<18} {c.paint('path', c.SLATE)}")
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
        _row(f"  {c.paint('--commit-prompt, -c', c.CYAN):<18} Copy git diff prompt to clipboard for AI commit")
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
        raw_path = f"  {root}"
        if _vlen(raw_path) > inner_w - 4:
            raw_path = f"  ...{raw_path[-(inner_w - 10):]}"
        self.render_panel(
            title="",
            lines=[f" {c.paint(raw_path, c.WHITE)}"],
            border_color=c.SLATE,
        )
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
        total = len(operations)
        lines = [""]
        for idx, op in enumerate(operations, 1):
            num = c.paint(f"{idx:>2}.", c.SLATE)
            op_text = self.describe_op(op)
            lines.append(f"  {num} {op_text}")
        if deferred_count:
            lines.append("")
            warn_msg = f"    {deferred_count} op(s) after {deferred_reason} verified at runtime"
            lines.append(c.paint(warn_msg, c.AMBER))
        lines.append("")
        self.render_panel(
            title=c.paint(f" Planned Operations ({total}) ", c.WHITE, bold=True),
            lines=lines,
            border_color=c.SLATE,
        )
        print()

    def start_apply(self, total: int) -> None:
        c = self.palette
        title = c.paint(f" Executing ({total}) ", c.WHITE, bold=True)
        print(self.panel_top(title, c.SLATE))

    def step_done(self, idx: int, total: int, message: str) -> None:
        c = self.palette
        icon = c.paint("✓", c.GREEN)
        step = c.paint(f"[{idx}/{total}]", c.SLATE)
        content = f"  {icon} {step} {message}"
        print(self.panel_row(content, c.SLATE))

    def command(self, cmd: str) -> None:
        c = self.palette
        prompt = c.paint("❯", c.CYAN, bold=True)
        content = f"  {prompt} {c.paint(cmd, c.WHITE)}"
        print(self.panel_row(content, c.SLATE), flush=True)

    def confirm(self, diff_text: str | None = None) -> bool:
        hint = "Apply these changes? [y/N/v] (v: view diff)" if diff_text else "Apply these changes? [y/N]"
        while True:
            answer = self.prompt_choice(hint, default="n").lower()
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
        w_max = max(cols - 2, 80)
        lines = []
        for line in diff_text.splitlines():
            stripped = line.rstrip()
            if stripped.startswith("---") or stripped.startswith("+++"):
                lines.append(c.paint(stripped, c.CYAN, bold=True))
            elif stripped.startswith("@@"):
                lines.append(c.paint(stripped, c.AMBER))
            elif stripped.startswith("+"):
                lines.append(c.paint(stripped, c.GREEN))
            elif stripped.startswith("-"):
                lines.append(c.paint(stripped, c.RED))
            else:
                lines.append(c.paint(stripped, c.SLATE))
        print()
        self.render_panel(
            title=c.paint(" Proposed Plan Diff ", c.CYAN, bold=True),
            lines=lines,
            border_color=c.SLATE,
            max_cols=w_max,
        )
        print()

    def cancelled(self) -> None:
        c = self.palette
        print(c.paint("Cancelled. No files were modified.\n", c.SLATE))

    def instructions_copied(self, path: Path) -> None:
        c = self.palette
        lines = [
            f"    {c.paint('Instructions copied to your clipboard!', c.WHITE, bold=True)}",
            f"    {c.paint(f'Source: {path.name}', c.SLATE)}",
            f"    {c.paint('Tip: Paste (⌘V) into your AI chat to generate plan blocks.', c.AMBER)}",
        ]
        print()
        self.render_panel(
            title=c.paint(" Prompt Copied ", c.GREEN, bold=True),
            lines=lines,
            border_color=c.SLATE,
        )
        print()

    def commit_prompt_copied(self, prompt_len: int) -> None:
        c = self.palette
        lines = [
            f"    {c.paint('Git diff prompt copied to your clipboard!', c.WHITE, bold=True)}",
            f"    {c.paint(f'Size: {prompt_len} characters', c.SLATE)}",
            f"    {c.paint('Tip: Paste (⌘V / Ctrl+V) into your AI chat to generate a COMMIT plan.', c.AMBER)}",
        ]
        print()
        self.render_panel(
            title=c.paint(" Commit Prompt Copied ", c.GREEN, bold=True),
            lines=lines,
            border_color=c.SLATE,
        )
        print()
        print(f"  {c.paint(msg3, c.AMBER)}{' ' * pad3} ")
        print(f"{c.paint(bot, c.SLATE)}\n")

    def interactive_menu(self, root: Path) -> str:
        c = self.palette
        self.header(root)
        lines = [
            "",
            f"  {c.paint('1.', c.CORAL, bold=True)} {c.paint('Apply plan', c.WHITE, bold=True)} from clipboard",
            f"  {c.paint('2.', c.CORAL, bold=True)} {c.paint('Dry-run', c.WHITE, bold=True)} validate plan from clipboard",
            f"  {c.paint('3.', c.CORAL, bold=True)} {c.paint('Copy AI prompt', c.WHITE, bold=True)} instructions to clipboard ({c.paint('-p', c.CYAN)})",
            f"  {c.paint('4.', c.CORAL, bold=True)} {c.paint('Quick guide', c.WHITE, bold=True)} & syntax reference ({c.paint('-h', c.CYAN)})",
            f"  {c.paint('5.', c.CORAL, bold=True)} {c.paint('Check for updates', c.WHITE, bold=True)} from GitHub ({c.paint('update', c.CYAN)})",
            f"  {c.paint('6.', c.CORAL, bold=True)} {c.paint('Undo last plan', c.WHITE, bold=True)} revert file changes ({c.paint('undo', c.CYAN)})",
            f"  {c.paint('7.', c.CORAL, bold=True)} {c.paint('Commit prompt', c.WHITE, bold=True)} copy git diff prompt to clipboard ({c.paint('-c', c.CYAN)})",
            f"  {c.paint('q.', c.SLATE, bold=True)} Exit",
            "",
        ]
        self.render_panel(
            title=c.paint(" Menu ", c.CORAL, bold=True),
            lines=lines,
            border_color=c.SLATE,
        )
        print()
        try:
            return self.prompt_choice("Choose an option [1/2/3/4/5/6/7/q]", default="q").lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return "q"

    def prompt_commit(self, default_msg: str) -> bool:
        c = self.palette
        w = self._width()
        inner_w = w - 4
def prompt_commit(self, default_msg: str) -> bool:
    c = self.palette
    lines = [
        f"  Message: {c.paint(default_msg, c.WHITE, bold=True)}"
    ]
    print()
    self.render_panel(
        title=c.paint(" Git Commit ", c.CYAN, bold=True),
        lines=lines,
        border_color=c.SLATE,
    )
    answer = self.prompt_choice("Commit these changes to git? [y/N]", default="n").lower()
    return answer in {"y", "yes"}

def commit_success(self, commit_output: str, commit_msg: str) -> None:
    c = self.palette
    lines = [f"    {c.paint(commit_msg, c.WHITE, bold=True)}"]
    first_line = next((ln.strip() for ln in commit_output.split("\n") if ln.strip()), "")
    if first_line:
        lines.append(f"  {c.paint(first_line, c.SLATE)}")
    self.render_panel(
        title=c.paint(" Committed ", c.GREEN, bold=True),
        lines=lines,
        border_color=c.SLATE,
    )
    print()

def commit_failed(self, error: str) -> None:
    c = self.palette
    lines = [f"    {c.paint(error, c.AMBER)}"]
    self.render_panel(
        title=c.paint(" Commit Skipped ", c.AMBER, bold=True),
        lines=lines,
        border_color=c.SLATE,
    )
    print()

def dry_run(self) -> None:
    c = self.palette
    lines = [f"    {c.paint('All checks passed. Plan is completely valid!', c.WHITE)}"]
    self.render_panel(
        title=c.paint(" Dry Run Validation ", c.GREEN, bold=True),
        lines=lines,
        border_color=c.SLATE,
    )
    print()

def done(self, has_git: bool = False) -> None:
    c = self.palette
    print(self.panel_bottom(c.SLATE))
    lines = [
        f"    {c.paint('All operations applied cleanly.', c.WHITE, bold=True)}"
    ]
    if has_git:
        lines.append(f"    Review changes: {c.paint('git diff', c.CYAN)}")
    print()
    self.render_panel(
        title=c.paint(" Success ", c.GREEN, bold=True),
        lines=lines,
        border_color=c.SLATE,
    )
    print()

    def warn(self, message: str) -> None:
        c = self.palette
        print(c.paint(f"  ▲ {message}", c.AMBER, bold=True))

    def error(self, message: str) -> None:
        c = self.palette
        def error(self, message: str) -> None:
            c = self.palette
            cols = shutil.get_terminal_size((80, 24)).columns
            w_max = max(cols - 2, 80)
            panel_lines = []
            raw_lines = message.split("\n")
            in_diff = False
            for line in raw_lines:
                stripped = line.strip()
                if not stripped:
                    panel_lines.append("")
                    continue
                if set(stripped) == {"-"}:
                    in_diff = True
                    panel_lines.append(c.paint("─" * (min(w_max - 8, 60)), c.SLATE))
                    continue
                if in_diff:
                    if stripped.startswith("---") or stripped.startswith("+++"):
                        panel_lines.append(c.paint(f"  {stripped}", c.CYAN))
                    elif stripped.startswith("@@"):
                        panel_lines.append(c.paint(f"  {stripped}", c.AMBER))
                    elif stripped.startswith("-"):
                        panel_lines.append(c.paint(f"  {stripped}", c.RED))
                    elif stripped.startswith("+"):
                        panel_lines.append(c.paint(f"  {stripped}", c.GREEN))
                    else:
                        panel_lines.append(c.paint(f"  {stripped}", c.SLATE))
                    continue
                if "ERR|" in stripped:
                    parts = stripped.split("ERR|", 1)
                    prefix = parts[0]
                    err_token = f"ERR|{parts[1]}"
                    colored_err = c.paint(err_token, c.AMBER, bold=True)
                    panel_lines.append(f"  {prefix}{colored_err}")
                elif stripped.startswith("Closest candidate"):
                    badge = c.paint(" SIMILARITY MATCH ", c.CYAN, bold=True)
                    panel_lines.append(f"  {badge} {c.paint(stripped, c.WHITE)}")
                else:
                    panel_lines.append(f"  {c.paint(line, c.RED)}")

            hint = ""
            if "ERR|SEARCH_TOO_BIG" in message:
                hint = "Tip: SEARCH block is too large (limit is 60 lines / 4000 chars). Use a smaller unique anchor."
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
            elif "ERR|UNKNOWN_COMMAND" in message:
                hint = "Tip: Unsupported command. Check the hint or run 'code-exec -h' for supported instructions."
            elif "ERR|PATCH_FAILED" in message:
                hint = "Tip: Unified diff hunk could not be matched. Verify context lines or use EDIT."

            if hint:
                panel_lines.append("")
                panel_lines.append(f"  {c.paint(hint, c.AMBER)}")

            info_note = "Diagnostics prompt copied to clipboard for your AI chat."
            panel_lines.append(f"  {c.paint(info_note, c.SLATE)}")
            panel_lines.append("")

            print(file=sys.stderr)
            self.render_panel(
                title=c.paint(" Execution / Validation Error ", c.RED, bold=True),
                lines=panel_lines,
                border_color=c.RED,
                max_cols=w_max,
                file=sys.stderr,
            )
            print(file=sys.stderr)
            print(f"  {hint_str}{' ' * pad}  ", file=sys.stderr)

        info_note = "ℹ Diagnostics prompt copied to clipboard for your AI chat."
        pad_info = max(0, inner_w - _vlen(info_note) - 4)
        print(f"  {c.paint(f'    {info_note}', c.SLATE)}{' ' * pad_info}  ", file=sys.stderr)

        print(f" {' ' * (inner_w + 2)} ", file=sys.stderr)
        print(f"{c.paint(bot, c.RED)}\n", file=sys.stderr)

    def command_failed(self, error: str, backup_dir: Path | None) -> None:
        c = self.palette
        w = self._width()
        inner_w = w - 4
        bot = f"╰{'─' * (w - 2)}╯"
        def command_failed(self, error: str, backup_dir: Path | None) -> None:
            c = self.palette
            print(self.panel_bottom(c.SLATE), file=sys.stderr)
            lines = [
                f"    {c.paint(error, c.RED)}",
                f"    {c.paint('Stopped. Earlier changes kept.', c.AMBER)}",
            ]
            if backup_dir is not None:
                lines.append(f"    {c.paint(f'Backups: {backup_dir}', c.SLATE)}")
            print(file=sys.stderr)
            self.render_panel(
                title=c.paint(" Command Failed ", c.RED, bold=True),
                lines=lines,
                border_color=c.RED,
                file=sys.stderr,
            )
            print(file=sys.stderr)

        def apply_interrupted(self, exc: Exception) -> None:
            c = self.palette
            print(self.panel_bottom(c.SLATE), file=sys.stderr)
            msg = "    Execution interrupted by user." if isinstance(exc, KeyboardInterrupt) else f"    {exc}"
            lines = [f"  {c.paint(msg, c.RED)}"]
            print(file=sys.stderr)
            self.render_panel(
                title=c.paint(" Interrupted ", c.RED, bold=True),
                lines=lines,
                border_color=c.RED,
                file=sys.stderr,
            )
            print(file=sys.stderr)

        def rollback_report(
            self,
            errors: list[str],
            count: int,
            backup_dir: Path | None,
            ran_commands: list[str],
        ) -> None:
            c = self.palette
            if errors:
                lines = [f"    {c.paint(err, c.RED)}" for err in errors]
                self.render_panel(
                    title=c.paint(" Incomplete Rollback ", c.RED, bold=True),
                    lines=lines,
                    border_color=c.RED,
                    file=sys.stderr,
                )
                print(file=sys.stderr)
            else:
                lines = [
                    f"    {c.paint(f'Rolled back {count} change(s); directory restored.', c.SLATE)}"
                ]
                self.render_panel(
                    title=c.paint(" Rollback Complete ", c.GREEN, bold=True),
                    lines=lines,
                    border_color=c.SLATE,
                    file=sys.stderr,
                )
                print(file=sys.stderr)
            if ran_commands:
                print(c.paint("  Commands executed prior to rollback:", c.SLATE), file=sys.stderr)
                for cmd in ran_commands:
                    print(c.paint(f"      {cmd}", c.SLATE), file=sys.stderr)


ui = TerminalUI()
