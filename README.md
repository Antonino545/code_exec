# CODE-EXEC

A deterministic, atomic local code executor and guardrailed runtime designed for AI coding workflows.

`code-exec` provides an agentic execution workflow without paying for expensive autonomous coding subscriptions or API tokens. You can chat with any AI model or web interface (Claude, ChatGPT, Gemini, local LLMs), copy the AI's response to your clipboard, and let `code-exec` automatically isolate the plan, validate consistency, sandbox shell executions, apply changes, and commit files atomically.

```text
  ____ ___  ____  _____   _______  _______ ____ 
 / ___/ _ \|  _ \| ____| | ____\ \/ / ____/ ___|
| |  | | | | | | |  _|   |  _|  \  /|  _| | |   
| |__| |_| | |_| | |___  | |___ /  \| |___| |___
 \____\___/|____/|_____| |_____/_/\_\_____|\____|
  Local Deterministic Coding Agent  v1.2
```

---

## Key Highlights
- **Full AI Response Extraction**: Copy the *entire* conversational response. `code-exec` extracts the executable ````code_exec```` block and automatically ignores conversational prose, markdown headers, and unrelated code snippets.
- **Sandboxed Execution & Guardrails**: Whitelists safe verification runners (`pytest`, `unittest`, `npm test`, `cargo test`, `ruff`). Demands interactive confirmation for unvetted scripts or chained shell commands (`&&`, `;`, `|`), isolating unvetted runs with network restrictions (`sandbox-exec` on macOS, `unshare` on Linux).
- **Persistent Rollback (`undo`)**: Retains project-local transaction journals and file snapshots under `.code_exec/backups/`, enabling full rollback via `code-exec undo`.
- **Pre-Execution Diff Preview**: Inspect formatted unified diffs before confirming with `--diff` or by pressing `v` at the confirmation prompt.
- **Sensitive Credential Protection**: Blocks accidental modification or deletion of secrets, environment files, and CI workflows (`.env*`, `*.pem`, `*.key`, `id_rsa`, `.github/workflows/*`).
- **Atomic Consistency Checks & Identity Protection**: Catches conflicting or duplicate file modifications, self-nesting moves, and same-path operations before touching disk.
- **Interactive TUI & Visual Indicators**: Claude Code-inspired typography, responsive boxed menus, folder tree visualizer, colorized diff previewer, and live animated searching indicators during pattern matching.
- **Multi-Tier Intelligent Matcher**: 8-tier engine supporting whitespace/indentation tolerance, JSX normalization, token-aware structural matching for Python and JS/TS, fuzzy similarity matching, and 8-line boundary anchor fallback for oversized blocks.
- **Pattern-Based File Management**: Glob and regex pattern matching support for `MOVE` and `COPY` operations (e.g., `MOVE test* -> tests` or `MOVE regex:^temp_.* -> /tmp`).
- **Ambiguous Match Diagnostics**: Reports exact line numbers and candidate ranges when a search block matches multiple times, allowing immediate anchor refinement.
- **Scoped Git Commits**: Automatically stages and commits *only* the specific files modified by the plan, leaving other untracked or modified files in your repo untouched.

---

## Installation & Setup

### Automatic One-Line Installation

#### macOS & Linux
Run via `curl`:
```bash
curl -fsSL [https://raw.githubusercontent.com/antonino54/code_exec/main/install.sh](https://raw.githubusercontent.com/antonino54/code_exec/main/install.sh) | bash
```

Or from a local clone of this repository:
```bash
./install.sh
```

#### Windows (PowerShell)
Run via `irm`:
```powershell
irm [https://raw.githubusercontent.com/antonino54/code_exec/main/install.ps1](https://raw.githubusercontent.com/antonino54/code_exec/main/install.ps1) | iex
```

Or from a local clone:
```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

---

### Manual Package Installation

`code-exec` also provides a standard `pyproject.toml` configuration:

```bash
# Using pipx (isolated global CLI)
pipx install .

# Or using uv
uv tool install .

# Or editable local development install
pip install -e .
```

---

## Usage

### 1. Interactive Menu
When you run `code-exec` without arguments in an interactive shell, it launches the card menu:

```zsh
code-exec
```

```text
╭─ code-exec · /path/to/project ─────────────────────────────╮
│                                                            │
│  1. Apply plan from clipboard                              │
│  2. Dry-run validate plan from clipboard                   │
│  3. Copy AI prompt instructions to clipboard (-p)          │
│  4. Quick guide & syntax reference (-h)                    │
│  q. Exit                                                   │
│                                                            │
╰────────────────────────────────────────────────────────────╯
```

### 2. Direct CLI Execution
You can bypass the menu for instant automation:

```zsh
# Apply plan directly from clipboard
code-exec apply

# Inspect proposed unified diff before execution
code-exec apply --diff

# Revert the most recently applied plan
code-exec undo

# Copy git diff prompt to clipboard for AI commit generation
code-exec -c
# Or alias: code-exec docommit

# Validate plan without touching files
code-exec --dry-run

# Syntax and delimiter validation only (no filesystem checks)
code-exec --check

# Visual directory tree preview of proposed changes
code-exec --tree

# Apply immediately without confirmation prompt
code-exec --yes

# Read plan from a file instead of clipboard
code-exec --file plan.txt

