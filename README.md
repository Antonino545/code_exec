# code-exec 🐾

A deterministic, atomic local code executor designed for AI coding workflows.

`code-exec` provides a practical solution to avoid manually copying and pasting multiple code snippets, file changes, and diffs from web chatboxes, without requiring expensive autonomous coding agent subscriptions or API tokens. You chat with whatever model or web interface you prefer, copy the generated plan block with a single click, and let `code-exec` validate, apply, and commit the changes atomically.

---

## Features

- **Clipboard-First Workflow**: Copy an AI execution plan (`Ctrl+C` / `⌘C`) and apply it instantly in your project directory.
- **Cost-Effective & Autonomous-Free**: Get agentic execution locally without paying for expensive AI agent subscriptions or API credit burn.
- **Interactive Launcher Menu**: Simply run `code-exec` to choose between applying, dry-running, copying the AI prompt, or viewing the guide.
- **Atomic Operations & Rollback**: File operations are preflighted and backed up. If a validation step fails, all file modifications are safely rolled back.
- **Tolerant Search & Replace**: Matches local anchors even with harmless indentation, quote variations, or emoji/unicode drift.
- **Scoped Git Commits**: Uses the `COMMIT` instruction to prompt and stage *only* the files touched by the plan, keeping your working tree clean.

---

## Installation & Global Setup

### macOS

#### Option 1: Global Launcher Script (Recommended)
```zsh
sudo tee /usr/local/bin/code-exec << 'EOF'
#!/usr/bin/env zsh
exec python3 "/Users/antonino54/Documents/Project/Personal Project/code_exec/code_exec.py" "$@"
EOF

sudo chmod +x /usr/local/bin/code-exec
```

#### Option 2: Zsh Alias
Add to your `~/.zshrc`:
```zsh
alias code-exec='python3 "/Users/antonino54/Documents/Project/Personal Project/code_exec/code_exec.py"'
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

Every plan starts with a `THINK` block explaining the rationale and ends with file operations and an optional `COMMIT` command:

| Command | Syntax | Description |
| :--- | :--- | :--- |
| `CREATE` | `CREATE path <<< content >>>` | Creates a new file. Fails if the file already exists. |
| `EDIT` | `EDIT path SEARCH <<<...>>> REPLACE <<<...>>>` | Surgically replaces a unique code anchor. |
| `DELETE` | `DELETE path` | Removes a file or directory safely. |
| `MOVE` | `MOVE src -> dst` | Moves/renames a file or directory. |
| `COPY` | `COPY src -> dst` | Copies a file or folder. |
| `MKDIR` | `MKDIR path` | Creates a directory recursively. |
| `RUN` | `RUN shell command` | Runs a shell command inside the project root. |
| `COMMIT` | `COMMIT message` | Prompts to git commit only the modified files. |

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
