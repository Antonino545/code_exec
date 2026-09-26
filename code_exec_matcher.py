from __future__ import annotations

import difflib
import io
import re
import tokenize
from dataclasses import dataclass
from pathlib import Path

from typing import Callable

from code_exec_types import FuzzyCandidate, MatchResult, OpError
from code_exec_ui import ui

# Set to True via --verbose to trace which matching tier was used for each SEARCH block.
VERBOSE: bool = False

# Dual-Threshold Activation Zones
CONFIDENT_THRESHOLD: float = 0.90
RESOLVER_THRESHOLD: float = 0.72

# Fuzzy policy: "prompt" (interactive TTY default), "strict" (fail if < 0.90), "auto" (accept >= 0.72)
FUZZY_POLICY: str = "prompt"

# Optional interactive resolver callback hooked by the UI/CLI
# Signature: (target, needle, candidates) -> MatchResult | None
FUZZY_RESOLVER: Callable[[str, str, list[FuzzyCandidate]], MatchResult | None] | None = None


def _verbose_note(msg: str) -> None:
    if VERBOSE:
        ui.warn(f"[matcher] {msg}")



@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: list[tuple[str, str]]


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


def _normalize_code_line_for_fuzzy(line: str) -> str:
    """Normalize a code line for semantic comparison: strips comments, normalizes quotes/commas/semicolons."""
    s = re.sub(r"#.*$", "", line)
    s = re.sub(r"//.*$", "", s)
    s = s.replace('"', "'").replace("`", "'")
    s = re.sub(r",\s*([\]\}\)])", r"\1", s)
    s = re.sub(r",\s*$", "", s)
    s = re.sub(r";\s*$", "", s)
    s = re.sub(r"\\vert\{\}\s*\\vert\{\}", "||", s)
    s = re.sub(r"\\vert\{\}", "|", s)
    s = re.sub(r"&amp;\s*&amp;", "&&", s)
    s = re.sub(r"\\&\s*\\&", "&&", s)
    s = _strip_symbols_and_emojis(s)
    return s.strip()


def _semantic_similarity(needle_str: str, cand_str: str) -> float:
    """
    Computes a semantic-aware similarity score between 0.0 and 1.0.
    Heavily penalizes identifier and keyword differences, while being tolerant
    to formatting, quotes, comments, trailing commas, and whitespace.
    """
    needle_lines = [_normalize_code_line_for_fuzzy(ln) for ln in needle_str.split("\n")]
    cand_lines = [_normalize_code_line_for_fuzzy(ln) for ln in cand_str.split("\n")]
    needle_norm = "\n".join(ln for ln in needle_lines if ln)
    cand_norm = "\n".join(ln for ln in cand_lines if ln)

    if not needle_norm and not cand_norm:
        return 1.0
    if not needle_norm or not cand_norm:
        return 0.0

    if needle_norm == cand_norm:
        return 1.0

    # 1. Character-level SequenceMatcher ratio
    char_matcher = difflib.SequenceMatcher(None, needle_norm, cand_norm)
    char_ratio = char_matcher.ratio()

    # 2. Token-level SequenceMatcher ratio on identifiers, keywords, numbers
    needle_tokens = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b|\b\d+\b", needle_norm)
    cand_tokens = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b|\b\d+\b", cand_norm)

    if needle_tokens and cand_tokens:
        tok_matcher = difflib.SequenceMatcher(None, needle_tokens, cand_tokens)
        tok_ratio = tok_matcher.ratio()

        # If semantic words/identifiers have diverged, heavily penalize
        if tok_ratio < 0.65:
            return min(char_ratio, tok_ratio) * 0.75
        return 0.55 * tok_ratio + 0.45 * char_ratio
    else:
        return char_ratio


