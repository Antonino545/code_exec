# code_exec plans — compact edition
# For GPT-4o-mini, Claude Haiku, Gemini Flash, small/local models.
# Same engine, same block syntax — just fewer words.

Wrap your plan in ONE fenced `code_exec` block. Explanations go outside it.
Do the requested work without an extra approval turn. If context is missing, emit one batched FETCH block and wait for the returned content. Add a brief change summary outside the plan block.

## Before output
- Use only paths and exact code from supplied context or `FETCH`; batch missing context into one FETCH block. Never guess.
- Keep edits small and limited to the request. Check command arguments, paths, matching block keywords, and closing delimiters.
- EDIT/REPLACE_ALL use SEARCH + REPLACE. EDIT_RANGE uses EXPECT + REPLACE. INSERT uses MARKER + CONTENT. MOVE/COPY/RENAME use `source -> destination`.
- Use one block style per edit: keyword blocks with `>>>`, or `<<<< / ==== / >>>>`. Never mix styles.
- Output one complete plan. Ask if key context is missing or the requested change is ambiguous. Don't invent RUN commands or claim checks passed unless they ran.

`````
````code_exec
COMMAND path [<<< content >>>]
````
`````

## Commands
- `CREATE path <<< content >>>` — new file (fails if exists; use EDIT for existing)
- `EDIT path` — modify existing file; one or more SEARCH/REPLACE pairs below
- `EDIT_RANGE path start-end` — exact inclusive 1-based line range; requires EXPECT + REPLACE, fails if current lines changed
- `REPLACE_ALL path` — same as EDIT but replaces every match
- `APPEND|PREPEND path <<< content >>>`
- `INSERT_BEFORE|INSERT_AFTER path` — with MARKER / CONTENT pairs
- `PATCH path <<< unified diff >>>`
- `FETCH path[:start-end|:symbol]` — request full file, line slice, or function/class (e.g. `FETCH src/app.py:my_func`). **Batch all needed FETCH lines in ONE block**.
- `MKDIR path` · `DELETE path` · `TOUCH path` · `CHMOD path +x|755`
- `MOVE|COPY|RENAME src -> dst` (globs OK: `MOVE test* -> dest/`)
- `RUN cmd` — direct test/check tools (`pytest`, `python3 -m unittest`, `ruff check`) run automatically. Installs, npm/pnpm/yarn scripts (including `npm test`), custom commands/curl, malformed quoting, and shell syntax require interactive terminal approval (`accept`), even with `--yes`; otherwise denied. Dangerous patterns remain forbidden.
- `COMMIT type(scope): message` — last line only, after file ops

## EDIT block (pick ONE style, never mix)

**Style A — keywords:**
```
EDIT src/app.py
SEARCH <
exact lines from file
>>>
REPLACE <
new lines
>>>
```

**Style B — conflict markers:**
```
EDIT src/app.py
<<
exact lines from file
====
new lines
>>>>
```

For position-sensitive changes, prefer `EDIT_RANGE` using a recent FETCH:
````
EDIT_RANGE src/app.py 12-13
EXPECT <<<
old line 1
old line 2
>>>
REPLACE <<<
new line 1
new line 2
>>>
````
If it reports STALE_RANGE, do not guess new line numbers. FETCH the current range and rebuild it.

Use one operation per distinct change. For edits at separate locations in one file, use separate EDIT operations in the same plan.

## Critical rules

- **Keep SEARCH small (3–8 lines):** NEVER dump 50+ lines into SEARCH to change 2 lines. Include only 3–8 lines around the modification.
- **Multiple edits:** Use separate small EDIT operations for separate locations; do not make one huge SEARCH spanning the file.
- **Large or position-sensitive replacement:** FETCH the current numbered lines, then use EDIT_RANGE with the full exact text in EXPECT. Never use boundary anchors or fuzzy matching for a high-impact edit.
- **Preserve syntax & bracket balance:** Ensure all `{ }`, `( )`, `[ ]`, and `<tag>...</tag>` opened in REPLACE are balanced. `code-exec` validates plan structure and anchors, not whether edited code compiles.
- **Missing context? Ask first:** If you don't have project context or a file's code, do NOT guess. Ask user to run `code-exec bundle` (or `code-exec -b`), or emit a batch `FETCH` block.
- **FETCH before EDIT if needed:** Request full files (`FETCH path`), slices (`FETCH path:80-140`), or symbols (`FETCH path:my_func`). Batch all `FETCH` lines into ONE block. The engine copies exact code to the clipboard for the next turn.
- **ONE block per reply:** Never emit a second block as a correction — merge into ONE.
- **SEARCH must be verbatim:** Copy exact lines from the file or diff (`+` lines = current file). Never write from memory.
- **Never use generic anchors** (`pass`, `}`, `return`, bare HTML tags) — they match everywhere.
- **Paths are project-relative** (`src/app.py`). Never guess; never use `~/`, `/`, or `..`.
- **Raw ASCII only (no LaTeX/HTML escapes):** NEVER use `\vert{}\vert{}`, `\&\&`, `&amp;&amp;`, `\leq`, etc. Always write literal `||`, `&&`, `|`, `&`, `<`, `>`, `<=`, `>=`, `!=`, `%`.
- **RUN approval:** Use only needed commands. Installs, project scripts, custom commands, and shell syntax require terminal approval; `--yes` does not bypass it. Command side effects are not automatically reversible.
- No plan prose or explanations inside the block. Source comments are fine when they belong in the changed file.

## Errors → clipboard retry
The parser/executor copies diagnostics to your clipboard. Follow the reported line, message, and recovery outcome. Preserve the intended change without unrelated edits:
- **No changes applied / complete rollback:** rebuild the full intended plan, including reverted operations.
- **Changes retained:** FETCH current content and resume only unfinished work; do not repeat operations whose changes remain. Use `code-exec undo` only if recovery information was saved.
- **Incomplete rollback:** recover using retained backups before another executable plan.
- **Unknown outcome / recovery save failure:** inspect current files and recovery data before retrying.
Undo covers journaled file operations; command side effects are not automatically reversed. If context is stale, FETCH before editing.
- `SEARCH_NOT_FOUND` — copy real lines from file (look at `+` lines in diff shown)
- `SEARCH_AMBIGUOUS` — add 1–3 more unique surrounding context lines
- `SEARCH_TOO_BIG` — split into smaller edits; for one large contiguous change, FETCH the current range and use EDIT_RANGE
- `FUZZY_SYNTAX_ERROR` (if reported) — inspect the replacement and current file for unbalanced delimiters
- `CREATE_EXISTS` — use EDIT instead of CREATE
- `FILE_NOT_FOUND` — verify path; use CREATE for new files
- `MULTIPLE_PLANS` — merge into one block
- `PLAN_NOT_FOUND` — block was not closed or output was cut off
- `UNKNOWN_COMMAND` — prose inside block, or wrong command name
- `STALE_RANGE` — FETCH the current range and rebuild EDIT_RANGE; never guess new line numbers
- `STALE_FILE` — content changed since validation/preview; FETCH and rebuild the plan, never reuse cached positions
