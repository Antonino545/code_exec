from __future__ import annotations

import difflib
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Callable

from code_exec_types import COMMANDS, Operation, OpError, clean_path

COMMAND_ALIASES = {
    "UPDATE": "EDIT",
    "MODIFY": "EDIT",
    "CHANGE": "EDIT",
    "WRITE": "CREATE",
    "NEW": "CREATE",
    "ADD": "CREATE",
    "REMOVE": "DELETE",
    "RM": "DELETE",
    "DEL": "DELETE",
    "MV": "MOVE",
    "CP": "COPY",
    "APPEND_LINE": "APPEND",
    "PREPEND_LINE": "PREPEND",
    "RUN_COMMAND": "RUN",
    "EXEC": "RUN",
    "EXECUTE": "RUN",
}

# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #

# Several `code_exec` blocks in one reply are merged in order (with a warning).
# Set to False to restore the old hard ERR|MULTIPLE_PLANS failure.
MERGE_MULTIPLE_PLANS = True

# A reply with no plan at all (a normal answer, a question...) is not an error: extract_plan
# returns "" and parse_operations("") returns []. A plan that was started but is broken or cut
# off still raises. Set to True to restore the old hard ERR|PLAN_NOT_FOUND.
NO_PLAN_IS_ERROR = False

# Print tolerance warnings (through code_exec_ui when available). Tests turn this off.
PRINT_WARNINGS = True

# Everything the parser tolerated ("restored escaped delimiters", "ignored prose", ...).
# Read and clear it with take_warnings().
WARNINGS: list[str] = []
_PRINTED: set[str] = set()


def _warn(message: str) -> None:
    if message not in WARNINGS:
        WARNINGS.append(message)
    if PRINT_WARNINGS and message not in _PRINTED:
        _PRINTED.add(message)
        try:
            from code_exec_ui import ui

            ui.warn(message)
        except Exception:  # noqa: BLE001 - UI is optional
            print(f"  ▲ {message}", file=sys.stderr)


def take_warnings() -> list[str]:
    """Return and clear the tolerance warnings collected since the last call."""
    out = WARNINGS[:]
    WARNINGS.clear()
    return out


# --------------------------------------------------------------------------- #
# Block delimiters
#
# Two block styles are accepted:
#
# 1. Keyword style (canonical)
#        EDIT path
#        SEARCH <<<        ...        >>>
#        REPLACE <<<       ...        >>>
#    Chats and markdown renderers often eat or shorten the brackets, so an
#    opener may be 1-5 `<` and a closer 2-5 `>`. A lone `>` is deliberately NOT
#    a closer: it is very common on its own line in JSX/HTML
#    (`<div\n  className="x"\n>`), so accepting it would truncate blocks.
#
# 2. Conflict-marker style (git / aider), no SEARCH/REPLACE keywords
#        EDIT path
#        <<<<   (or <<<<<<< SEARCH)
#        old text
#        ====   (or =======)
#        new text
#        >>>>   (or >>>>>>> REPLACE)
#
# Either style can be repeated under one command to express several edits.
# --------------------------------------------------------------------------- #
_OPEN_LINE = re.compile(r"^<{1,5}$")          # opener on its own line
_OPEN_SUFFIX = re.compile(r"^(.*?)\s*<{1,5}$")  # opener at the end of a command line
_NEST_OPEN = re.compile(r"^<{3,5}$")          # nested block inside content
_CLOSE_STRICT = re.compile(r"^>{2,5}$")
_DIFF_OPEN = re.compile(r"^<{3,}\s*(?:SEARCH|ORIGINAL|OLD|MARKER)?\s*:?\s*$", re.IGNORECASE)
_DIFF_SEP = re.compile(r"^={3,}\s*$")
_DIFF_CLOSE = re.compile(r"^>{3,}\s*(?:REPLACE|UPDATED|NEW|CONTENT)?\s*:?\s*$", re.IGNORECASE)
_ARROW = re.compile(r"^(.+?)\s*(?:-+>|=+>|→|➜)\s*(.+)$")

