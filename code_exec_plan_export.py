from __future__ import annotations

import fnmatch
import os
import re
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
    "context",
    "context/",
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


def load_ignore_patterns(root: Path | None = None, ignore_filename: str | None = ".code-exec-ignore") -> tuple[list[str], str]:
    """
    Loads exclusion patterns, always preserving DEFAULT_IGNORE_PATTERNS (caches, temp, artifacts).
    Checks candidate ignore files in priority order:
      1. Explicitly provided filename (if valid)
      2. .code-exec-ignore
      3. code-exec-ignore
      4. .ignorefile
      5. .gitignore
    """
    if root is None:
        root = Path.cwd().resolve()
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
        if op.command in {"FETCH", "CHMOD"}:
            targets.append(str(op.args[0]))
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


def generate_skeleton(file_path: Path, max_lines: int = 40) -> str:
    """Generates a compact structural outline of a file to minimize prompt tokens."""
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""

    lines = content.splitlines()
    if len(lines) <= max_lines:
        return content

    suffix = file_path.suffix.lower()
    rel_name = file_path.name

    # Python: parse AST to extract function and class signatures
    if suffix == ".py":
        import ast
        try:
            tree = ast.parse(content)
            skel_lines = [
                f"# Skeleton: {rel_name} ({len(lines)} lines)",
                f"# Use 'FETCH {rel_name}:<start>-<end>' to inspect full implementation\n",
            ]
            doc = ast.get_docstring(tree)
            if doc:
                first_doc = doc.strip().split("\n")[0]
                skel_lines.append(f'"""{first_doc}"""\n')
            for node in ast.iter_child_nodes(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                    args = [a.arg for a in node.args.args]
                    fdoc = ast.get_docstring(node)
                    doc_hint = f'  # """{fdoc.strip().splitlines()[0][:50]}..."""' if fdoc else ""
                    skel_lines.append(f"{prefix} {node.name}({', '.join(args)}): ... (lines {node.lineno}-{node.end_lineno}){doc_hint}")
                elif isinstance(node, ast.ClassDef):
                    bases = [getattr(b, "id", getattr(b, "attr", "...")) for b in node.bases]
                    base_str = f"({', '.join(bases)})" if bases else ""
                    skel_lines.append(f"class {node.name}{base_str}: ... (lines {node.lineno}-{node.end_lineno})")
                    for sub in node.body:
                        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            mprefix = "async def" if isinstance(sub, ast.AsyncFunctionDef) else "def"
                            margs = [a.arg for a in sub.args.args]
                            skel_lines.append(f"    {mprefix} {sub.name}({', '.join(margs)}): ... (lines {sub.lineno}-{sub.end_lineno})")
            if len(skel_lines) > 2:
                return "\n".join(skel_lines) + "\n"
        except Exception:
            pass

    # JavaScript / TypeScript / React JSX & TSX signature extraction
    if suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}:
        out_lines = [
            f"// Skeleton: {rel_name} ({len(lines)} lines)",
            f"// Use 'FETCH {rel_name}:<start>-<end>' to inspect full implementation\n",
        ]
        
        # 1. Capture imports and top-level JSDocs
        in_jsdoc = False
        curr_doc: list[str] = []
        
        # Regex matching ONLY top-level declarations (no leading spaces/tabs)
        top_decl_pattern = re.compile(
            r"^(export\s+)?(default\s+)?(async\s+)?(function\*?|class|interface|type|const|let|var)\s+([A-Za-z0-9_$]+)(\s*:\s*[^=]+)?(\s*=\s*(?:React\.(?:memo|forwardRef)|use[A-Z0-9]\w*|\([^)]*\)\s*=>|\w+))?"
        )
        export_named_pattern = re.compile(r"^export\s+\{([^}]+)\}")

        for idx, ln in enumerate(lines, 1):
            raw_stripped = ln.strip()
            
            # Track block comments / JSDocs at top level
            if ln.startswith("/**") or (in_jsdoc and ln.startswith(" *")):
                in_jsdoc = True
                curr_doc.append(raw_stripped)
                continue
            if in_jsdoc and (ln.startswith(" */") or raw_stripped.endswith("*/")):
                curr_doc.append(raw_stripped)
                in_jsdoc = False
                continue

            # Capture imports
            if ln.startswith("import ") or ln.startswith("import{"):
                clean_imp = re.sub(r"\s+", " ", raw_stripped)
                out_lines.append(clean_imp)
                curr_doc.clear()
                continue

            # Only inspect statements starting at column 0 (indentation == 0)
            if ln and not ln[0].isspace():
                m = top_decl_pattern.match(ln)
                if m:
                    # Append associated doc if present
                    if curr_doc:
                        out_lines.extend(curr_doc[-6:])
                        curr_doc.clear()
                    
                    # Clean signature
                    sig = ln.split("{")[0].split(";")[0].rstrip()
                    if "=>" in sig:
                        sig = sig.split("=>")[0].strip() + " => ..."
                    elif "=" in sig and not ("(" in sig and ")" in sig):
                        sig = sig.split("=")[0].strip()
                    else:
                        sig = sig.rstrip()
                    out_lines.append(f"{sig} ... (line {idx})")
                    continue
                
                m_exp = export_named_pattern.match(ln)
                if m_exp:
                    out_lines.append(f"export {{ {re.sub(r'[ \t\n]+', ' ', m_exp.group(1)).strip()} }} (line {idx})")
                    curr_doc.clear()
                    continue

            # Reset doc if normal line encountered
            if not in_jsdoc and raw_stripped:
                curr_doc.clear()

        if len(out_lines) > 2:
            return "\n".join(out_lines) + f"\n\n// ... [{len(lines)} total lines]\n"

    # Default fallback: keep header lines + truncation hint
    head = lines[:15]
    tail = lines[-3:] if len(lines) > 20 else []
    sep = [f"\n... [{len(lines) - len(head) - len(tail)} lines omitted. Request full content with: FETCH {rel_name}]\n"]
    return "\n".join(head + sep + tail) + "\n"


