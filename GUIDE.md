# code-exec Beginner's Guide

`code-exec` is a deterministic, sandboxed local code executor that turns conversational AI replies into verified file operations without requiring paid IDE plugins or autonomous subscriptions.

---

## 4-Step Core Workflow

```text
 1. Copy Prompt           2. Export Context          3. Chat with AI          4. Run code-exec
 ┌──────────────┐       ┌──────────────────────┐   ┌───────────────────┐    ┌────────────────┐
 │ code-exec -p │ ────> │  code-exec export-   │ ─>│ Paste context +   │ ──>│   code-exec    │
 │ (to clipboard│       │  context --compact   │   │ prompt in AI chat │    │  (apply plan)  │
 └──────────────┘       └──────────────────────┘   └───────────────────┘    └────────────────┘
```

1. **Copy the AI instructions** to your clipboard:
   ```bash
   code-exec -p
   ```
2. **Export the clean, token-optimized context**:
   ```bash
   code-exec export-context --compact
   ```
   *(Creates a lightweight `context/` folder containing structural skeletons and `PROJECT_INDEX.md`)*
3. **Send to your AI model**:
   Paste the instructions and attach/upload the generated `context/` folder, then request your feature or bugfix.
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
| Simulate without modifying files | `code-exec --dry-run` |
| Preview diff before confirmation | `code-exec apply --diff` |
| Revert the previous run | `code-exec undo` |
| Copy AI instructions with file tree | `code-exec -p --with-tree` |
| Generate Git commit prompt | `code-exec -c` |
| Read plan from file or stdin | `code-exec --file plan.txt` |
| Check syntax only (no disk reads) | `code-exec --check` |