_PAIR_COMMANDS = {"EDIT", "REPLACE_ALL", "INSERT_BEFORE", "INSERT_AFTER"}
_BLOCK_KEYWORDS = {"SEARCH", "REPLACE", "MARKER", "CONTENT", "THINK", "END_THINK", "END_OF_FILE"}
_SHELL_WORDS = {
    "npm", "npx", "pnpm", "yarn", "pytest", "python", "python3", "pip", "pip3", "uv", "cargo", "go",
    "ruff", "black", "git", "node", "make", "bash", "sh", "cd", "ls", "cat", "echo",
}
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"))
_THINK_TAG = re.compile(r"(?ims)^[ \t]*<(think(?:ing)?)>.*?</\1>[ \t]*\n?")


# --------------------------------------------------------------------------- #
# Clipboard
# --------------------------------------------------------------------------- #

def get_clipboard() -> str:
    env = None

    if sys.platform == "darwin":
        commands = [["pbpaste"]]
        env = {**os.environ, "LANG": "en_US.UTF-8"}
    elif sys.platform.startswith("linux"):
        commands = [
            ["wl-paste", "--no-newline"],
            ["xclip", "-selection", "clipboard", "-o"],
            ["xsel", "--clipboard", "--output"],
        ]
    elif sys.platform == "win32":
        commands = [[
            "powershell", "-NoProfile", "-Command",
            "[Console]::OutputEncoding = [Text.Encoding]::UTF8; Get-Clipboard -Raw",
        ]]
    else:
        commands = []

    for command in commands:
        try:
            result = subprocess.run(command, capture_output=True, check=True, timeout=10, env=env)
        except (OSError, subprocess.SubprocessError):
            continue
        return result.stdout.decode("utf-8", errors="replace")

    tried = ", ".join(c[0] for c in commands) or "no clipboard tool for this platform"
    raise RuntimeError(
        f"Could not read the clipboard (tried: {tried}). Use --file PLAN.txt instead."
    )


def set_clipboard(text: str) -> None:
    if sys.platform == "darwin":
        commands = [["pbcopy"]]
    elif sys.platform.startswith("linux"):
        commands = [
            ["wl-copy"],
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
        ]
    elif sys.platform == "win32":
        commands = [[
            "powershell", "-NoProfile", "-Command",
            "[Console]::InputEncoding = [Text.Encoding]::UTF8; Set-Clipboard",
        ]]
    else:
        commands = []

    for command in commands:
        try:
            subprocess.run(command, input=text.encode("utf-8"), check=True, timeout=10)
            return
        except (OSError, subprocess.SubprocessError):
            continue

    tried = ", ".join(c[0] for c in commands) or "no clipboard tool for this platform"
    raise RuntimeError(f"Could not copy to clipboard (tried: {tried}).")


# --------------------------------------------------------------------------- #
# Normalisation: undo damage done by chats / renderers / copy-paste
# --------------------------------------------------------------------------- #

def _first_token(text: str) -> str:
    m = re.match(r"\s*([A-Za-z_]+)", text)
    return m.group(1) if m else ""


def _known_line_tokens() -> set[str]:
    return set(COMMANDS) | set(COMMAND_ALIASES) | {"SEARCH", "REPLACE", "MARKER", "CONTENT"}


def _fix_line(line: str) -> str:
    """Restore escaped delimiters and typographic characters on delimiter / command lines only."""
    s = line.rstrip()

    # A delimiter that is the whole line: `&lt;&lt;&lt;`, `\<\<\<`, `&gt;&gt;&gt;`, `\>\>\>`
    m = re.fullmatch(r"\s*((?:&lt;|\\<){3,})", s)
    if m:
        return "<" * len(re.findall(r"&lt;|\\<", m.group(1)))
    m = re.fullmatch(r"\s*((?:&gt;|\\>){2,})", s)
    if m:
        return ">" * len(re.findall(r"&gt;|\\>", m.group(1)))

    # Command / keyword lines: escaped trailing opener, smart quotes, unicode arrows.
    if _first_token(s).upper() in _known_line_tokens():
        m = re.search(r"(?:&lt;|\\<){1,5}\s*$", s)
        if m:
            s = s[: m.start()] + "<" * len(re.findall(r"&lt;|\\<", m.group(0)))
        s = s.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
        s = re.sub(r"\s[–—−]+>", " ->", s)
        return s if s != line.rstrip() else line
    return line