def _find_high_similarity_candidates(
    doc: str,
    needle: str,
    min_threshold: float = 0.72,
    capture_newline: bool = False,
) -> list[tuple[float, int, int, int, int, str]]:
    """
    Scans doc using elastic sliding windows and boundary-constrained matching.
    Returns sorted list of (similarity, start_offset, end_offset, s_line, e_line, preview).
    """
    doc_lines = doc.split("\n")
    needle_lines = [ln for ln in needle.split("\n")]

    while needle_lines and not needle_lines[0].strip():
        needle_lines.pop(0)
    while needle_lines and not needle_lines[-1].strip():
        needle_lines.pop()

    if not needle_lines:
        return []

    k = len(needle_lines)
    needle_raw_str = "\n".join(needle_lines)

    # Oversized blocks (> MAX_SEARCH_LINES or > MAX_SEARCH_CHARS) must NOT run full-text
    # sliding-window SequenceMatcher across every line of the file (takes minutes of CPU time).
    if k > MAX_SEARCH_LINES or len(needle_raw_str) > MAX_SEARCH_CHARS:
        return []

    offsets = []
    pos = 0
    for line in doc_lines:
        offsets.append(pos)
        pos += len(line) + 1

    candidate_windows: set[tuple[int, int]] = set()

    # 1. Elastic sliding window sizes (k - 4 to k + 5)
    min_w = max(1, k - 4)
    max_w = min(len(doc_lines), k + 5)
    for w in range(min_w, max_w + 1):
        for i in range(len(doc_lines) - w + 1):
            candidate_windows.add((i, i + w - 1))

    bounded_windows: set[tuple[int, int]] = set()
    # 2. Boundary-constrained window detection (when head & tail match)
    if k >= 3:
        head_text = _normalize_code_line_for_fuzzy(needle_lines[0])
        tail_text = _normalize_code_line_for_fuzzy(needle_lines[-1])
        if head_text and tail_text:
            head_matches = [
                idx for idx, ln in enumerate(doc_lines)
                if _normalize_code_line_for_fuzzy(ln) == head_text
            ]
            tail_matches = [
                idx for idx, ln in enumerate(doc_lines)
                if _normalize_code_line_for_fuzzy(ln) == tail_text
            ]
            for h_idx in head_matches:
                for t_idx in tail_matches:
                    if t_idx >= h_idx and abs((t_idx - h_idx + 1) - k) <= 8:
                        candidate_windows.add((h_idx, t_idx))
                        bounded_windows.add((h_idx, t_idx))

    # Pre-normalize lines once to avoid millions of regex calls in tight comparison loops
    doc_norm_lines = [_normalize_code_line_for_fuzzy(ln) for ln in doc_lines]
    needle_norm_lines = [_normalize_code_line_for_fuzzy(ln) for ln in needle_lines]
    needle_norm_set = set(ln for ln in needle_norm_lines if ln)

    evaluated: list[tuple[float, int, int]] = []
    seen_spans: set[tuple[int, int]] = set()
    for s_line, e_line in candidate_windows:
        # Trim leading and trailing blank lines so candidate spans wrap code tightly
        while s_line < e_line and not doc_lines[s_line].strip():
            s_line += 1
        while e_line > s_line and not doc_lines[e_line].strip():
            e_line -= 1

        if (s_line, e_line) in seen_spans:
            continue
        seen_spans.add((s_line, e_line))

        # Fast set overlap filter for medium/large blocks:
        # Avoid checking windows sharing almost no lines with needle.
        if k >= 10 and needle_norm_set and (s_line, e_line) not in bounded_windows:
            cand_norm_slice = doc_norm_lines[s_line : e_line + 1]
            overlap = len(needle_norm_set.intersection(cand_norm_slice))
            if overlap / len(needle_norm_set) < 0.20:
                continue

        cand_str = "\n".join(doc_lines[s_line : e_line + 1])
        # Fast length pre-filter
        cand_len = len(cand_str)
        needle_len = len(needle_raw_str)
        if 2 * min(cand_len, needle_len) / max(1, cand_len + needle_len) < min_threshold - 0.20:
            continue

        sm = difflib.SequenceMatcher(None, needle_raw_str, cand_str)
        # Fast pre-filtering with quick_ratio
        if sm.quick_ratio() < min_threshold - 0.15:
            continue
        sim = _semantic_similarity(needle_raw_str, cand_str)
        if (s_line, e_line) in bounded_windows:
            sim = min(1.0, sim + 0.06)
        if sim >= min_threshold:
            evaluated.append((sim, s_line, e_line))

    if not evaluated:
        return []

    # Sort descending by similarity, earlier lines in document first
    evaluated.sort(key=lambda x: (-x[0], x[1]))

    # Deduplicate overlapping windows (keep highest similarity for each region)
    deduped: list[tuple[float, int, int]] = []
    occupied_lines: set[int] = set()
    for sim, s, e in evaluated:
        span_range = set(range(s, e + 1))
        overlap = span_range.intersection(occupied_lines)
        if len(overlap) > len(span_range) * 0.4:
            continue
        occupied_lines.update(span_range)
        deduped.append((sim, s, e))

    results: list[tuple[float, int, int, int, int, str]] = []
    for sim, s, e in deduped:
        start_offset = offsets[s]
        if capture_newline and e + 1 < len(offsets):
            end_offset = offsets[e + 1]
        else:
            tail = doc_lines[e]
            if tail.endswith("\r"):
                tail = tail[:-1]
            end_offset = offsets[e] + len(tail)
        preview = doc_lines[s].strip()
        if len(preview) > 60:
            preview = preview[:57] + "..."
        results.append((sim, start_offset, end_offset, s, e, preview))

    return results


def _find_high_similarity_match(
    doc: str, needle: str, threshold: float = 0.88, capture_newline: bool = False
) -> tuple[int, int, int, int, float] | None:
    """
    Locates a uniquely matching line block with >= threshold structural similarity,
    ignoring emojis, symbols, and minor attribute drifts.
    Strictly refuses to match if multiple close candidates exist.
    """
    candidates = _find_high_similarity_candidates(
        doc, needle, min_threshold=threshold, capture_newline=capture_newline
    )
    if not candidates:
        return None

    best_sim, start_offset, end_offset, best_start, best_end, _ = candidates[0]
    best_range = set(range(best_start, best_end + 1))
    for sim, _, _, s, e, _ in candidates[1:]:
        cand_range = set(range(s, e + 1))
        if not cand_range.intersection(best_range) and sim >= threshold - 0.05:
            return None
    return (start_offset, end_offset, best_start, best_end, best_sim)


