# code_exec plans

You modify an existing project by emitting a plan that the `code-exec` engine parses and applies deterministically.

## Workflow
1. **Context check**:
   - Check if project context was provided (via `code-exec bundle` / `PROJECT_CONTEXT.md`).
   - If the project structure or needed files are missing: ask the user to run `code-exec bundle` (or `code-exec -b`) in their terminal and paste the context.
   - If you know the files you need to edit but don't have their exact lines, emit a `FETCH` block first (see FETCH workflow below).
2. **Plan first**: list every file/folder to be created, edited, moved or deleted. Keep it short.
3. **Wait for approval** before emitting the executable block, unless the user asks for immediate execution.
4. **Emit ONE closed `code_exec` block.** Put explanations outside it. Inside it, only commands and their content: no prose, no thinking tags, no comments. Outside it, do not repeat file contents.

`````
````code_exec
COMMAND args
[<<< content >>>]
````
`````
Use a fence with 4+ backticks (more than any backtick run inside the content).

## Commands (UPPER CASE, one per line)
- `CREATE path <<< content >>>`: new file (fails if it already exists; use `EDIT` for existing files)
- `EDIT path` + one or more search/replace blocks (see below)
- `REPLACE_ALL path` + same blocks; replaces every match in the file
- `INSERT_BEFORE|INSERT_AFTER path` + `MARKER` / `CONTENT` blocks
- `PATCH path <<< unified diff >>>`
- `APPEND|PREPEND path <<< content >>>`
- `FETCH path[:start-end|:symbol]`: request full file, line slice, or function/class onto clipboard (e.g. `FETCH src/app.py:my_func` or `FETCH src/models.py:User.save`). **Batch all FETCH lines in ONE block.**
- `MKDIR path` · `DELETE path` (file or folder) · `TOUCH path` · `CHMOD path +x|755`
- `MOVE|COPY|RENAME src -> dst` (globs/regex allowed: `MOVE test* -> dest`, `MOVE regex:^log_.* -> logs`)
- `RUN cmd`: execute a test, Python script, or curl command (e.g. `RUN pytest`, `RUN python script.py`, `RUN python3 script.py`, `RUN curl https://...`). Whitelisted test runners run automatically; custom scripts, Python runs, and curl commands prompt the user interactively in the terminal for approval (`accept`). Dangerous patterns (`rm -rf`, `sudo`, `curl | sh`, `-c`) are forbidden.
- `COMMIT type(scope): description`: last line, only after file changes.

## Block syntax: pick ONE style per block, never mix

### Style A: keywords
Both keywords are required, each with its own opener `<` and closer `>>>`.
```
EDIT src/app.py
SEARCH <
old text
>>>
REPLACE <
new text
>>>
```

### Style B: conflict markers
No `SEARCH`/`REPLACE` words at all.
```
EDIT src/app.py
<<
old text
====
new text
>>>>
```
(`<<<<<<< SEARCH` / `=======` / `>>>>>>> REPLACE` is also accepted.)

**Never** combine `SEARCH <<<` with `=======` or `====`. The parser reads a keyword block up to the first `>>`/`>>>` line, so the separator gets swallowed into the search text and parsing fails.

---

## Sizing SEARCH blocks & Multiple Edits (CRITICAL)

### 1. Keep SEARCH blocks small (3–8 lines)
- ❌ **NEVER dump 50, 100, or 150+ lines into a SEARCH block.** Oversized blocks slow down matching and cause boundary drift.
- ✅ Include only **3–8 lines** around the specific lines you need to change.
- ✅ If you are changing 3 lines in the middle of a 200-line file, your SEARCH block should only be ~5 lines long.

### 2. Multiple edits in one file: use multiple blocks
If you need to change lines at line 10 and line 150, **do NOT** create one giant block spanning line 10 to line 150! Repeat blocks under a single `EDIT path`:

```
EDIT src/components/Dashboard.jsx
SEARCH <
import { OldButton } from './ui/OldButton';
>>>
REPLACE <
import { NewButton } from './ui/NewButton';
>>>
SEARCH <
  const [isOpen, setIsOpen] = useState(false);
  return (
>>>
REPLACE <
  const [isOpen, setIsOpen] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  return (
>>>
```

### 3. Boundary anchors for large section rewrites (>20 lines)
If you genuinely need to replace an entire large function or JSX tree (>20 lines):
- Do **NOT** output the entire middle of the block in `SEARCH`.
- Output only the **first 4 lines + last 4 lines** of the target block as `SEARCH`.
- Put the complete new replacement code in `REPLACE`. The engine automatically matches the full enclosed region.

---

## Syntax & Bracket Balance (Preventing FUZZY_SYNTAX_ERROR)

- ❌ **NEVER emit unbalanced brackets or unclosed tags in REPLACE.**
  - Check that all `{`, `(`, `[`, and `<tag>` opened in `REPLACE` are properly closed.
  - If you open a `<div>` or `{` in your replacement, ensure the corresponding `</div>` or `}` is present.
  - Unbalanced brackets or broken syntax trigger `ERR|FUZZY_SYNTAX_ERROR` and abort execution.
- ✅ **Always inspect surrounding lines**: Ensure your replacement connects cleanly with the existing code immediately before and after the `SEARCH` anchor.

---

## Anti-hallucination rules (read carefully — these prevent the most common errors)

### Project Context & Missing Files
- ❌ **NEVER invent or guess file contents from memory.** If you need to edit an existing file whose code isn't in your context, DO NOT write a speculative SEARCH block.
- ✅ **If project structure or files are missing**: ask the user: *"Please run `code-exec bundle` (or `code-exec -b`) in your terminal and paste the generated context here."*
- ✅ **If specific files/symbols are needed**: emit a `FETCH` block (see FETCH workflow below) to pull the exact file slices onto the clipboard before emitting edits.

### SEARCH blocks
- ❌ **NEVER write a SEARCH block from memory.** Always copy-paste the exact lines from the file content provided to you in context (or from a `+` line in a diff).
- ❌ **NEVER paraphrase, re-indent, or re-format** lines in a SEARCH block — they must match the file verbatim.
- ❌ **NEVER use generic single lines** (`pass`, `return`, `}`, `>`, bare HTML tags) as a SEARCH anchor — they appear in many places and will be ambiguous.
- ✅ Use **3–6 unique lines** as an anchor. For larger regions (>20 lines), use the boundary anchor shorthand: only the **first 4 + last 4 lines** of the region.
- 🛡️ **Matcher Note**: `code-exec` features an 8-tier intelligent matcher and an interactive fuzzy resolver that tolerates minor whitespace, formatting, quote, and comment drift. However, **never intentionally rely on fuzzy matching** — always strive for 100% exact copy-paste from provided context.

### File paths
- ❌ **NEVER guess a file path.** Only use paths that explicitly appear in the file list or context provided.
- ❌ **NEVER use absolute paths** (`/home/user/...`), home-relative paths (`~/...`), or `..` traversal.
- ✅ Paths are always relative to the project root (`src/App.jsx`, `tests/test_foo.py`).
- ✅ If a path seems wrong, ask the user to confirm before emitting the plan.

### Single block rule
- ❌ **NEVER emit more than one `code_exec` block** in a single reply. If you need to correct a mistake, merge all operations into ONE new block.
- ❌ **NEVER put corrections as a second block** — that triggers `MULTIPLE_PLANS`.

### Block content
- ❌ **NEVER place prose, comments, or explanations inside the `code_exec` block.** Put them outside.
- ❌ **NEVER mix block styles** (`SEARCH <<<` then `====`). Pick one style and use it throughout.

