from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from code_exec_sandbox import build_sandboxed_command, validate_run_command
from code_exec_types import (
    CommandFailed,
    OpError,
    PROTECTED_FILE_EXACT,
    PROTECTED_NAMES,
    PROTECTED_SUFFIXES,
    ROOT,
    Unverifiable,
    clean_path,
)
from code_exec_ui import ui


BACKUP_ROOT = ROOT / ".code_exec" / "backups"
MAX_BACKUP_HISTORY = 5

def _default_file_mode() -> int:
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


NEW_FILE_MODE = _default_file_mode()


def rel(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def safe_path(value: str, *, follow_leaf: bool = True) -> Path:
    """
    Resolve a project-relative path, refusing anything outside ROOT, ROOT
    itself, and protected names such as .git.
    """
    if not value or "\0" in value:
        raise OpError(f"ERR|INVALID_PATH|{value} - Empty or invalid path")

    candidate = Path(os.path.normpath(os.path.join(ROOT, value)))

    try:
        if follow_leaf:
            resolved = candidate.resolve()
        else:
            resolved = candidate.parent.resolve() / candidate.name
        relative = resolved.relative_to(ROOT)
    except (ValueError, OSError, RuntimeError):
        raise OpError(f"ERR|INVALID_PATH|{value} - Path outside project: {value}") from None

    if not relative.parts:
        raise OpError(f"ERR|INVALID_PATH|{value} - Refusing to operate on the project root: {value!r}")

    if any(part.lower() in PROTECTED_NAMES for part in relative.parts):
        raise OpError(f"ERR|PROTECTED_PATH|{value} - Refusing to touch protected path: {value}")

    # Guard sensitive and secret files
    file_name = resolved.name.lower()
    if file_name in PROTECTED_FILE_EXACT or any(file_name.endswith(ext) for ext in PROTECTED_SUFFIXES):
        raise OpError(f"ERR|FILE_PROTECTED|{value} - Refusing to touch sensitive credential/key file")

    # Guard CI/CD workflows
    rel_posix = relative.as_posix().lower()
    if rel_posix.startswith(".github/workflows/") or rel_posix == ".gitlab-ci.yml":
        raise OpError(f"ERR|FILE_PROTECTED|{value} - Refusing to touch CI/CD workflow definition")

    return resolved


def read_text(path: Path) -> str:
    """Read UTF-8 text without newline translation (CRLF stays CRLF)."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise OpError(f"Cannot read {rel(path)}: {exc.strerror or exc}") from None

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise OpError(f"{rel(path)} is not valid UTF-8 text (binary file?)") from None


def atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    """Write via a temp file in the same directory, then rename over target."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, NEW_FILE_MODE if mode is None else mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def newline_style(text: str) -> str:
    """Dominant line ending of an existing file."""
    crlf = text.count("\r\n")
    return "\r\n" if crlf and crlf * 2 >= text.count("\n") else "\n"


def with_newlines(text: str, newline: str) -> str:
    clean = text.replace("\r\n", "\n")
    return clean if newline == "\n" else clean.replace("\n", newline)


def _remove(path: Path) -> None:
    if not os.path.lexists(path):
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


class VirtualFS:
    """In-memory overlay on top of the real tree (never writes to disk)."""

    def __init__(self):
        self.state: dict[Path, tuple] = {}

    def _lookup(self, path: Path):
        if path in self.state:
            return self.state[path]
        for parent in path.parents:
            if parent in self.state:
                return ("gone",)
        return None

    def lexists(self, path):
        entry = self._lookup(path)
        return os.path.lexists(path) if entry is None else entry[0] != "gone"

    def is_file(self, path):
        entry = self._lookup(path)
        return path.is_file() if entry is None else entry[0] in {"file", "bin"}

    def is_dir(self, path):
        entry = self._lookup(path)
        return path.is_dir() if entry is None else entry[0] == "dir"

    def read(self, path):
        entry = self._lookup(path)
        if entry is None:
            return read_text(path)
        if entry[0] == "file":
            return entry[1]
        raise OpError(f"{rel(path)} is not valid UTF-8 text (binary file?)")

    def _drop_children(self, path):
        for key in [k for k in self.state if path in k.parents]:
            del self.state[key]

    def _mkparents(self, directory):
        current = ROOT
        for part in directory.relative_to(ROOT).parts:
            current = current / part
            if self.lexists(current):
                if not self.is_dir(current):
                    raise OpError(f"{rel(current)} exists but is not a directory")
            else:
                self.state[current] = ("dir",)

    def write(self, path, text):
        self._mkparents(path.parent)
        self.state[path] = ("file", text)

    def mkdir(self, path):
        self._mkparents(path)

    def delete(self, path):
        self._drop_children(path)
        self.state[path] = ("gone",)

    def copy(self, src, dst):
        if self.is_dir(src):
            raise Unverifiable("a directory move/copy")
        try:
            entry = ("file", self.read(src))
        except OpError:
            entry = ("bin",)
        self._mkparents(dst.parent)
        self._drop_children(dst)
        self.state[dst] = entry

    def move(self, src, dst):
        self.copy(src, dst)
        self.delete(src)


class RealFS:
    """Applies operations for real and records how to undo each one."""

    def __init__(self, timeout: int):
        self.timeout = timeout
        self.journal: list[tuple] = []
        self.backup_dir: Path | None = None
        self.ran_commands: list[str] = []
        self._counter = 0

    def lexists(self, path):
        return os.path.lexists(path)

    def is_file(self, path):
        return path.is_file()

    def is_dir(self, path):
        return path.is_dir()

    def read(self, path):
        return read_text(path)

    def _slot(self) -> Path:
        if self.backup_dir is None:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            self.backup_dir = BACKUP_ROOT / ts
            self.backup_dir.mkdir(parents=True, exist_ok=True)
        self._counter += 1
        return self.backup_dir / str(self._counter)

    def _mkparents(self, directory: Path):
        missing = []
        current = directory
        while not os.path.lexists(current):
            missing.append(current)
            current = current.parent
        if not current.is_dir():
            raise OpError(f"{rel(current)} exists but is not a directory")
        for folder in reversed(missing):
            try:
                folder.mkdir()
            except OSError as exc:
                raise OpError(
                    f"Cannot create directory {rel(folder)}: {exc.strerror or exc}"
                ) from None
            self.journal.append(("rmdir", folder))

    def write(self, path, text):
        self._mkparents(path.parent)
        mode = None

        if os.path.lexists(path):
            slot = self._slot()
            try:
                shutil.copy2(path, slot)
            except OSError as exc:
                raise OpError(f"Cannot back up {rel(path)}: {exc.strerror or exc}") from None
            mode = stat.S_IMODE(path.stat().st_mode)
            self.journal.append(("restore", path, slot))
        else:
            self.journal.append(("remove", path))

        try:
            atomic_write(path, text, mode)
        except OSError as exc:
            raise OpError(f"Cannot write {rel(path)}: {exc.strerror or exc}") from None

    def mkdir(self, path):
        self._mkparents(path)

    def delete(self, path):
        slot = self._slot()
        try:
            shutil.move(str(path), str(slot))
        except (OSError, shutil.Error) as exc:
            raise OpError(f"Cannot delete {rel(path)}: {exc}") from None
        self.journal.append(("restore", path, slot))

    def move(self, src, dst):
        self._mkparents(dst.parent)
        try:
            shutil.move(str(src), str(dst))
        except (OSError, shutil.Error) as exc:
            raise OpError(f"Cannot move {rel(src)}: {exc}") from None
        self.journal.append(("move_back", dst, src))

    def copy(self, src, dst):
        self._mkparents(dst.parent)
        self.journal.append(("remove", dst))
        try:
            if src.is_dir():
                shutil.copytree(src, dst, symlinks=True)
            else:
                shutil.copy2(src, dst)
        except (OSError, shutil.Error) as exc:
            raise OpError(f"Cannot copy {rel(src)}: {exc}") from None

    def run(self, command: str):
        needs_prompt = validate_run_command(command)
        ui.command(command)

        if needs_prompt:
            c = ui.palette
            print(c.paint("\n  ⚠️  SECURITY WARNING: Unvetted / Generic Execution Requested", c.AMBER))
            ans = input(c.paint(f"  ❯ Allow '{command}' to execute? [y/N]: ", c.CORAL, bold=True)).strip().lower()
            if ans not in {"y", "yes"}:
                raise CommandFailed(f"User denied execution of: {command}")

        actual_cmd, extra_env = build_sandboxed_command(command, needs_prompt)

        self.ran_commands.append(actual_cmd)
        limit = self.timeout or None
        run_env = os.environ.copy()
        if extra_env:
            run_env.update(extra_env)

        try:
            result = subprocess.run(actual_cmd, shell=True, cwd=ROOT, timeout=limit, env=run_env)
        except subprocess.TimeoutExpired:
            raise CommandFailed(
                f"Command timed out after {self.timeout}s: {actual_cmd}"
            ) from None
        if result.returncode != 0:
            raise CommandFailed(
                f"Command failed (exit {result.returncode}): {actual_cmd}"
            )

    def rollback(self) -> list[str]:
        errors = []
        for entry in reversed(self.journal):
            kind, path = entry[0], entry[1]
            try:
                if kind == "rmdir":
                    try:
                        path.rmdir()
                    except OSError:
                        pass
                elif kind == "remove":
                    _remove(path)
                elif kind == "restore":
                    _remove(path)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(entry[2]), str(path))
                elif kind == "move_back":
                    entry[2].parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(path), str(entry[2]))
            except Exception as exc:
                errors.append(f"{kind} {rel(path)}: {exc}")
        self.journal.clear()
        return errors

    def save_backup_manifest(self) -> None:
        """Persists rollback manifest to disk to allow subsequent `undo` commands."""
        if self.backup_dir is None or not self.journal:
            return
        manifest_entries = []
        for entry in self.journal:
            item = {"kind": entry[0], "path": rel(entry[1])}
            if len(entry) > 2:
                if isinstance(entry[2], Path):
                    try:
                        item["slot"] = str(entry[2].relative_to(self.backup_dir))
                    except ValueError:
                        item["slot"] = rel(entry[2])
                else:
                    item["slot"] = str(entry[2])
            manifest_entries.append(item)

        manifest_data = {
            "timestamp": datetime.now().isoformat(),
            "journal": manifest_entries,
            "ran_commands": self.ran_commands,
        }
        manifest_file = self.backup_dir / "manifest.json"
        manifest_file.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

        # Prune older backups beyond MAX_BACKUP_HISTORY
        try:
            backups = sorted([d for d in BACKUP_ROOT.iterdir() if d.is_dir()], key=lambda d: d.name)
            while len(backups) > MAX_BACKUP_HISTORY:
                oldest = backups.pop(0)
                shutil.rmtree(oldest, ignore_errors=True)
        except OSError:
            pass

    def cleanup(self):
        if self.backup_dir is not None:
            shutil.rmtree(self.backup_dir, ignore_errors=True)
            self.backup_dir = None