# Pipe instructions via stdin
cat plan.txt | code-exec --file - --yes
```

### 3. Copy AI Prompt (`-p`)
Copy the system instructions from `code_exec_instructions.md` straight to your clipboard so you can paste (`⌘V`) them into any AI chat:

```zsh
code-exec -p
```

---

## Supported Plan Commands

The AI can analyze and explain its reasoning in plain text outside the code block. Inside the code block, list the operations followed by an optional `COMMIT`:

| Command | Syntax | Description |
| :--- | :--- | :--- |
| `CREATE` | `CREATE path <<< content >>>` | Creates a new file. Fails if the file already exists. |
| `EDIT` | `EDIT path SEARCH <<<...>>> REPLACE <<<...>>>` | Surgically replaces a unique code anchor. |
| `PATCH` | `PATCH path <<< unified diff >>>` | Applies unified diff with line-drift tolerance. |
| `DELETE` | `DELETE path` | Removes a file or directory safely. |
| `MOVE` | `MOVE src -> dst` | Moves a file or directory. Supports glob (`test*`) and regex (`regex:...`). |
| `COPY` | `COPY src -> dst` | Copies a file or folder. Supports glob (`test*`) and regex (`regex:...`). |
| `RENAME` | `RENAME src -> dst` | Renames a file or folder. |
| `MKDIR` | `MKDIR path` | Creates a directory path recursively. |
| `APPEND` | `APPEND path <<< content >>>` | Appends content to the end of a file. |
| `PREPEND` | `PREPEND path <<< content >>>` | Prepends content to the start of a file. |
| `INSERT_BEFORE` | `INSERT_BEFORE path MARKER <<<...>>> CONTENT <<<...>>>` | Inserts content immediately before an anchor. |
| `INSERT_AFTER` | `INSERT_AFTER path MARKER <<<...>>> CONTENT <<<...>>>` | Inserts content immediately after an anchor. |
| `FETCH` | `FETCH path[:start-end]` | Loads requested file or line range to clipboard for lazy context. |
| `RUN` | `RUN shell command` | Runs a whitelisted or user-approved sandboxed command. |
| `COMMIT` | `COMMIT message` | Scoped commit staging only modified files. |

> **Note on Block Delimiters**: `<<<` can be placed either on its own line or inline immediately after the instruction/keyword (e.g. `CREATE path <<<`).

---

## Intelligent Matching Engine

The search and replace engine (`code_exec_matcher.py`) executes through an 8-tier fallback pipeline to handle formatting drift without false positives:

1. **Exact Substring Match**: Verifies byte-for-byte fidelity.
2. **Trailing Whitespace / CRLF Tolerance**: Normalizes line-end discrepancies and trailing space.
3. **Indentation Tolerance**: Strips common leading indentation when code blocks are shifted inside functions or classes.
4. **Harmless Whitespace Collapsing**: Collapses internal runs of spaces and tabs.
5. **JSX / HTML Normalization**: Matches attributes with mixed quotes (`'` vs `"`), whitespace variations, and self-closing tags (`/>`).
6. **Token-Aware Structural Matching**:
   - **Python**: Normalizes string quotes, ignores comments, and drops optional trailing commas in lists/tuples.
   - **JS / TS**: Normalizes quotes, removes comments, and handles optional trailing commas before brackets.
7. **High-Similarity Fuzzy Matching**: Employs structural sequence matching ($\ge 90\%$ threshold) to overcome minor punctuation drift.
8. **Boundary Anchor Fallback (Token Saver)**: Resolves oversized search blocks ($>60$ lines) by anchoring on the first 4 and last 4 lines of the block, bypassing intermediate body drift.

---

## Security & Guardrails

`code-exec` incorporates multi-layered execution safeguards:

1. **Command Whitelisting**:
   - Verification tools (`python3 -m unittest`, `pytest`, `npm test`, `cargo test`, `ruff`, `black`, `git status`, etc.) execute cleanly.
   - Generic or unvetted scripts (`python script.py`, `node app.js`, `bash`) require explicit interactive confirmation (`[y/N]`).
   - Destructive patterns (`rm -rf`, `sudo`, `curl | sh`, inline execution flags like `-c` or `-i`) are strictly blocked.

2. **Network Sandboxing**:
   - Unvetted script runs are wrapped inside OS sandboxes to drop outbound network access (`sandbox-exec -p '(version 1) (allow default) (deny network-outbound)'` on macOS, and `unshare -r -n` on Linux).

3. **Protected Paths**:
   - Plans are forbidden from touching `.git`, the project root, environment files (`.env*`), private keys (`*.key`, `*.pem`, `id_rsa`), and CI workflows (`.github/workflows/*`).

4. **Multi-File Consistency Verification**:
   - `preflight()` statically inspects plans for conflicting sequences (e.g. duplicate creations or editing after deletion) before any changes touch your disk.

---

## Color Themes

`code-exec` supports custom color themes via environment variables or a configuration file.

### Built-in Themes
- `claude` (Default Anthropic Terracotta `#D97757` and sage green)
- `catppuccin` (Mocha pastel tones)
- `tokyo-night` (Modern dark blue/violet)

### Switching Themes

Per command:
```zsh
CODE_EXEC_THEME=catppuccin code-exec
```

Persisted globally:
```zsh
mkdir -p ~/.config/code_exec
echo "tokyo-night" > ~/.config/code_exec/theme
```
