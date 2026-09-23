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


def _extract_go_skeleton(rel_name: str, lines: list[str]) -> str | None:
    out_lines = [
        f"// Skeleton: {rel_name} ({len(lines)} lines)",
        f"// Use 'FETCH {rel_name}:<start>-<end>' to inspect full implementation\n",
    ]
    curr_doc: list[str] = []
    in_import_block = False
    in_const_var_block = False
    import_count = 0

    fn_pattern = re.compile(r"^func\s+(?:\([^)]+\)\s+)?([A-Za-z0-9_]+)\s*\([^)]*\)")
    type_pattern = re.compile(r"^type\s+([A-Za-z0-9_]+)\s+(struct|interface|[A-Za-z0-9_*\[\]]+)")

    for idx, ln in enumerate(lines, 1):
        raw_stripped = ln.strip()

        if ln.startswith("//"):
            curr_doc.append(raw_stripped)
            continue

        if ln.startswith("package "):
            out_lines.append(ln.rstrip())
            curr_doc.clear()
            continue

        if ln.startswith("import ("):
            in_import_block = True
            out_lines.append("import ( ... )")
            curr_doc.clear()
            continue
        if in_import_block:
            if ln.startswith(")"):
                in_import_block = False
            continue
        if ln.startswith("import "):
            if import_count == 0:
                out_lines.append(ln.rstrip())
            import_count += 1
            curr_doc.clear()
            continue

        if ln.startswith("const (") or ln.startswith("var ("):
            in_const_var_block = True
            out_lines.append(f"{ln.split()[0]} ( ... )")
            curr_doc.clear()
            continue
        if in_const_var_block:
            if ln.startswith(")"):
                in_const_var_block = False
            continue

        if ln and not ln[0].isspace():
            m_fn = fn_pattern.match(ln)
            if m_fn:
                if curr_doc:
                    out_lines.extend(curr_doc[-4:])
                    curr_doc.clear()
                sig = ln.split("{")[0].rstrip()
                out_lines.append(f"{sig} ... (line {idx})")
                continue

            m_type = type_pattern.match(ln)
            if m_type:
                if curr_doc:
                    out_lines.extend(curr_doc[-4:])
                    curr_doc.clear()
                sig = ln.split("{")[0].rstrip()
                out_lines.append(f"{sig} ... (line {idx})")
                continue

            if ln.startswith("const ") or ln.startswith("var "):
                out_lines.append(f"{ln.rstrip()} (line {idx})")
                curr_doc.clear()
                continue

        if raw_stripped:
            curr_doc.clear()

    if len(out_lines) > 2:
        return "\n".join(out_lines) + f"\n\n// ... [{len(lines)} total lines]\n"
    return None


def _extract_rust_skeleton(rel_name: str, lines: list[str]) -> str | None:
    out_lines = [
        f"// Skeleton: {rel_name} ({len(lines)} lines)",
        f"// Use 'FETCH {rel_name}:<start>-<end>' to inspect full implementation\n",
    ]
    curr_doc: list[str] = []

    decl_pattern = re.compile(
        r"^(?:pub(?:\([^)]+\))?\s+)?(?:async\s+)?(?:unsafe\s+)?(fn|struct|enum|trait|type|impl|mod|const|static)\s+([A-Za-z0-9_]+)?"
    )

    for idx, ln in enumerate(lines, 1):
        raw_stripped = ln.strip()

        if ln.startswith("///") or ln.startswith("//!"):
            curr_doc.append(raw_stripped)
            continue
        if ln.startswith("#["):
            curr_doc.append(raw_stripped)
            continue

        if ln.startswith("use ") or (ln.startswith("pub use ") and ";" in ln):
            out_lines.append(raw_stripped)
            curr_doc.clear()
            continue

        if ln and not ln[0].isspace():
            m = decl_pattern.match(ln)
            if m:
                if curr_doc:
                    out_lines.extend(curr_doc[-4:])
                    curr_doc.clear()
                sig = ln.split("{")[0].split(";")[0].rstrip()
                out_lines.append(f"{sig} ... (line {idx})")
                continue

        if raw_stripped:
            curr_doc.clear()

    if len(out_lines) > 2:
        return "\n".join(out_lines) + f"\n\n// ... [{len(lines)} total lines]\n"
    return None