def _tokenize_python_line(line_str: str) -> list[tuple[int, str]]:
    """Extract semantic tokens from a single Python line, normalizing quotes and trailing commas."""
    stripped = line_str.strip()
    if not stripped or stripped.startswith("#"):
        return []
    try:
        raw_tokens = list(tokenize.tokenize(io.BytesIO(stripped.encode("utf-8")).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        norm = re.sub(r"#.*$", "", stripped)
        norm = re.sub(r",\s*([\]\}\)])", r"\1", norm)
        norm = re.sub(r"^('''|\"\"\"|'|\")|('''|\"\"\"|'|\")$", "", norm)
        return [(tokenize.NAME, norm.strip())]

    tokens: list[tuple[int, str]] = []
    for t in raw_tokens:
        if t.type in (tokenize.ENCODING, tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.ENDMARKER):
            continue
        val = t.string
        if t.type == tokenize.STRING:
            if (val.startswith(("'", '"')) and len(val) >= 2) or (val.startswith(("'''", '"""')) and len(val) >= 6):
                val = re.sub(r"^('''|\"\"\"|'|\")|('''|\"\"\"|'|\")$", "", val)
        tokens.append((t.type, val))

    cleaned: list[tuple[int, str]] = []
    for idx, (ttype, sval) in enumerate(tokens):
        if ttype == tokenize.OP and sval == ",":
            if idx + 1 < len(tokens) and tokens[idx + 1][1] in ("]", "}", ")"):
                continue
        cleaned.append((ttype, sval))
    return cleaned


def _match_python_tokens(doc: str, needle: str) -> list[tuple[int, int, int, int]]:

    want_raw = [ln for ln in needle.split("\n") if ln.strip()]
    if not want_raw:
        return []

    want_toks = [_tokenize_python_line(ln) for ln in want_raw]
    want_toks = [t for t in want_toks if t]
    if not want_toks:
        return []

    doc_lines = doc.split("\n")
    offsets = []
    pos = 0
    for line in doc_lines:
        offsets.append(pos)
        pos += len(line) + 1

    size = len(want_toks)
    spans = []
    for i in range(len(doc_lines) - size + 1):
        window_toks = [_tokenize_python_line(doc_lines[i + j]) for j in range(size)]
        if window_toks == want_toks:
            last = i + size - 1
            tail = doc_lines[last]
            if tail.endswith("\r"):
                tail = tail[:-1]
            end_pos = offsets[last] + len(tail)
            spans.append((offsets[i], end_pos, i, last))

    return spans


def _match_js_tokens(doc: str, needle: str) -> list[tuple[int, int, int, int]]:
    """
    Token-aware lexical normalization for JS/TS code:
    normalizes quotes, ignores optional trailing commas, and collapses operator spacing.
    """
    def js_norm_line(s: str) -> str:
        s = re.sub(r"//.*$", "", s)
        s = re.sub(r"/\*.*?\*/", "", s)
        s = s.replace('"', "'").replace("`", "'")
        s = re.sub(r",\s*([}\]])", r"\1", s)
        s = re.sub(r"\\vert\{\}\s*\\vert\{\}", "||", s)
        s = re.sub(r"\\vert\{\}", "|", s)
        s = re.sub(r"&amp;\s*&amp;", "&&", s)
        s = re.sub(r"\\&\s*\\&", "&&", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    doc_lines = doc.split("\n")
    want_raw = [ln for ln in needle.split("\n") if ln.strip()]
    if not want_raw:
        return []

    want_norm = [js_norm_line(ln) for ln in want_raw if js_norm_line(ln)]
    if not want_norm:
        return []

    offsets = []
    pos = 0
    for line in doc_lines:
        offsets.append(pos)
        pos += len(line) + 1

    size = len(want_norm)
    spans = []
    for i in range(len(doc_lines) - size + 1):
        window_norm = [js_norm_line(doc_lines[i + j]) for j in range(size)]
        if window_norm == want_norm:
            last = i + size - 1
            tail = doc_lines[last]
            if tail.endswith("\r"):
                tail = tail[:-1]
            end_pos = offsets[last] + len(tail)
            spans.append((offsets[i], end_pos, i, last))

    return spans


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

    # For oversized blocks, avoid quadratic sliding window across the entire file
    if k > MAX_SEARCH_LINES:
        first_ln = next((ln.strip() for ln in needle_lines if ln.strip()), "")
        first_norm = _strip_symbols_and_emojis(first_ln)
        for i, ln in enumerate(doc_lines):
            if _strip_symbols_and_emojis(ln.strip()) == first_norm:
                best_start = i
                best_end = min(len(doc_lines) - 1, i + k - 1)
                best_candidate_lines = doc_lines[best_start : best_end + 1]
                best_ratio = 0.60
                break
        if best_start == -1:
            step = max(1, k // 4)
            for i in range(0, max(1, len(doc_lines) - k + 1), step):
                candidate = doc_lines[i : i + k]
                candidate_norm = "\n".join(_strip_symbols_and_emojis(ln) for ln in candidate)
                matcher = difflib.SequenceMatcher(None, needle_norm, candidate_norm)
                ratio = matcher.quick_ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_start = i
                    best_end = min(len(doc_lines) - 1, i + k - 1)
                    best_candidate_lines = candidate
    else:
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
            f"------------------------------------------------------------\n"
            f"(Note for LLM: Lines starting with '+' show actual file content. Update your SEARCH block to match.)"
        )
    return ""


MAX_SEARCH_LINES = 60
MAX_SEARCH_CHARS = 4000
# Blocks larger than this trigger boundary-anchor matching FIRST (head-N + tail-N lines),
# bypassing the slow full-text tiers for an immediate O(n) scan.
BOUNDARY_ANCHOR_THRESHOLD = 30


def _format_spans_error(err_prefix: str, target: str, spans: list[tuple[int, int, int, int]]) -> OpError:
    matches_text = []
    for i, (_, _, s, e) in enumerate(spans[:10], 1):
        if s != e:
            matches_text.append(f"Match {i}: lines {s + 1}-{e + 1}")
        else:
            matches_text.append(f"Match {i}: line {s + 1}")
    if len(spans) > 10:
        matches_text.append(f"... (+{len(spans) - 10} more)")
    match_str = "\n".join(matches_text)
    msg = (
        f"ERR|{err_prefix}_AMBIGUOUS\n\n"
        "CODE_EXEC_ERROR\n"
        "status: error\n"
        "type: DUPLICATE_MATCH\n"
        f"operation: {err_prefix}\n"
        f"file: {target}\n\n"
        "Expected: 1 match\n"
        f"Found: {len(spans)} matches\n\n"
        f"{match_str}\n\n"
        "The requested code is not unique.\n"
        "Choose which match should be edited, or specify that one duplicate should be removed."
    )
    return OpError(msg)


def find_unique(doc: str, needle: str, what: str, target: str) -> MatchResult:
    with ui.searching(target):
        return _find_unique_impl(doc, needle, what, target)


def find_all(doc: str, needle: str, target: str) -> list[tuple[int, int]]:
    """
    Find ALL non-overlapping occurrences of needle in doc using the same
    tolerance tiers as find_unique (trailing whitespace, indentation, whitespace
    collapse, JSX normalization).  Returns spans as (start, end) byte offsets
    sorted in REVERSE order (right-to-left) so callers can splice without offset drift.

    Raises OpError if no match is found at any tier.
    """
    if not needle:
        raise OpError("SEARCH block is empty")

    want_raw = needle.split("\n")
    while want_raw and not want_raw[0].strip():
        want_raw.pop(0)
    while want_raw and not want_raw[-1].strip():
        want_raw.pop()
    if not want_raw:
        raise OpError("SEARCH block is empty")

    capture_nl = needle.endswith("\n")
    doc_lines = doc.split("\n")

    def _spans_to_offsets(spans):
        return [(s, e) for s, e, _, _ in spans]

    # Tier 1: exact
    if doc.count(needle) > 0:
        offsets = []
        start_idx = 0
        while True:
            pos = doc.find(needle, start_idx)
            if pos == -1:
                break
            offsets.append((pos, pos + len(needle)))
            start_idx = pos + len(needle)
        return list(reversed(offsets))

    # Tier 2: trailing whitespace
    want2 = [ln.rstrip("\r ") for ln in want_raw]
    spans = _match_line_spans(doc_lines, want2, mode="trailing", capture_newline=capture_nl)
    if spans:
        return list(reversed(_spans_to_offsets(spans)))

    # Tier 3: indentation
    want3 = [ln.strip() for ln in want_raw]
    spans = _match_line_spans(doc_lines, want3, mode="indent", capture_newline=capture_nl)
    if spans:
        return list(reversed(_spans_to_offsets(spans)))

    # Tier 4: whitespace collapse
    want4 = [re.sub(r"[ \t]+", " ", ln.strip()) for ln in want_raw]
    spans = _match_line_spans(doc_lines, want4, mode="whitespace", capture_newline=capture_nl)
    if spans:
        return list(reversed(_spans_to_offsets(spans)))

    # Tier 5: JSX normalization
    want5 = [_normalize_jsx_line(ln) for ln in want_raw]
    spans = _match_line_spans(doc_lines, want5, mode="jsx", capture_newline=capture_nl)
    if spans:
        return list(reversed(_spans_to_offsets(spans)))

    first = next((ln.strip() for ln in needle.split("\n") if ln.strip()), "")
    preview = first if len(first) <= 60 else first[:57] + "..."
    raise OpError(f"ERR|SEARCH_NOT_FOUND|{target} (REPLACE_ALL, first line: {preview!r})")


def _match_boundary_anchors(
    doc_lines: list[str],
    want_raw: list[str],
    target: str,
    what: str,
    tier_label: str = "Tier 0",
) -> MatchResult | None:
    """
    Intelligent boundary-anchor matching for large or oversized SEARCH blocks.
    Tries multiple anchor sizes and normalizations (indent, JSX, whitespace, trailing)
    and resolves ambiguities based on expected block size.
    """
    if len(want_raw) < 4:
        return None

    target_lower = target.lower()
    is_web = target_lower.endswith(
        (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".html", ".htm", ".vue", ".svelte", ".astro")
    )
    # Prefer indent first, then JSX normalization if web file, then whitespace/trailing
    modes = ["indent", "jsx", "whitespace", "trailing"] if is_web else ["indent", "whitespace", "trailing"]

    max_anchor = min(8, max(4, len(want_raw) // 2))
    anchor_sizes = list(range(4, max_anchor + 1))
    for fallback_size in [3, 2]:
        if fallback_size not in anchor_sizes and len(want_raw) >= fallback_size * 2:
            anchor_sizes.append(fallback_size)

    for mode in modes:
        for anchor_size in anchor_sizes:
            head_lines = [ln.strip() for ln in want_raw[:anchor_size] if ln.strip()]
            tail_lines = [ln.strip() for ln in want_raw[-anchor_size:] if ln.strip()]
            if not head_lines or not tail_lines:
                continue

            head_spans = _match_line_spans(doc_lines, head_lines, mode=mode)
            tail_spans = _match_line_spans(doc_lines, tail_lines, mode=mode)

            if not head_spans or not tail_spans:
                continue

            # Case 1: Unique head and unique tail
            if len(head_spans) == 1 and len(tail_spans) == 1:
                h_start, _, h_sline, _ = head_spans[0]
                _, t_end, _, t_eline = tail_spans[0]
                if h_start < t_end:
                    ui.warn(
                        f"Large {what} block matched via boundary anchors "
                        f"({anchor_size}+{anchor_size} lines) at lines {h_sline + 1}-{t_eline + 1} in {target}"
                    )
                    _verbose_note(
                        f"{tier_label} (boundary anchors {anchor_size}+{anchor_size}, mode={mode}): "
                        f"matched {target} lines {h_sline + 1}-{t_eline + 1}"
                    )
                    return MatchResult(
                        h_start,
                        t_end,
                        f" (large block matched via {anchor_size}+{anchor_size} boundary anchors)",
                        True,
                        (h_sline, t_eline),
                    )

            # Case 2: Unique head, multiple tails (e.g. repeated closing tags/brackets)
            if len(head_spans) == 1 and len(tail_spans) > 1:
                h_start, _, h_sline, _ = head_spans[0]
                valid_tails = [ts for ts in tail_spans if ts[0] > h_start]
                if valid_tails:
                    best_tail = min(valid_tails, key=lambda ts: abs((ts[3] - h_sline + 1) - len(want_raw)))
                    diff = abs((best_tail[3] - h_sline + 1) - len(want_raw))
                    if diff <= max(15, int(len(want_raw) * 0.40)):
                        _, t_end, _, t_eline = best_tail
                        ui.warn(
                            f"Large {what} block matched via boundary anchors "
                            f"({anchor_size}+{anchor_size} lines) at lines {h_sline + 1}-{t_eline + 1} in {target}"
                        )
                        _verbose_note(
                            f"{tier_label} (boundary anchors {anchor_size}+{anchor_size}, mode={mode}, disambiguated tail): "
                            f"matched {target} lines {h_sline + 1}-{t_eline + 1}"
                        )
                        return MatchResult(
                            h_start,
                            t_end,
                            f" (large block matched via {anchor_size}+{anchor_size} boundary anchors)",
                            True,
                            (h_sline, t_eline),
                        )

            # Case 3: Multiple heads, unique tail
            if len(head_spans) > 1 and len(tail_spans) == 1:
                _, t_end, _, t_eline = tail_spans[0]
                valid_heads = [hs for hs in head_spans if hs[1] < t_end]
                if valid_heads:
                    best_head = min(valid_heads, key=lambda hs: abs((t_eline - hs[2] + 1) - len(want_raw)))
                    diff = abs((t_eline - best_head[2] + 1) - len(want_raw))
                    if diff <= max(15, int(len(want_raw) * 0.40)):
                        h_start, _, h_sline, _ = best_head
                        ui.warn(
                            f"Large {what} block matched via boundary anchors "
                            f"({anchor_size}+{anchor_size} lines) at lines {h_sline + 1}-{t_eline + 1} in {target}"
                        )
                        _verbose_note(
                            f"{tier_label} (boundary anchors {anchor_size}+{anchor_size}, mode={mode}, disambiguated head): "
                            f"matched {target} lines {h_sline + 1}-{t_eline + 1}"
                        )
                        return MatchResult(
                            h_start,
                            t_end,
                            f" (large block matched via {anchor_size}+{anchor_size} boundary anchors)",
                            True,
                            (h_sline, t_eline),
                        )

    return None


def _find_unique_impl(doc: str, needle: str, what: str, target: str) -> MatchResult:
    """
    Multi-tier intelligent search with strict uniqueness.

    For LARGE blocks (> BOUNDARY_ANCHOR_THRESHOLD lines) the strategy is:
      0. Boundary anchors first  — try head-4 + tail-4, then expand to 5+5 … 8+8
         until a unique pair is found.  This is fast and correct for big blocks.
      If boundary anchors fail (head or tail not unique), fall through to normal tiers.

    For all blocks (including after failed boundary anchors):
      1. Exact byte-for-byte substring.
      2. Trailing whitespace / CRLF tolerant line match.
      3. Indentation-insensitive line match.
      4. Harmless whitespace collapse (tabs→spaces).
      5. JSX-aware line match.
      6. Language token-aware matching (Python / JS / TS).
      7. High-similarity fuzzy match (>= 90%).
      8. Oversized block boundary anchor last-resort (original position, as final fallback).
    """
    if needle == "":
        raise OpError(f"{what} block is empty")

    err_prefix = "SEARCH" if what == "SEARCH" else what

    # Guard against oversized search blocks with a warning instead of a hard block
    needle_lines_count = len(needle.split("\n"))
    needle_char_count = len(needle)
    is_oversized = needle_lines_count > MAX_SEARCH_LINES or needle_char_count > MAX_SEARCH_CHARS
    if is_oversized:
        ui.warn(
            f"Oversized {what} block in {target} ({needle_lines_count} lines, {needle_char_count} chars; "
            f"preferred limit is {MAX_SEARCH_LINES} lines). "
            f"Using boundary anchors (first 4 + last 4 lines)..."
        )

    doc_lines = doc.split("\n")

    want_raw = needle.split("\n")
    while want_raw and not want_raw[0].strip():
        want_raw.pop(0)
    while want_raw and not want_raw[-1].strip():
        want_raw.pop()

    if not want_raw:
        raise OpError(f"{what} block is empty")

    capture_nl = needle.endswith("\n")

    # ---- Tier 1: Exact match (always first — fast O(n), preserves byte-exact result) ----
    count = doc.count(needle)
    if count == 1:
        start = doc.index(needle)
        _verbose_note(f"Tier 1 (exact): matched {target}")
        return MatchResult(start, start + len(needle), "", False, None)
    if count > 1:
        spans = []
        start_idx = 0
        while True:
            pos = doc.find(needle, start_idx)
            if pos == -1:
                break
            s_line = doc[:pos].count("\n")
            e_line = doc[: pos + len(needle)].count("\n")
            spans.append((pos, pos + len(needle), s_line, e_line))
            start_idx = pos + 1
        raise _format_spans_error(err_prefix, target, spans)

    # ---- Tier 0: Boundary anchors — for large blocks that didn't exact-match ----
    # For large blocks the LLM wrote a huge SEARCH but only the first/last few lines
    # need to anchor the region. Try 4+4 first, then expand until unique.
    # This fires BEFORE the slow full-text tiers (trailing ws, indent, fuzzy...).
    if len(want_raw) > BOUNDARY_ANCHOR_THRESHOLD or is_oversized:
        _verbose_note(f"Tier 0 (boundary anchors, large block {len(want_raw)} lines): trying {target}")
        anchor_match = _match_boundary_anchors(doc_lines, want_raw, target, what, tier_label="Tier 0")
        if anchor_match is not None:
            return anchor_match
        _verbose_note(f"Tier 0 (boundary anchors): failed for {target}, falling through to normal tiers")

    # ---- Tier 2: Trailing whitespace tolerant ----
    want_trailing = [ln.rstrip("\r ") for ln in want_raw]
    spans = _match_line_spans(doc_lines, want_trailing, mode="trailing", capture_newline=capture_nl)
    if len(spans) == 1:
        start, end, i, last = spans[0]
        _verbose_note(f"Tier 2 (trailing whitespace): matched {target} at lines {i + 1}-{last + 1}")
        return MatchResult(start, end, " (matched ignoring trailing whitespace)", True, (i, last))
    if len(spans) > 1:
        raise _format_spans_error(err_prefix, target, spans)
    _verbose_note(f"Tier 2 (trailing whitespace): no match in {target}")

    # ---- Tier 3: Indentation tolerant ----
    want_indent = [ln.strip() for ln in want_raw]
    spans = _match_line_spans(doc_lines, want_indent, mode="indent", capture_newline=capture_nl)
    if len(spans) == 1:
        start, end, i, last = spans[0]
        _verbose_note(f"Tier 3 (indentation): matched {target} at lines {i + 1}-{last + 1}")
        return MatchResult(start, end, " (matched with indentation tolerance)", True, (i, last))
    if len(spans) > 1:
        raise _format_spans_error(err_prefix, target, spans)
    _verbose_note(f"Tier 3 (indentation): no match in {target}")

    # ---- Tier 4: Harmless whitespace normalization (spaces collapsed) ----
    want_ws = [re.sub(r"[ \t]+", " ", ln.strip()) for ln in want_raw]
    spans = _match_line_spans(doc_lines, want_ws, mode="whitespace", capture_newline=capture_nl)
    if len(spans) == 1:
        start, end, i, last = spans[0]
        _verbose_note(f"Tier 4 (whitespace collapse): matched {target} at lines {i + 1}-{last + 1}")
        return MatchResult(start, end, " (matched with whitespace normalization)", True, (i, last))
    if len(spans) > 1:
        raise _format_spans_error(err_prefix, target, spans)
    _verbose_note(f"Tier 4 (whitespace collapse): no match in {target}")

    # ---- Tier 5: JSX-aware line match ----
    want_jsx = [_normalize_jsx_line(ln) for ln in want_raw]
    spans = _match_line_spans(doc_lines, want_jsx, mode="jsx", capture_newline=capture_nl)
    if len(spans) == 1:
        start, end, i, last = spans[0]
        _verbose_note(f"Tier 5 (JSX normalization): matched {target} at lines {i + 1}-{last + 1}")
        return MatchResult(start, end, " (matched with JSX normalization)", True, (i, last))
    if len(spans) > 1:
        raise _format_spans_error(err_prefix, target, spans)
    _verbose_note(f"Tier 5 (JSX): no match in {target}")

    # ---- Tier 6: Language token-aware matching (Python / JS / TS) ----
    target_lower = target.lower()
    if target_lower.endswith(".py"):
        py_spans = _match_python_tokens(doc, needle)
        if len(py_spans) == 1:
            start, end, i, last = py_spans[0]
            _verbose_note(f"Tier 6a (Python tokens): matched {target} at lines {i + 1}-{last + 1}")
            return MatchResult(start, end, " (matched with Python token normalization)", True, (i, last))
        if len(py_spans) > 1:
            raise _format_spans_error(err_prefix, target, py_spans)
        _verbose_note(f"Tier 6a (Python tokens): no match in {target}")

    if target_lower.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")):
        js_spans = _match_js_tokens(doc, needle)
        if len(js_spans) == 1:
            start, end, i, last = js_spans[0]
            _verbose_note(f"Tier 6b (JS/TS tokens): matched {target} at lines {i + 1}-{last + 1}")
            return MatchResult(start, end, " (matched with JS/TS token normalization)", True, (i, last))
        if len(js_spans) > 1:
            raise _format_spans_error(err_prefix, target, js_spans)
        _verbose_note(f"Tier 6b (JS/TS tokens): no match in {target}")

    # ---- Fast Check for Oversized Blocks Before Slow Fuzzy Search ----
    if is_oversized:
        anchor_match = _match_boundary_anchors(doc_lines, want_raw, target, what, tier_label="Tier 8")
        if anchor_match is not None:
            return anchor_match
        _verbose_note(f"Oversized block in {target} failed boundary anchors and normalized tiers; skipping slow fuzzy scan.")
        diagnostic = _find_closest_match(doc, needle)
        first = next((ln.strip() for ln in needle.split("\n") if ln.strip()), "")
        preview = first if len(first) <= 60 else first[:57] + "..."
        raise OpError(f"ERR|{err_prefix}_NOT_FOUND|{target} (first line: {preview!r}){diagnostic}")

    # ---- Tier 7: Dual-Threshold Fuzzy Matching ----
    candidates_raw = _find_high_similarity_candidates(
        doc, needle, min_threshold=RESOLVER_THRESHOLD, capture_newline=capture_nl
    )
    candidates = [
        FuzzyCandidate(
            similarity=sim,
            start=s_off,
            end=e_off,
            start_line=s_line,
            end_line=e_line,
            preview=prev,
        )
        for sim, s_off, e_off, s_line, e_line, prev in candidates_raw
    ]

    if candidates:
        best = candidates[0]
        pct = int(best.similarity * 100)

        # Disambiguation check: multiple competing candidates
        competing = [
            c for c in candidates[1:]
            if c.similarity >= RESOLVER_THRESHOLD and (best.similarity - c.similarity) < 0.10
        ]
        if competing:
            _verbose_note(f"Tier 7 (fuzzy ambiguity): {len(candidates)} candidates found in {target}")
            if FUZZY_RESOLVER is not None:
                ui.stop_searching()
                resolved = FUZZY_RESOLVER(target, needle, candidates)
                if resolved is not None:
                    return resolved
            cand_locs = ", ".join(f"lines {c.start_line + 1}-{c.end_line + 1} ({int(c.similarity * 100)}%)" for c in candidates[:4])
            raise OpError(f"ERR|{err_prefix}_AMBIGUOUS|{target}|matched {len(candidates)} fuzzy candidates at [{cand_locs}]")

        # Single dominant candidate: Confident Zone vs Resolver Zone
        if best.similarity >= CONFIDENT_THRESHOLD:
            if pct < 100:
                ui.warn(f"Fuzzy match ({pct}% similarity) accepted for {target} at lines {best.start_line + 1}-{best.end_line + 1}")
                note = f" (fuzzy matched with {pct}% similarity)"
            else:
                note = " (matched ignoring non-standard symbols)"
            _verbose_note(f"Tier 7 (fuzzy confident {pct}%): matched {target} at lines {best.start_line + 1}-{best.end_line + 1}")
            return MatchResult(best.start, best.end, note, True, (best.start_line, best.end_line), similarity=best.similarity, candidates=candidates)

        else:
            # Resolver Zone (RESOLVER_THRESHOLD <= similarity < CONFIDENT_THRESHOLD)
            _verbose_note(f"Tier 7 (fuzzy borderline {pct}%): candidate found at lines {best.start_line + 1}-{best.end_line + 1}")
            if FUZZY_RESOLVER is not None:
                ui.stop_searching()
                resolved = FUZZY_RESOLVER(target, needle, [best])
                if resolved is not None:
                    return resolved
                else:
                    raise OpError(f"ERR|{err_prefix}_FUZZY_REJECTED|{target} - rejected candidate at lines {best.start_line + 1}-{best.end_line + 1} ({pct}% similarity)")
            elif FUZZY_POLICY == "auto":
                ui.warn(f"🛡️ Borderline fuzzy match ({pct}% similarity) auto-accepted for {target} at lines {best.start_line + 1}-{best.end_line + 1}")
                note = f" (auto-accepted borderline fuzzy match with {pct}% similarity)"
                return MatchResult(best.start, best.end, note, True, (best.start_line, best.end_line), similarity=best.similarity, candidates=candidates)
            else:
                # Strict / Non-interactive: provide clear diagnostic guidance
                diagnostic = _find_closest_match(doc, needle)
                raise OpError(
                    f"ERR|{err_prefix}_NOT_FOUND|{target} (candidate found at lines {best.start_line + 1}-{best.end_line + 1} with {pct}% similarity, below {int(CONFIDENT_THRESHOLD * 100)}% threshold).{diagnostic}"
                )

    _verbose_note(f"Tier 7 (fuzzy): no match in {target}")

    # ---- Tier 8: Boundary anchor last-resort ----
    if len(want_raw) > BOUNDARY_ANCHOR_THRESHOLD:
        anchor_match = _match_boundary_anchors(doc_lines, want_raw, target, what, tier_label="Tier 8")
        if anchor_match is not None:
            return anchor_match
        _verbose_note(f"Tier 8 (boundary anchors): no match in {target}")

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


def parse_unified_diff(patch_text: str, target: str = "") -> list[Hunk]:
    """Parses standard unified diff text into structured Hunk objects."""
    clean_text = patch_text.replace("\r\n", "\n")
    raw_lines = clean_text.split("\n")
    hunks: list[Hunk] = []
    i = 0
    while i < len(raw_lines):
        line = raw_lines[i]
        m = re.match(r"^@@\s*-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s*@@", line)
        if m:
            old_start = int(m.group(1))
            old_len = int(m.group(2)) if m.group(2) is not None else 1
            new_start = int(m.group(3))
            new_len = int(m.group(4)) if m.group(4) is not None else 1
            hunk_lines: list[tuple[str, str]] = []
            i += 1
            while i < len(raw_lines):
                curr = raw_lines[i]
                if curr.startswith("@@"):
                    break
                if curr.startswith("+"):
                    hunk_lines.append(("+", curr[1:]))
                elif curr.startswith("-"):
                    hunk_lines.append(("-", curr[1:]))
                elif curr.startswith(" "):
                    hunk_lines.append((" ", curr[1:]))
                elif curr.startswith("\\"):
                    pass
                elif curr == "":
                    old_count = sum(1 for tag, _ in hunk_lines if tag in {" ", "-"})
                    if old_len > 0 and old_count >= old_len and (i == len(raw_lines) - 1 or raw_lines[i + 1].startswith("@@")):
                        pass
                    else:
                        hunk_lines.append((" ", ""))
                else:
                    hunk_lines.append((" ", curr))
                i += 1
            hunks.append(Hunk(old_start, old_len, new_start, new_len, hunk_lines))
        else:
            i += 1
    if not hunks:
        raise OpError(f"ERR|PATCH_FAILED|{target} - No valid diff hunks (@@ -start,len +start,len @@) found in PATCH content")
    return hunks


def apply_unified_patch(doc: str, patch_text: str, target: str, newline: str = "\n") -> tuple[str, int]:
    """Applies a multi-hunk unified diff with fuzzy line offset and whitespace tolerance."""
    clean_doc = doc.replace("\r\n", "\n")
    doc_lines = clean_doc.split("\n")
    hunks = parse_unified_diff(patch_text, target)
    current_offset = 0
    last_match_end = 0

    for hunk_idx, hunk in enumerate(hunks, 1):
        old_lines = [text for tag, text in hunk.lines if tag in {" ", "-"}]
        new_lines = [text for tag, text in hunk.lines if tag in {" ", "+"}]

        if not old_lines:
            target_idx = max(0, hunk.old_start + current_offset)
            target_idx = max(last_match_end, min(target_idx, len(doc_lines)))
            doc_lines[target_idx:target_idx] = new_lines
            current_offset += len(new_lines)
            last_match_end = target_idx + len(new_lines)
            continue

        k = len(old_lines)
        target_idx = (hunk.old_start - 1) + current_offset
        target_idx = max(last_match_end, min(target_idx, max(0, len(doc_lines) - k)))

        matched_span: tuple[int, int] | None = None
        match_mode = "exact"

        # Tier 1: Exact match
        if target_idx + k <= len(doc_lines) and doc_lines[target_idx : target_idx + k] == old_lines:
            matched_span = (target_idx, target_idx + k)
        else:
            candidates = [
                j for j in range(last_match_end, len(doc_lines) - k + 1)
                if doc_lines[j : j + k] == old_lines
            ]
            if candidates:
                best_j = min(candidates, key=lambda j: abs(j - target_idx))
                matched_span = (best_j, best_j + k)

        # Tier 2: Trailing whitespace tolerance
        if matched_span is None:
            want_trailing = [ln.rstrip("\r ") for ln in old_lines]
            if target_idx + k <= len(doc_lines) and [ln.rstrip("\r ") for ln in doc_lines[target_idx : target_idx + k]] == want_trailing:
                matched_span = (target_idx, target_idx + k)
                match_mode = "trailing"
            else:
                candidates = [
                    j for j in range(last_match_end, len(doc_lines) - k + 1)
                    if [ln.rstrip("\r ") for ln in doc_lines[j : j + k]] == want_trailing
                ]
                if candidates:
                    best_j = min(candidates, key=lambda j: abs(j - target_idx))
                    matched_span = (best_j, best_j + k)
                    match_mode = "trailing"

        # Tier 3: Indentation tolerance
        if matched_span is None:
            want_indent = [ln.strip() for ln in old_lines]
            if target_idx + k <= len(doc_lines) and [ln.strip() for ln in doc_lines[target_idx : target_idx + k]] == want_indent:
                matched_span = (target_idx, target_idx + k)
                match_mode = "indent"
            else:
                candidates = [
                    j for j in range(last_match_end, len(doc_lines) - k + 1)
                    if [ln.strip() for ln in doc_lines[j : j + k]] == want_indent
                ]
                if candidates:
                    best_j = min(candidates, key=lambda j: abs(j - target_idx))
                    matched_span = (best_j, best_j + k)
                    match_mode = "indent"

        # Tier 4: Whitespace collapsed
        if matched_span is None:
            want_ws = [re.sub(r"[ \t]+", " ", ln.strip()) for ln in old_lines]
            if target_idx + k <= len(doc_lines) and [re.sub(r"[ \t]+", " ", ln.strip()) for ln in doc_lines[target_idx : target_idx + k]] == want_ws:
                matched_span = (target_idx, target_idx + k)
                match_mode = "whitespace"
            else:
                candidates = [
                    j for j in range(last_match_end, len(doc_lines) - k + 1)
                    if [re.sub(r"[ \t]+", " ", ln.strip()) for ln in doc_lines[j : j + k]] == want_ws
                ]
                if candidates:
                    best_j = min(candidates, key=lambda j: abs(j - target_idx))
                    matched_span = (best_j, best_j + k)
                    match_mode = "whitespace"

        # Tier 5: High similarity fuzzy match (>= 0.85)
        if matched_span is None:
            needle_str = "\n".join(_strip_symbols_and_emojis(ln) for ln in old_lines)
            best_r = 0.0
            best_cand = None
            window_sizes = {max(1, k - 1), k, k + 1}
            for w in window_sizes:
                for j in range(last_match_end, len(doc_lines) - w + 1):
                    cand_str = "\n".join(_strip_symbols_and_emojis(ln) for ln in doc_lines[j : j + w])
                    sm = difflib.SequenceMatcher(None, needle_str, cand_str)
                    if sm.quick_ratio() >= 0.80:
                        r = sm.ratio()
                        if r >= 0.85:
                            score = (r, -abs(j - target_idx))
                            if best_cand is None or score > (best_r, -abs(best_cand[0] - target_idx)):
                                best_r = r
                                best_cand = (j, j + w)
            if best_cand is not None:
                matched_span = best_cand
                match_mode = "fuzzy"

        if matched_span is None:
            needle_text = "\n".join(old_lines)
            diagnostic = _find_closest_match(clean_doc, needle_text)
            raise OpError(
                f"ERR|PATCH_FAILED|{target} - Hunk #{hunk_idx} at line {hunk.old_start} could not be matched.{diagnostic}"
            )

        start_idx, end_idx = matched_span
        replacement = list(new_lines)
        if match_mode == "indent" and old_lines and replacement:
            doc_indent = _get_leading_indent(doc_lines[start_idx])
            old_indent = _get_leading_indent(old_lines[0])
            if doc_indent != old_indent:
                adj = []
                for ln in replacement:
                    if not ln.strip():
                        adj.append(ln)
                    elif old_indent and ln.startswith(old_indent):
                        adj.append(doc_indent + ln[len(old_indent):])
                    else:
                        adj.append(doc_indent + ln.lstrip(" \t"))
                replacement = adj

        doc_lines[start_idx : end_idx] = replacement
        lines_removed = end_idx - start_idx
        current_offset += len(replacement) - lines_removed
        last_match_end = start_idx + len(replacement)

    new_doc = "\n".join(doc_lines)
    if doc.endswith("\n") and not new_doc.endswith("\n"):
        new_doc += "\n"
    from code_exec_fs import with_newlines
    return with_newlines(new_doc, newline), len(hunks)
