"""
code_exec_completions.py - Shell autocompletion generator and installer for code-exec.
Supports Zsh, Bash, and Fish shells.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ACTIONS = [
    ("apply", "Read clipboard or file and execute code plan"),
    ("watch", "Monitor clipboard in background for AI code plans"),
    ("bundle", "Export compact single-file context bundle (PROJECT_CONTEXT.md)"),
    ("pack", "Alias for bundle"),
    ("outline", "Alias for bundle"),
    ("digest", "Alias for bundle"),
    ("export-context", "Export clean context directory tree"),
    ("undo", "Revert last applied operations from backup"),
    ("prompt", "Copy standard system prompt instructions to clipboard"),
    ("commit", "Copy git diff prompt to clipboard for AI commit generation"),
    ("theme", "Switch or inspect active terminal color theme"),
    ("themes", "List all available terminal color themes"),
    ("fetch", "Fetch and copy specific lines from a file to clipboard"),
    ("update", "Check and update code-exec to latest version from GitHub"),
    ("check", "Validate and check plan syntax without filesystem changes"),
    ("help", "Show interactive usage guide and command reference"),
    ("completions", "Generate shell completion script (bash, zsh, fish)"),
    ("install-completions", "Automatically install completions for your active shell"),
]

FLAGS = [
    ("-h", "--help", "Show usage guide and command options"),
    ("-c", "--commit-prompt", "Copy git diff prompt to clipboard"),
    ("-p", "--prompt", "Copy prompt instructions to clipboard"),
    ("-b", "--bundle", "Export compact single-file context bundle"),
    ("-w", "--watch", "Start clipboard watch mode"),
    ("-y", "--yes", "Apply operations without confirmation"),
    ("-d", "--diff", "Display unified diff before applying"),
    ("-t", "--tree", "Display proposed directory tree structure"),
    ("-v", "--verbose", "Show detailed matching tier diagnostic logs"),
    ("-V", "--version", "Show program version number"),
    ("-C", "--project-dir", "Run in specified directory"),
    ("", "--dry-run", "Simulate operations without touching filesystem"),
    ("", "--check", "Debug mode: syntax-check plan without touching filesystem"),
    ("", "--clipboard", "Read plan instructions from clipboard"),
    ("", "--file", "Read plan instructions from a file path"),
    ("", "--full", "Force uncompressed full files in bundle export"),
    ("", "--compact", "Export compact skeleton outline for context"),
    ("", "--short", "With -p: copy compact prompt for smaller/local models"),
    ("", "--with-tree", "With -p: append current file tree to instructions"),
    ("", "--verify", "Run verification command after applying (e.g. pytest)"),
    ("", "--timeout", "Seconds allowed per RUN command"),
    ("", "--no-run", "Refuse plans containing RUN commands"),
    ("", "--no-commit", "Skip git commit prompt even if COMMIT is present"),
    ("", "--ignore-file", "Path or name of custom ignore configuration file"),
    ("", "--target-dir", "Target directory for context export"),
    ("", "--install-completions", "Install shell autocompletions for active shell"),
]

THEMES = ["claude", "catppuccin", "tokyo-night"]


def generate_zsh_completions() -> str:
    """Generates a native Zsh completion function script (_code-exec)."""
    actions_desc = "\n".join([f"            '{act}:{desc}'" for act, desc in ACTIONS])
    themes_desc = "\n".join([f"                    '{t}:Theme {t}'" for t in THEMES])

    return f"""#compdef code-exec

