"""
code_exec_ui.py - Modern, minimal terminal UI inspired by Claude Code.

Visual language
---------------
* One panel style for everything (rounded box, titled top edge, optional dividers).
* One icon per meaning:  ✓ success   ✗ error   ▲ warning   ● info   ❯ running   ◆ brand
* One colour per meaning: green ok / red error / amber warn+edit / cyan info+move / coral brand+run.
* Aligned columns everywhere (plan, guide, menu) and lines that never overflow the border.
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

    THEME_FILE = Path.home() / ".config" / "code_exec" / "theme"
    DEFAULT_THEME = "claude"

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
            try:
                theme_name = self.THEME_FILE.read_text(encoding="utf-8").strip().lower()
            except OSError:
                theme_name = ""
        self._apply_theme(theme_name)

    def _apply_theme(self, name: str) -> None:
        """Load a theme's colours into attributes (blank when colour is disabled)."""
        if name not in self.themes:
            name = self.DEFAULT_THEME
        self.current_theme = name
        for key, value in self.themes[name].items():
            setattr(self, key, value if self.enabled else "")

    def paint(self, text: str, color: str, bold: bool = False) -> str:
        if not self.enabled:
            return text
        prefix = f"{self.BOLD}{color}" if bold else color
        return f"{prefix}{text}{self.RESET}"

    def set_theme(self, name: str) -> None:
        name = name.strip().lower()
        if name not in self.themes:
            return
        self._apply_theme(name)
        try:
            self.THEME_FILE.parent.mkdir(parents=True, exist_ok=True)
            self.THEME_FILE.write_text(name, encoding="utf-8")
        except OSError:
            pass


_ANSI_STRIP_RE = re.compile(r"\033\[[0-9;]*m")
_DIV = "\x00div:"  # sentinel: a panel "line" that renders as a divider


def _vlen(text: str) -> int:
    """Calculates visible character length without counting ANSI escape bytes."""
    return len(_ANSI_STRIP_RE.sub("", text))


