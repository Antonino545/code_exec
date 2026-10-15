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
            if self.state.get(parent) == ("gone",):
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

    def touch(self, path):
        if not self.lexists(path):
            self.write(path, "")

    def chmod(self, path, mode):
        if not self.lexists(path):
            raise OpError(f"ERR|FILE_NOT_FOUND|{rel(path)}")

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

    def run(self, command: str):
        pass


class RealFS:
    """Applies operations for real and records how to undo each one."""

    def __init__(self, timeout: int):
        self.timeout = timeout
        self.journal: list[tuple] = []
        self.backup_dir: Path | None = None
        self.ran_commands: list[str] = []
        self.ran_outputs: list[str] = []
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

    def touch(self, path: Path):
        self._mkparents(path.parent)
        if not os.path.lexists(path):
            self.journal.append(("remove", path))
        path.touch()

    def chmod(self, path: Path, mode_str: str):
        if not os.path.lexists(path):
            raise OpError(f"ERR|FILE_NOT_FOUND|{rel(path)}")
        old_mode = stat.S_IMODE(path.stat().st_mode)
        self.journal.append(("chmod", path, old_mode))
        if mode_str in {"+x", "a+x", "u+x"}:
            new_mode = old_mode | 0o111
        elif mode_str in {"-x", "a-x"}:
            new_mode = old_mode & ~0o111
        else:
            try:
                new_mode = int(mode_str, 8)
            except ValueError:
                raise OpError(f"ERR|INVALID_MODE|Invalid chmod mode: {mode_str}")
        try:
            os.chmod(path, new_mode)
        except OSError as exc:
            raise OpError(f"Cannot chmod {rel(path)}: {exc}") from None

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
            if not sys.stdin.isatty():
                raise CommandFailed(f"Execution denied: interactive terminal approval required for: {command}")
            c = ui.palette
            print(c.paint("\n  ⚠️  SECURITY WARNING: Custom Execution Requested", c.AMBER, bold=True))
            try:
                ans = input(c.paint(f"  ❯ Allow '{command}' to execute? [y] Accept / [N] Deny: ", c.CORAL, bold=True)).strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "n"
            if ans not in {"y", "yes", "accept", "a", "ok"}:
                raise CommandFailed(f"Execution denied by user for: {command}")

        actual_cmd, extra_env = build_sandboxed_command(command, needs_prompt)

        self.ran_commands.append(actual_cmd)
        limit = self.timeout or None
        run_env = os.environ.copy()
        if extra_env:
            run_env.update(extra_env)

        try:
            result = subprocess.run(
                actual_cmd,
                shell=True,
                cwd=ROOT,
                timeout=limit,
                env=run_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except subprocess.TimeoutExpired:
            raise CommandFailed(
                f"Command timed out after {self.timeout}s: {actual_cmd}"
            ) from None
        except OSError as exc:
            raise CommandFailed(f"Cannot execute command: {actual_cmd}: {exc}") from None

        stdout_text = getattr(result, "stdout", "")
        if isinstance(stdout_text, str) and stdout_text:
            sys.stdout.write(stdout_text)
            if not stdout_text.endswith("\n"):
                sys.stdout.write("\n")
            sys.stdout.flush()
            if stdout_text.strip():
                self.ran_outputs.append(stdout_text.strip())

        combined_output = "\n\n".join(out for out in self.ran_outputs if out.strip())
        if combined_output.strip():
            try:
                from code_exec_parser import set_clipboard
                set_clipboard(combined_output.strip())
                c = ui.palette
                print(c.paint("  📋 Run output copied to clipboard!\n", c.AMBER, bold=True))
            except Exception:
                pass

        if getattr(result, "returncode", 0) != 0:
            err_msg = f"Command failed (exit {result.returncode}): {actual_cmd}"
            if isinstance(stdout_text, str) and stdout_text.strip():
                err_msg += f"\n\nOutput:\n{stdout_text.strip()}"
            raise CommandFailed(err_msg)

    def rollback(self) -> list[str]:
        errors = []
        while self.journal:
            entry = self.journal[-1]
            kind, path = entry[0], entry[1]
            try:
                _restore_entry(kind, path, entry[2] if len(entry) > 2 else None)
                self.journal.pop()
            except Exception as exc:
                errors.append(f"{kind} {rel(path)}: {exc}")
                # Earlier entries can depend on this restoration. Preserve
                # them and their backup slots for recovery in the same order.
                break
        return errors

    def save_backup_manifest(self, *, recovery_required: bool = False) -> None:
        """Persists rollback manifest to disk to allow subsequent `undo` commands."""
        if not self.journal:
            return
        if self.backup_dir is None:
            self._slot()  # CREATE-only plans still need an undo manifest.
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
            "recovery_required": recovery_required,
        }
        manifest_file = self.backup_dir / "manifest.json"
        atomic_write(manifest_file, json.dumps(manifest_data, indent=2))

        # Prune older backups beyond MAX_BACKUP_HISTORY
        try:
            backups = []
            for directory in BACKUP_ROOT.iterdir():
                if not directory.is_dir():
                    continue
                try:
                    previous = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue  # Unreadable recovery data must not be pruned.
                if isinstance(previous, dict) and not previous.get("undo_started") and not previous.get("recovery_required"):
                    backups.append(directory)
            backups.sort(key=lambda d: d.name)
            while len(backups) > MAX_BACKUP_HISTORY:
                oldest = backups.pop(0)
                shutil.rmtree(oldest, ignore_errors=True)
        except OSError:
            pass

    def cleanup(self):
        if self.backup_dir is not None:
            shutil.rmtree(self.backup_dir, ignore_errors=True)
            self.backup_dir = None


