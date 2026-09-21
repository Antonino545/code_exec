
# code_exec rules

You are a coding agent modifying an existing project. When changing files or running commands, output exactly ONE `code_exec` plan. Never output raw file contents outside it.

## Plan
Explain reasoning normally outside the block. Inside the block put ONLY executable commands:
```code_exec
COMMAND args
[<<< content >>>]
````

Use 4+ backticks if content contains ```.

## Commands

Allowed: CREATE, EDIT, PATCH, REPLACE_ALL, DELETE, MOVE, COPY, RENAME, MKDIR, TOUCH, CHMOD, APPEND, PREPEND, INSERT_BEFORE, INSERT_AFTER, RUN, COMMIT.

## Safety

* Paths must be project-relative; never `..`, absolute paths, `.git`, or root.
* Never modify `.env*`, secrets/keys/certs, or CI files.
* Never issue conflicting operations on the same file.
* RUN only: `python3 -m unittest`, `pytest`, `npm test`, `cargo test`, `ruff`.
* No `-c/-i/-e`, `rm -rf`, or `sudo`.
* End modifications with `COMMIT type(scope): description`.

## EDIT/PATCH

* SEARCH must match existing text and be unique.
* Keep SEARCH small: 3–6 lines, max 60 lines/4000 chars.
* Use multiple EDITs for unrelated changes.
* JSX: use unique classes/props/text, never generic tags.
* Change only what is necessary; preserve formatting.
* EDIT supports SEARCH/REPLACE blocks or divider syntax (<<<< ... ==== ... >>>>).
* PATCH must use valid unified diff.

## Validation

Matching tolerates whitespace/indentation, but meaningful code is never ignored. SEARCH must match exactly one location.

Common errors:

* PLAN_NOT_FOUND / MULTIPLE_PLANS → output exactly one valid block.
* SEARCH_NOT_FOUND → inspect diagnostic and use the actual file text.
* SEARCH_AMBIGUOUS → add nearby unique context.
* SEARCH_TOO_BIG → shrink SEARCH.
* CREATE_EXISTS → use EDIT/REPLACE_ALL.
* FILE_NOT_FOUND → verify path or CREATE.
* INVALID_PATH / FILE_PROTECTED → do not modify.
* CONFLICTING_OPERATIONS → combine/reorder operations.
* FORBIDDEN_COMMAND → use an allowed RUN command.
* PATCH_FAILED → fix diff context.

If execution fails, use the diagnostic feedback to correct the next plan.

## Commit

`code-exec -c` / `code-exec docommit` can generate a commit prompt from the current git diff.


