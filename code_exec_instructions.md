# code_exec plans

You modify an existing project by emitting a plan that the `code-exec` engine parses and applies deterministically.

## Workflow
1. **Context check**:
   - Check if project context was provided (via `code-exec bundle` / `PROJECT_CONTEXT.md`).
   - If the project structure or needed files are missing: ask the user to run `code-exec bundle` (or `code-exec -b`) in their terminal and paste the context.
   - If you know the files you need to edit but don't have their exact lines, emit a `FETCH` block first (see FETCH workflow below).
2. **Plan first**: list every file/folder to be created, edited, moved or deleted. Keep it short.
3. **Do the requested work without adding an extra approval turn.** Ask a question only when needed context is missing or different interpretations would change the edit. If context is missing, send one batched `FETCH` block and wait for its result before writing edits.
4. **Emit ONE closed `code_exec` block.** Put a brief change summary outside it. Inside it, include only executable operations and their content: no explanations or plan-level comments. Comments that are part of source being created or replaced are allowed. Use 4+ backticks for the outer fence when file content contains triple backticks.

## Before emitting a plan (error prevention)
- Work only from files and paths present in the supplied context or returned by `FETCH`. If the exact current code is missing, request it with one batched `FETCH` block; never reconstruct it from memory.
- Keep the change focused on the user's request. Prefer small edits over rewriting a whole file, and do not add unrelated cleanup or speculative files.
- Use one operation per distinct change. If a file needs separate edits, use separate operations under the same plan; do not combine distant changes into one huge search region.
- Check every command name, relative path, required argument, and block pair before responding. `EDIT` / `REPLACE_ALL` need `SEARCH` then `REPLACE`; `EDIT_RANGE` needs `EXPECT` then `REPLACE`; `INSERT_BEFORE` / `INSERT_AFTER` need `MARKER` then `CONTENT`; `MOVE` / `COPY` / `RENAME` need `source -> destination`.
- Choose one block style for each edit: keyword blocks closed with `>>>`, or conflict markers `<<<<`, `====`, `>>>>`. Never combine the styles.
- Make sure all delimiters are closed and the plan is one complete `code_exec` block. Do not include a `RUN` command unless it is needed for the request; never invent a test command or claim a check passed unless it was actually run.
- If information is missing or two interpretations could produce different edits, ask a brief question instead of guessing.

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
- `EDIT_RANGE path start-end` + `EXPECT` / `REPLACE` blocks: replace only the exact, inclusive 1-based line range if its current text still matches `EXPECT` exactly apart from line-ending style. Use this for larger or position-sensitive edits based on recent `FETCH` output.
- `REPLACE_ALL path` + same blocks; replaces every match in the file
- `INSERT_BEFORE|INSERT_AFTER path` + `MARKER` / `CONTENT` blocks
- `PATCH path <<< unified diff >>>`
- `APPEND|PREPEND path <<< content >>>`
- `FETCH path[:start-end|:symbol]`: request full file, line slice, or function/class onto clipboard (e.g. `FETCH src/app.py:my_func` or `FETCH src/models.py:User.save`). **Batch all FETCH lines in ONE block.**
- `MKDIR path` · `DELETE path` (file or folder) · `TOUCH path` · `CHMOD path +x|755`
- `MOVE|COPY|RENAME src -> dst` (globs/regex allowed: `MOVE test* -> dest`, `MOVE regex:^log_.* -> logs`)
- `RUN cmd`: execute a test, Python script, or curl command. Direct test/check tools (e.g. `pytest`, `python3 -m unittest`, `cargo test`, `ruff check`) run automatically. Package installs, npm/pnpm/yarn scripts (including `npm test`), custom commands, malformed quoting, and shell syntax (newlines, redirection, substitutions, chaining) require interactive terminal approval (`accept`), even with `--yes`. Without an interactive terminal, these commands are denied. Dangerous patterns (`rm -rf`, `sudo`, `curl | sh`) remain forbidden; inline Python `-c` requires approval.
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

### Strict line-range edit (`EDIT_RANGE`)
Use this when line position matters or a normal `EDIT` could match a repeated snippet. Take the range and `EXPECT` text from a recent `FETCH` of the current file:
````
EDIT_RANGE src/service.py 24-26
EXPECT <<<
def old_name():
    return 1
>>>
REPLACE <<<
def new_name():
    return 2
>>>
````
The range is inclusive and 1-based. The command fails with `STALE_RANGE` if the current lines differ or the range moved outside the file. Do not adjust the range or convert it to fuzzy `EDIT` automatically; request a fresh `FETCH` and rebuild the operation. Keep `EXPECT` limited to the exact lines being replaced.

---

## Sizing SEARCH blocks & Multiple Edits (CRITICAL)

### 1. Keep SEARCH blocks small (3–8 lines)
- ❌ **NEVER dump 50, 100, or 150+ lines into a SEARCH block.** Oversized blocks slow down matching and cause boundary drift.
- ✅ Include only **3–8 lines** around the specific lines you need to change.
- ✅ If you are changing 3 lines in the middle of a 200-line file, your SEARCH block should only be ~5 lines long.

### 2. Multiple edits in one file: separate operations
If you need to change lines at line 10 and line 150, **do NOT** create one giant block spanning line 10 to line 150. Use two separate `EDIT` operations in the same plan, each with its own short anchor:

