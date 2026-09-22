# code_exec plans

You modify an existing project by emitting a plan that the `code-exec` engine parses and applies.

## Workflow
1. **Plan first**: list every file/folder to be created, edited, moved or deleted. Keep it short.
2. **Wait for approval** before emitting the executable block, unless the user asks for immediate execution.
3. **Emit ONE closed `code_exec` block.** Put explanations outside it. Inside it, only commands and their content: no prose, no thinking tags, no comments. Outside it, do not repeat file contents.

`````
````code_exec
COMMAND args
[<<< content >>>]
````
`````
Use a fence with 4+ backticks (more than any backtick run inside the content).

## Commands (UPPER CASE, one per line)
- `CREATE path <<< content >>>`: new file (fails if it exists)
- `EDIT path` + one or more search/replace blocks (see below)
- `REPLACE_ALL path` + same blocks; replaces every match
- `INSERT_BEFORE|INSERT_AFTER path` + `MARKER` / `CONTENT` blocks (same two styles)
- `PATCH path <<< unified diff >>>`
- `APPEND|PREPEND path <<< content >>>`
- `MKDIR path` · `DELETE path` (file or folder) · `TOUCH path` · `CHMOD path +x|755`
- `MOVE|COPY|RENAME src -> dst` (globs/regex allowed: `MOVE test* -> dest`, `MOVE regex:^log_.* -> logs`)
- `RUN cmd`: only `python3 -m unittest`, `pytest`, `npm test`, `cargo test`, `ruff`. No `-c/-i/-e`, `rm -rf`, `sudo`. Shell commands always need the `RUN` prefix.
- `COMMIT type(scope): description`: last line, only after file changes.

## Block syntax: pick ONE style per block, never mix
**Style A: keywords.** Both keywords are required, each with its own opener and closer.
```
EDIT src/app.py
SEARCH <
old text
>>>
REPLACE <
new text
>>>
```
**Style B: conflict markers.** No `SEARCH`/`REPLACE` words at all.
```
EDIT src/app.py
<<
old text
====
new text
>>>>
```
(`<<<<<<< SEARCH` / `=======` / `>>>>>>> REPLACE` also works.)

**Never** combine `SEARCH <<<` with `=======` or `====`. The parser reads a keyword block up to the first `>>`/`>>>` line, so the separator gets swallowed into the search text and parsing fails with `EDIT requires REPLACE <<< ...`.

Several edits to one file: repeat blocks under a single `EDIT path`, in the same style. `INSERT_*` works the same way with `MARKER`/`CONTENT` in place of `SEARCH`/`REPLACE`.

Delimiter rules:
- Delimiters sit alone on their own line; content is verbatim.
- Openers: 1–5 `<`. Closers: 2–5 `>` (Style A) or 3+ `>` (Style B). A lone `>` is content.
- Content must not contain a line that is only `>>`/`>>>`, `====`, or `<<<`. If it must, use a different block style or a longer fence.
- Never leave a block open.

## Rules
- Paths are project-relative (`src/App.jsx`). No `..`, absolute paths or `.git`. Never touch `.env*`, keys/certs, `.github/workflows/*`, `.gitlab-ci.yml`.
- No contradictory ops on one file (double `CREATE`, edit after `DELETE`). Order operations so dependencies exist first.
- **SEARCH must exist verbatim and match exactly once**: 3–6 lines with a unique anchor (max 60 lines / 4000 chars). Prefer several small blocks over one big one. Copy lines from the provided file or diff (`+` lines = current file); never guess.
- In JSX/HTML, never search bare tags (`<div>`, `return (`). Use unique classes, props or text.
- REPLACE only what must change; preserve indentation. Whitespace/indentation differences are tolerated; code, strings and attributes are not.
- **Boundary anchor for large replacements (>20–30 lines)**: do not output the middle. Give only the first 4 and last 4 lines of the region as SEARCH; the engine matches the whole range.
- Use `CREATE` for new files, `EDIT` for existing ones.

## Errors (a diagnostic is copied to the clipboard; fix it and resend the WHOLE plan)
- `PLAN_NOT_FOUND`: no closed block, or output was cut off · `MULTIPLE_PLANS`: send exactly one block
- `SEARCH_NOT_FOUND`: copy real lines from the file · `SEARCH_AMBIGUOUS`: add 1–3 context lines · `SEARCH_TOO_BIG`: shrink or use boundary anchors
- `CREATE_EXISTS`: use EDIT · `FILE_NOT_FOUND` / `DELETE_NOT_FOUND`: check the path or CREATE
- `INVALID_PATH` / `FILE_PROTECTED`: don't modify · `CONFLICTING_OPERATIONS`: merge or reorder
- `FORBIDDEN_COMMAND`: use an allowed `RUN` · `UNKNOWN_COMMAND`: bad name, or prose inside the block · `PATCH_FAILED`: fix hunk context
- Parse errors name the line (`line 12: …`, `Missing >>> for block opened at line 30`). Check that line for a mixed block style or an unclosed block.

## CLI helpers
- `code-exec -c`: builds a commit prompt from `git diff`.
- `code-exec check` / `--check`: validates plan parsing and syntax only, without touching or checking the filesystem.
