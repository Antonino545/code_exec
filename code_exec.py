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
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from code_exec_ui import ui


ROOT = Path.cwd().resolve()

# Path components that plans may never touch.
PROTECTED_NAMES = {".git"}

# Sensitive file basenames and patterns that plans may never modify or delete.
PROTECTED_FILE_EXACT = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.staging",
    ".env.test",
    "id_rsa",
    "id_ed25519",
}

PROTECTED_SUFFIXES = {
    ".pem",
    ".key",
    ".crt",
    ".cer",
    ".pfx",
    ".p12",
}

DEFAULT_RUN_TIMEOUT = 600  # seconds per RUN command; 0 disables the limit

# Allowed executables and tools for RUN commands
ALLOWED_RUN_COMMAND_PREFIXES = (
    "python3 -m unittest",
    "python3 -m pytest",
    "pytest",
    "unittest",
    "npm test",
    "npm run ",
    "npm install",
    "pnpm test",
    "pnpm run ",
    "yarn test",
    "yarn run ",
    "cargo test",
    "cargo check",
    "cargo build",
    "go test",
    "go build",
    "ruff ",
    "black ",
    "flake8",
    "mypy",
    "git status",
    "git diff",
)

# Commands that are permitted ONLY with explicit interactive terminal approval
INTERACTIVE_ONLY_PREFIXES = (
    "python ",
    "python3 ",
    "node ",
    "bash ",
    "sh ",
)

# High-risk patterns forbidden in RUN commands even if prefix matches
FORBIDDEN_RUN_SUBSTRINGS = (
    "rm -rf",
    "rm -fr",
    "sudo",
    "mkfs",
    "dd if=",
    ":(){ :|:& };:",
    "> /dev/sd",
    "curl ",
    "wget ",
    "| sh",
    "| bash",
    "chmod 777",
    "chown -R",
)


# ============================================================
# Errors
# ============================================================

class OpError(Exception):
    """An operation cannot be performed."""


class CommandFailed(OpError):
    """A RUN command failed. File changes made so far are kept."""


class Unverifiable(Exception):
    """The dry-run simulation cannot predict what happens next."""


# ============================================================
# Path helpers
# ============================================================