def _restore_entry(kind: str, path: Path, slot=None) -> None:
    """Restore one journal entry, retaining snapshots until recovery succeeds."""
    if kind == "rmdir":
        if os.path.lexists(path):
            path.rmdir()
    elif kind == "remove":
        _remove(path)
    elif kind == "restore":
        if slot is None or not os.path.lexists(slot):
            raise OpError(f"Required backup content missing for {rel(path)}")
        _remove(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if slot.is_dir() and not slot.is_symlink():
            shutil.copytree(slot, path, symlinks=True)
        else:
            shutil.copy2(slot, path, follow_symlinks=False)
    elif kind == "move_back":
        if slot is None or not os.path.lexists(path):
            raise OpError(f"Required moved path missing: {rel(path)}")
        if os.path.lexists(slot):
            raise OpError(f"Cannot restore move over existing path: {rel(slot)}")
        slot.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(slot))
    elif kind == "chmod":
        os.chmod(path, int(slot))
    else:
        raise OpError(f"Unknown backup operation: {kind}")


def undo_last_run() -> tuple[bool, str]:
    """Revert the newest saved plan, safely resuming a partial undo."""
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

    if not isinstance(data, dict):
        return False, f"Invalid backup manifest; backup retained at {target_backup}"
    journal = data.get("journal", [])
    if not isinstance(journal, list) or any(not isinstance(item, dict) for item in journal):
        return False, f"Invalid backup journal; backup retained at {target_backup}"
    restored_count = 0
    for item in reversed(journal):
        if item.get("done"):
            continue
        if item.get("in_progress"):
            return False, f"Previous undo progress could not be saved safely; inspect recovery data at {target_backup} before retrying."
        kind = item.get("kind")
        rel_path = item.get("path")
        slot = item.get("slot")
        try:
            path = safe_path(rel_path, follow_leaf=False)
            resolved_slot = slot
            if kind == "restore":
                resolved_slot = target_backup / slot if slot else None
                if resolved_slot is not None:
                    # Do not follow the leaf: backed-up symlinks are restored
                    # as symlinks, but their slot must stay in this backup.
                    (resolved_slot.parent.resolve() / resolved_slot.name).relative_to(target_backup.resolve())
            elif kind == "move_back":
                resolved_slot = safe_path(slot, follow_leaf=False)
            data["undo_started"] = True
            item["in_progress"] = True
            atomic_write(manifest_file, json.dumps(data, indent=2))
            try:
                _restore_entry(kind, path, resolved_slot)
            except Exception:
                item.pop("in_progress", None)
                atomic_write(manifest_file, json.dumps(data, indent=2))
                raise
            item.pop("in_progress", None)
            item["done"] = True
            atomic_write(manifest_file, json.dumps(data, indent=2))
            restored_count += 1
        except Exception as exc:
            return False, f"Failed to undo {kind} on {rel_path}: {exc}\nBackup retained at {target_backup}."

    shutil.rmtree(target_backup, ignore_errors=True)
    return True, f"Successfully reverted {restored_count} change(s) from backup {target_backup.name}."