def create_plan_folder(
    plan_text: str | None = None,
    output_dirname: str = "context",
    ignore_filename: str | None = ".code-exec-ignore",
    root: Path | None = None,
    export_all: bool = True,
    compact: bool = False,
) -> dict[str, int | str | bool]:
    """
    Creates or cleanly refreshes a clean project context folder,
    excluding temporary files, caches, and build artifacts.
    Computes total file count and estimated LLM tokens.
    """
    if root is None:
        root = Path.cwd().resolve()
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

    index_entries = []
    for file_path in included_files:
        rel_path = file_path.relative_to(root)
        dest_file = target_dir / rel_path
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        if compact:
            file_body = generate_skeleton(file_path)
            dest_file.write_text(file_body, encoding="utf-8")
        else:
            shutil.copy2(file_path, dest_file)
            try:
                file_body = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                file_body = ""
        toks = estimate_tokens(file_body)
        total_tokens += toks
        total_bytes += len(file_body.encode("utf-8"))
        index_entries.append((rel_path.as_posix(), len(file_body.splitlines()), toks))

    if compact:
        index_lines = [
            "# Project Skeleton Index (Token-Optimized)",
            "",
            "This context was generated in **compact skeleton mode** to conserve tokens.",
            "Files contain function and class signatures, docstrings, and line ranges.",
            "",
            "### Instructions for the AI:",
            "1. Formulate your modification plan.",
            "2. If you need the full verbatim content or exact lines of any file to construct precise `SEARCH` blocks, request them first:",
            "   ```code_exec",
            "   FETCH path/to/file.py",
            "   FETCH path/to/file.py:50-100",
            "   ```",
            "3. The user or runner will return the exact file contents in the next turn.",
            "",
            "### File Overview:",
            "| Path | Lines | Est. Tokens |",
            "| :--- | :--- | :--- |",
        ]
        for ipath, ilines, itoks in sorted(index_entries):
            index_lines.append(f"| `{ipath}` | {ilines} | ~{itoks:,} |")
        (target_dir / "PROJECT_INDEX.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")

    return {
        "location": f"{output_dirname}/",
        "included": len(included_files),
        "ignored": ignored_count,
        "tokens": total_tokens,
        "size_kb": round(total_bytes / 1024, 1),
        "ignore_file": used_ignore,
        "compact": compact,
    }