def _dedent_plan(text: str) -> str:
    """If the whole plan is uniformly indented (e.g. pasted from a list), remove the indent."""
    nonblank = [ln for ln in text.split("\n") if ln.strip()]
    if not nonblank:
        return text
    indent = min(len(ln) - len(ln.lstrip()) for ln in nonblank)
    if indent > 0 and _is_strict_command(nonblank[0]):
        return textwrap.dedent(text)
    return text


def _normalize(text: str) -> str:
    text = (
        text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ").translate(_ZERO_WIDTH)
    )
    text, removed = _THINK_TAG.subn("", text)
    if removed:
        _warn(f"ignored {removed} <thinking> block(s)")

    fixed = 0
    lines = []
    for line in text.split("\n"):
        new = _fix_line(line)
        if new != line:
            fixed += 1
        lines.append(new)
    if fixed:
        _warn(f"restored {fixed} escaped or typographic delimiter/command line(s)")
    return _dedent_plan("\n".join(lines))


def _norm_path(raw: str) -> str:
    """`**./src\\a.js**` -> `src/a.js`; absolute paths inside the project become relative."""
    p = raw.strip().strip("*`'\"").strip()
    if "\\" in p and "/" not in p:
        p = p.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    if p.startswith("/"):
        try:
            rel = Path(p).resolve().relative_to(Path.cwd().resolve())
        except (ValueError, OSError):
            return p  # leave it: the executor rejects it as INVALID_PATH
        _warn(f"absolute path made project-relative: {rel.as_posix()}")
        return rel.as_posix()
    return p


def _path(raw: str) -> str:
    return clean_path(_norm_path(raw))


# --------------------------------------------------------------------------- #
# Line classification
# --------------------------------------------------------------------------- #

def _is_strict_command(line: str) -> bool:
    """`EDIT path`, `RUN pytest` ... : exact upper-case command followed by an argument."""
    m = re.match(r"^([A-Z_]+):?\s+\S", line.strip())
    return bool(m) and (m.group(1) in COMMANDS or m.group(1) in COMMAND_ALIASES)


def _is_prose(line: str) -> bool:
    """
    True for commentary that can safely be skipped at the edges of a plan.
    Anything that could be a (mistyped) command, shell command or structural marker is
    NOT prose, so real mistakes still produce an error instead of vanishing silently.
    """
    s = line.strip()
    if s.startswith(("<", "=", ">")):
        return False
    tok = _first_token(s)
    if not tok:
        return True
    up = tok.upper()
    if up in _BLOCK_KEYWORDS or tok.lower() in _SHELL_WORDS:
        return False
    if up in COMMANDS or up in COMMAND_ALIASES:
        # "Create a new file called foo" is a sentence; "Create src/a.py <<<" is a command.
        sentence = (
            tok.istitle()
            and len(s.split()) >= 4
            and "/" not in s
            and "\\" not in s
            and "->" not in s
            and not s.endswith("<")
        )
        return sentence
    if difflib.get_close_matches(up, sorted(COMMANDS), n=1, cutoff=0.8):
        return False  # probably a typo of a command
    return True


def _has_later_command(lines: list[str], start: int) -> bool:
    return any(_is_strict_command(ln) for ln in lines[start:])


# --------------------------------------------------------------------------- #
# Plan extraction
# --------------------------------------------------------------------------- #

def _is_code_exec_fence(info: str) -> bool:
    """Accept `code_exec`, `code-exec`, `code exec`, `CODE_EXEC`, `code_exec plan`, `code_exec:` ..."""
    return re.sub(r"[^a-z]", "", info.lower()).startswith("codeexec")


def _parses_cleanly(text: str) -> bool:
    try:
        return bool(_parse_text(text, lambda _msg: None))
    except (OpError, ValueError):
        return False


