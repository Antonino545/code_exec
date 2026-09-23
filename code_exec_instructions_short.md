# code_exec plans — compact edition
# For GPT-4o-mini, Claude Haiku, Gemini Flash, small/local models.
# Same engine, same block syntax — just fewer words.

Wrap your plan in ONE fenced `code_exec` block. Explanations go outside it.

`````
````code_exec
COMMAND path [<<< content >>>]
````
`````

## Commands
- `CREATE path <<< content >>>` — new file (fails if exists; use EDIT for existing)
- `EDIT path` — modify existing file; one or more SEARCH/REPLACE pairs below
- `REPLACE_ALL path` — same as EDIT but replaces every match
- `APPEND|PREPEND path <<< content >>>`
- `INSERT_BEFORE|INSERT_AFTER path` — with MARKER / CONTENT pairs
- `PATCH path <<< unified diff >>>`
- `FETCH path[:start-end|:symbol]` — request full file, line slice, or function/class (e.g. `FETCH src/app.py:my_func`). **Batch all needed FETCH lines in ONE block**.
- `MKDIR path` · `DELETE path` · `TOUCH path` · `CHMOD path +x|755`
- `MOVE|COPY|RENAME src -> dst` (globs OK: `MOVE test* -> dest/`)
- `RUN cmd` — allowed: `pytest`, `python3 -m unittest`, `npm test`, `cargo test`, `ruff`, `black`
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

Multiple edits: repeat pairs under one `EDIT path`. Same style throughout.

## Critical rules

- **Missing context? Ask first.** If you don't have project context or a file's code, do NOT guess. Ask user to run `code-exec bundle` (or `code-exec -b`), or emit a batch `FETCH` block.
- **FETCH before EDIT if needed.** Request full files (`FETCH path`), slices (`FETCH path:80-140`), or symbols (`FETCH path:my_func`). Batch all `FETCH` lines into ONE block. The engine copies exact code to the clipboard for the next turn.
- **ONE block per reply.** Never emit a second block as a correction — merge into ONE.
- **SEARCH must be verbatim.** Copy exact lines from the file or diff (`+` lines = current file). Never write from memory.
- **3–6 unique lines** as anchor (max 60 lines). For large regions: first 4 + last 4 lines only.
- **Never use generic anchors** (`pass`, `}`, `return`, bare HTML tags) — they match everywhere.
- **Paths are project-relative** (`src/app.py`). Never guess; never use `~/`, `/`, or `..`.
- No prose inside the block. No comments. No explanations.

## Errors → clipboard retry
On error the diagnostic is copied to your clipboard. Fix ONLY the failing operation:
- `SEARCH_NOT_FOUND` — copy real lines from file (look at `+` lines in diff shown)
- `SEARCH_AMBIGUOUS` — add more unique context lines
- `FILE_NOT_FOUND` — verify path; use CREATE for new files
- `MULTIPLE_PLANS` — merge into one block
- `PLAN_NOT_FOUND` — block was not closed or output was cut off
- `UNKNOWN_COMMAND` — prose inside block, or wrong command name