def _extract_jvm_csharp_skeleton(rel_name: str, lines: list[str], suffix: str) -> str | None:
    out_lines = [
        f"// Skeleton: {rel_name} ({len(lines)} lines)",
        f"// Use 'FETCH {rel_name}:<start>-<end>' to inspect full implementation\n",
    ]
    curr_doc: list[str] = []
    in_block_doc = False

    class_pattern = re.compile(
        r"^(?:(?:public|protected|private|internal|abstract|final|sealed|static|open|data|value)\s+)*(class|interface|enum|record|object|trait|struct)\s+([A-Za-z0-9_]+)"
    )
    method_pattern = re.compile(
        r"^(?:(?:public|protected|private|internal|abstract|final|static|override|virtual|suspend|async|inline)\s+)*(?:fun\s+([A-Za-z0-9_]+)|(?:[A-Za-z0-9_<>,\[\]?]+)\s+([A-Za-z0-9_]+)\s*\([^)]*\))"
    )

    for idx, ln in enumerate(lines, 1):
        raw_stripped = ln.strip()

        if ln.startswith("/**") or (in_block_doc and ln.startswith(" *")):
            in_block_doc = True
            curr_doc.append(raw_stripped)
            continue
        if in_block_doc and (ln.startswith(" */") or raw_stripped.endswith("*/")):
            curr_doc.append(raw_stripped)
            in_block_doc = False
            continue
        if ln.startswith("///"):
            curr_doc.append(raw_stripped)
            continue

        if ln.startswith("package ") or ln.startswith("namespace ") or ln.startswith("import ") or ln.startswith("using "):
            out_lines.append(raw_stripped)
            curr_doc.clear()
            continue

        if ln.startswith("@") or (suffix == ".cs" and ln.startswith("[") and ln.endswith("]")):
            curr_doc.append(raw_stripped)
            continue

        if ln and (not ln[0].isspace() or ln.startswith("    ") or ln.startswith("\t")):
            indent = "    " if (ln.startswith("    ") or ln.startswith("\t")) else ""
            m_class = class_pattern.match(raw_stripped)
            if m_class:
                if curr_doc:
                    out_lines.extend(curr_doc[-4:])
                    curr_doc.clear()
                sig = raw_stripped.split("{")[0].rstrip()
                out_lines.append(f"{indent}{sig} ... (line {idx})")
                continue

            m_method = method_pattern.match(raw_stripped)
            if m_method and ("(" in raw_stripped and ")" in raw_stripped):
                if curr_doc:
                    out_lines.extend(curr_doc[-2:])
                    curr_doc.clear()
                sig = raw_stripped.split("{")[0].split(";")[0].rstrip()
                out_lines.append(f"{indent}{sig} ... (line {idx})")
                continue

        if raw_stripped and not in_block_doc:
            curr_doc.clear()

    if len(out_lines) > 2:
        return "\n".join(out_lines) + f"\n\n// ... [{len(lines)} total lines]\n"
    return None


def _extract_c_cpp_skeleton(rel_name: str, lines: list[str]) -> str | None:
    out_lines = [
        f"// Skeleton: {rel_name} ({len(lines)} lines)",
        f"// Use 'FETCH {rel_name}:<start>-<end>' to inspect full implementation\n",
    ]
    curr_doc: list[str] = []

    type_pattern = re.compile(r"^(?:typedef\s+)?(struct|class|enum|union)\s+([A-Za-z0-9_]+)?")
    func_pattern = re.compile(r"^(?:[A-Za-z0-9_*&:]+\s+)+([A-Za-z0-9_:]+)\s*\([^)]*\)\s*(?:const)?\s*(?:noexcept)?\s*[{;]?")

    for idx, ln in enumerate(lines, 1):
        raw_stripped = ln.strip()

        if ln.startswith("//") or ln.startswith("/*") or ln.startswith(" *"):
            curr_doc.append(raw_stripped)
            continue

        if ln.startswith("#include") or ln.startswith("#define"):
            out_lines.append(raw_stripped)
            curr_doc.clear()
            continue

        if ln and not ln[0].isspace():
            m_type = type_pattern.match(ln)
            if m_type:
                if curr_doc:
                    out_lines.extend(curr_doc[-4:])
                    curr_doc.clear()
                sig = ln.split("{")[0].rstrip()
                out_lines.append(f"{sig} ... (line {idx})")
                continue

            m_func = func_pattern.match(ln)
            if m_func and ("(" in ln and ")" in ln):
                if curr_doc:
                    out_lines.extend(curr_doc[-3:])
                    curr_doc.clear()
                sig = ln.split("{")[0].split(";")[0].rstrip()
                out_lines.append(f"{sig} ... (line {idx})")
                continue

        if raw_stripped:
            curr_doc.clear()

    if len(out_lines) > 2:
        return "\n".join(out_lines) + f"\n\n// ... [{len(lines)} total lines]\n"
    return None