def _find_plan_start(lines: list[str]) -> int | None:
    """Index of the first line of an unfenced plan (skipping leading prose), or None."""
    first: int | None = None
    for idx, raw in enumerate(lines):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if first is None:
            first = idx
        if s.split()[0] == "THINK":
            rest = [ln.strip() for ln in lines[idx:]]
            if idx == first and "END_THINK" in rest and any(_is_strict_command(ln) for ln in rest):
                return idx
            continue
        if _is_strict_command(s):
            # A plan must start the reply; after leading prose it must also parse cleanly,
            # so a sentence like "RUN the tests now" can never be mistaken for a plan.
            # RUN/COMMIT take free text, so they are only trusted as the very first line.
            free_text = s.split()[0].rstrip(":") in {"RUN", "COMMIT", "EXEC", "EXECUTE", "RUN_COMMAND"}
            if (idx == first and not (free_text and len(lines) > idx + 1 and _is_prose(lines[idx]))) or (
                idx != first and not free_text and _parses_cleanly("\n".join(lines[idx:]))
            ):
                return idx
    return None


def extract_plan(text: str) -> str:
    """
    Extract the executable code_exec plan block from an AI response.
    Ignores conversational explanations, Markdown prose, and unrelated code blocks.
    """
    sanitized = _normalize(text)
    lines = sanitized.split("\n")

    plans: list[str] = []
    unclosed: list[str] = []

    def keep_unclosed(block: str, what: str) -> None:
        # Forgot the closing fence, but every operation is complete: accept it.
        if _parses_cleanly(block):
            _warn(f"closing marker missing for {what}; plan is complete, applying it")
            plans.append(block)
        else:
            unclosed.append(f"unclosed {what}")

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Format 2: CODE_EXEC_PLAN ... END_CODE_EXEC_PLAN
        if stripped.upper().replace(" ", "_") == "CODE_EXEC_PLAN":
            i += 1
            block_lines = []
            found_end = False
            while i < len(lines):
                if lines[i].strip().upper().replace(" ", "_") == "END_CODE_EXEC_PLAN":
                    found_end = True
                    i += 1
                    break
                block_lines.append(lines[i])
                i += 1
            block = "\n".join(block_lines)
            if found_end:
                plans.append(block)
            else:
                keep_unclosed(block, "CODE_EXEC_PLAN block")
            continue

        # Format 1: Code fences (``` or ~~~)
        fence_match = re.match(r"^[ \t]*(`{3,}|~{3,})(.*)$", line)
        if fence_match:
            fence_chars = fence_match.group(1)
            fence_char = fence_chars[0]
            fence_len = len(fence_chars)
            is_code_exec = _is_code_exec_fence(fence_match.group(2).strip())

            i += 1
            block_lines = []
            found_end = False
            content_depth = 0

            while i < len(lines):
                curr = lines[i]
                curr_stripped = curr.strip()

                if is_code_exec:
                    if _NEST_OPEN.match(curr_stripped):
                        content_depth += 1
                    elif _CLOSE_STRICT.match(curr_stripped):
                        if content_depth > 0:
                            content_depth -= 1

                if content_depth == 0:
                    close_match = re.match(r"^[ \t]*(`{3,}|~{3,})[ \t]*$", curr)
                    if close_match:
                        c_chars = close_match.group(1)
                        if c_chars[0] == fence_char and len(c_chars) >= fence_len:
                            found_end = True
                            i += 1
                            break

                if is_code_exec:
                    block_lines.append(curr)
                i += 1

            if is_code_exec:
                block = "\n".join(block_lines)
                if found_end:
                    plans.append(block)
                else:
                    keep_unclosed(block, "code_exec block")
            continue

        i += 1

    if unclosed:
        if len(plans) == 0:
            raise OpError(f"ERR|PLAN_NOT_FOUND|{unclosed[0]}")
        raise OpError(f"ERR|MULTIPLE_PLANS|{len(plans) + len(unclosed)}")

    if len(plans) == 1:
        return plans[0]

    if len(plans) > 1:
        if MERGE_MULTIPLE_PLANS:
            _warn(f"{len(plans)} plan blocks found; merged in order (prefer a single block)")
            return "\n".join(plans)
        raise OpError(f"ERR|MULTIPLE_PLANS|{len(plans)}")

    # Format 3: unfenced plan (optionally wrapped in a plain fence, optionally after some prose)
    trimmed = sanitized.strip()
    candidate = trimmed

    m_wrap = re.match(r"^(`{3,}|~{3,})(?:[a-zA-Z0-9_-]*)\n(.*)\n\1$", trimmed, re.DOTALL)
    if m_wrap:
        candidate = m_wrap.group(2).strip()

    cand_lines = candidate.split("\n")
    start = _find_plan_start(cand_lines)
    if start is not None:
        if start > 0:
            _warn(f"ignored {start} line(s) of text before the plan")
        return "\n".join(cand_lines[start:])
    if NO_PLAN_IS_ERROR:
        raise OpError("ERR|PLAN_NOT_FOUND")
    return ""