def rel(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def clean_path(raw: str) -> str:
    """Trim whitespace and one layer of matching quotes/backticks."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'`":
        raw = raw[1:-1].strip()
    return raw


def safe_path(value: str, *, follow_leaf: bool = True) -> Path:
    """
    Resolve a project-relative path, refusing anything outside ROOT, ROOT
    itself, and protected names such as .git.
    """
    if not value or "\0" in value:
        raise OpError(f"ERR|INVALID_PATH|{value} - Empty or invalid path")

    candidate = Path(os.path.normpath(os.path.join(ROOT, value)))

    try:
        if follow_leaf:
            resolved = candidate.resolve()
        else:
            resolved = candidate.parent.resolve() / candidate.name
        relative = resolved.relative_to(ROOT)
    except (ValueError, OSError, RuntimeError):
        raise OpError(f"ERR|INVALID_PATH|{value} - Path outside project: {value}") from None

    if not relative.parts:
        raise OpError(f"ERR|INVALID_PATH|{value} - Refusing to operate on the project root: {value!r}")

    if any(part.lower() in PROTECTED_NAMES for part in relative.parts):
        raise OpError(f"ERR|PROTECTED_PATH|{value} - Refusing to touch protected path: {value}")

    # Guard sensitive and secret files
    file_name = resolved.name.lower()
    if file_name in PROTECTED_FILE_EXACT or any(file_name.endswith(ext) for ext in PROTECTED_SUFFIXES):
        raise OpError(f"ERR|FILE_PROTECTED|{value} - Refusing to touch sensitive credential/key file")

    # Guard CI/CD workflows
    rel_posix = relative.as_posix().lower()
    if rel_posix.startswith(".github/workflows/") or rel_posix == ".gitlab-ci.yml":
        raise OpError(f"ERR|FILE_PROTECTED|{value} - Refusing to touch CI/CD workflow definition")

    return resolved


# ============================================================
# Text helpers
# ============================================================

def _default_file_mode() -> int:
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


NEW_FILE_MODE = _default_file_mode()


def read_text(path: Path) -> str:
    """Read UTF-8 text without newline translation (CRLF stays CRLF)."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise OpError(f"Cannot read {rel(path)}: {exc.strerror or exc}") from None

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise OpError(f"{rel(path)} is not valid UTF-8 text (binary file?)") from None


def atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    """Write via a temp file in the same directory, then rename over target."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, NEW_FILE_MODE if mode is None else mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def newline_style(text: str) -> str:
    """Dominant line ending of an existing file."""
    crlf = text.count("\r\n")
    return "\r\n" if crlf and crlf * 2 >= text.count("\n") else "\n"


def with_newlines(text: str, newline: str) -> str:
    return text if newline == "\n" else text.replace("\n", newline)


# ============================================================
# Intelligent Multi-Tier Matchers & Unicode Normalizers
# ============================================================

@dataclass
class MatchResult:
    start: int
    end: int
    note: str
    fuzzy: bool
    line_range: tuple[int, int] | None = None


def _get_leading_indent(s: str) -> str:
    return s[: len(s) - len(s.lstrip(" \t"))]


def _normalize_jsx_line(line: str) -> str:
    """Normalize quotes, collapse repeated spaces, and format self-closing tags."""
    s = line.strip().replace('"', "'")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s*/>", " />", s)
    return s


def _strip_symbols_and_emojis(line: str) -> str:
    """
    Strips all emojis, bullets (•), arrows (➜, →), non-breaking spaces (\xa0),
    and non-ASCII symbols that clipboard pipelines or LLM prompts omit or turn to spaces.
    Preserves alphanumeric characters and standard ASCII syntax.
    """
    # Replace any character that isn't standard ASCII printable with a space
    s = re.sub(r"[^\x20-\x7E\t]", " ", line)
    s = _normalize_jsx_line(s)
    return " ".join(s.split())


def _match_line_spans(doc_lines: list[str], want: list[str], mode: str) -> list[tuple[int, int, int, int]]:
    """
    Find matching line spans across various normalization modes.
    Returns list of (start_char_offset, end_char_offset, start_line_idx, end_line_idx).
    """
    if not want:
        return []

    offsets = []
    pos = 0
    for line in doc_lines:
        offsets.append(pos)
        pos += len(line) + 1

    size = len(want)
    spans = []

    for i in range(len(doc_lines) - size + 1):
        matched = False
        if mode == "trailing":
            matched = all(doc_lines[i + j].rstrip("\r ") == want[j] for j in range(size))
        elif mode == "indent":
            matched = all(doc_lines[i + j].strip() == want[j] for j in range(size))
        elif mode == "whitespace":
            matched = all(re.sub(r"[ \t]+", " ", doc_lines[i + j].strip()) == want[j] for j in range(size))
        elif mode == "jsx":
            matched = all(_normalize_jsx_line(doc_lines[i + j]) == want[j] for j in range(size))
        elif mode == "symbols":
            matched = all(_strip_symbols_and_emojis(doc_lines[i + j]) == want[j] for j in range(size))

        if matched:
            last = i + size - 1
            tail = doc_lines[last]
            if tail.endswith("\r"):
                tail = tail[:-1]
            spans.append((offsets[i], offsets[last] + len(tail), i, last))

    return spans


def _find_high_similarity_match(doc: str, needle: str, threshold: float = 0.88) -> tuple[int, int, int, int, float] | None:
    """
    Locates a uniquely matching line block with >= 88% structural similarity,
    ignoring emojis, symbols, and minor attribute drifts.
    Strictly refuses to match if multiple close candidates exist.
    """
    doc_lines = doc.split("\n")
    needle_lines = [ln for ln in needle.split("\n")]

    while needle_lines and not needle_lines[0].strip():
        needle_lines.pop(0)
    while needle_lines and not needle_lines[-1].strip():
        needle_lines.pop()

    if not needle_lines:
        return None

    k = len(needle_lines)
    needle_norm = "\n".join(_strip_symbols_and_emojis(ln) for ln in needle_lines)

    offsets = []
    pos = 0
    for line in doc_lines:
        offsets.append(pos)
        pos += len(line) + 1

    candidates = []
    window_sizes = {max(1, k - 2), max(1, k - 1), k, k + 1, k + 2}

    for w in window_sizes:
        for i in range(len(doc_lines) - w + 1):
            cand_lines = doc_lines[i : i + w]
            cand_norm = "\n".join(_strip_symbols_and_emojis(ln) for ln in cand_lines)
            matcher = difflib.SequenceMatcher(None, needle_norm, cand_norm)
            if matcher.quick_ratio() >= threshold - 0.05:
                r = matcher.ratio()
                if r >= threshold:
                    candidates.append((r, i, i + w - 1))

    if not candidates:
        return None

    # Sort descending by similarity
    candidates.sort(key=lambda x: x[0], reverse=True)
    best_r, best_start, best_end = candidates[0]

    # Verify uniqueness: ensure no competing candidate is within 5% of best score
    best_range = set(range(best_start, best_end + 1))
    for r, s, e in candidates[1:]:
        cand_range = set(range(s, e + 1))
        if not cand_range.intersection(best_range) and r >= threshold - 0.05:
            return None  # Ambiguous match; unsafe to proceed

    start_offset = offsets[best_start]
    tail = doc_lines[best_end]
    if tail.endswith("\r"):
        tail = tail[:-1]
    end_offset = offsets[best_end] + len(tail)
    return (start_offset, end_offset, best_start, best_end, best_r)


def _find_closest_match(doc: str, needle: str) -> str:
    """Scan file with a sliding window to generate a diagnostic diff for the closest candidate."""
    doc_lines = doc.split("\n")
    needle_lines = [ln for ln in needle.split("\n")]

    while needle_lines and not needle_lines[0].strip():
        needle_lines.pop(0)
    while needle_lines and not needle_lines[-1].strip():
        needle_lines.pop()

    if not needle_lines:
        return ""

    k = len(needle_lines)
    needle_norm = "\n".join(_strip_symbols_and_emojis(ln) for ln in needle_lines)

    best_ratio = 0.0
    best_start = -1
    best_end = -1
    best_candidate_lines = []

    window_sizes = {max(1, k - 1), k, k + 1, k + 2}
    for w in window_sizes:
        for i in range(len(doc_lines) - w + 1):
            candidate = doc_lines[i : i + w]
            candidate_norm = "\n".join(_strip_symbols_and_emojis(ln) for ln in candidate)
            matcher = difflib.SequenceMatcher(None, needle_norm, candidate_norm)
            ratio = matcher.quick_ratio()
            if ratio > 0.35:
                ratio = matcher.ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_start = i
                    best_end = i + w - 1
                    best_candidate_lines = candidate

    if best_ratio >= 0.40 and best_start >= 0:
        start_line = best_start + 1
        end_line = best_end + 1
        pct = int(best_ratio * 100)

        diff = list(
            difflib.unified_diff(
                needle_lines,
                best_candidate_lines,
                fromfile="Expected (SEARCH)",
                tofile=f"File candidate (lines {start_line}-{end_line})",
                lineterm="",
            )
        )
        diff_text = "\n".join(diff[:16])
        if len(diff) > 16:
            diff_text += f"\n... ({len(diff) - 16} more diff lines)"

        return (
            f"\n\nClosest candidate found at lines {start_line}-{end_line} ({pct}% similarity):\n"
            f"------------------------------------------------------------\n"
            f"{diff_text}\n"
            f"------------------------------------------------------------"
        )
    return ""


def find_unique(doc: str, needle: str, what: str, target: str) -> MatchResult:
    """
    Multi-tier intelligent search with strict uniqueness:
      1. Exact byte-for-byte substring.
      2. Trailing whitespace / CRLF tolerant line match.
      3. Indentation-insensitive line match.
      4. JSX / Quote & Whitespace tolerant token match.
      5. Symbol / Emoji / Unicode tolerant match (ignores stripped emojis, bullets, arrows).
      6. High-similarity auto-match (ignores minor attribute & whitespace drift).
    Fails with diagnostic comparison if 0 matches are found, or if any tier is ambiguous.
    """
    if needle == "":
        raise OpError(f"{what} block is empty")

    err_prefix = "SEARCH" if what == "SEARCH" else what

    # ---- Tier 1: Exact match ----
    count = doc.count(needle)
    if count == 1:
        start = doc.index(needle)
        return MatchResult(start, start + len(needle), "", False, None)
    if count > 1:
        raise OpError(f"ERR|{err_prefix}_AMBIGUOUS|{target}|{count}")

    doc_lines = doc.split("\n")

    want_raw = needle.split("\n")
    while want_raw and not want_raw[0].strip():
        want_raw.pop(0)
    while want_raw and not want_raw[-1].strip():
        want_raw.pop()

    if not want_raw:
        raise OpError(f"{what} block is empty")

    # ---- Tier 2: Trailing whitespace tolerant ----
    want_trailing = [ln.rstrip("\r ") for ln in want_raw]
    spans = _match_line_spans(doc_lines, want_trailing, mode="trailing")
    if len(spans) == 1:
        start, end, i, last = spans[0]
        return MatchResult(start, end, " (matched ignoring trailing whitespace)", True, (i, last))
    if len(spans) > 1:
        raise OpError(f"ERR|{err_prefix}_AMBIGUOUS|{target}|{len(spans)}")

    # ---- Tier 3: Indentation tolerant ----
    want_indent = [ln.strip() for ln in want_raw]
    spans = _match_line_spans(doc_lines, want_indent, mode="indent")
    if len(spans) == 1:
        start, end, i, last = spans[0]
        return MatchResult(start, end, " (matched with indentation tolerance)", True, (i, last))
    if len(spans) > 1:
        raise OpError(f"ERR|{err_prefix}_AMBIGUOUS|{target}|{len(spans)}")

    # ---- Tier 4: Harmless whitespace normalization (spaces collapsed) ----
    want_ws = [re.sub(r"[ \t]+", " ", ln.strip()) for ln in want_raw]
    spans = _match_line_spans(doc_lines, want_ws, mode="whitespace")
    if len(spans) == 1:
        start, end, i, last = spans[0]
        return MatchResult(start, end, " (matched with whitespace normalization)", True, (i, last))
    if len(spans) > 1:
        raise OpError(f"ERR|{err_prefix}_AMBIGUOUS|{target}|{len(spans)}")

    # ---- Tier 5: JSX-aware line match ----
    want_jsx = [_normalize_jsx_line(ln) for ln in want_raw]
    spans = _match_line_spans(doc_lines, want_jsx, mode="jsx")
    if len(spans) == 1:
        start, end, i, last = spans[0]
        return MatchResult(start, end, " (matched with JSX normalization)", True, (i, last))
    if len(spans) > 1:
        raise OpError(f"ERR|{err_prefix}_AMBIGUOUS|{target}|{len(spans)}")

    # ---- Diagnostic failure ----
    diagnostic = _find_closest_match(doc, needle)
    first = next((ln.strip() for ln in needle.split("\n") if ln.strip()), "")
    preview = first if len(first) <= 60 else first[:57] + "..."
    raise OpError(f"ERR|{err_prefix}_NOT_FOUND|{target} (first line: {preview!r}){diagnostic}")


def _adjust_indentation(doc: str, line_range: tuple[int, int], search_text: str, replace_text: str, newline: str) -> str:
    """Preserves destination file indentation when replacement block was written with shifted indentation."""
    doc_lines = doc.split("\n")
    first_doc_line = doc_lines[line_range[0]]
    doc_indent = _get_leading_indent(first_doc_line)

    search_lines = [ln for ln in search_text.split("\n") if ln.strip()]
    replace_lines = replace_text.split(newline)

    if not search_lines or not [ln for ln in replace_lines if ln.strip()]:
        return replace_text

    search_indent = _get_leading_indent(search_lines[0])
    first_replace_line = next((ln for ln in replace_lines if ln.strip()), "")
    replace_indent = _get_leading_indent(first_replace_line)

    if search_indent == replace_indent and search_indent != doc_indent:
        adjusted = []
        for ln in replace_lines:
            if not ln.strip():
                adjusted.append(ln)
            elif ln.startswith(search_indent):
                adjusted.append(doc_indent + ln[len(search_indent) :])
            else:
                adjusted.append(doc_indent + ln.lstrip(" \t"))
        return newline.join(adjusted)

    return replace_text


# ============================================================
# Clipboard / input
# ============================================================

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


# ============================================================
# Operation
# ============================================================

@dataclass
class Operation:
    command: str
    args: tuple
    data: str | None = None
    extra: str | None = None


# ============================================================
# Parser
# ============================================================

def extract_plan(text: str) -> str:
    """
    Extract the executable code_exec plan block from an AI response.
    Ignores conversational explanations, Markdown prose, and unrelated code blocks.

    Supported formats:
      1. ```code_exec ... ``` (or ````code_exec ... ```` or ~~~code_exec)
      2. CODE_EXEC_PLAN ... END_CODE_EXEC_PLAN
      3. Legacy response starting with THINK
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

                # Track <<< / >>> blocks so nested ``` in content blocks don't close fence
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

    # Strip single outer fence if the entire response is wrapped in ```text ... ```
    m_wrap = re.match(r"^(`{3,}|~{3,})(?:[a-zA-Z0-9_-]*)\n(.*)\n\1$", trimmed, re.DOTALL)
    if m_wrap:
        candidate = m_wrap.group(2).strip()

    cand_lines = [ln for ln in candidate.split("\n") if ln.strip()]
    cand_idx = 0
    while cand_idx < len(cand_lines) and cand_lines[cand_idx].strip().startswith("#"):
        cand_idx += 1

    if cand_idx < len(cand_lines) and cand_lines[cand_idx].strip() == "THINK":
        has_end_think = any(ln.strip() == "END_THINK" for ln in cand_lines)
        has_command = any(
            re.match(r"^([A-Z_]+)(?:\s+.*)?$", ln.strip()) and ln.strip().split()[0] in COMMANDS
            for ln in cand_lines
        )
        if has_end_think and has_command:
            return candidate

    raise OpError("ERR|PLAN_NOT_FOUND")


COMMANDS = {
    "CREATE", "EDIT", "DELETE", "MOVE", "COPY", "RENAME", "MKDIR",
    "INSERT_BEFORE", "INSERT_AFTER", "APPEND", "PREPEND", "RUN",
    "COMMIT",
}


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
            raise ValueError(f"Missing >>> for block opened with <<<")
        return "\n".join(lines[start:k]), k + 1

    while j < len(lines) and not lines[j].strip():
        j += 1

    if j < len(lines) and lines[j].strip().startswith("<<<"):
        rem = lines[j].strip()[3:].strip()
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

    path = clean_path(rest)

    if command in {"DELETE", "MKDIR"}:
        return Operation(command, (path,)), i

    inline_block = False
    if rest.endswith("<<<"):
        rest = rest[:-3].strip()
        inline_block = True

    path = clean_path(rest)

    if command in {"CREATE", "APPEND", "PREPEND"}:
        content, i = read_block(lines, i, inline_started=inline_block)
        return Operation(command, (path,), content), i

    if command == "EDIT":
        i, search_inline = expect_keyword(lines, i, "SEARCH", command)
        search, i = read_block(lines, i, inline_started=search_inline)
        i, replace_inline = expect_keyword(lines, i, "REPLACE", command)
        replace, i = read_block(lines, i, inline_started=replace_inline)
        return Operation("EDIT", (path,), search, replace), i

    # INSERT_BEFORE / INSERT_AFTER
    i, marker_inline = expect_keyword(lines, i, "MARKER", command)
    marker, i = read_block(lines, i, inline_started=marker_inline)
    i, content_inline = expect_keyword(lines, i, "CONTENT", command)
    content, i = read_block(lines, i, inline_started=content_inline)
    return Operation(command, (path,), marker, content), i


def parse_operations(text: str) -> list[Operation]:
    # Replace non-breaking spaces (\xa0) with regular spaces across the entire input
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


# ============================================================
# Filesystems
# ============================================================

def _remove(path: Path) -> None:
    if not os.path.lexists(path):
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


class VirtualFS:
    """In-memory overlay on top of the real tree (never writes to disk)."""

    def __init__(self):
        self.state: dict[Path, tuple] = {}

    def _lookup(self, path: Path):
        if path in self.state:
            return self.state[path]
        for parent in path.parents:
            if parent in self.state:
                return ("gone",)
        return None

    def lexists(self, path):
        entry = self._lookup(path)
        return os.path.lexists(path) if entry is None else entry[0] != "gone"

    def is_file(self, path):
        entry = self._lookup(path)
        return path.is_file() if entry is None else entry[0] in {"file", "bin"}

    def is_dir(self, path):
        entry = self._lookup(path)
        return path.is_dir() if entry is None else entry[0] == "dir"

    def read(self, path):
        entry = self._lookup(path)
        if entry is None:
            return read_text(path)
        if entry[0] == "file":
            return entry[1]
        raise OpError(f"{rel(path)} is not valid UTF-8 text (binary file?)")

    def _drop_children(self, path):
        for key in [k for k in self.state if path in k.parents]:
            del self.state[key]

    def _mkparents(self, directory):
        current = ROOT
        for part in directory.relative_to(ROOT).parts:
            current = current / part
            if self.lexists(current):
                if not self.is_dir(current):
                    raise OpError(f"{rel(current)} exists but is not a directory")
            else:
                self.state[current] = ("dir",)

    def write(self, path, text):
        self._mkparents(path.parent)
        self.state[path] = ("file", text)

    def mkdir(self, path):
        self._mkparents(path)

    def delete(self, path):
        self._drop_children(path)
        self.state[path] = ("gone",)

    def copy(self, src, dst):
        if self.is_dir(src):
            raise Unverifiable("a directory move/copy")
        try:
            entry = ("file", self.read(src))
        except OpError:
            entry = ("bin",)
        self._mkparents(dst.parent)
        self._drop_children(dst)
        self.state[dst] = entry

    def move(self, src, dst):
        self.copy(src, dst)
        self.delete(src)


class RealFS:
    """Applies operations for real and records how to undo each one."""

    def __init__(self, timeout: int):
        self.timeout = timeout
        self.journal: list[tuple] = []
        self.backup_dir: Path | None = None
        self.ran_commands: list[str] = []
        self._counter = 0

    def lexists(self, path):
        return os.path.lexists(path)

    def is_file(self, path):
        return path.is_file()

    def is_dir(self, path):
        return path.is_dir()

    def read(self, path):
        return read_text(path)

    def _slot(self) -> Path:
        if self.backup_dir is None:
            self.backup_dir = Path(tempfile.mkdtemp(prefix="code_exec_backup_"))
        self._counter += 1
        return self.backup_dir / str(self._counter)

    def _mkparents(self, directory: Path):
        missing = []
        current = directory
        while not os.path.lexists(current):
            missing.append(current)
            current = current.parent
        if not current.is_dir():
            raise OpError(f"{rel(current)} exists but is not a directory")
        for folder in reversed(missing):
            try:
                folder.mkdir()
            except OSError as exc:
                raise OpError(
                    f"Cannot create directory {rel(folder)}: {exc.strerror or exc}"
                ) from None
            self.journal.append(("rmdir", folder))

    def write(self, path, text):
        self._mkparents(path.parent)
        mode = None

        if os.path.lexists(path):
            slot = self._slot()
            try:
                shutil.copy2(path, slot)
            except OSError as exc:
                raise OpError(f"Cannot back up {rel(path)}: {exc.strerror or exc}") from None
            mode = stat.S_IMODE(path.stat().st_mode)
            self.journal.append(("restore", path, slot))
        else:
            self.journal.append(("remove", path))

        try:
            atomic_write(path, text, mode)
        except OSError as exc:
            raise OpError(f"Cannot write {rel(path)}: {exc.strerror or exc}") from None

    def mkdir(self, path):
        self._mkparents(path)

    def delete(self, path):
        slot = self._slot()
        try:
            shutil.move(str(path), str(slot))
        except (OSError, shutil.Error) as exc:
            raise OpError(f"Cannot delete {rel(path)}: {exc}") from None
        self.journal.append(("restore", path, slot))

    def move(self, src, dst):
        self._mkparents(dst.parent)
        try:
            shutil.move(str(src), str(dst))
        except (OSError, shutil.Error) as exc:
            raise OpError(f"Cannot move {rel(src)}: {exc}") from None
        self.journal.append(("move_back", dst, src))

    def copy(self, src, dst):
        self._mkparents(dst.parent)
        self.journal.append(("remove", dst))
        try:
            if src.is_dir():
                shutil.copytree(src, dst, symlinks=True)
            else:
                shutil.copy2(src, dst)
        except (OSError, shutil.Error) as exc:
            raise OpError(f"Cannot copy {rel(src)}: {exc}") from None

    def run(self, command: str):
        needs_prompt = validate_run_command(command)
        ui.command(command)

        if needs_prompt:
            c = ui.palette
            print(c.paint(f"\n  ⚠️  SECURITY WARNING: Unvetted / Generic Execution Requested", c.AMBER))
            ans = input(c.paint(f"  ❯ Allow '{command}' to execute? [y/N]: ", c.CORAL, bold=True)).strip().lower()
            if ans not in {"y", "yes"}:
                raise CommandFailed(f"User denied execution of: {command}")

        actual_cmd, extra_env = build_sandboxed_command(command, needs_prompt)

        self.ran_commands.append(actual_cmd)
        limit = self.timeout or None
        run_env = os.environ.copy()
        if extra_env:
            run_env.update(extra_env)

        try:
            result = subprocess.run(actual_cmd, shell=True, cwd=ROOT, timeout=limit, env=run_env)
        except subprocess.TimeoutExpired:
            raise CommandFailed(
                f"Command timed out after {self.timeout}s: {actual_cmd}"
            ) from None
        if result.returncode != 0:
            raise CommandFailed(
                f"Command failed (exit {result.returncode}): {actual_cmd}"
            )


    def rollback(self) -> list[str]:
        errors = []
        for entry in reversed(self.journal):
            kind, path = entry[0], entry[1]
            try:
                if kind == "rmdir":
                    try:
                        path.rmdir()
                    except OSError:
                        pass
                elif kind == "remove":
                    _remove(path)
                elif kind == "restore":
                    _remove(path)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(entry[2]), str(path))
                elif kind == "move_back":
                    entry[2].parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(path), str(entry[2]))
            except Exception as exc:
                errors.append(f"{kind} {rel(path)}: {exc}")
        self.journal.clear()
        return errors

    def cleanup(self):
        if self.backup_dir is not None:
            shutil.rmtree(self.backup_dir, ignore_errors=True)
            self.backup_dir = None