_code_exec() {{
    local curcontext="$curcontext" state line
    typeset -A opt_args

    local -a subcommands
    subcommands=(
{actions_desc}
    )

    local -a themes
    themes=(
{themes_desc}
    )

    _arguments -C \\
        '(-h --help)'{{-h,--help}}'[Show usage guide and options]' \\
        '(-c --commit-prompt --docommit)'{{-c,--commit-prompt,--docommit}}'[Copy git diff prompt to clipboard]' \\
        '(-p --prompt --copy-instructions)'{{-p,--prompt,--copy-instructions}}'[Copy prompt instructions to clipboard]' \\
        '(-b --bundle --single-file)'{{-b,--bundle,--single-file}}'[Export compact single-file context bundle]' \\
        '(-w --watch)'{{-w,--watch}}'[Start clipboard watch mode]' \\
        '(-y --yes)'{{-y,--yes}}'[Apply operations without confirmation]' \\
        '--diff[Display unified diff of file changes before applying]' \\
        '--tree[Display proposed directory tree structure]' \\
        '--check[Validate syntax only without reading or checking files]' \\
        '--clipboard[Read instructions from clipboard]' \\
        '--file[Read instructions from file]:file:_files' \\
        '--dry-run[Validate and show operations without applying]' \\
        '--no-run[Refuse plans that contain RUN commands]' \\
        '--no-commit[Skip git commit prompt even if COMMIT is present]' \\
        '--timeout[Seconds allowed per RUN command]:seconds:' \\
        '--with-tree[Append current file tree to copied instructions]' \\
        '(-v --verbose)'{{-v,--verbose}}'[Show matching tier diagnostics]' \\
        '--short[Copy compact instructions for local models]' \\
        '(-V --version)'{{-V,--version}}'[Show version number]' \\
        '(-C --project-dir)'{{-C,--project-dir}}'[Run in specified directory]:directory:_files -/' \\
        '--full[Export full file contents in bundle instead of compact skeletons]' \\
        '--compact[Export compact skeleton for context]' \\
        '--verify[Run verification command after applying]:command:' \\
        '--ignore-file[Custom ignore file path]:file:_files' \\
        '--target-dir[Target directory for context export]:directory:_files -/' \\
        '--install-completions[Install shell autocompletions for active shell]' \\
        '1:action:->action' \\
        '*:args:->args' && return 0

    case $state in
        action)
            _describe -t subcommands 'code-exec commands' subcommands
            _files
            ;;
        args)
            case $line[1] in
                theme)
                    _describe -t themes 'color themes' themes
                    ;;
                fetch|get|read)
                    _files
                    ;;
                completions)
                    local -a shells
                    shells=('bash:Bash shell completions' 'zsh:Zsh completions' 'fish:Fish completions')
                    _describe -t shells 'supported shells' shells
                    ;;
                bundle|pack|outline|export-context)
                    _files -/
                    ;;
                *)
                    _files
                    ;;
            esac
            ;;
    esac
}}

_code_exec "$@"
"""


def generate_bash_completions() -> str:
    """Generates a native Bash programmable completion script."""
    actions_str = " ".join([act for act, _ in ACTIONS])
    flags_list: list[str] = []
    for short, long, _ in FLAGS:
        if short:
            flags_list.append(short)
        flags_list.append(long)
    flags_str = " ".join(flags_list)
    themes_str = " ".join(THEMES)

    return f"""# Bash completion for code-exec

_code_exec_completions() {{
    local cur prev words cword
    _init_completion -n : 2>/dev/null || {{
        cur="${{COMP_WORDS[COMP_CWORD]}}"
        prev="${{COMP_WORDS[COMP_CWORD-1]}}"
    }}

    local actions="{actions_str}"
    local flags="{flags_str}"
    local themes="{themes_str}"
    local shells="bash zsh fish"

    case "$prev" in
        theme)
            COMPREPLY=( $(compgen -W "$themes" -- "$cur") )
            return 0
            ;;
        completions)
            COMPREPLY=( $(compgen -W "$shells" -- "$cur") )
            return 0
            ;;
        fetch|get|read|--file|--ignore-file)
            COMPREPLY=( $(compgen -f -- "$cur") )
            return 0
            ;;
        -C|--project-dir|--target-dir)
            COMPREPLY=( $(compgen -d -- "$cur") )
            return 0
            ;;
    esac

    if [[ "$cur" == -* ]]; then
        COMPREPLY=( $(compgen -W "$flags" -- "$cur") )
        return 0
    fi

    if [[ $COMP_CWORD -eq 1 ]]; then
        COMPREPLY=( $(compgen -W "$actions" -- "$cur") $(compgen -f -- "$cur") )
        return 0
    fi

    COMPREPLY=( $(compgen -f -- "$cur") )
    return 0
}}