def has_plan(text: str) -> bool:
    """True if the reply contains a plan (used to skip the run silently when it does not)."""
    try:
        return bool(extract_plan(text).strip())
    except OpError:
        return True  # a plan was attempted but is broken: let the caller report the error


# --------------------------------------------------------------------------- #
# Block reading
# --------------------------------------------------------------------------- #

def _find_close(lines: list[str], start: int, closer: re.Pattern[str]) -> int:
    """Index of the line that closes a block whose content begins at `start`, or -1."""
    depth = 1
    for k in range(start, len(lines)):
        line_str = lines[k].strip()
        if _NEST_OPEN.match(line_str):
            depth += 1
        elif closer.match(line_str):
            depth -= 1
            if depth == 0:
                return k
    return -1


def _read_delimited(lines: list[str], start: int) -> tuple[str, int]:
    """Read block content beginning at `start` (the opener line is already consumed)."""
    k = _find_close(lines, start, _CLOSE_STRICT)
    if k < 0:
        raise ValueError(f"Missing >>> for block opened at line {start}")
    return "\n".join(lines[start:k]), k + 1


def read_block(lines: list[str], i: int, inline_started: bool = False) -> tuple[str, int]:
    """Read a `<<< ... >>>` block (any `<`/`>` run) or raw lines up to END_OF_FILE."""
    if inline_started:
        return _read_delimited(lines, i)

    j = i
    while j < len(lines) and (not lines[j].strip() or lines[j].strip().startswith("#")):
        j += 1
    if j < len(lines) and _OPEN_LINE.match(lines[j].strip()):
        return _read_delimited(lines, j + 1)

    k = i
    while k < len(lines) and lines[k].strip() != "END_OF_FILE":
        k += 1
    if k >= len(lines):
        raise ValueError(f"Missing END_OF_FILE for block starting at line {i + 1}")
    return "\n".join(lines[i:k]), k + 1


def expect_keyword(lines: list[str], i: int, keyword: str, command: str) -> tuple[int, bool]:
    """
    Accept `SEARCH`, `SEARCH <<<`, `SEARCH <`, `search:`, `Search: <<<` ... (case-insensitive).
    Returns (next line index, whether the block opener was on the keyword line).
    """
    while i < len(lines) and (not lines[i].strip() or lines[i].strip().startswith("#")):
        i += 1
    if i >= len(lines):
        raise ValueError(f"{command} requires {keyword} <<< ... >>> (plan ended before it)")
    line = lines[i].strip()
    m = re.match(rf"^{keyword}\s*:?\s*(<{{1,5}})?\s*$", line, re.IGNORECASE)
    if m:
        return i + 1, bool(m.group(1))
    raise ValueError(
        f"{command} requires {keyword} <<< ... >>> or a <<<< / ==== / >>>> block "
        f"(line {i + 1}, found {line[:40]!r})"
    )


def _try_diff_pair(lines: list[str], i: int, command: str) -> tuple[str, str, int] | None:
    """
    Read a conflict-marker pair (`<<<<` old `====` new `>>>>`) starting at line `i`.
    Returns (first, second, next_index), or None if the next line is not an opener.
    The closer is located first so a stray `====` in a later operation is never mistaken
    for this block's separator.
    """
    j = i
    while j < len(lines) and (not lines[j].strip() or lines[j].strip().startswith("#")):
        j += 1
    if j >= len(lines) or not _DIFF_OPEN.match(lines[j].strip()):
        return None

    start = j + 1
    end = next((k for k in range(start, len(lines)) if _DIFF_CLOSE.match(lines[k].strip())), -1)
    if end < 0:
        raise ValueError(f"{command}: missing >>>> for block opened at line {j + 1}")
    sep = next((k for k in range(start, end) if _DIFF_SEP.match(lines[k].strip())), -1)
    if sep < 0:
        raise ValueError(f"{command}: missing ==== separator in block opened at line {j + 1}")
    return "\n".join(lines[start:sep]), "\n".join(lines[sep + 1:end]), end + 1