def _fit(text: str, width: int) -> str:
    """Truncate to `width` visible characters (ANSI-aware) so nothing breaks the border."""
    if _vlen(text) <= width:
        return text
    out: list[str] = []
    visible, i, limit = 0, 0, max(0, width - 1)
    while i < len(text) and visible < limit:
        m = _ANSI_STRIP_RE.match(text, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        out.append(text[i])
        visible += 1
        i += 1
    reset = "\033[0m" if "\033[" in text else ""
    return "".join(out) + "…" + reset


class _TreeNode:
    def __init__(self, name: str, is_dir: bool = False):
        self.name = name
        self.is_dir = is_dir
        self.action = ""
        self.extra = ""
        self.children: dict[str, _TreeNode] = {}


class TerminalUI:
    """Claude Code inspired TUI with responsive boxed cards and status panels."""

    MAX_COLS = 100    # standard panel width cap
    WIDE_COLS = 240   # diffs / diagnostics: use the whole terminal

    # kind -> (icon, palette colour name)
    _KINDS = {
        "ok": ("✓", "GREEN"),
        "err": ("✗", "RED"),
        "warn": ("▲", "AMBER"),
        "info": ("●", "CYAN"),
        "run": ("❯", "CYAN"),
        "brand": ("◆", "CORAL"),
        "plain": ("", "WHITE"),
    }

    def __init__(self):
        self.palette = TerminalPalette()

    # ------------------------------------------------------------------ #
    # Panel primitives
    # ------------------------------------------------------------------ #

    def _width(self, max_cols: int | None = None) -> int:
        target_cap = max_cols or self.MAX_COLS
        is_tty = (hasattr(sys.stdout, "isatty") and sys.stdout.isatty()) or (
            hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
        )
        if not is_tty:
            return target_cap
        cols = shutil.get_terminal_size((target_cap, 24)).columns
        return min(max(cols - 4, 58), target_cap)

    def _edge(self, left: str, right: str, title: str, border_color: str | None, max_cols: int | None) -> str:
        c = self.palette
        w = self._width(max_cols)
        b = border_color or c.SLATE
        if title:
            dashes = max(0, w - 3 - _vlen(title))
            return f"{c.paint(left + '─', b)}{title}{c.paint('─' * dashes + right, b)}"
        return c.paint(left + "─" * (w - 2) + right, b)

    def panel_top(self, title: str = "", border_color: str | None = None, max_cols: int | None = None) -> str:
        return self._edge("╭", "╮", title, border_color, max_cols)

    def panel_bottom(self, border_color: str | None = None, max_cols: int | None = None) -> str:
        return self._edge("╰", "╯", "", border_color, max_cols)

    def panel_divider(self, label: str = "", border_color: str | None = None, max_cols: int | None = None) -> str:
        c = self.palette
        title = c.paint(f" {label} ", c.WHITE, bold=True) if label else ""
        return self._edge("├", "┤", title, border_color, max_cols)

    def panel_row(self, content: str = "", border_color: str | None = None, max_cols: int | None = None) -> str:
        c = self.palette
        inner_w = self._width(max_cols) - 4
        b = border_color or c.SLATE
        content = _fit(content, inner_w)
        pad = max(0, inner_w - _vlen(content))
        return f"{c.paint('│', b)} {content}{' ' * pad} {c.paint('│', b)}"

    def render_panel(
        self,
        title: str = "",
        lines: list[str] | None = None,
        border_color: str | None = None,
        max_cols: int | None = None,
        file=None,
    ) -> None:
        print(self.panel_top(title, border_color, max_cols), file=file)
        for line in lines or []:
            if line.startswith(_DIV):
                print(self.panel_divider(line[len(_DIV):], border_color, max_cols), file=file)
            else:
                print(self.panel_row(line, border_color, max_cols), file=file)
        print(self.panel_bottom(border_color, max_cols), file=file)

    # ------------------------------------------------------------------ #
    # Building blocks (used by every screen so they all look the same)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _line_count(text: str | None) -> int:
        return 0 if not text else text.count("\n") + 1

    def _title(self, text: str, kind: str = "plain") -> str:
        icon, color_name = self._KINDS[kind]
        color = getattr(self.palette, color_name)
        label = f" {icon} {text} " if icon else f" {text} "
        return self.palette.paint(label, color, bold=True)

    def _line(self, text: str = "", color: str | None = None, bold: bool = False) -> str:
        """One row of panel content with the standard 2-space gutter."""
        if color:
            return f"  {self.palette.paint(text, color, bold)}"
        return f"  {text}"

    def _kv(self, label: str, value: str) -> str:
        """Aligned `label   value` row (label dim, value pre-coloured by caller)."""
        return f"  {self.palette.paint(f'{label:<8}', self.palette.SLATE)}{value}"

    def _tip(self, text: str) -> str:
        c = self.palette
        return f"  {c.paint('Tip', c.AMBER, bold=True)}  {c.paint(text, c.SLATE)}"

    def _divider(self, label: str = "") -> str:
        return _DIV + label

    def _card(self, title: str, kind: str, lines: list[str], wide: bool = False, file=None) -> None:
        """Standard message card: blank line, titled panel, blank line."""
        c = self.palette
        border = {"err": c.RED, "warn": c.AMBER}.get(kind)
        print(file=file)
        self.render_panel(
            title=self._title(title, kind),
            lines=lines,
            border_color=border,
            max_cols=self.WIDE_COLS if wide else self.MAX_COLS,
            file=file,
        )
        print(file=file)

    def _diff_line(self, line: str) -> str:
        c = self.palette
        if line.startswith(("---", "+++")):
            return c.paint(line, c.CYAN, bold=True)
        if line.startswith("@@"):
            return c.paint(line, c.AMBER)
        if line.startswith("+"):
            return c.paint(line, c.GREEN)
        if line.startswith("-"):
            return c.paint(line, c.RED)
        return c.paint(line, c.SLATE)

    _BADGE_LABELS = {"REPLACE_ALL": "REPLACE", "INSERT_BEFORE": "BEFORE", "INSERT_AFTER": "AFTER"}

    def _badge(self, cmd: str) -> str:
        """Fixed-width (9 col) command badge, coloured by what the command does."""
        c = self.palette
        colors = {
            "CREATE": c.GREEN, "TOUCH": c.GREEN, "MKDIR": c.GREEN,
            "APPEND": c.GREEN, "PREPEND": c.GREEN,
            "INSERT_BEFORE": c.GREEN, "INSERT_AFTER": c.GREEN,
            "EDIT": c.AMBER, "PATCH": c.AMBER, "REPLACE_ALL": c.AMBER,
            "DELETE": c.RED,
            "MOVE": c.CYAN, "COPY": c.CYAN, "RENAME": c.CYAN,
            "CHMOD": c.CYAN, "COMMIT": c.CYAN,
            "RUN": c.CORAL,
        }
        label = self._BADGE_LABELS.get(cmd, cmd)
        return c.paint(f" {label:<7} ", colors.get(cmd, c.SLATE), bold=True)

    def prompt_choice(self, prompt_text: str, default: str = "") -> str:
        c = self.palette
        text = prompt_text.strip().rstrip(":")
        prompt_str = f"  {c.paint('❯', c.CORAL, bold=True)} {c.paint(text, c.WHITE, bold=True)} "
        try:
            val = input(prompt_str).strip()
            return val if val else default
        except (EOFError, KeyboardInterrupt):
            print()
            return default

    # ------------------------------------------------------------------ #
    # Plan display
    # ------------------------------------------------------------------ #

    def _op_parts(self, op: Operation) -> tuple[str, str, int, int]:
        """Return (target, stat, added_lines, removed_lines) for one operation."""
        c = self.palette
        cmd, args = op.command, op.args
        add = rem = 0
        target = str(args[0]) if args else ""

        if cmd in {"MOVE", "COPY", "RENAME"}:
            return f"{args[0]} {c.paint('→', c.SLATE)} {args[1]}", "", 0, 0
        if cmd == "CHMOD":
            return f"{args[0]} {c.paint(args[1], c.WHITE)}", "", 0, 0
        if cmd == "COMMIT":
            return c.paint(f'"{args[0]}"', c.WHITE), "", 0, 0

        if cmd == "CREATE":
            add = self._line_count(op.data)
        elif cmd in {"EDIT", "REPLACE_ALL"}:
            rem, add = self._line_count(op.data), self._line_count(op.extra)
        elif cmd == "PATCH":
            for ln in (op.data or "").splitlines():
                if ln.startswith("+") and not ln.startswith("+++"):
                    add += 1
                elif ln.startswith("-") and not ln.startswith("---"):
                    rem += 1
        elif cmd in {"INSERT_BEFORE", "INSERT_AFTER"}:
            add = self._line_count(op.extra)
        elif cmd in {"APPEND", "PREPEND"}:
            add = self._line_count(op.data)
        elif cmd == "RUN":
            target = " ".join(str(a) for a in args)

        stat: list[str] = []
        if add:
            stat.append(c.paint(f"+{add}", c.GREEN))
        if rem:
            stat.append(c.paint(f"−{rem}", c.RED))
        if cmd == "REPLACE_ALL":
            stat.append(c.paint("all", c.SLATE))
        return target, " ".join(stat), add, rem

    def describe_op(self, op: Operation) -> str:
        target, stat, _, _ = self._op_parts(op)
        return f"{self._badge(op.command)} {target} {stat}".rstrip()

    def show_plan(
        self,
        operations: list[Operation],
        deferred_reason: str | None = None,
        deferred_count: int = 0,
    ) -> None:
        c = self.palette
        total = len(operations)
        inner_w = self._width() - 4
        nw = len(str(total))
        parts = [self._op_parts(op) for op in operations]

        # Column layout: gutter(2) index(nw) gap(2) badge(9) gap(1) target  gap(2) stat
        stat_w = max((_vlen(p[1]) for p in parts), default=0)
        cap = max(20, inner_w - (2 + nw + 2 + 9 + 1) - (stat_w + 2 if stat_w else 0))
        target_w = min(max((_vlen(p[0]) for p in parts), default=0), cap)

        lines = [""]
        files: set[str] = set()
        total_add = total_rem = 0
        for idx, (op, (target, stat, add, rem)) in enumerate(zip(operations, parts), 1):
            target = _fit(target, target_w)
            pad = " " * (target_w - _vlen(target))
            num = c.paint(f"{idx:>{nw}}", c.SLATE)
            lines.append(f"  {num}  {self._badge(op.command)} {target}{pad}  {stat}".rstrip())
            if op.command not in {"RUN", "COMMIT"} and op.args:
                files.add(str(op.args[0]))
            total_add += add
            total_rem += rem

        lines.append("")
        if deferred_count:
            lines.append(self._line(f"▲ {deferred_count} op(s) after {deferred_reason} verified at runtime", c.AMBER))
            lines.append("")
        if files:
            summary = [c.paint(f"{len(files)} file{'s' if len(files) != 1 else ''}", c.SLATE)]
            if total_add:
                summary.append(c.paint(f"+{total_add}", c.GREEN))
            if total_rem:
                summary.append(c.paint(f"−{total_rem}", c.RED))
            lines += [self._divider(), self._line("  ".join(summary))]

        noun = "operation" if total == 1 else "operations"
        self.render_panel(title=self._title(f"Plan · {total} {noun}", "brand"), lines=lines)
        print()

    # ------------------------------------------------------------------ #
    # Guide / banner / header / menu
    # ------------------------------------------------------------------ #

    def show_guide(self) -> None:
        c = self.palette
        lines: list[str] = []

        def row(label: str, args: str, color: str, note: str = "", bold: bool = False) -> None:
            # Pad the plain label first, then colour it, so columns align with or without ANSI.
            gap = " " * max(1, 24 - len(label))
            text = f"{c.paint(label, color, bold=bold)}{gap}{c.paint(args, c.SLATE)}"
            lines.append(self._line(f"{text} {note}" if note else text))

        lines.append(self._divider("Workflow"))
        lines.append(self._line(f"{c.paint('1.', c.CORAL, bold=True)} Run {c.paint('code-exec -p', c.CYAN)} to copy the AI instructions to your clipboard."))
        lines.append(self._line(f"{c.paint('2.', c.CORAL, bold=True)} Ask your AI for a change; it returns one executable plan block."))
        lines.append(self._line(f"{c.paint('3.', c.CORAL, bold=True)} Copy the plan, then run {c.paint('code-exec', c.GREEN, bold=True)} (or {c.paint('code-exec apply', c.GREEN)})."))

        lines.append(self._divider("Plan commands"))
        row("CREATE", "path", c.GREEN, "<<< content >>>", bold=True)
        row("EDIT", "path", c.AMBER, "SEARCH <<<...>>> REPLACE <<<...>>>", bold=True)
        row("PATCH", "path", c.AMBER, "<<< unified diff >>>", bold=True)
        row("REPLACE_ALL", "path", c.AMBER, "SEARCH <<<...>>> REPLACE <<<...>>>", bold=True)
        row("DELETE", "path", c.RED, "(file or folder)", bold=True)
        row("MKDIR", "path", c.GREEN, "(create folder)", bold=True)
        row("CHMOD", "path mode", c.CYAN, "(+x, 755, 644)", bold=True)
        row("TOUCH", "path", c.GREEN, bold=True)
        row("MOVE", "src -> dst", c.CYAN, bold=True)
        row("RUN", "shell command", c.CORAL, bold=True)
        row("COMMIT", "commit message", c.CYAN, "(stages only modified files)", bold=True)

        lines.append(self._divider("Commands"))
        row("code-exec", "Open the interactive launcher menu", c.WHITE)
        row("code-exec apply", "Apply the plan from your clipboard", c.WHITE)
        row("code-exec export-context", "Export clean .context folder (files + tokens)", c.WHITE)
        row("code-exec undo", "Revert the last applied plan", c.WHITE)
        row("code-exec update", "Install the latest version from GitHub", c.WHITE)
        row("code-exec theme <name>", f"Set theme: {', '.join(c.themes)}", c.WHITE)

        lines.append(self._divider("Options"))
        row("--diff", "Show a unified diff before applying", c.CYAN)
        row("--tree", "Show the proposed folder/directory structure", c.CYAN)
        row("--dry-run", "Validate the plan without modifying files", c.CYAN)
        row("--check", "Parse and validate syntax only (no file lookups)", c.CYAN)
        row("--yes", "Apply without asking for confirmation", c.CYAN)
        row("--no-commit", "Skip the commit prompt", c.CYAN)
        row("--export-context", "Export clean .context folder (alias: export-concet)", c.CYAN)
        row("--ignore-file <name>", "Specify custom ignore file (default: .ignorefile)", c.CYAN)
        row("--file <path>", "Read the plan from a file ('-' for stdin)", c.CYAN)
        row("--prompt, -p", "Copy the AI instructions prompt", c.CYAN)
        row("--commit-prompt, -c", "Copy a git diff prompt for AI commits", c.CYAN)
        row("--help, -h", "Show this guide", c.CYAN)
        lines.append("")
        self._card("code-exec · Quick Guide", "brand", lines)

    def banner(self) -> None:
        c = self.palette
        art = [
            r"  ____ ___  ____  _____   _______  _______ ____ ",
            r" / ___/ _ \|  _ \| ____| | ____\ \/ / ____/ ___|",
            r"| |  | | | | | | |  _|   |  _|  \  /|  _| | |   ",
            r"| |__| |_| | |_| | |___  | |___ /  \| |___| |___",
            r" \____\___/|____/|_____| |_____/_/\_\_____|\____|",
        ]
        shades = [c.CORAL, c.CORAL, c.CORAL, c.AMBER, c.AMBER]
        print()
        for line, shade in zip(art, shades):
            print(c.paint(line, shade, bold=True))
        print(
            f"  {c.paint('◆', c.CORAL)} {c.paint('Local Deterministic Coding Agent', c.SLATE)}"
            f" {c.paint('·', c.SLATE)} {c.paint('v1.3', c.CYAN)}"
        )
        print()

    def header(self, root: Path) -> None:
        c = self.palette
        inner_w = self._width() - 4
        self.banner()
        path = str(root)
        home = str(Path.home())
        if path == home or path.startswith(home + os.sep):
            path = "~" + path[len(home):]
        if len(path) > inner_w - 6:
            path = "…" + path[-(inner_w - 7):]
        self.render_panel(
            title=self._title("Project", "plain"),
            lines=[self._line(f"{c.paint('▸', c.CORAL)} {c.paint(path, c.WHITE)}")],
        )
        print()

    def project_root(self, root: Path) -> None:
        self.header(root)

    def interactive_menu(self, root: Path) -> str:
        c = self.palette
        self.header(root)
        items = [
            ("1", "Apply plan", "from clipboard", ""),
            ("2", "Dry-run", "validate plan from clipboard", "--dry-run"),
            ("3", "Copy AI prompt", "instructions to clipboard", "-p"),
            ("4", "Quick guide", "syntax & CLI reference", "-h"),
            ("5", "Check updates", "install latest from GitHub", "update"),
            ("6", "Undo last plan", "revert file changes", "undo"),
            ("7", "Commit prompt", "git diff prompt to clipboard", "-c"),
            ("8", "Export context", "clean project folder (.context)", "export-context"),
        ]
        lines = [""]
        for key, label, desc, flag in items:
            key_s = c.paint(key, c.CORAL, bold=True)
            label_s = c.paint(f"{label:<16}", c.WHITE, bold=True)
            desc_s = c.paint(f"{desc:<30}", c.SLATE)
            flag_s = c.paint(flag, c.CYAN) if flag else ""
            lines.append(self._line(f"{key_s}  {label_s}{desc_s}  {flag_s}".rstrip()))
        lines.append(self._line(f"{c.paint('q', c.SLATE, bold=True)}  {c.paint('Exit', c.SLATE)}"))
        lines.append("")
        self.render_panel(title=self._title("Menu", "brand"), lines=lines)
        print()
        try:
            return self.prompt_choice("Choose an option [1-8/q]", default="q").lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return "q"

    # ------------------------------------------------------------------ #
    # Apply flow
    # ------------------------------------------------------------------ #

    def start_apply(self, total: int) -> None:
        noun = "step" if total == 1 else "steps"
        print(self.panel_top(self._title(f"Executing · {total} {noun}", "run")))

    def step_done(self, idx: int, total: int, message: str) -> None:
        c = self.palette
        icon = c.paint("✓", c.GREEN)
        step = c.paint(f"{idx:>{len(str(total))}}/{total}", c.SLATE)
        print(self.panel_row(f"  {icon} {step}  {message}"))

    def command(self, cmd: str) -> None:
        c = self.palette
        prompt = c.paint("❯", c.CYAN, bold=True)
        print(self.panel_row(f"  {prompt} {c.paint(cmd, c.WHITE)}"), flush=True)

    def build_tree_lines(self, operations: list[Operation]) -> list[str]:
        root_nodes: dict[str, _TreeNode] = {}

        for op in operations:
            cmd = op.command
            args = op.args
            if cmd in {"RUN", "COMMIT"} or not args:
                continue

            if cmd in {"MOVE", "COPY", "RENAME"}:
                src = str(args[0])
                dst = str(args[1])
                target_is_dir = src.endswith("/") or dst.endswith("/")
                action = "move" if cmd in {"MOVE", "RENAME"} else "copy"
                extra = f"from {src}"
                raw_path = dst
            elif cmd == "CHMOD":
                raw_path = str(args[0])
                target_is_dir = False
                action = ""
                extra = str(args[1]) if len(args) > 1 else ""
            elif cmd == "DELETE":
                raw_path = str(args[0])
                target_is_dir = raw_path.endswith("/")
                action = "delete"
                extra = ""
            elif cmd == "MKDIR":
                raw_path = str(args[0])
                target_is_dir = True
                action = "mkdir"
                extra = ""
            elif cmd in {"CREATE", "TOUCH"}:
                raw_path = str(args[0])
                target_is_dir = False
                action = "create"
                extra = ""
            else:
                raw_path = str(args[0])
                target_is_dir = False
                action = "edit"
                extra = ""

            clean = raw_path.strip().replace("\\", "/").rstrip("/")
            parts = [p for p in clean.split("/") if p and p != "."]
            if not parts:
                continue

            curr = root_nodes
            for i, part in enumerate(parts):
                is_leaf = (i == len(parts) - 1)
                part_is_dir = target_is_dir if is_leaf else True
                if part not in curr:
                    curr[part] = _TreeNode(part, is_dir=part_is_dir)
                node = curr[part]
                if part_is_dir:
                    node.is_dir = True
                if is_leaf:
                    if action:
                        node.action = action
                    if extra:
                        node.extra = extra
                curr = node.children

        if not root_nodes:
            return []

        c = self.palette

        def render_children(children: dict[str, _TreeNode], prefix: str = "") -> list[str]:
            out = []
            sorted_nodes = sorted(
                children.values(),
                key=lambda n: (0 if n.is_dir else 1, n.name.lower())
            )
            for idx, child in enumerate(sorted_nodes):
                is_last = (idx == len(sorted_nodes) - 1)
                branch = "└── " if is_last else "├── "
                next_prefix = prefix + ("    " if is_last else "│   ")

                branch_str = c.paint(prefix + branch, c.SLATE)
                if child.is_dir:
                    name_color = c.RED if child.action == "delete" else c.CYAN
                    name_str = c.paint(child.name + "/", name_color, bold=True)
                else:
                    name_color = c.RED if child.action == "delete" else c.WHITE
                    name_str = c.paint(child.name, name_color)

                tags = []
                if child.action == "delete":
                    tags.append(c.paint("deleted", c.RED))
                elif child.action == "move":
                    tags.append(c.paint(child.extra or "moved", c.CYAN))
                elif child.action == "copy":
                    tags.append(c.paint(child.extra or "copied", c.CYAN))
                elif child.extra:
                    tags.append(c.paint(child.extra, c.GREEN if "+" in child.extra else c.SLATE))

                tag_str = f" {c.paint('(', c.SLATE)}{', '.join(tags)}{c.paint(')', c.SLATE)}" if tags else ""
                out.append(f"  {branch_str}{name_str}{tag_str}")

                if child.children:
                    out.extend(render_children(child.children, next_prefix))
            return out

        lines: list[str] = []
        sorted_roots = sorted(
            root_nodes.values(),
            key=lambda n: (0 if n.is_dir else 1, n.name.lower())
        )

        for idx, root in enumerate(sorted_roots):
            if idx > 0:
                lines.append("")
            tags = []
            if root.action == "delete":
                tags.append(c.paint("deleted", c.RED))
            elif root.action == "move":
                tags.append(c.paint(root.extra or "moved", c.CYAN))
            elif root.action == "copy":
                tags.append(c.paint(root.extra or "copied", c.CYAN))
            elif root.extra:
                tags.append(c.paint(root.extra, c.GREEN if "+" in root.extra else c.SLATE))
            tag_str = f" {c.paint('(', c.SLATE)}{', '.join(tags)}{c.paint(')', c.SLATE)}" if tags else ""

            if root.is_dir:
                root_color = c.RED if root.action == "delete" else c.CYAN
                lines.append(f"  {c.paint(root.name + '/', root_color, bold=True)}{tag_str}")
                if root.children:
                    lines.extend(render_children(root.children, prefix="  "))
            else:
                lines.append(f"  {c.paint(root.name, c.WHITE)}{tag_str}")

        return lines

    def show_tree(self, operations: list[Operation]) -> None:
        lines = self.build_tree_lines(operations)
        if not lines:
            return
        self.render_panel(title=self._title("Directory structure", "info"), lines=[""] + lines + [""])
        print()

    def confirm(self, diff_text: str | None = None, on_view_tree=None) -> bool:
        options = ["y/N"]
        hints = []
        if diff_text:
            options.append("v")
            hints.append("v: view diff")
        if on_view_tree:
            options.append("t")
            hints.append("t: view tree")

        hint_body = "/".join(options)
        hint_extra = f" ({', '.join(hints)})" if hints else ""
        prompt_str = f"Apply these changes? [{hint_body}]{hint_extra}"

        while True:
            answer = self.prompt_choice(prompt_str, default="n").lower()
            if answer in {"v", "view"} and diff_text:
                self.show_diff(diff_text)
                continue
            if answer in {"t", "tree"} and on_view_tree:
                on_view_tree()
                continue
            return answer in {"y", "yes"}

    def searching(self, target: str):
        """Context manager displaying a live searching indicator during expensive scans."""
        class _SearchStatus:
            def __init__(self, ui_inst, file_target: str):
                self.ui = ui_inst
                self.target = file_target
                self.stop_event = None
                self.thread = None

            def __enter__(self):
                import threading
                import time

                self.stop_event = threading.Event()
                is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
                if not is_tty:
                    return self

                c = self.ui.palette

                def _animate():
                    dots = [".  ", ".. ", "...", "   "]
                    idx = 0
                    time.sleep(0.18)
                    while not self.stop_event.is_set():
                        dot = dots[idx % len(dots)]
                        msg = f"\r  {c.paint('●', c.CYAN)} Searching in {c.paint(self.target, c.WHITE)} {c.paint(dot, c.AMBER)}"
                        sys.stdout.write(msg)
                        sys.stdout.flush()
                        idx += 1
                        time.sleep(0.18)
                    sys.stdout.write("\r\033[K")
                    sys.stdout.flush()

                self.thread = threading.Thread(target=_animate, daemon=True)
                self.thread.start()
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                if self.stop_event:
                    self.stop_event.set()
                if self.thread and self.thread.is_alive():
                    self.thread.join(timeout=0.3)
                    sys.stdout.write("\r\033[K")
                    sys.stdout.flush()

        return _SearchStatus(self, target)

    def searching(self, target: str):
        """Context manager displaying a live searching indicator during expensive scans."""
        class _SearchStatus:
            def __init__(self, ui_inst, file_target: str):
                self.ui = ui_inst
                self.target = file_target
                self.stop_event = None
                self.thread = None

            def __enter__(self):
                import threading
                import time

                self.stop_event = threading.Event()
                is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
                if not is_tty:
                    return self

                c = self.ui.palette

                def _animate():
                    dots = [".  ", ".. ", "...", "   "]
                    idx = 0
                    time.sleep(0.18)
                    while not self.stop_event.is_set():
                        dot = dots[idx % len(dots)]
                        msg = f"\r  {c.paint('●', c.CYAN)} Searching in {c.paint(self.target, c.WHITE)} {c.paint(dot, c.AMBER)}"
                        sys.stdout.write(msg)
                        sys.stdout.flush()
                        idx += 1
                        time.sleep(0.18)
                    sys.stdout.write("\r\033[K")
                    sys.stdout.flush()

                self.thread = threading.Thread(target=_animate, daemon=True)
                self.thread.start()
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                if self.stop_event:
                    self.stop_event.set()
                if self.thread and self.thread.is_alive():
                    self.thread.join(timeout=0.3)
                    sys.stdout.write("\r\033[K")
                    sys.stdout.flush()

        return _SearchStatus(self, target)

    def show_diff(self, diff_text: str) -> None:
        c = self.palette
        if not diff_text.strip():
            print(c.paint("\n  No diff detected for planned operations.\n", c.SLATE))
            return
        lines = [self._diff_line(ln.rstrip()) for ln in diff_text.splitlines()]
        self._card("Proposed diff", "info", lines, wide=True)

    def cancelled(self) -> None:
        c = self.palette
        print(c.paint("  Cancelled. No files were modified.\n", c.SLATE))

    def done(self) -> None:
        c = self.palette
        print(self.panel_bottom())  # close the "Executing" panel
        lines = [
            self._line("All operations applied cleanly.", c.WHITE, bold=True),
            self._kv("Review", c.paint("git diff", c.CYAN)),
        ]
        self._card("Success", "ok", lines)

    def dry_run(self) -> None:
        c = self.palette
        print(self.panel_bottom())
        self._card("Dry run", "ok", [
            self._line("All operations simulated cleanly.", c.WHITE, bold=True),
            self._kv("Files", c.paint("unchanged", c.SLATE)),
        ])

    def parse_check_success(self, count: int) -> None:
        c = self.palette
        noun = "operation" if count == 1 else "operations"
        self._card("Syntax Check Passed", "ok", [
            self._line(f"Parsed {count} {noun} cleanly without errors.", c.WHITE, bold=True),
            self._kv("Mode", c.paint("parser-only (debug)", c.CYAN)),
            self._kv("Filesystem", c.paint("not touched / not queried", c.SLATE)),
        ])

    # ------------------------------------------------------------------ #
    # Clipboard / commit flow
    # ------------------------------------------------------------------ #

    def instructions_copied(self, path: Path) -> None:
        c = self.palette
        self._card("Prompt copied", "ok", [
            self._line("Instructions copied to your clipboard.", c.WHITE, bold=True),
            self._kv("Source", c.paint(path.name, c.WHITE)),
            self._tip("Paste (⌘V / Ctrl+V) into your AI chat to generate plan blocks."),
        ])

    def commit_prompt_copied(self, prompt_len: int) -> None:
        c = self.palette
        self._card("Commit prompt copied", "ok", [
            self._line("Git diff prompt copied to your clipboard.", c.WHITE, bold=True),
            self._kv("Size", c.paint(f"{prompt_len} characters", c.WHITE)),
            self._tip("Paste (⌘V / Ctrl+V) into your AI chat to generate a COMMIT plan."),
        ])

    def prompt_commit(self, default_msg: str) -> bool:
        c = self.palette
        self._card("Git commit", "info", [
            self._kv("Message", c.paint(default_msg, c.WHITE, bold=True)),
        ])
        answer = self.prompt_choice("Commit these changes to git? [y/N]", default="n").lower()
        return answer in {"y", "yes"}

    def commit_success(self, commit_output: str, commit_msg: str) -> None:
        c = self.palette
        lines = [self._kv("Message", c.paint(commit_msg, c.WHITE, bold=True))]
        first_line = next((ln.strip() for ln in commit_output.split("\n") if ln.strip()), "")
        if first_line:
            lines.append(self._kv("Result", c.paint(first_line, c.SLATE)))
        self._card("Committed", "ok", lines)

    def commit_failed(self, error: str) -> None:
        c = self.palette
        self._card("Commit skipped", "warn", [self._line(error, c.AMBER)])

    def plan_export_success(self, location: str, included: int, ignored: int, ignore_file: str, tokens: int = 0, size_kb: float = 0.0) -> None:
        c = self.palette
        token_str = f"~{tokens:,} tokens ({size_kb} KB)"
        lines = [
            f"  {c.paint('Location:', c.SLATE):<18} {c.paint(location, c.CYAN, bold=True)}",
            f"  {c.paint('Files included:', c.SLATE):<18} {c.paint(str(included), c.GREEN, bold=True)}",
            f"  {c.paint('Est. Tokens:', c.SLATE):<18} {c.paint(token_str, c.AMBER, bold=True)}",
            f"  {c.paint('Files ignored:', c.SLATE):<18} {c.paint(str(ignored), c.SLATE)}",
            f"  {c.paint('Ignore file:', c.SLATE):<18} {c.paint(ignore_file, c.WHITE)}",
        ]
        self._card("Clean Context Exported", "ok", lines)

    # ------------------------------------------------------------------ #
    # Warnings, errors & recovery
    # ------------------------------------------------------------------ #

    def warn(self, message: str) -> None:
        c = self.palette
        print(c.paint(f"  ▲ {message}", c.AMBER, bold=True))

    def _error_lines(self, message: str, rule_w: int) -> list[str]:
        """Colourise a diagnostic: ERR| tokens, similarity matches and embedded diffs."""
        c = self.palette
        out: list[str] = []
        in_diff = False
        for line in message.split("\n"):
            stripped = line.strip()
            if not stripped:
                out.append("")
                continue
            if set(stripped) == {"-"}:
                in_diff = True
                out.append(c.paint("─" * min(rule_w, 60), c.SLATE))
                continue
            if in_diff:
                out.append(self._line(self._diff_line(stripped)))
                continue
            if "ERR|" in stripped:
                prefix, token = stripped.split("ERR|", 1)
                out.append(self._line(f"{prefix}{c.paint('ERR|' + token, c.AMBER, bold=True)}"))
            elif stripped.startswith("Closest candidate"):
                badge = c.paint(" SIMILARITY ", c.CYAN, bold=True)
                out.append(self._line(f"{badge} {c.paint(stripped, c.WHITE)}"))
            else:
                out.append(self._line(line, c.RED))
        return out

    _ERROR_HINTS = (
        ("SEARCH_TOO_BIG", "SEARCH block is too large (limit is 60 lines / 4000 chars). Use a smaller unique anchor."),
        ("SEARCH_NOT_FOUND", "SEARCH block didn't match. Compare against the diff above and add unique lines."),
        ("SEARCH_AMBIGUOUS", "SEARCH target matches multiple locations. Include more surrounding lines for uniqueness."),
        ("CONFLICTING_OPERATIONS", "The plan performs contradictory operations on the same file. Separate or order your edits."),
        ("FORBIDDEN_COMMAND", "RUN command not permitted. Use whitelisted test runners or run manually."),
        ("FILE_PROTECTED", "Target is a sensitive file/key. Modify configuration files manually."),
        ("MULTIPLE_PLANS", "Multiple plan blocks found. Provide a single plan block per response."),
        ("UNKNOWN_COMMAND", "Unsupported command. Check the hint or run 'code-exec -h' for supported instructions."),
        ("PATCH_FAILED", "Unified diff hunk could not be matched. Verify context lines or use EDIT."),
    )

    def _error_hint(self, message: str) -> str:
        for code, text in self._ERROR_HINTS:
            if f"ERR|{code}" in message:
                return text
        return ""

    def error(self, message: str) -> None:
        c = self.palette
        rule_w = max(shutil.get_terminal_size((80, 24)).columns - 10, 60)
        lines = self._error_lines(message, rule_w=rule_w)
        lines += ["", self._divider()]
        hint = self._error_hint(message)
        if hint:
            lines.append(self._tip(hint))
        lines.append(self._line(f"{c.paint('●', c.SLATE)} {c.paint('Diagnostics prompt copied to clipboard for your AI chat.', c.SLATE)}"))
        self._card("Execution / validation error", "err", lines, wide=True, file=sys.stderr)

    def command_failed(self, error: str, backup_dir: Path | None) -> None:
        c = self.palette
        print(self.panel_bottom(), file=sys.stderr)  # close the "Executing" panel
        lines = [
            self._line(error, c.RED),
            self._line("Stopped. Earlier changes kept.", c.AMBER),
        ]
        if backup_dir is not None:
            lines.append(self._kv("Backups", c.paint(str(backup_dir), c.SLATE)))
        self._card("Command failed", "err", lines, file=sys.stderr)

    def apply_interrupted(self, exc: Exception) -> None:
        c = self.palette
        print(self.panel_bottom(), file=sys.stderr)  # close the "Executing" panel
        msg = "Execution interrupted by user." if isinstance(exc, KeyboardInterrupt) else str(exc)
        self._card("Interrupted", "err", [self._line(msg, c.RED)], file=sys.stderr)

    def rollback_report(
        self,
        errors: list[str],
        count: int,
        backup_dir: Path | None,
        ran_commands: list[str],
    ) -> None:
        c = self.palette
        if errors:
            lines = [self._line(err, c.RED) for err in errors]
            title, kind = "Incomplete rollback", "err"
        else:
            lines = [self._line(f"Rolled back {count} change(s); directory restored.", c.WHITE)]
            title, kind = "Rollback complete", "ok"
        if ran_commands:
            lines += ["", self._divider("Commands run before rollback")]
            lines += [self._line(f"❯ {cmd}", c.SLATE) for cmd in ran_commands]
        self._card(title, kind, lines, file=sys.stderr)


ui = TerminalUI()