complete -F _code_exec_completions code-exec
"""


def generate_fish_completions() -> str:
    """Generates a native Fish completion script."""
    lines = ["# Fish completions for code-exec\n"]
    for act, desc in ACTIONS:
        clean_desc = desc.replace("'", "\\'")
        lines.append(f"complete -c code-exec -n '__fish_use_subcommand' -a '{act}' -d '{clean_desc}'")

    for short, long, desc in FLAGS:
        clean_desc = desc.replace("'", "\\'")
        s_part = f"-s {short.lstrip('-')} " if short else ""
        l_part = f"-l {long.lstrip('-')}"
        lines.append(f"complete -c code-exec {s_part}{l_part} -d '{clean_desc}'")

    for t in THEMES:
        lines.append(f"complete -c code-exec -n '__fish_seen_subcommand_from theme' -a '{t}' -d 'Theme {t}'")

    for sh in ["bash", "zsh", "fish"]:
        lines.append(f"complete -c code-exec -n '__fish_seen_subcommand_from completions' -a '{sh}' -d '{sh.capitalize()} completion'")

    return "\n".join(lines) + "\n"


def install_completions(shell_name: str | None = None) -> tuple[bool, str, str]:
    """
    Installs completions for the specified shell or detects automatically from $SHELL.
    Returns: (success: bool, shell_detected: str, message: str)
    """
    if not shell_name:
        shell_env = os.environ.get("SHELL", "").lower()
        if "zsh" in shell_env:
            shell_name = "zsh"
        elif "fish" in shell_env:
            shell_name = "fish"
        elif "bash" in shell_env:
            shell_name = "bash"
        else:
            shell_name = "zsh" if sys.platform == "darwin" else "bash"

    shell_name = shell_name.strip().lower()
    home = Path.home()

    try:
        if shell_name == "zsh":
            zsh_dir = home / ".zsh" / "completions"
            zsh_dir.mkdir(parents=True, exist_ok=True)
            comp_file = zsh_dir / "_code-exec"
            comp_file.write_text(generate_zsh_completions(), encoding="utf-8")

            # Ensure ~/.zshrc sources fpath
            zshrc = home / ".zshrc"
            rc_content = zshrc.read_text(encoding="utf-8", errors="replace") if zshrc.exists() else ""
            fpath_snippet = 'fpath=(~/.zsh/completions $fpath)\nautoload -Uz compinit && compinit -u\n'
            source_comment = '# code-exec completions\n'
            if ".zsh/completions" not in rc_content:
                with open(zshrc, "a", encoding="utf-8") as f:
                    f.write(f"\n{source_comment}{fpath_snippet}")

            return True, "zsh", str(comp_file)

        elif shell_name == "bash":
            bash_comp_dir = home / ".bash_completion.d"
            if bash_comp_dir.is_dir():
                target_file = bash_comp_dir / "code-exec"
                target_file.write_text(generate_bash_completions(), encoding="utf-8")
                return True, "bash", str(target_file)
            else:
                target_file = home / ".code_exec_completion.bash"
                target_file.write_text(generate_bash_completions(), encoding="utf-8")
                bashrc = home / ".bashrc"
                rc_content = bashrc.read_text(encoding="utf-8", errors="replace") if bashrc.exists() else ""
                snippet = f'\n# code-exec completions\n[ -f "{target_file}" ] && source "{target_file}"\n'
                if ".code_exec_completion.bash" not in rc_content:
                    with open(bashrc, "a", encoding="utf-8") as f:
                        f.write(snippet)
                return True, "bash", str(target_file)

        elif shell_name == "fish":
            fish_dir = home / ".config" / "fish" / "completions"
            fish_dir.mkdir(parents=True, exist_ok=True)
            target_file = fish_dir / "code-exec.fish"
            target_file.write_text(generate_fish_completions(), encoding="utf-8")
            return True, "fish", str(target_file)

        else:
            return False, shell_name, f"Unsupported shell '{shell_name}'. Supported shells: zsh, bash, fish."

    except Exception as exc:
        return False, shell_name, str(exc)