def _read_pair(lines: list[str], i: int, command: str) -> tuple[str, str, int]:
    """One (search, replace) or (marker, content) pair, in either block style."""
    pair = _try_diff_pair(lines, i, command)
    if pair:
        return pair
    first_kw, second_kw = ("MARKER", "CONTENT") if command.startswith("INSERT") else ("SEARCH", "REPLACE")
    i, first_inline = expect_keyword(lines, i, first_kw, command)
    first, i = read_block(lines, i, inline_started=first_inline)
    i, second_inline = expect_keyword(lines, i, second_kw, command)
    second, i = read_block(lines, i, inline_started=second_inline)
    return first, second, i


def _peek_pair(lines: list[str], i: int, command: str) -> tuple[str, str, int] | None:
    """If another pair follows the one just read, read it (same command and path)."""
    j = i
    while j < len(lines) and (not lines[j].strip() or lines[j].strip().startswith("#")):
        j += 1
    if j >= len(lines):
        return None
    s = lines[j].strip()
    keyword = "MARKER" if command.startswith("INSERT") else "SEARCH"
    if not (_DIFF_OPEN.match(s) or re.match(rf"^{keyword}(?:\s*:?\s*<{1,5}|\s*:?\s*$)", s, re.IGNORECASE)):
        return None
    return _read_pair(lines, j, command)


# --------------------------------------------------------------------------- #
# Instruction parsing
# --------------------------------------------------------------------------- #

def _unwrap(text: str) -> str:
    """Drop a leading `$ ` prompt and wrapping quotes/backticks around a one-line argument."""
    text = text.strip()
    if text.startswith("$ "):
        text = text[2:].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "`'\"":
        text = text[1:-1].strip()
    return text


def _resolve_command(raw: str, rest: str) -> str | None:
    """Canonical command for `raw` (case-insensitive, aliases allowed) or None."""
    upper = raw.strip().upper()
    if upper in COMMANDS:
        return upper
    alias = COMMAND_ALIASES.get(upper)
    # `rm -rf x` / `cp -r a b` are shell commands, not plan commands.
    if alias and not rest.lstrip().startswith("-"):
        return alias
    return None


def _parse_instruction(lines: list[str], i: int) -> tuple[Operation, int]:
    line = lines[i].strip()
    match = re.match(r"^([A-Za-z_]+):?(?:\s+(.*))?$", line)
    raw_cmd = (match.group(1) if match else line.split()[0]).strip()
    rest = (match.group(2) or "").strip() if match else ""

    command = _resolve_command(raw_cmd, rest) if match else None
    if command is None:
        cmd_upper = raw_cmd.upper()
        if cmd_upper in COMMAND_ALIASES:
            hint = f" Did you mean '{COMMAND_ALIASES[cmd_upper]}'?"
        elif raw_cmd.lower() in _SHELL_WORDS:
            hint = f" Shell commands must be prefixed with RUN. Did you mean 'RUN {line}'?"
        else:
            matches = difflib.get_close_matches(cmd_upper, sorted(COMMANDS), n=1, cutoff=0.6)
            hint = f" Did you mean '{matches[0]}'?" if matches else ""
            if not matches:
                hint = " If this is commentary, put it outside the code_exec block."
        raise OpError(f"ERR|UNKNOWN_COMMAND|{raw_cmd} - Unsupported command.{hint}")

    i += 1

    if not rest:
        raise ValueError(f"{command} requires an argument")

    if command in {"RUN", "COMMIT"}:
        return Operation(command, (_unwrap(rest),)), i

    if command in {"MOVE", "COPY", "RENAME"}:
        pair = _ARROW.match(rest)
        if not pair:
            raise ValueError(f"{command} requires 'source -> destination'")
        return Operation(command, (_path(pair[1]), _path(pair[2]))), i

    if command == "CHMOD":
        parts = rest.rsplit(None, 1)
        if len(parts) != 2:
            raise ValueError("CHMOD requires 'path mode' (e.g. CHMOD run.sh +x or 755)")
        return Operation("CHMOD", (_path(parts[0]), parts[1].strip())), i

    if command in {"DELETE", "MKDIR", "TOUCH"}:
        return Operation(command, (_path(rest.rstrip(":")),)), i

    # Commands that carry a content block. The opener may trail the path: `CREATE a.py <<<`.
    inline_block = False
    opener = _OPEN_SUFFIX.match(rest)
    if opener and opener.group(1):
        rest = opener.group(1)
        inline_block = True

    path = _path(rest.rstrip(":").strip())

    if command in {"CREATE", "APPEND", "PREPEND", "PATCH"}:
        content, i = read_block(lines, i, inline_started=inline_block)
        return Operation(command, (path,), content), i

    # EDIT / REPLACE_ALL (search, replace) and INSERT_BEFORE / INSERT_AFTER (marker, content)
    first, second, i = _read_pair(lines, i, command)
    return Operation(command, (path,), first, second), i


