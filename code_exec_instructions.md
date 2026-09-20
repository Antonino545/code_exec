# Instructions for writing `code_exec` plans

You are a coding agent modifying an existing project.

When asked to modify/create/delete/move files or run commands, **always output a `code_exec` plan** enclosed in a single ```text``` code block. Never output raw file contents as standalone Markdown code blocks.

### Separation of Explanations and Plan
* **Explanations and context**: Place all conversational explanations, analysis, diagnoses, and rationale **outside** the code block as normal Markdown prose.
* **Executable code block**: Enclose **only** the executable plan (starting with `THINK` and ending with `COMMIT` or the last command) inside the ` ```text ` block so the user can copy the plan directly with a single click.

### Running Plans
* **Interactive menu**: Typing `code-exec` in the terminal opens an interactive menu.
* **Direct execution**: Typing `code-exec apply` (or choosing option 1) reads and applies the clipboard plan.

## Format

```text
THINK
Reason about the change and identify the exact target.
END_THINK

COMMAND argument
[content block if required]
```

`THINK` is never executed.

---

# EDIT

```text
EDIT path
SEARCH
<<<
existing text
>>>
REPLACE
<<<
new text
>>>
```

`EDIT` replaces exactly one occurrence.

---

# SEARCH rules — CRITICAL

### General

1. **SEARCH must be based on text that actually exists in the target file. Never invent or guess it.**

2. Keep SEARCH **small but unique**. Normally target **3–6 lines**.

3. Do not copy an entire component, function, slide, section, or JSX subtree when only a small part needs changing.

4. Search **locally around the actual target**, not from a distant parent marker.

5. If multiple unrelated locations need changes, use **multiple small EDIT operations**.

6. SEARCH longer than 6 lines is allowed only when necessary for uniqueness. Explain why in `THINK`.

7. SEARCH must identify exactly one target.

**The goal is not the smallest SEARCH. The goal is the smallest UNIQUE SEARCH.**

---

# JSX / React rules

React/JSX contains many repeated HTML-like elements, so generic tags are unreliable.

### Never use generic JSX alone

Do not use these as SEARCH by themselves:

```text
<div>
<span>
<section>
<button>
<p>
return (
</div>
```

Instead use distinctive context:

```text
<div className="smart-home-buttons">
```

or:

```text
<WasteScheduleSlide
  schedule={schedule}
```

or a unique combination of component name, class, ID, prop, text, or nearby lines.

### JSX opening tags

Opening tags may span multiple lines.

Always use the **actual structure from the file**.

Do not reconstruct:

```text
<Component prop={value}>
```

if the actual file contains:

```text
<Component
  prop={value}
  otherProp={otherValue}
>
```

### Nested JSX

When changing a deeply nested element:

* search near the actual element;
* do not search the entire parent;
* do not include all children;
* do not use a slide/page marker as the beginning of a huge SEARCH.

For example, do not use:

```text
{/* SLIDE 0 */}
...30+ lines...
<Target />
```

when `<Target />` can be identified locally.

### JSX modification

When changing:

* a prop → search the relevant opening tag;
* a class → search the relevant `className`;
* a style → search the relevant style/property;
* text → search the relevant text and nearby context;
* a child → search the child itself;
* a component → search its distinctive opening tag.

---

# SEARCH matching / validator rules

The `code_exec.py` validator should make SEARCH matching tolerant of **harmless formatting differences** while remaining strict about identity and uniqueness.

Matching should follow this order:

### 1. Exact match

Try the SEARCH text exactly as provided first.

### 2. Normalized whitespace match

If exact matching fails, retry while ignoring harmless differences such as:

* indentation;
* tabs vs spaces;
* trailing whitespace;
* CRLF vs LF;
* equivalent whitespace between lines.

Do **not** ignore meaningful characters, JSX syntax, attributes, strings, comments, or punctuation.

### 3. JSX-aware matching

For `.jsx` / `.tsx` files, matching may account for harmless JSX indentation/formatting differences.

For example:

SEARCH:

```text
<div className="waste-schedule">
```

may match:

```text
      <div className="waste-schedule">
```

because indentation is not semantically relevant.

However:

```text
<div>
```

must **not** automatically match an arbitrary `<div>` elsewhere in the file.

### 4. Uniqueness remains mandatory

After normalization:

* **0 matches → validation error**
* **1 match → valid**
* **2+ matches → validation error**

Never automatically choose the first match.

### 5. Apply using the real file span

When normalized matching succeeds, the executor must replace the **actual matched text/span in the file**, not a reconstructed or normalized version of the file.

This preserves the file's original formatting.

### 6. Useful validation errors

If SEARCH fails, report:

* the file;
* the first line of SEARCH;
* which matching strategies were attempted;
* whether a similar/normalized match was found;
* useful nearby context when possible.

If multiple matches exist, report the number of matches and enough context to help the AI make SEARCH more specific.

---

# Exactness rules

SEARCH must never change meaningful code.

Do not normalize or ignore:

* JSX attributes;
* attribute values;
* strings;
* comments;
* operators;
* punctuation;
* component names;
* variable names;
* expressions;
* HTML/JSX hierarchy.

Only harmless formatting differences may be normalized.

---

# REPLACE rules

Replace only what is necessary.

Do not:

* rewrite unrelated JSX;
* reformat surrounding code;
* replace an entire parent component for a small change;
* change indentation unnecessarily;
* modify unrelated props or children.

For JSX:

**small SEARCH + small REPLACE = preferred.**

---

# THINK

For non-trivial edits, identify:

* target file;
* exact component/element;
* exact property/child being changed;
* why SEARCH is unique;
* why SEARCH is minimal;
* whether multiple EDITs are safer.

Do not put speculative code in THINK.

---

# Other commands

```text
CREATE path
<<<
content
>>>
```

```text
DELETE path
```

```text
MOVE source -> destination
COPY source -> destination
RENAME source -> destination
```

```text
MKDIR path
```

`APPEND`, `PREPEND`, `INSERT_BEFORE`, and `INSERT_AFTER` follow the same exact-content and uniqueness principles.

---

# RUN

```text
RUN command
```

Minimize RUN commands.

Prefer file operations when possible.

RUN commands are **not rolled back**, so put them after file modifications whenever possible.

---

# COMMIT

Plans modifying project files should end with a `COMMIT` command providing a concise, Conventional Commits-style message describing the change (e.g. `feat: ...`, `fix: ...`, `refactor: ...`):

COMMIT feat(auth): add token validation middleware

After successfully applying file changes, `code_exec` will ask the user if they want to create a git commit using this message.

`code_exec` will stage and commit **only the specific files modified by the plan**, leaving any other unrelated untracked or modified files in the working directory untouched.

---

# Safety

Paths must be relative to the project root.

Never use:

* absolute paths;
* `..`;
* paths outside the project root;
* symlinks pointing outside the project root.

Never modify `.git` or the project root itself.

Binary/non-UTF-8 files cannot be edited.

---

# Content blocks

Prefer:

```text
<<<
content
>>>
```

Use `END_OF_FILE` only when necessary.

---

# FINAL RULE

**SEARCH LOCALLY → MATCH UNIQUELY → TOLERATE HARMLESS FORMATTING → REPLACE MINIMALLY.**


For React/JSX:

**FIND THE ACTUAL ELEMENT → USE A DISTINCTIVE LOCAL ANCHOR → DO NOT COPY LARGE JSX SUBTREES.**

The validator may tolerate harmless formatting differences, but it must **never tolerate ambiguity**.

**Never guess. Never select the first ambiguous match. Never modify more than the uniquely identified target.**
