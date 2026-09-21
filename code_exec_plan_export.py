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
    ".idea",
    ".idea/",
    ".vscode",
    ".vscode/",
    ".storage",
    ".storage/",
    "secrets",
    "secrets/",
    "*.secret*",
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
    # Lockfiles & package manager state
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lockb",
    "poetry.lock",
    "Cargo.lock",
    "composer.lock",
    "Gemfile.lock",
    # OS & editor metadata
    ".DS_Store",
    "Thumbs.db",
    # Minified assets & sourcemaps
    "*.min.js",
    "*.min.css",
    "*.map",
    # Archives & binaries
    "*.tar",
    "*.tar.gz",
    "*.zip",
    "*.rar",
    "*.7z",
    "*.gz",
    "*.pdf",
    # Media & fonts
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.ico",
    "*.svg",
    "*.webp",
    "*.mp3",
    "*.mp4",
    "*.mov",
    "*.avi",
    "*.woff",
    "*.woff2",
    "*.ttf",
    "*.eot",
]


def load_ignore_patterns(root: Path = ROOT, ignore_filename: str | None = ".code-exec-ignore") -> tuple[list[str], str]:
    """
    Loads exclusion patterns, always preserving DEFAULT_IGNORE_PATTERNS (caches, temp, artifacts).
    Checks candidate ignore files in priority order:
      1. Explicitly provided filename (if valid)
      2. .code-exec-ignore
      3. code-exec-ignore
      4. .ignorefile
      5. .gitignore
    """
    patterns: list[str] = list(DEFAULT_IGNORE_PATTERNS)
    candidates: list[str] = []
    if ignore_filename and ignore_filename != ".code-exec-ignore":
        candidates.append(ignore_filename)
    candidates.extend([".code-exec-ignore", "code-exec-ignore", ".ignorefile", ".gitignore"])

    found_path: Path | None = None
    used_file = "(built-in defaults)"

    for candidate in candidates:
        if not candidate:
            continue
        p = root / candidate
        if p.is_file():
            found_path = p
            used_file = candidate
            break

    if found_path is not None:
        try:
            lines = found_path.read_text(encoding="utf-8", errors="replace").splitlines()
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

    clean_pat = pattern.rstrip("/")
    pat_clean = clean_pat.lstrip("/")

    # Check exact match or prefix directory match (matches all files under that folder)
    if rel_path_posix == pat_clean or rel_path_posix.startswith(f"{pat_clean}/"):
        return True
    if fnmatch.fnmatch(rel_path_posix, f"*/{pat_clean}") or fnmatch.fnmatch(rel_path_posix, f"*/{pat_clean}/*"):
        return True

    parts = rel_path_posix.split("/")
    filename = parts[-1]

    if "/" not in clean_pat:
        if fnmatch.fnmatch(filename, clean_pat):
            return True
        # If any directory segment matches the pattern
        for part in parts:
            if fnmatch.fnmatch(part, clean_pat):
                return True
    else:
        if fnmatch.fnmatch(rel_path_posix, pat_clean) or fnmatch.fnmatch(rel_path_posix, f"{pat_clean}/*"):
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


def estimate_tokens(text: str) -> int:
    """Estimates LLM token count using a blend of whitespace splits and character length."""
    if not text:
        return 0
    words = len(text.split())
    chars = len(text)
    # Heuristic: ~4 chars per token for code/prose, blended with word count
    return max(words, int(chars / 3.8))


def create_plan_folder(
    plan_text: str | None = None,
    output_dirname: str = ".context",
    ignore_filename: str | None = ".code-exec-ignore",
    root: Path = ROOT,
    export_all: bool = True,
) -> dict[str, int | str]:
    """
    Creates or cleanly refreshes a clean project context folder,
    excluding temporary files, caches, and build artifacts.
    Computes total file count and estimated LLM tokens.
    """
    target_dir = root / output_dirname
    patterns, used_ignore = load_ignore_patterns(root, ignore_filename)

    # 1. Immediately wipe any existing context folder first to avoid scanning old exports
    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)
    target_dir.mkdir(parents=True, exist_ok=True)

    # 2. Collect candidate files from clean workspace
    candidate_files: list[Path] = []
    requested_paths = extract_files_from_plan(plan_text or "") if not export_all else []

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
            # Never include target_dir even if partially created
            try:
                if target_dir in p.parents or p.resolve() == target_dir.resolve():
                    continue
            except (OSError, RuntimeError):
                pass
            if p.is_file():
                candidate_files.append(p)

    included_files: list[Path] = []
    ignored_count = 0
    total_bytes = 0
    total_tokens = 0

    for file_path in candidate_files:
        try:
            rel_path = file_path.relative_to(root)
        except ValueError:
            continue

        if is_path_ignored(rel_path, is_dir=False, patterns=patterns):
            ignored_count += 1
            continue

        included_files.append(file_path)
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
            total_tokens += estimate_tokens(content)
            total_bytes += file_path.stat().st_size
        except OSError:
            pass

    for file_path in included_files:
        rel_path = file_path.relative_to(root)
        dest_file = target_dir / rel_path
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_path, dest_file)

    return {
        "location": f"{output_dirname}/",
        "included": len(included_files),
        "ignored": ignored_count,
        "tokens": total_tokens,
        "size_kb": round(total_bytes / 1024, 1),
        "ignore_file": used_ignore,
    }