def _parse_text(text: str, warn: Callable[[str], None]) -> list[Operation]:
    lines = text.split("\n")
    operations: list[Operation] = []
    leading_skipped = 0
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        if not line or line.startswith(("```", "~~~")) or line.startswith("#"):
            i += 1
            continue

        if line == "THINK":
            i += 1
            while i < len(lines) and lines[i].strip() != "END_THINK":
                i += 1
            if i >= len(lines):
                raise ValueError(f"line {i + 1}: THINK block missing END_THINK")
            i += 1
            continue

        lineno = i + 1

        # Commentary before the first command is skipped (and reported).
        if not operations and _is_prose(line):
            leading_skipped += 1
            i += 1
            continue

        # If a bare SEARCH/MARKER block appears after an EDIT/INSERT operation,
        # attach it to the preceding operation instead of failing as an unknown command.
        if operations and operations[-1].command in _PAIR_COMMANDS:
            last_cmd = operations[-1].command
            kw = "MARKER" if last_cmd.startswith("INSERT") else "SEARCH"
            if _DIFF_OPEN.match(line) or re.match(rf"^{kw}(?:\s*:?\s*<{1,5}|\s*:?\s*$)", line, re.IGNORECASE):
                try:
                    first, second, i = _read_pair(lines, i, last_cmd)
                    operations.append(Operation(last_cmd, operations[-1].args, first, second))
                    continue
                except ValueError as exc:
                    raise ValueError(f"line {lineno}: {exc}") from None

        try:
            operation, i = _parse_instruction(lines, i)
        except OpError as exc:
            # Commentary after the last command is skipped; mid-plan surprises still fail.
            if operations and _is_prose(line) and not _has_later_command(lines, i + 1):
                warn(f"ignored trailing text after the plan (from line {lineno})")
                break
            raise OpError(f"line {lineno}: {exc}") from None
        except ValueError as exc:
            raise ValueError(f"line {lineno}: {exc}") from None
        operations.append(operation)

        # Several SEARCH/REPLACE (or MARKER/CONTENT) pairs under one command become
        # separate operations on the same path.
        if operation.command in _PAIR_COMMANDS:
            extra = 0
            while True:
                try:
                    nxt = _peek_pair(lines, i, operation.command)
                except ValueError as exc:
                    raise ValueError(f"line {lineno}: {exc}") from None
                if nxt is None:
                    break
                first, second, i = nxt
                operations.append(Operation(operation.command, operation.args, first, second))
                extra += 1
            if extra:
                warn(
                    f"line {lineno}: {extra + 1} blocks under one {operation.command} "
                    f"applied as {extra + 1} separate operations"
                )

    if leading_skipped:
        warn(f"ignored {leading_skipped} line(s) of text before the plan")
    return operations


def parse_operations(text: str) -> list[Operation]:
    return _parse_text(_normalize(text), _warn)