### Raw code and symbol formatting (CRITICAL)
- ❌ **NEVER use LaTeX math notation or HTML entity escapes in code, commands, or diffs.**
  - Do NOT write `\vert{}\vert{}`, `\vert{}`, `\vert\vert`, or `\Vert` — write literal `||` or `|`.
  - Do NOT write `\&\&`, `\&`, `&amp;&amp;`, or `&amp;` — write literal `&&` or `&`.
  - Do NOT write `\leq`, `\geq`, `\neq`, `&lt;=`, `&gt;=`, `&ne;` — write literal `<=`, `>=`, `!=`.
  - Do NOT write `\%`, `\sim`, `\textasciitilde`, `\textasciicircum` — write literal `%`, `~`, `^`.
- ✅ **ALWAYS output raw literal ASCII characters.** All text inside the `code_exec` block is raw source code, NEVER LaTeX or Markdown math formatting.

---

## Skeleton context & FETCH workflow (Batch Requests)

When provided with skeleton or compact context to conserve tokens, or whenever you need exact file contents:
1. Examine the project tree, classes, and function signatures.
2. **Never guess file contents from memory**: If you need to edit an existing file whose code isn't in your context, DO NOT write a speculative SEARCH block. Request the code with `FETCH`.
3. **Batch all FETCH requests into a SINGLE block**: List all needed files, line slices, or symbols together in one turn instead of asking turn-by-turn.
4. **Supported FETCH patterns**:
   - **Full file**: `FETCH src/types.py`
   - **Line slice**: `FETCH src/app.py:80-140` (or `FETCH src/app.py:50` for lines around line 50)
   - **Function or method**: `FETCH src/services/auth.py:verify_token` or `FETCH src/models.py:User.save`
   - **React / JS component**: `FETCH src/components/Header.jsx:Header` or `FETCH src/utils.ts:calculateTotal`

```code_exec
FETCH src/matcher.py:100-160
FETCH src/types.py:FuzzyCandidate
FETCH src/components/Navbar.jsx:Navbar
FETCH src/utils/helpers.js:1-50
```

5. The `code-exec` engine reads all requested files/slices/symbols and copies their contents directly onto the user's clipboard (or saves them to `context/FETCHED_CONTEXT.md` if the payload is very large). In the next turn, emit the definitive `EDIT` / `CREATE` operations based on the exact lines returned.

---

## Errors & Fast Diagnostic Recovery
When an execution fails, a diagnostic error is automatically copied to the user's clipboard. When the user pastes the error, emit a single revised `code_exec` block with the fix:

| Error Code | Cause | Immediate Fix |
|---|---|---|
| `SEARCH_NOT_FOUND` | SEARCH anchor does not match file lines verbatim | Copy 3–5 exact lines from the provided file or diff (`+` lines) |
| `SEARCH_AMBIGUOUS` | Anchor matches in multiple places | Add 1–3 unique surrounding context lines above or below |
| `SEARCH_TOO_BIG` | Block exceeds 60 lines or 4000 characters | Split into multiple small edits or use first 4 + last 4 boundary anchor |
| `FUZZY_SYNTAX_ERROR` | Replacement has unbalanced `{ }`, `( )`, `[ ]` or unclosed JSX tags | Fix bracket/tag balance in `REPLACE` or provide exact verbatim `SEARCH` |
| `CREATE_EXISTS` | File already exists on disk | Use `EDIT path` instead of `CREATE path` |
| `FILE_NOT_FOUND` | File does not exist to edit | Check path spelling, or use `CREATE` if file is new |
| `MULTIPLE_PLANS` | Two or more `code_exec` blocks emitted | Combine all changes into a single `code_exec` block |
| `PLAN_NOT_FOUND` | Fence was unclosed or cut off | Ensure code block closes with ` ```` ` and is not truncated |
| `UNKNOWN_COMMAND` | Prose or comment inside code block | Move all explanations outside the fenced block |

## CLI helpers
- `code-exec -c`: builds a commit prompt from `git diff`.
- `code-exec check` / `--check`: validates plan parsing and syntax only, without touching or checking the filesystem.
- `code-exec -p --short`: copies the compact edition of these instructions for fast/small models.
