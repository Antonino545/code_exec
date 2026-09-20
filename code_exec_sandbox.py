from __future__ import annotations

import os
import re
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
    """Wraps unvetted generic commands in OS-level sandboxing (dropping network where supported)."""
    if not needs_prompt:
        return command, None

    extra_env = None
    actual_cmd = command

    if sys.platform.startswith("linux"):
        actual_cmd = f"unshare -r -n {command}"
    elif sys.platform == "darwin":
        actual_cmd = f"sandbox-exec -p '(version 1) (allow default) (deny network-outbound)' {command}"
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
    """Returns True if the command requires mandatory interactive confirmation, False otherwise."""
    trimmed = cmd.strip()
    if not trimmed:
        raise OpError("ERR|FORBIDDEN_COMMAND|empty command")

    cmd_lower = trimmed.lower()

    # Block destructive/network substrings
    for forbidden in FORBIDDEN_RUN_SUBSTRINGS:
        if forbidden in cmd_lower:
            raise OpError(f"ERR|FORBIDDEN_COMMAND|{trimmed} - contains forbidden pattern '{forbidden}'")

    # Block inline Python/Bash scripts and interactive REPLs
    if re.search(r"\b(python[0-9.]*|node|bash|sh|perl|ruby)\s+(-[a-zA-Z]*c|--command|-i|-e)\b", cmd_lower):
        raise OpError(f"ERR|FORBIDDEN_COMMAND|{trimmed} - inline execution or interactive shell flags are forbidden")

    has_chaining = any(op in trimmed for op in SHELL_CHAINING_OPERATORS)

    if any(cmd_lower.startswith(prefix) for prefix in ALLOWED_RUN_COMMAND_PREFIXES):
        return True if has_chaining else False

    if any(cmd_lower.startswith(prefix) for prefix in INTERACTIVE_ONLY_PREFIXES):
        return True

    allowed_list = ", ".join(f"'{p.strip()}'" for p in ALLOWED_RUN_COMMAND_PREFIXES[:5]) + ", ..."
    raise OpError(f"ERR|FORBIDDEN_COMMAND|{trimmed} - command not in whitelist (allowed: {allowed_list})")
