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
- **Atomic Consistency Checks**: Catches conflicting or duplicate file modifications in a plan before touching disk (e.g., duplicate `CREATE` commands or `EDIT` after `DELETE`).
- **Interactive TUI & Diagnostic Cards**: Claude Code-inspired typography, responsive boxed menus, colorized diff visualizer, and machine-readable error cards (`ERR|...`).
- **Tolerant Search & Replace**: Multi-tier matching handles indentation variations, collapsed whitespace, CRLF vs LF, and JSX tag formatting without newline drift.
- **Scoped Git Commits**: Automatically stages and commits *only* the specific files modified by the plan, leaving other untracked or modified files in your repo untouched.

---

## Installation & Global Setup

### Recommended: Install via pipx / uv (Standard Packaging)
`code-exec` provides a standard `pyproject.toml` package configuration. You can install it globally with:

```bash
# Using pipx (isolated global CLI)
pipx install .

# Or using uv
uv tool install .

# Or editable development install
pip install -e .
```

### Alternative: Shell Launcher Scripts

#### macOS
Create a launcher in `/usr/local/bin` (replace `/path/to/code_exec` with the absolute path to your repo):
```zsh
sudo tee /usr/local/bin/code-exec << 'EOF'
#!/usr/bin/env zsh
exec python3 "/path/to/code_exec/code_exec.py" "$@"
EOF
sudo chmod +x /usr/local/bin/code-exec
```

#### Option 2: Shell Alias
Add to your `~/.zshrc` or `~/.bashrc`:
```zsh
alias code-exec='python3 "/path/to/code_exec/code_exec.py"'
```
Then reload: `source ~/.zshrc`.

---

### Ubuntu / Debian Linux

1. Ensure clipboard support is installed for your display server:
   ```bash
   # For X11:
   sudo apt install -y xclip
   # Or for Wayland:
   sudo apt install -y wl-clipboard
   ```

2. Create a global launcher script:
   ```bash
   sudo tee /usr/local/bin/code-exec << 'EOF'
   #!/usr/bin/env bash
   exec python3 "/path/to/code_exec/code_exec.py" "$@"
   EOF

   sudo chmod +x /usr/local/bin/code-exec
   ```

   *(Replace `/path/to/code_exec` with the absolute path to your cloned repository).*

---

### Windows

#### Option 1: PowerShell Profile Function (Recommended)
1. Open your PowerShell profile (`notepad $PROFILE`).
2. Add the following function:
   ```powershell
   function code-exec {
       python "C:\path\to\code_exec\code_exec.py" @args
   }
   ```
3. Save the file and restart PowerShell or run `. $PROFILE`.

#### Option 2: CMD / Batch Wrapper
Create a file named `code-exec.bat` inside a folder that is in your system `PATH` (such as `C:\Windows` or a dedicated `C:\bin` directory):
```cmd
@echo off
python "C:\path\to\code_exec\code_exec.py" %*
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

Plans start with a `THINK` block explaining the change and contain one or more operations followed by an optional `COMMIT`:

| Command | Syntax | Description |
| :--- | :--- | :--- |
| `CREATE` | `CREATE path <<< content >>>` | Creates a new file. Fails if the file already exists. |
| `EDIT` | `EDIT path SEARCH <<<...>>> REPLACE <<<...>>>` | Surgically replaces a unique code anchor. |
| `DELETE` | `DELETE path` | Removes a file or directory safely. |
| `MOVE` | `MOVE src -> dst` | Moves a file or directory. |
| `COPY` | `COPY src -> dst` | Copies a file or folder. |
| `RENAME` | `RENAME src -> dst` | Renames a file or folder. |
| `MKDIR` | `MKDIR path` | Creates a directory path recursively. |
| `APPEND` | `APPEND path <<< content >>>` | Appends content to the end of a file. |
| `PREPEND` | `PREPEND path <<< content >>>` | Prepends content to the start of a file. |
| `INSERT_BEFORE` | `INSERT_BEFORE path MARKER <<<...>>> CONTENT <<<...>>>` | Inserts content immediately before an anchor. |
| `INSERT_AFTER` | `INSERT_AFTER path MARKER <<<...>>> CONTENT <<<...>>>` | Inserts content immediately after an anchor. |
| `RUN` | `RUN shell command` | Runs a whitelisted or user-approved sandboxed command. |
| `COMMIT` | `COMMIT message` | Scoped commit staging only modified files. |

> **Note on Block Delimiters**: `<<<` can be placed either on its own line or inline immediately after the instruction/keyword (e.g. `CREATE path <<<`).

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