def undo_last_run() -> tuple[bool, str]:
    """Reverts file changes made by the most recent successful plan."""
    if not BACKUP_ROOT.exists():
        return False, "No backups found (.code_exec/backups does not exist)."
    backups = sorted(
        [d for d in BACKUP_ROOT.iterdir() if d.is_dir() and (d / "manifest.json").exists()],
        key=lambda d: d.name,
    )
    if not backups:
        return False, "No previous backup found to undo."

    target_backup = backups[-1]
    manifest_file = target_backup / "manifest.json"
    try:
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"Failed to read backup manifest: {exc}"

    journal = data.get("journal", [])
    errors = []
    restored_count = 0
    for item in reversed(journal):
        kind = item.get("kind")
        rel_path = item.get("path")
        slot = item.get("slot")
        if not rel_path:
            continue
        path = ROOT / rel_path
        try:
            if kind == "rmdir":
                if path.exists() and path.is_dir():
                    try:
                        path.rmdir()
                    except OSError:
                        pass
            elif kind == "remove":
                _remove(path)
                restored_count += 1
            elif kind == "restore" and slot:
                slot_path = target_backup / slot
                if slot_path.exists():
                    _remove(path)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(slot_path, path)
                    restored_count += 1
            elif kind == "move_back" and slot:
                src_path = ROOT / slot
                if path.exists():
                    src_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(path), str(src_path))
                    restored_count += 1
        except Exception as exc:
            errors.append(f"Failed to undo {kind} on {rel_path}: {exc}")

    shutil.rmtree(target_backup, ignore_errors=True)
    if errors:
        return False, "\n".join(errors)
    return True, f"Successfully reverted {restored_count} change(s) from backup {target_backup.name}."
