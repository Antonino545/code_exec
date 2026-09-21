from __future__ import annotations

import difflib
import os
import re
import subprocess
import sys
from pathlib import Path

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
# --------------------------------------------------------------------------- #
_OPEN_LINE = re.compile(r"^<{1,5}$")          # opener on its own line
_OPEN_SUFFIX = re.compile(r"^(.*?)\s*<{1,5}$")  # opener at the end of a command line
_NEST_OPEN = re.compile(r"^<{3,5}$")          # nested block inside content
_CLOSE_STRICT = re.compile(r"^>{2,5}$")
_DIFF_OPEN = re.compile(r"^<{3,}\s*(?:SEARCH|ORIGINAL|OLD|MARKER)?\s*:?\s*$", re.IGNORECASE)
_DIFF_SEP = re.compile(r"^={3,}\s*$")
_DIFF_CLOSE = re.compile(r"^>{3,}\s*(?:REPLACE|UPDATED|NEW|CONTENT)?\s*:?\s*$", re.IGNORECASE)
_ARROW = re.compile(r"^(.+?)\s*(?:-+>|=+>|→|➜)\s*(.+)$")


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


def _is_code_exec_fence(info: str) -> bool:
    """Accept `code_exec`, `code-exec`, `code exec`, `CODE_EXEC`, `code_exec plan`, `code_exec:` ..."""
    return re.sub(r"[^a-z]", "", info.lower()).startswith("codeexec")


def extract_plan(text: str) -> str:
    """
    Extract the executable code_exec plan block from an AI response.
    Ignores conversational explanations, Markdown prose, and unrelated code blocks.
    """
    sanitized = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\u00a0", " ")
    lines = sanitized.split("\n")

    plans: list[str] = []
    unclosed: list[str] = []

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
            if found_end:
                plans.append("\n".join(block_lines))
            else:
                unclosed.append("unclosed CODE_EXEC_PLAN block")
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
                if found_end:
                    plans.append("\n".join(block_lines))
                else:
                    unclosed.append("unclosed code_exec block")
            continue

        i += 1

    if unclosed:
        if len(plans) == 0:
            raise OpError(f"ERR|PLAN_NOT_FOUND|{unclosed[0]}")
        raise OpError(f"ERR|MULTIPLE_PLANS|{len(plans) + len(unclosed)}")

    if len(plans) == 1:
        return plans[0]

    if len(plans) > 1:
        raise OpError(f"ERR|MULTIPLE_PLANS|{len(plans)}")

    # Format 3: bare plan (no fence) - starts directly with a command, or a legacy THINK block
    trimmed = sanitized.strip()
    candidate = trimmed

    m_wrap = re.match(r"^(`{3,}|~{3,})(?:[a-zA-Z0-9_-]*)\n(.*)\n\1$", trimmed, re.DOTALL)
    if m_wrap:
        candidate = m_wrap.group(2).strip()

    cand_lines = [ln for ln in candidate.split("\n") if ln.strip()]
    cand_idx = 0
    while cand_idx < len(cand_lines) and cand_lines[cand_idx].strip().startswith("#"):
        cand_idx += 1
    if cand_idx < len(cand_lines):
        first_word = cand_lines[cand_idx].strip().split()[0].rstrip(":")
        if first_word == "THINK":
            has_end_think = any(ln.strip() == "END_THINK" for ln in cand_lines)
            has_command = any(
                re.match(r"^([A-Z_]+)(?:\s+.*)?$", ln.strip()) and ln.strip().split()[0] in COMMANDS
                for ln in cand_lines
            )
            if has_end_think and has_command:
                return candidate
        elif first_word in COMMANDS or first_word in COMMAND_ALIASES:
            return candidate
    raise OpError("ERR|PLAN_NOT_FOUND")


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
        elif raw_cmd.lower() in {"npm", "pnpm", "yarn", "pytest", "python", "python3", "cargo", "go", "ruff", "black", "git"}:
            hint = f" Shell commands must be prefixed with RUN. Did you mean 'RUN {line}'?"
        else:
            matches = difflib.get_close_matches(cmd_upper, sorted(COMMANDS), n=1, cutoff=0.6)
            hint = f" Did you mean '{matches[0]}'?" if matches else ""
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
        return Operation(command, (clean_path(pair[1]), clean_path(pair[2]))), i

    if command == "CHMOD":
        parts = rest.rsplit(None, 1)
        if len(parts) != 2:
            raise ValueError("CHMOD requires 'path mode' (e.g. CHMOD run.sh +x or 755)")
        return Operation("CHMOD", (clean_path(parts[0]), parts[1].strip())), i

    if command in {"DELETE", "MKDIR", "TOUCH"}:
        return Operation(command, (clean_path(rest.rstrip(":")),)), i

    # Commands that carry a content block. The opener may trail the path: `CREATE a.py <<<`.
    inline_block = False
    opener = _OPEN_SUFFIX.match(rest)
    if opener and opener.group(1):
        rest = opener.group(1)
        inline_block = True

    path = clean_path(rest.rstrip(":").strip())

    if command in {"CREATE", "APPEND", "PREPEND", "PATCH"}:
        content, i = read_block(lines, i, inline_started=inline_block)
        return Operation(command, (path,), content), i

    if command in {"EDIT", "REPLACE_ALL"}:
        pair = _try_diff_pair(lines, i, command)
        if pair:
            search, replace, i = pair
            return Operation(command, (path,), search, replace), i
        i, search_inline = expect_keyword(lines, i, "SEARCH", command)
        search, i = read_block(lines, i, inline_started=search_inline)
        i, replace_inline = expect_keyword(lines, i, "REPLACE", command)
        replace, i = read_block(lines, i, inline_started=replace_inline)
        return Operation(command, (path,), search, replace), i

    # INSERT_BEFORE / INSERT_AFTER
    pair = _try_diff_pair(lines, i, command)
    if pair:
        marker, content, i = pair
        return Operation(command, (path,), marker, content), i
    i, marker_inline = expect_keyword(lines, i, "MARKER", command)
    marker, i = read_block(lines, i, inline_started=marker_inline)
    i, content_inline = expect_keyword(lines, i, "CONTENT", command)
    content, i = read_block(lines, i, inline_started=content_inline)
    return Operation(command, (path,), marker, content), i


def parse_operations(text: str) -> list[Operation]:
    sanitized_text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\u00a0", " ")
    lines = sanitized_text.split("\n")
    operations = []
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
        try:
            operation, i = _parse_instruction(lines, i)
        except OpError as exc:
            raise OpError(f"line {lineno}: {exc}") from None
        except ValueError as exc:
            raise ValueError(f"line {lineno}: {exc}") from None
        operations.append(operation)

    return operations