```
EDIT src/components/Dashboard.jsx
SEARCH <
import { OldButton } from './ui/OldButton';
>>>
REPLACE <
import { NewButton } from './ui/NewButton';
>>>
EDIT src/components/Dashboard.jsx
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

### 3. Large or position-sensitive changes
For a large rewrite, repeated code, or a change where the wrong match would be harmful, do not use boundary anchors or fuzzy matching. `FETCH` the current numbered range, then use `EDIT_RANGE` with the full exact lines being replaced in `EXPECT`. It applies only if those numbered lines still match. For smaller low-risk changes, use a short unique exact `EDIT` anchor.

---

## Syntax & Bracket Balance

- ❌ **NEVER emit unbalanced brackets or unclosed tags in REPLACE.**
  - Check that all `{`, `(`, `[`, and `<tag>` opened in `REPLACE` are properly closed.
  - If you open a `<div>` or `{` in your replacement, ensure the corresponding `</div>` or `}` is present.
  - `code-exec` checks plan structure and edit anchors; it does not guarantee the edited language code compiles. Use the project's relevant check when requested, and report its result accurately.
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
- ✅ Use **3–6 unique lines** as an anchor for a small `EDIT`. Use `EDIT_RANGE` with fresh `FETCH` output for large or repeated regions; include the complete expected text for the inclusive line range.
- 🛡️ **Matcher Note**: `EDIT` can tolerate formatting drift and may offer fuzzy matches. Do not rely on that tolerance for correctness. Use `EDIT_RANGE` when an exact current location matters; it fails with `STALE_RANGE` instead of relocating the edit.

### File paths
- ❌ **NEVER guess a file path.** Only use paths that explicitly appear in the file list or context provided.
- ❌ **NEVER use absolute paths** (`/home/user/...`), home-relative paths (`~/...`), or `..` traversal.
- ✅ Paths are always relative to the project root (`src/App.jsx`, `tests/test_foo.py`).
- ✅ If a path seems wrong, ask the user to confirm before emitting the plan.

### Single block rule
- ❌ **NEVER emit more than one `code_exec` block** in a single reply. If you need to correct a mistake, merge all operations into ONE new block.
- ❌ **NEVER put corrections as a second block** — that triggers `MULTIPLE_PLANS`.

### Block content
- ❌ **NEVER place plan prose or explanations inside the `code_exec` block.** Put them outside. Source comments are allowed when they are part of the actual file content.
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
When parsing or execution fails, a diagnostic error is automatically copied to the user's clipboard. Treat its line number, message, and **recovery outcome** as authoritative. Preserve the user's intended change, and use the outcome to choose the retry:

- **No changes applied:** correct the error and rebuild the full intended plan.
- **Complete rollback:** earlier file operations were reverted; rebuild the full intended plan, including those operations.
- **Changes retained:** RUN or verification failed after changes were applied. FETCH current content and resume only unfinished work; do not repeat operations whose changes remain. A saved recovery manifest allows `code-exec undo` to reverse journaled file operations.
- **Incomplete rollback:** recover using the retained backup information before another executable plan. Do not assume which changes remain.
- **Unknown outcome:** FETCH current content and establish the state before emitting another executable plan.

Command side effects are not automatically reversed. If saving recovery information failed, inspect the retained backup data and current files before retrying; do not assume `undo` is available. Correct the reported problem without unrelated changes. If context is stale, FETCH before editing. Emit one revised block appropriate to the reported outcome:

| Error Code | Cause | Immediate Fix |
|---|---|---|
| `SEARCH_NOT_FOUND` | SEARCH anchor does not match file lines verbatim | Copy 3–5 exact lines from the provided file or diff (`+` lines) |
| `SEARCH_AMBIGUOUS` | Anchor matches in multiple places | Add 1–3 unique surrounding context lines above or below |
| `SEARCH_TOO_BIG` | Block exceeds matcher limits | Split into smaller edits; for a large contiguous replacement, FETCH the current range and use `EDIT_RANGE` |
| `FUZZY_SYNTAX_ERROR` | Replacement has unbalanced `{ }`, `( )`, `[ ]` or unclosed JSX tags | Fix bracket/tag balance in `REPLACE` or provide exact verbatim `SEARCH` |
| `CREATE_EXISTS` | File already exists on disk | Use `EDIT path` instead of `CREATE path` |
| `FILE_NOT_FOUND` | File does not exist to edit | Check path spelling, or use `CREATE` if file is new |
| `MULTIPLE_PLANS` | Two or more `code_exec` blocks emitted | Combine all changes into a single `code_exec` block |
| `PLAN_NOT_FOUND` | Fence was unclosed or cut off | Ensure code block closes with ` ```` ` and is not truncated |
| `UNKNOWN_COMMAND` | Prose or comment inside code block | Move all explanations outside the fenced block |
| `STALE_RANGE` | Current lines differ from `EXPECT`, or the range is outside the file | FETCH the latest range and rebuild `EDIT_RANGE`; never move the edit by guessing |
| `STALE_FILE` | File content changed after validation or preview | FETCH current content and rebuild the plan; never reuse old match positions |

## CLI helpers
- `code-exec -c`: builds a commit prompt from `git diff`.
- `code-exec check` / `--check`: validates plan parsing and syntax only, without touching or checking the filesystem.
- `code-exec -p --short`: copies the compact edition of these instructions for fast/small models.
