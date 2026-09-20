from __future__ import annotations

import difflib
import re
from pathlib import Path

from code_exec_types import MatchResult, OpError
from code_exec_ui import ui


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
    s = re.sub(r"[^\x20-\x7E\t]", " ", line)
    s = _normalize_jsx_line(s)
    return " ".join(s.split())


def _match_line_spans(
    doc_lines: list[str], want: list[str], mode: str, capture_newline: bool = False
) -> list[tuple[int, int, int, int]]:
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
            if capture_newline and last + 1 < len(offsets):
                end_pos = offsets[last + 1]
            else:
                tail = doc_lines[last]
                if tail.endswith("\r"):
                    tail = tail[:-1]
                end_pos = offsets[last] + len(tail)
            spans.append((offsets[i], end_pos, i, last))
    return spans


def _find_high_similarity_match(
    doc: str, needle: str, threshold: float = 0.88, capture_newline: bool = False
) -> tuple[int, int, int, int, float] | None:
    """
    Locates a uniquely matching line block with >= threshold structural similarity,
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

    candidates.sort(key=lambda x: x[0], reverse=True)
    best_r, best_start, best_end = candidates[0]

    best_range = set(range(best_start, best_end + 1))
    for r, s, e in candidates[1:]:
        cand_range = set(range(s, e + 1))
        if not cand_range.intersection(best_range) and r >= threshold - 0.05:
            return None

    start_offset = offsets[best_start]
    if capture_newline and best_end + 1 < len(offsets):
        end_offset = offsets[best_end + 1]
    else:
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


MAX_SEARCH_LINES = 18
MAX_SEARCH_CHARS = 800


def find_unique(doc: str, needle: str, what: str, target: str) -> MatchResult:
    """
    Multi-tier intelligent search with strict uniqueness:
      1. Exact byte-for-byte substring.
      2. Trailing whitespace / CRLF tolerant line match.
      3. Indentation-insensitive line match.
      4. JSX / Quote & Whitespace tolerant token match.
      5. Symbol / Emoji / Unicode tolerant match (ignores stripped emojis, bullets, arrows).
      6. High-similarity fuzzy auto-match (>= 90% similarity).
    Fails with diagnostic comparison if 0 matches are found, or if any tier is ambiguous.
    """
    if needle == "":
        raise OpError(f"{what} block is empty")

    err_prefix = "SEARCH" if what == "SEARCH" else what

    # Guard against oversized search blocks in large files
    needle_lines_count = len(needle.split("\n"))
    needle_char_count = len(needle)
    if needle_lines_count > MAX_SEARCH_LINES or needle_char_count > MAX_SEARCH_CHARS:
        raise OpError(
            f"ERR|SEARCH_TOO_BIG|{target}|({needle_lines_count} lines, {needle_char_count} chars) - "
            f"SEARCH block too large. Use a concise 3-6 line unique anchor instead of whole blocks or components."
        )

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

    capture_nl = needle.endswith("\n")

    # ---- Tier 2: Trailing whitespace tolerant ----
    want_trailing = [ln.rstrip("\r ") for ln in want_raw]
    spans = _match_line_spans(doc_lines, want_trailing, mode="trailing", capture_newline=capture_nl)
    if len(spans) == 1:
        start, end, i, last = spans[0]
        return MatchResult(start, end, " (matched ignoring trailing whitespace)", True, (i, last))
    if len(spans) > 1:
        raise OpError(f"ERR|{err_prefix}_AMBIGUOUS|{target}|{len(spans)}")

    # ---- Tier 3: Indentation tolerant ----
    want_indent = [ln.strip() for ln in want_raw]
    spans = _match_line_spans(doc_lines, want_indent, mode="indent", capture_newline=capture_nl)
    if len(spans) == 1:
        start, end, i, last = spans[0]
        return MatchResult(start, end, " (matched with indentation tolerance)", True, (i, last))
    if len(spans) > 1:
        raise OpError(f"ERR|{err_prefix}_AMBIGUOUS|{target}|{len(spans)}")

    # ---- Tier 4: Harmless whitespace normalization (spaces collapsed) ----
    want_ws = [re.sub(r"[ \t]+", " ", ln.strip()) for ln in want_raw]
    spans = _match_line_spans(doc_lines, want_ws, mode="whitespace", capture_newline=capture_nl)
    if len(spans) == 1:
        start, end, i, last = spans[0]
        return MatchResult(start, end, " (matched with whitespace normalization)", True, (i, last))
    if len(spans) > 1:
        raise OpError(f"ERR|{err_prefix}_AMBIGUOUS|{target}|{len(spans)}")

    # ---- Tier 5: JSX-aware line match ----
    want_jsx = [_normalize_jsx_line(ln) for ln in want_raw]
    spans = _match_line_spans(doc_lines, want_jsx, mode="jsx", capture_newline=capture_nl)
    if len(spans) == 1:
        start, end, i, last = spans[0]
        return MatchResult(start, end, " (matched with JSX normalization)", True, (i, last))
    if len(spans) > 1:
        raise OpError(f"ERR|{err_prefix}_AMBIGUOUS|{target}|{len(spans)}")

    # ---- Tier 6: High-similarity fuzzy match (>= 90%) ----
    fuzzy = _find_high_similarity_match(doc, needle, threshold=0.90, capture_newline=capture_nl)
    if fuzzy is not None:
        start, end, s_line, e_line, sim = fuzzy
        pct = int(sim * 100)
        ui.warn(f"Fuzzy match ({pct}% similarity) accepted for {target} at lines {s_line + 1}-{e_line + 1}")
        return MatchResult(start, end, f" (fuzzy matched with {pct}% similarity)", True, (s_line, e_line))

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
