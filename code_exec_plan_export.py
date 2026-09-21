from __future__ import annotations

import fnmatch
import os
import shutil
from pathlib import Path

from code_exec_parser import extract_plan, parse_operations
from code_exec_types import PROTECTED_FILE_EXACT, PROTECTED_NAMES, PROTECTED_SUFFIXES, ROOT, OpError

DEFAULT_IGNORE_PATTERNS = [
    ".git",
    ".git/",
    ".code_exec",
    ".code_exec/",
    ".plan-only",
    ".plan-only/",
    ".context",
    ".context/",
    "node_modules",
    "node_modules/",
    "dist",
    "dist/",
    "build",
    "build/",
    "coverage",
    "coverage/",
    ".tmp",
    ".tmp/",
    "temp",
    "temp/",
    ".cache",
    ".cache/",
    "*.tmp",
    "*.log",
    "*.bak",
    "*.pyc",
    "__pycache__",
    "__pycache__/",
    ".env*",
]


def load_ignore_patterns(root: Path = ROOT, ignore_filename: str = ".ignorefile") -> tuple[list[str], str]:
    """Loads exclusion patterns from .ignorefile (or falls back to .gitignore or defaults)."""
    ignore_path = root / ignore_filename
    used_file = ignore_filename

    patterns: list[str] = list(DEFAULT_IGNORE_PATTERNS)

    if not ignore_path.is_file():
        fallback = root / ".gitignore"
        if fallback.is_file():
            ignore_path = fallback
            used_file = ".gitignore"
        else:
            used_file = "(built-in defaults)"
            return patterns, used_file

    try:
        lines = ignore_path.read_text(encoding="utf-8", errors="replace").splitlines()
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            patterns.append(line)
    except OSError:
        pass

    return patterns, used_file


def _pattern_matches(rel_path_posix: str, is_dir: bool, pattern: str) -> bool:
    pattern = pattern.strip()
    if not pattern:
        return False

    dir_only = pattern.endswith("/")
    clean_pat = pattern.rstrip("/")

    if dir_only and not is_dir:
        parts = rel_path_posix.split("/")[:-1]
        for part in parts:
            if fnmatch.fnmatch(part, clean_pat):
                return True
        return False

    parts = rel_path_posix.split("/")
    filename = parts[-1]

    if "/" not in clean_pat:
        if fnmatch.fnmatch(filename, clean_pat):
            return True
        for part in parts:
            if fnmatch.fnmatch(part, clean_pat):
                return True
    else:
        pat_clean = clean_pat.lstrip("/")
        if fnmatch.fnmatch(rel_path_posix, pat_clean) or fnmatch.fnmatch(rel_path_posix, f"*/{pat_clean}"):
            return True

    return False


def is_path_ignored(rel_path: Path, is_dir: bool, patterns: list[str]) -> bool:
    rel_posix = rel_path.as_posix()
    parts = [p.lower() for p in rel_path.parts]

    if any(part in PROTECTED_NAMES for part in parts):
        return True
    file_name = rel_path.name.lower()
    if file_name in PROTECTED_FILE_EXACT or any(file_name.endswith(ext) for ext in PROTECTED_SUFFIXES):
        return True

    for pat in patterns:
        if _pattern_matches(rel_posix, is_dir, pat):
            return True
    return False


def extract_files_from_plan(plan_text: str) -> list[str]:
    """Extracts target file paths explicitly referenced in plan instructions."""
    if not plan_text or not plan_text.strip():
        return []

    try:
        clean_plan = extract_plan(plan_text)
        operations = parse_operations(clean_plan)
    except Exception:
        return []

    targets = []
    for op in operations:
        if op.command in {"RUN", "COMMIT"} or not op.args:
            continue
        for arg in op.args:
            arg_str = str(arg).strip().replace("\\", "/").rstrip("/")
            if arg_str and not any(ch in arg_str for ch in ("*", "?", "[")) and not arg_str.startswith("regex:"):
                targets.append(arg_str)

    return list(dict.fromkeys(targets))


def create_plan_folder(
    plan_text: str | None = None,
    output_dirname: str = ".context",
    ignore_filename: str = ".ignorefile",
    root: Path = ROOT,
) -> dict[str, int | str]:
    """
    Creates or cleanly refreshes a plan-only folder with minimal relevant files.
    """
    target_dir = root / output_dirname
    patterns, used_ignore = load_ignore_patterns(root, ignore_filename)

    requested_paths = extract_files_from_plan(plan_text or "")
    candidate_files: list[Path] = []

    if requested_paths:
        for p_str in requested_paths:
            p = root / p_str
            if p.is_file():
                candidate_files.append(p)
            elif p.is_dir():
                for sub in p.rglob("*"):
                    if sub.is_file():
                        candidate_files.append(sub)

        for manifest in ("package.json", "pyproject.toml", "Cargo.toml", "go.mod", "README.md"):
            m_path = root / manifest
            if m_path.is_file() and m_path not in candidate_files:
                candidate_files.append(m_path)
    else:
        for p in root.rglob("*"):
            if p.is_file():
                candidate_files.append(p)

    included_files: list[Path] = []
    ignored_count = 0

    for file_path in candidate_files:
        try:
            rel_path = file_path.relative_to(root)
        except ValueError:
            continue

        if is_path_ignored(rel_path, is_dir=False, patterns=patterns):
            ignored_count += 1
            continue

        included_files.append(file_path)

    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)
    target_dir.mkdir(parents=True, exist_ok=True)

    for file_path in included_files:
        rel_path = file_path.relative_to(root)
        dest_file = target_dir / rel_path
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_path, dest_file)

    return {
        "location": f"{output_dirname}/",
        "included": len(included_files),
        "ignored": ignored_count,
        "ignore_file": used_ignore,
    }
