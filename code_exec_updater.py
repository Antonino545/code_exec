from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from code_exec_types import ROOT
from code_exec_ui import ui

GITHUB_REPO = "Antonino545/code_exec"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/commits/main"
GITHUB_RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main"

CORE_MODULES = [
    "code_exec.py",
    "code_exec_ui.py",
    "code_exec_types.py",
    "code_exec_sandbox.py",
    "code_exec_matcher.py",
    "code_exec_parser.py",
    "code_exec_fs.py",
    "code_exec_plan_export.py",
    "code_exec_updater.py",
    "code_exec_instructions.md",
    "code_exec_instructions_short.md",
    "pyproject.toml",
]


def fetch_latest_commit_metadata() -> dict[str, str]:
    req = urllib.request.Request(
        GITHUB_API_URL,
        headers={
            "User-Agent": "code-exec-updater",
            "Accept": "application/vnd.github.v3+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to connect to GitHub API: {exc}") from None

    commit_data = payload.get("commit", {})
    author_data = commit_data.get("author", {})
    return {
        "sha": payload.get("sha", "")[:7],
        "full_sha": payload.get("sha", ""),
        "message": commit_data.get("message", "").split("\n")[0],
        "author": author_data.get("name", "Unknown"),
        "date": author_data.get("date", ""),
    }


def get_local_commit_sha(directory: Path) -> str | None:
    git_dir = directory / ".git"
    if git_dir.exists():
        try:
            res = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=directory,
                capture_output=True,
                text=True,
                check=True,
            )
            return res.stdout.strip()
        except Exception:
            return None
    return None


def update_code_exec(target_dir: Path | None = None) -> bool:
    c = ui.palette
    install_dir = target_dir or Path(__file__).resolve().parent

    print(c.paint(f"\n  📡 Checking for updates from https://github.com/{GITHUB_REPO} (main)...", c.SLATE))

    try:
        remote = fetch_latest_commit_metadata()
    except Exception as exc:
        ui.error(f"ERR|UPDATE_FAILED|{exc}")
        return False

    local_sha = get_local_commit_sha(install_dir)

    print(f"  Latest remote commit : {c.paint(remote['sha'], c.CYAN, bold=True)} ({remote['date'][:10]})")
    print(f"  Author               : {c.paint(remote['author'], c.WHITE)}")
    print(f"  Message              : {c.paint(remote['message'], c.WHITE, bold=True)}")
    if local_sha:
        print(f"  Current local commit : {c.paint(local_sha, c.AMBER)}")

    if local_sha and local_sha == remote["sha"]:
        print(c.paint(f"\n  ✔ code-exec is already up to date at commit {local_sha}!\n", c.GREEN, bold=True))
        return True

    prompt = c.paint("\n❯ Update code-exec to this commit? [y/N]: ", c.CORAL, bold=True)
    try:
        ans = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False

    if ans not in {"y", "yes"}:
        print(c.paint("  Update cancelled.\n", c.SLATE))
        return False

    # Check if installed via pipx or uv tool
    if "pipx" in sys.prefix:
        print(c.paint("  Detected pipx environment. Upgrading via pipx...", c.SLATE))
        try:
            res = subprocess.run(["pipx", "upgrade", "code-exec"], capture_output=True, text=True, check=True)
            print(c.paint(f"  ✔ {res.stdout.strip()}", c.GREEN))
            print(c.paint(f"\n  ✨ code-exec successfully updated!\n", c.GREEN, bold=True))
            return True
        except Exception as exc:
            ui.error(f"ERR|PIPX_UPGRADE_FAILED|{exc}")
            return False

    if "uv/tools" in sys.prefix or "uv" in sys.prefix:
        print(c.paint("  Detected uv tool environment. Upgrading via uv tool...", c.SLATE))
        try:
            res = subprocess.run(["uv", "tool", "upgrade", "code-exec"], capture_output=True, text=True, check=True)
            print(c.paint(f"  ✔ {res.stdout.strip()}", c.GREEN))
            print(c.paint(f"\n  ✨ code-exec successfully updated!\n", c.GREEN, bold=True))
            return True
        except Exception as exc:
            ui.error(f"ERR|UV_UPGRADE_FAILED|{exc}")
            return False

    if (install_dir / ".git").exists():
        print(c.paint(f"  Updating git repository at {install_dir}...", c.SLATE))
        try:
            subprocess.run(["git", "fetch", "origin", "main"], cwd=install_dir, check=True)
            res = subprocess.run(["git", "pull", "origin", "main"], cwd=install_dir, capture_output=True, text=True, check=True)
            print(c.paint(f"  ✔ {res.stdout.strip()}", c.GREEN))
            print(c.paint(f"\n  ✨ code-exec successfully updated to {remote['sha']}!\n", c.GREEN, bold=True))
            return True
        except subprocess.CalledProcessError as exc:
            ui.error(f"ERR|GIT_PULL_FAILED|{exc.stderr or exc}")
            return False

    print(c.paint(f"  Downloading updated modules to staging directory...", c.SLATE))
    with tempfile.TemporaryDirectory() as tmp_dir:
        staging_dir = Path(tmp_dir)
        downloaded = {}
        for mod in CORE_MODULES:
            file_url = f"{GITHUB_RAW_BASE}/{mod}"
            try:
                req = urllib.request.Request(file_url, headers={"User-Agent": "code-exec-updater"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    content = resp.read().decode("utf-8")
                staging_file = staging_dir / mod
                staging_file.parent.mkdir(parents=True, exist_ok=True)
                staging_file.write_text(content, encoding="utf-8")
                downloaded[mod] = staging_file
                print(f"      Downloaded {mod}")
            except Exception as exc:
                ui.error(f"ERR|DOWNLOAD_FAILED|Could not update {mod}: {exc}")
                return False

        # Atomically copy all validated files into the install directory
        for mod, staging_file in downloaded.items():
            target_path = install_dir / mod
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staging_file, target_path)
            print(f"      Updated {mod}")

    print(c.paint(f"\n    code-exec successfully updated to {remote['sha']}!\n", c.GREEN, bold=True))
    return True