def _extract_ruby_skeleton(rel_name: str, lines: list[str]) -> str | None:
    out_lines = [
        f"# Skeleton: {rel_name} ({len(lines)} lines)",
        f"# Use 'FETCH {rel_name}:<start>-<end>' to inspect full implementation\n",
    ]
    curr_doc: list[str] = []

    for idx, ln in enumerate(lines, 1):
        raw_stripped = ln.strip()

        if ln.startswith("#"):
            curr_doc.append(raw_stripped)
            continue

        if ln.startswith("require ") or ln.startswith("require_relative "):
            out_lines.append(raw_stripped)
            curr_doc.clear()
            continue

        if ln and (not ln[0].isspace() or ln.startswith("  ") or ln.startswith("\t")):
            indent = "  " if (ln.startswith("  ") or ln.startswith("\t")) else ""
            if raw_stripped.startswith("class ") or raw_stripped.startswith("module "):
                if curr_doc:
                    out_lines.extend(curr_doc[-3:])
                    curr_doc.clear()
                out_lines.append(f"{indent}{raw_stripped} ... (line {idx})")
                continue
            if raw_stripped.startswith("def "):
                if curr_doc:
                    out_lines.extend(curr_doc[-2:])
                    curr_doc.clear()
                out_lines.append(f"{indent}{raw_stripped} ... (line {idx})")
                continue

        if raw_stripped:
            curr_doc.clear()

    if len(out_lines) > 2:
        return "\n".join(out_lines) + f"\n\n# ... [{len(lines)} total lines]\n"
    return None


def _extract_php_skeleton(rel_name: str, lines: list[str]) -> str | None:
    out_lines = [
        f"// Skeleton: {rel_name} ({len(lines)} lines)",
        f"// Use 'FETCH {rel_name}:<start>-<end>' to inspect full implementation\n",
    ]
    curr_doc: list[str] = []

    decl_pattern = re.compile(r"^(?:(?:final|abstract|readonly)\s+)?(class|interface|trait|enum)\s+([A-Za-z0-9_]+)")
    func_pattern = re.compile(r"^(?:(?:public|protected|private|static|abstract|final)\s+)*function\s+([A-Za-z0-9_]+)\s*\([^)]*\)")

    for idx, ln in enumerate(lines, 1):
        raw_stripped = ln.strip()

        if ln.startswith("//") or ln.startswith("/*") or ln.startswith(" *") or ln.startswith("#"):
            curr_doc.append(raw_stripped)
            continue

        if ln.startswith("<?php") or ln.startswith("namespace ") or ln.startswith("use "):
            out_lines.append(raw_stripped)
            curr_doc.clear()
            continue

        if ln and (not ln[0].isspace() or ln.startswith("    ") or ln.startswith("\t")):
            indent = "    " if (ln.startswith("    ") or ln.startswith("\t")) else ""
            m_decl = decl_pattern.match(raw_stripped)
            if m_decl:
                if curr_doc:
                    out_lines.extend(curr_doc[-3:])
                    curr_doc.clear()
                sig = raw_stripped.split("{")[0].rstrip()
                out_lines.append(f"{indent}{sig} ... (line {idx})")
                continue

            m_func = func_pattern.match(raw_stripped)
            if m_func:
                if curr_doc:
                    out_lines.extend(curr_doc[-2:])
                    curr_doc.clear()
                sig = raw_stripped.split("{")[0].split(";")[0].rstrip()
                out_lines.append(f"{indent}{sig} ... (line {idx})")
                continue

        if raw_stripped:
            curr_doc.clear()

    if len(out_lines) > 2:
        return "\n".join(out_lines) + f"\n\n// ... [{len(lines)} total lines]\n"
    return None


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

    # Go: package, types, funcs, methods
    if suffix == ".go":
        res = _extract_go_skeleton(rel_name, lines)
        if res:
            return res

    # Rust: use, structs, enums, traits, impls, fns
    if suffix == ".rs":
        res = _extract_rust_skeleton(rel_name, lines)
        if res:
            return res

    # Java, Kotlin, C#
    if suffix in {".java", ".kt", ".kts", ".cs"}:
        res = _extract_jvm_csharp_skeleton(rel_name, lines, suffix)
        if res:
            return res

    # C, C++
    if suffix in {".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hxx"}:
        res = _extract_c_cpp_skeleton(rel_name, lines)
        if res:
            return res

    # Ruby
    if suffix == ".rb":
        res = _extract_ruby_skeleton(rel_name, lines)
        if res:
            return res

    # PHP
    if suffix == ".php":
        res = _extract_php_skeleton(rel_name, lines)
        if res:
            return res

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