def build_sandboxed_command(command: str, needs_prompt: bool) -> tuple[str, dict[str, str] | None]:
    """Wraps unvetted generic commands in OS-level sandboxing (dropping network where supported)."""
    if not needs_prompt:
        return command, None

    extra_env = None
    actual_cmd = command

    if sys.platform.startswith("linux"):
        actual_cmd = f"unshare -r -n {command}"
    elif sys.platform == "darwin":
        actual_cmd = f"sandbox-exec -p '(version 1) (allow default) (deny network-outbound)' {command}"
    elif sys.platform == "win32":
        extra_env = {
            "HTTP_PROXY": "http://127.0.0.1:0",
            "HTTPS_PROXY": "http://127.0.0.1:0",
            "ALL_PROXY": "http://127.0.0.1:0",
            "NO_PROXY": "",
        }
        escaped_cmd = command.replace('"', '""')
        actual_cmd = f'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Restricted -Command "{escaped_cmd}"'

    return actual_cmd, extra_env


def build_sandboxed_command(command: str, needs_prompt: bool) -> tuple[str, dict[str, str] | None]:
    """Wraps unvetted generic commands in OS-level sandboxing (dropping network where supported)."""
    if not needs_prompt:
        return command, None

    extra_env = None
    actual_cmd = command

    if sys.platform.startswith("linux"):
        actual_cmd = f"unshare -r -n {command}"
    elif sys.platform == "darwin":
        actual_cmd = f"sandbox-exec -p '(version 1) (allow default) (deny network-outbound)' {command}"
    elif sys.platform == "win32":
        extra_env = {
            "HTTP_PROXY": "http://127.0.0.1:0",
            "HTTPS_PROXY": "http://127.0.0.1:0",
            "ALL_PROXY": "http://127.0.0.1:0",
            "NO_PROXY": "",
        }
        escaped_cmd = command.replace('"', '""')
        actual_cmd = f'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Restricted -Command "{escaped_cmd}"'

    return actual_cmd, extra_env


