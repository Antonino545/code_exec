from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from code_exec_types import COMMANDS, Operation, OpError, clean_path


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
        if stripped == "CODE_EXEC_PLAN":
            i += 1
            block_lines = []
            found_end = False
            while i < len(lines):
                if lines[i].strip() == "END_CODE_EXEC_PLAN":
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
            info = fence_match.group(2).strip().lower()

            is_code_exec = (
                info == "code_exec"
                or info == "code-exec"
                or info.startswith("code_exec ")
                or info.startswith("code_exec:")
            )

            i += 1
            block_lines = []
            found_end = False
            content_depth = 0

            while i < len(lines):
                curr = lines[i]
                curr_stripped = curr.strip()

                if is_code_exec:
                    if curr_stripped == "<<<":
                        content_depth += 1
                    elif curr_stripped == ">>>":
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

    # Format 3: Backward compatibility for legacy response starting with THINK
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
        first_word = cand_lines[cand_idx].strip().split()[0]
        if first_word == "THINK":
            has_end_think = any(ln.strip() == "END_THINK" for ln in cand_lines)
            has_command = any(
                re.match(r"^([A-Z_]+)(?:\s+.*)?$", ln.strip()) and ln.strip().split()[0] in COMMANDS
                for ln in cand_lines
            )
            if has_end_think and has_command:
                return candidate
        elif first_word in COMMANDS:
            return candidate
    raise OpError("ERR|PLAN_NOT_FOUND")


def read_block(lines: list[str], i: int, inline_started: bool = False) -> tuple[str, int]:
    """Read a `<<< ... >>>` block or raw lines up to END_OF_FILE."""
    j = i
    if inline_started:
        start = j
        k = start
        depth = 1
        while k < len(lines):
            line_str = lines[k].strip()
            if line_str == "<<<":
                depth += 1
            elif line_str == ">>>":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        if k >= len(lines):
            raise ValueError("Missing >>> for block opened with <<<")
        return "\n".join(lines[start:k]), k + 1

    while j < len(lines) and not lines[j].strip():
        j += 1

    if j < len(lines) and lines[j].strip().startswith("<<<"):
        start = j + 1
        k = start
        depth = 1
        while k < len(lines):
            line_str = lines[k].strip()
            if line_str == "<<<":
                depth += 1
            elif line_str == ">>>":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        if k >= len(lines):
            raise ValueError(f"Missing >>> for block opened at line {j + 1}")
        return "\n".join(lines[start:k]), k + 1

    k = i
    while k < len(lines) and lines[k].strip() != "END_OF_FILE":
        k += 1
    if k >= len(lines):
        raise ValueError(f"Missing END_OF_FILE for block starting at line {i + 1}")
    return "\n".join(lines[i:k]), k + 1


def expect_keyword(lines: list[str], i: int, keyword: str, command: str) -> tuple[int, bool]:
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines):
        raise ValueError(f"{command} requires {keyword} (line {i + 1})")
    line = lines[i].strip()
    if line == keyword:
        return i + 1, False
    if line.startswith(keyword) and line[len(keyword):].strip() == "<<<":
        return i + 1, True
    raise ValueError(f"{command} requires {keyword} (line {i + 1})")


def _parse_instruction(lines: list[str], i: int) -> tuple[Operation, int]:
    line = lines[i].strip()
    match = re.match(r"^([A-Z_]+)(?:\s+(.*))?$", line)

    if not match or match.group(1) not in COMMANDS:
        raise ValueError(f"Unknown instruction: {line!r}")

    command = match.group(1)
    rest = (match.group(2) or "").strip()
    i += 1

    if not rest:
        raise ValueError(f"{command} requires an argument")

    if command in {"RUN", "COMMIT"}:
        return Operation(command, (rest,)), i

    if command in {"MOVE", "COPY", "RENAME"}:
        pair = re.match(r"^(.+?)\s*->\s*(.+)$", rest)
        if not pair:
            raise ValueError(f"{command} requires 'source -> destination'")
        return Operation(command, (clean_path(pair[1]), clean_path(pair[2]))), i

    if command == "CHMOD":
        parts = rest.rsplit(None, 1)
        if len(parts) != 2:
            raise ValueError("CHMOD requires 'path mode' (e.g. CHMOD run.sh +x or 755)")
        return Operation("CHMOD", (clean_path(parts[0]), parts[1].strip())), i

    path = clean_path(rest)
    if command in {"DELETE", "MKDIR", "TOUCH"}:
        return Operation(command, (path,)), i

    inline_block = False
    if rest.endswith("<<<"):
        rest = rest[:-3].strip()
        inline_block = True

    path = clean_path(rest)

    if command in {"CREATE", "APPEND", "PREPEND"}:
        content, i = read_block(lines, i, inline_started=inline_block)
        return Operation(command, (path,), content), i

    if command in {"EDIT", "REPLACE_ALL"}:
        i, search_inline = expect_keyword(lines, i, "SEARCH", command)
        search, i = read_block(lines, i, inline_started=search_inline)
        i, replace_inline = expect_keyword(lines, i, "REPLACE", command)
        replace, i = read_block(lines, i, inline_started=replace_inline)
        return Operation(command, (path,), search, replace), i

    # INSERT_BEFORE / INSERT_AFTER
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

        if not line or line.startswith("```") or line.startswith("#"):
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
        except ValueError as exc:
            raise ValueError(f"line {lineno}: {exc}") from None
        operations.append(operation)

    return operations