def ensure_gitignore_entry(root: Path, entry: str) -> bool:
    """Ensures an entry (e.g. 'context/' or 'PROJECT_CONTEXT.md') is present in the project's .gitignore."""
    gitignore_path = root / ".gitignore"
    clean_entry = entry.strip()
    is_file = clean_entry.endswith(".md") or (root / clean_entry).is_file()
    clean_formatted = clean_entry.rstrip("/") if is_file else (clean_entry.rstrip("/") + "/")
    entry_variants = {clean_entry, clean_formatted, "/" + clean_entry, "/" + clean_formatted}

    if gitignore_path.is_file():
        try:
            content = gitignore_path.read_text(encoding="utf-8", errors="replace")
            for line in content.splitlines():
                if line.strip() in entry_variants:
                    return False  # Already present
            # Append entry cleanly
            delimiter = "" if content.endswith("\n") or not content else "\n"
            gitignore_path.write_text(f"{content}{delimiter}{clean_formatted}\n", encoding="utf-8")
            return True
        except OSError:
            return False
    elif (root / ".git").exists():
        # Git repository exists but no .gitignore yet: create one
        try:
            gitignore_path.write_text(f"{clean_formatted}\n", encoding="utf-8")
            return True
        except OSError:
            return False
    return False


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

    # Automatically ensure both .code_exec/ and the context folder are ignored in git
    ensure_gitignore_entry(root, ".code_exec")
    ensure_gitignore_entry(root, output_dirname)

    patterns, used_ignore = load_ignore_patterns(root, ignore_filename)

    # 1. Immediately wipe any existing context folder and bundle file first to avoid scanning old exports
    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)
    stale_bundle = root / "PROJECT_CONTEXT.md"
    if stale_bundle.exists():
        try:
            stale_bundle.unlink()
        except OSError:
            pass
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


def create_plan_bundle(
    plan_text: str | None = None,
    output_file: str = "PROJECT_CONTEXT.md",
    ignore_filename: str | None = ".code-exec-ignore",
    root: Path | None = None,
    export_all: bool = True,
    compact: bool = True,
) -> dict[str, int | str | bool]:
    """
    Creates a single consolidated markdown bundle (PROJECT_CONTEXT.md) containing
    a directory outline, compact file skeletons, and AI instructions.
    Ideal for single-file drag-and-drop into chat LLMs (Claude, ChatGPT, etc.).
    """
    if root is None:
        root = Path.cwd().resolve()
    target_path = root / output_file

    ensure_gitignore_entry(root, ".code_exec")
    ensure_gitignore_entry(root, output_file)

    patterns, used_ignore = load_ignore_patterns(root, ignore_filename)

    # 1. Immediately wipe any existing context folder and target bundle file to avoid scanning old exports
    context_dir = root / "context"
    if context_dir.exists():
        shutil.rmtree(context_dir, ignore_errors=True)
    if target_path.exists():
        try:
            target_path.unlink()
        except OSError:
            pass

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
            try:
                if p.resolve() == target_path.resolve():
                    continue
            except (OSError, RuntimeError):
                pass
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

    included_files.sort(key=lambda p: p.relative_to(root).as_posix())

    bundle_sections = [
        f"# Project Context: {root.name}",
        "",
        "> Auto-generated by `code-exec export-context --bundle`.",
        "> When generating code changes, respond with a single fenced ````code_exec```` block.",
        "",
        "## File Index",
        "",
        "| Path | Mode |",
        "| :--- | :--- |",
    ]

    for fp in included_files:
        rel_posix = fp.relative_to(root).as_posix()
        mode_str = "Skeleton" if compact else "Full"
        bundle_sections.append(f"| `{rel_posix}` | {mode_str} |")

    bundle_sections.append("")
    bundle_sections.append("---")
    bundle_sections.append("")

    for fp in included_files:
        rel_posix = fp.relative_to(root).as_posix()
        if compact:
            body = generate_skeleton(fp)
        else:
            try:
                body = fp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                body = ""

        fence = "```"
        ext = fp.suffix.lstrip(".").lower() or "text"
        bundle_sections.append(f"### File: `{rel_posix}`")
        bundle_sections.append(f"{fence}{ext}\n{body}\n{fence}")
        bundle_sections.append("")

    content = "\n".join(bundle_sections)
    target_path.write_text(content, encoding="utf-8")
    total_tokens = estimate_tokens(content)
    total_bytes = len(content.encode("utf-8"))

    return {
        "location": output_file,
        "included": len(included_files),
        "ignored": ignored_count,
        "tokens": total_tokens,
        "size_kb": round(total_bytes / 1024, 1),
        "ignore_file": used_ignore,
        "compact": compact,
        "bundle": True,
    }