def validate_run_command(cmd: str) -> bool:
    """Returns True if the command requires mandatory interactive confirmation, False otherwise."""
    trimmed = cmd.strip()
    if not trimmed:
        raise OpError("ERR|FORBIDDEN_COMMAND|empty command")

    cmd_lower = trimmed.lower()

    # Block destructive/network substrings
    for forbidden in FORBIDDEN_RUN_SUBSTRINGS:
        if forbidden in cmd_lower:
            raise OpError(f"ERR|FORBIDDEN_COMMAND|{trimmed} - contains forbidden pattern '{forbidden}'")

    # Block inline Python/Bash scripts and interactive REPLs
    if re.search(r"\b(python[0-9.]*|node|bash|sh|perl|ruby)\s+(-[a-zA-Z]*c|--command|-i|-e)\b", cmd_lower):
        raise OpError(f"ERR|FORBIDDEN_COMMAND|{trimmed} - inline execution or interactive shell flags are forbidden")

    if any(cmd_lower.startswith(prefix) for prefix in ALLOWED_RUN_COMMAND_PREFIXES):
        return False
        
    if any(cmd_lower.startswith(prefix) for prefix in INTERACTIVE_ONLY_PREFIXES):
        return True

    allowed_list = ", ".join(f"'{p.strip()}'" for p in ALLOWED_RUN_COMMAND_PREFIXES[:5]) + ", ..."
    raise OpError(f"ERR|FORBIDDEN_COMMAND|{trimmed} - command not in whitelist (allowed: {allowed_list})")

    def cleanup(self):
        if self.backup_dir is not None:
            shutil.rmtree(self.backup_dir, ignore_errors=True)
            self.backup_dir = None

    def cleanup(self):
        if self.backup_dir is not None:
            shutil.rmtree(self.backup_dir, ignore_errors=True)
            self.backup_dir = None


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

            if command == "INSERT_BEFORE":
                new = doc[: match.start] + content + newline + doc[match.start :]
                done = "Inserted before marker in"
            else:
                new = doc[: match.end] + newline + content + doc[match.end :]
                done = "Inserted after marker in"
            note = match.note

        elif command == "APPEND":
            base = doc if (not doc or doc.endswith("\n")) else doc + newline
            new = base + with_newlines(op.data, newline) + newline
            note = ""
            done = "Appended to"

        else:
            bom = "\ufeff" if doc.startswith("\ufeff") else ""
            new = bom + with_newlines(op.data, newline) + newline + doc[len(bom) :]
            note = ""
            done = "Prepended to"

        fs.write(path, new)
        return f"{done} {args[0]}{note}"

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
                if op.command in {"EDIT", "APPEND", "PREPEND", "INSERT_BEFORE", "INSERT_AFTER"}:
                    if "DELETE" in history:
                        raise OpError(f"Operation {number} ({describe_operation(op)}): ERR|CONFLICTING_OPERATIONS|{path_arg} - attempting to EDIT a file that was DELETED in the same plan")
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

