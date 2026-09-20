from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path.cwd().resolve()

# Path components that plans may never touch.
PROTECTED_NAMES = {".git"}

# Sensitive file basenames and patterns that plans may never modify or delete.
PROTECTED_FILE_EXACT = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.staging",
    ".env.test",
    "id_rsa",
    "id_ed25519",
}

PROTECTED_SUFFIXES = {
    ".pem",
    ".key",
    ".crt",
    ".cer",
    ".pfx",
    ".p12",
}

DEFAULT_RUN_TIMEOUT = 600  # seconds per RUN command; 0 disables the limit

# Allowed executables and tools for RUN commands
ALLOWED_RUN_COMMAND_PREFIXES = (
    "python3 -m unittest",
    "python3 -m pytest",
    "pytest",
    "unittest",
    "npm test",
    "npm run ",
    "npm install",
    "pnpm test",
    "pnpm run ",
    "yarn test",
    "yarn run ",
    "cargo test",
    "cargo check",
    "cargo build",
    "go test",
    "go build",
    "ruff ",
    "black ",
    "flake8",
    "mypy",
    "git status",
    "git diff",
)

# Commands that are permitted ONLY with explicit interactive terminal approval
INTERACTIVE_ONLY_PREFIXES = (
    "python ",
    "python3 ",
    "node ",
    "bash ",
    "sh ",
)

# High-risk patterns forbidden in RUN commands even if prefix matches
FORBIDDEN_RUN_SUBSTRINGS = (
    "rm -rf",
    "rm -fr",
    "sudo",
    "mkfs",
    "dd if=",
    ":(){ :|:& };:",
    "> /dev/sd",
    "curl ",
    "wget ",
    "| sh",
    "| bash",
    "chmod 777",
    "chown -R",
)

COMMANDS = {
    "CREATE", "EDIT", "DELETE", "MOVE", "COPY", "RENAME", "MKDIR",
    "INSERT_BEFORE", "INSERT_AFTER", "APPEND", "PREPEND", "RUN",
    "COMMIT",
}


# ============================================================
# Errors
# ============================================================

class OpError(Exception):
    """An operation cannot be performed."""


class CommandFailed(OpError):
    """A RUN command failed. File changes made so far are kept."""


class Unverifiable(Exception):
    """The dry-run simulation cannot predict what happens next."""


# ============================================================
# Dataclasses
# ============================================================

@dataclass
class MatchResult:
    start: int
    end: int
    note: str
    fuzzy: bool
    line_range: tuple[int, int] | None = None


@dataclass
class Operation:
    command: str
    args: tuple
    data: str | None = None
    extra: str | None = None


def clean_path(raw: str) -> str:
    """Trim whitespace and one layer of matching quotes/backticks."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'`":
        raw = raw[1:-1].strip()
    return raw
