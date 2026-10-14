from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from code_exec_types import (
    ALLOWED_RUN_COMMAND_PREFIXES,
    FORBIDDEN_RUN_SUBSTRINGS,
    INTERACTIVE_ONLY_PREFIXES,
    SHELL_CHAINING_OPERATORS,
    OpError,
)


def build_sandboxed_command(command: str, needs_prompt: bool) -> tuple[str, dict[str, str] | None]:
    """Wraps unvetted generic commands in OS-level sandboxing (dropping network where supported, unless command needs network)."""
    if not needs_prompt:
        return command, None

    try:
        words = shlex.split(command)
    except ValueError:
        words = []
    is_network_cmd = bool(words) and (
        words[0] in {"curl", "wget"}
        or tuple(words[:2]) in {
            ("git", "clone"), ("pip", "install"), ("pip3", "install"),
            ("npm", "install"), ("npm", "ci"),
            ("pnpm", "install"), ("pnpm", "add"),
            ("yarn", "install"), ("yarn", "add"),
        }
        or tuple(words[:4]) in {("python", "-m", "pip", "install"), ("python3", "-m", "pip", "install")}
    )
    if is_network_cmd:
        # User explicitly approved a network command in the interactive terminal prompt.
        # Allow network traffic to proceed.
        return command, None

    extra_env = None
    actual_cmd = command

    if sys.platform.startswith("linux"):
        actual_cmd = f"unshare -r -n -- /bin/sh -c {shlex.quote(command)}"
    elif sys.platform == "darwin":
        actual_cmd = f"sandbox-exec -p '(version 1) (allow default) (deny network-outbound)' /bin/sh -c {shlex.quote(command)}"
    elif sys.platform == "win32":
        extra_env = {
            "HTTP_PROXY": "http://127.0.0.1:0",
            "HTTPS_PROXY": "http://127.0.0.1:0",
            "ALL_PROXY": "http://127.0.0.1:0",
            "NO_PROXY": "",
        }
        escaped_cmd = command.replace('"', '""')
        actual_cmd = f'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Restricted -Command "{escaped_cmd}"'

    return actual_cmd, extra_env


def validate_run_command(cmd: str) -> bool:
    """
    Returns True if the command requires mandatory interactive confirmation, False if auto-allowed.
    Raises OpError only for empty commands or dangerous / forbidden patterns (rm -rf, sudo, mkfs, etc.).
    """
    trimmed = cmd.strip()
    if not trimmed:
        raise OpError("ERR|FORBIDDEN_COMMAND|empty command")

    cmd_lower = trimmed.lower()

    # Block destructive/dangerous patterns
    for forbidden in FORBIDDEN_RUN_SUBSTRINGS:
        if forbidden.lower() in cmd_lower:
            raise OpError(f"ERR|FORBIDDEN_COMMAND|{trimmed} - contains forbidden pattern '{forbidden}'")

    # Conservative on purpose: even quoted shell characters require approval.
    # Check the original input so stripping cannot hide newline commands.
    if any(op in cmd for op in (*SHELL_CHAINING_OPERATORS, "\x00")):
        return True
    if sys.platform == "win32" and any(marker in cmd for marker in ("%", "!", "^")):
        return True
    try:
        words = shlex.split(trimmed)
    except ValueError:
        return True

    # Match complete executable/subcommand tokens, never executable prefixes.
    automatic = (tuple(prefix.split()) for prefix in ALLOWED_RUN_COMMAND_PREFIXES)
    if any(tuple(words[:len(prefix)]) == prefix for prefix in automatic):
        if words[:2] == ["git", "diff"] and any(
            word == "--ext-diff" or word.startswith("--output") for word in words[2:]
        ):
            return True
        return False
    options = words[1:words.index("--")] if "--" in words else words[1:]
    if words and words[0] == "black" and "--check" in options:
        return False
    if words[:2] == ["ruff", "format"] and "--check" in options:
        return False

    # Any other command (python script, build tools, npm, custom scripts)
    # is permitted with explicit interactive user confirmation in terminal
    return True