def fail(message: str) -> int:
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


def perform_git_commit(message: str, paths: list[str]) -> tuple[bool, str]:
    if not paths:
        return False, "No modified files to commit."
    try:
        # Stage only the specific files affected by the plan
        subprocess.run(["git", "add", "--", *paths], cwd=ROOT, check=True)
        
        # Check if any staged changes exist for these paths
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


def apply_plan(operations: list[Operation], timeout: int, no_commit: bool = False, auto_commit: bool = False) -> int:
    fs = RealFS(timeout)

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
                # Collect modified paths for scoped git commit
                if op.command in {"CREATE", "EDIT", "DELETE", "APPEND", "PREPEND", "INSERT_BEFORE", "INSERT_AFTER"}:
                    modified_paths.append(op.args[0])
                elif op.command in {"MOVE", "COPY", "RENAME"}:
                    modified_paths.extend([op.args[0], op.args[1]])

    except CommandFailed as exc:
        ui.command_failed(str(exc), fs.backup_dir)
        return 1

    except (Exception, KeyboardInterrupt) as exc:
        ui.apply_interrupted(exc)
        count = len(fs.journal)
        errors = fs.rollback()
        ui.rollback_report(errors, count, fs.backup_dir, fs.ran_commands)
        if not errors:
            fs.cleanup()
        return 1

    fs.cleanup()
    has_git = (ROOT / ".git").exists()
    ui.done(has_git=has_git)

    if has_git and commit_msg and not no_commit:
        should_commit = auto_commit or ui.prompt_commit(commit_msg)
        if should_commit:
            # Deduplicate paths while preserving order
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
                        help="Direct action: 'apply', 'run', or 'theme'")
    parser.add_argument("subarg", nargs="?", default=None,
                        help="Sub-argument for actions (e.g., theme name)")
    parser.add_argument("-h", "--help", action="store_true",
                        help="Show interactive usage guide and options")
    parser.add_argument("-p", "--prompt", "--copy-instructions", dest="prompt", action="store_true",
                        help="Copy code_exec_instructions.md to the clipboard for your AI prompt")
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

    # Handle actions or numeric menu shortcuts from CLI
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
    elif args.action == "5":
        themes = list(ui.palette.themes.keys())
        print(f"\n  🎨 Current theme: {ui.palette.current_theme}")
        print(f"  Available themes: {', '.join(themes)}")
        print(f"  Usage: code-exec theme <name>\n")
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
            print(f"  Usage: code-exec theme <name>\n")
            return 0
    elif args.action and not args.file and not Path(args.action).exists():
        return fail(f"Unknown command or file: '{args.action}'. Run 'code-exec' without arguments for the menu.")

    # Launch interactive menu if run without arguments in an interactive terminal
    is_interactive = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
    has_flags = any([args.help, args.prompt, args.file, args.dry_run, args.yes, args.no_run, args.no_commit])
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
        else:
            ui.error(f"Invalid option: {choice}")
            return 1

    if args.help:
        ui.show_guide()
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
    except ValueError as exc:
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

    if args.dry_run:
        ui.dry_run()
        return 0

    if not args.yes:
        if not ui.confirm():
            ui.cancelled()
            return 0

    return apply_plan(operations, args.timeout, no_commit=args.no_commit, auto_commit=args.yes)


if __name__ == "__main__":
    sys.exit(main())