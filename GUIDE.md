# code-exec Beginner's Guide

`code-exec` is a deterministic, sandboxed local code executor that turns conversational AI replies into verified file operations without requiring paid IDE plugins or autonomous subscriptions.

---

## 4-Step Core Workflow

```text
 1. Export Context          2. Copy Prompt             3. Chat with AI          4. Run code-exec
 ┌──────────────────────┐   ┌──────────────────────┐   ┌───────────────────┐    ┌────────────────┐
 │  code-exec bundle    │─> │ code-exec -p         │─> │ Paste/attach into │ ──>│   code-exec    │
 │  (auto-copied! 📋)   │   │ (or -p --short)      │   │ AI chat (Cmd+V)   │    │  (apply plan)  │
 └──────────────────────┘   └──────────────────────┘   └───────────────────┘    └────────────────┘
```

1. **Export the clean, token-optimized context**:
   ```bash
   # Single-file bundle (creates PROJECT_CONTEXT.md and auto-copies to clipboard!)
   code-exec bundle

   # Or export as a clean folder with structural skeletons
   code-exec export-context --compact
   ```
2. **Copy the AI instructions** to your clipboard:
   ```bash
   code-exec -p
   # Or for smaller/local models:
   code-exec -p --short
   ```
3. **Send to your AI model**:
   Paste the instructions and attach `PROJECT_CONTEXT.md` (or paste with `Cmd+V`) into chat (works with Claude, ChatGPT, Gemini, DeepSeek, and local LLMs), then ask for changes or features.
4. **Copy the AI's reply** and execute it:
   ```bash
   code-exec
   # or run directly:
   code-exec apply
   ```

---

## What Happens When You Run `code-exec`?

Before touching any files on disk, `code-exec` executes several layers of safeguards:

1. **Extraction**: Isolates the plan block and ignores chat greetings, explanations, and unrelated code.
2. **Preflight Simulation**: Simulates the plan in an in-memory virtual filesystem to catch path errors, conflicting writes, and invalid edits before touching disk.
3. **Diff Preview**: Lets you inspect the exact proposed file differences by pressing `v` (view diff) or `t` (view directory tree) before confirming.
4. **Automatic Rollback (`undo`)**: Backs up modified files under `.code_exec/backups/`. If an operation fails midway, changes roll back immediately. You can also revert the last run at any time via:
   ```bash
   code-exec undo
   ```

---

## Working with Large Projects (Token Saver)

To avoid sending entire multi-megabyte repositories to an LLM:

1. **Export a compact outline**:
   ```bash
   code-exec export-context --compact
   ```
   This generates lightweight skeletons with class/function signatures, saving 80–90% of prompt tokens.
2. **AI Requests Specific Files (`FETCH`)**:
   When the AI needs to inspect exact lines, it responds with:
   ```code_exec
   FETCH src/auth.py:20-60
   ```
3. **Load to Clipboard**:
   Run `code-exec`. It reads the requested range and copies it directly to your clipboard so you can paste it back to the AI.

---

## Common CLI Commands & Shortcuts

| Goal | Command |
| :--- | :--- |
| Interactive launcher menu | `code-exec` |
| Apply plan from clipboard | `code-exec apply` |
| Clipboard Watch Mode (real-time listener) | `code-exec watch` (or `-w`) |
| Install shell completions (Zsh, Bash, Fish) | `code-exec --install-completions` |
| Simulate without modifying files | `code-exec --dry-run` |
| Preview diff before confirmation | `code-exec apply --diff` |
| Revert the previous run | `code-exec undo` |
| Export single-file context bundle (auto-copies) | `code-exec bundle` (or `-b`) |
| Export context folder structure | `code-exec export-context` |
| Copy AI prompt (standard) | `code-exec -p` |
| Copy AI prompt (compact for small LLMs) | `code-exec -p --short` |
| Copy AI instructions with file tree | `code-exec -p --with-tree` |
| Generate Git commit prompt | `code-exec -c` |
| Read plan from file or stdin | `code-exec --file plan.txt` |
| Check syntax only (no disk reads) | `code-exec --check` |
| Target specific project directory | `code-exec -C /path/to/project` |
| Show version number | `code-exec -V` |

---

## Permanent AI Setup: Dedicated Gemini Gem & Custom GPT

Instead of copying instructions (`code-exec -p`) for every new conversation, you can create a dedicated custom persona in Gemini Web or ChatGPT. This permanently locks in the syntax rules so the AI never forgets them, even in long chats.

### Persona Configuration

* **Name**: `Code Exec Assistant`
* **Description**: `Expert full-stack coding assistant that outputs deterministic, executable code_exec plan blocks for instant local CLI execution.`
* **Instructions / System Prompt**:

````markdown
You are an expert software engineer designed to pair with the user's local deterministic coding CLI (`code-exec`).

Whenever you propose code changes, file creations, commands, or file deletions:
1. Provide your high-level thought process and explanation OUTSIDE the code block.
2. Put ALL executable file operations inside ONE single fenced `code_exec` block per response.

### Plan Block Syntax
Always use this exact fence:
```code_exec
COMMAND path [<<< content >>>]
```

### Supported Commands
- `CREATE path <<< content >>>` : Create a new file (fails if file already exists).
- `EDIT path` : Modify an existing file using SEARCH/REPLACE blocks.
- `DELETE path` : Delete a file.
- `FETCH path` or `FETCH path:start-end` or `FETCH path:symbol` : Request full file, line range, or function/class from the repository. Batch all FETCH commands in ONE block.
- `MOVE src -> dst` / `COPY src -> dst` / `RENAME src -> dst`
- `MKDIR path` / `TOUCH path` / `CHMOD path +x`
- `RUN cmd` : Whitelisted test runners and linters (`pytest`, `python3 -m unittest`, `npm test`, `cargo test`, `ruff`, etc.).
- `COMMIT type(scope): message` : Optional Git commit (must be the final line).

### EDIT Block Format (Always use verbatim code)
```code_exec
EDIT src/example.py
<<<<
exact lines from the file to replace
====
new replacement lines
>>>>
```

### Strict Rules to Avoid Errors
- **Never guess code you haven't seen**: If you need to inspect a file or function before making an edit, emit a `FETCH` block first.
- **One unified block**: Consolidate all file edits, creations, and commands into a single `code_exec` block rather than separate blocks per file.
- **Verbatim SEARCH anchors**: Include 3–6 exact, unique lines from the current file so the CLI matches unambiguously. Never use generic lines like `pass`, `return`, or lone brackets.
- **Project-relative paths**: Use clean relative paths (e.g., `src/app.py`). Never prefix with `/`, `~/`, or `../`.
- **No prose inside the block**: Keep comments and chat markdown out of the `code_exec` fence.
````

### How to Save in Your AI Web Interface

#### Google Gemini (Gemini Web)
1. Open [gemini.google.com](https://gemini.google.com).
2. In the left sidebar, click **Explore Gems** (or **Gem Manager**) → **New Gem**.
3. Paste the **Name**, **Description**, and **Instructions** from above.
4. Click **Save**. Open this Gem whenever working on your codebase.

#### OpenAI (ChatGPT Plus)
1. Open [chatgpt.com](https://chatgpt.com).
2. Click **Explore GPTs** → **Create** (top right) → **Configure**.
3. Paste the **Name**, **Description**, and **Instructions**.
4. Set access to **Only me** and click **Save**.

