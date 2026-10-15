"""Regression coverage for preview freshness, command approval, and recovery."""

import json
import os
import shlex
import stat
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import code_exec
import code_exec_execution as execution
import code_exec_fs as filesystem
import code_exec_types as types
from code_exec_sandbox import build_sandboxed_command, validate_run_command
from code_exec_types import CommandFailed, MatchResult, OpError, Operation


class SafeExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        original_cwd = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, original_cwd)
        self.contexts = ExitStack()
        self.addCleanup(self.contexts.close)
        for module in (code_exec, execution, filesystem, types):
            self.contexts.enter_context(patch.object(module, "ROOT", self.root))
        self.backups = self.root / ".code_exec" / "backups"
        self.contexts.enter_context(patch.object(filesystem, "BACKUP_ROOT", self.backups))
        self.contexts.enter_context(patch.object(execution, "ui", MagicMock()))
        self.contexts.enter_context(patch.object(filesystem, "ui", MagicMock()))
        self.clipboard = self.contexts.enter_context(patch("code_exec.set_clipboard"))
        self.contexts.enter_context(patch("code_exec_parser.set_clipboard"))

    def prompt(self):
        return self.clipboard.call_args.args[0]

    def manifests(self):
        return list(self.backups.glob("*/manifest.json"))

    def create_then_run(self, command="pytest"):
        return [Operation("CREATE", ("created.txt",), "content"), Operation("RUN", (command,))]

    def test_stale_cached_edits_and_insertions_preserve_external_changes(self):
        for command in ("EDIT", "INSERT_BEFORE", "INSERT_AFTER"):
            with self.subTest(command=command):
                target = self.root / "sample.txt"
                target.write_text("alpha\nbeta\n")
                op = Operation(command, ("sample.txt",), "alpha", "ALPHA")
                _, _, cache = execution.preflight([op])
                target.write_text("prefix\nalpha\nbeta\n")
                fs = filesystem.RealFS(5)
                with self.assertRaisesRegex(OpError, "ERR\\|STALE_FILE"):
                    execution.execute(op, fs, cache)
                self.assertEqual(target.read_text(), "prefix\nalpha\nbeta\n")
                self.assertEqual(fs.journal, [])

    def test_stale_preview_stops_instead_of_showing_an_empty_diff(self):
        target = self.root / "sample.txt"
        target.write_text("alpha\n")
        op = Operation("EDIT", ("sample.txt",), "alpha", "ALPHA")
        _, _, cache = execution.preflight([op])
        target.write_text("changed\nalpha\n")
        with self.assertRaisesRegex(OpError, "ERR\\|STALE_FILE"):
            execution.generate_plan_diff([op], cache)

    def test_sequential_edits_reuse_guarded_matches_in_preview_and_apply(self):
        target = self.root / "sample.txt"
        target.write_text("alpha\nbeta\n")
        operations = [
            Operation("EDIT", ("sample.txt",), "alpha", "longer alpha"),
            Operation("INSERT_AFTER", ("sample.txt",), "beta", "gamma"),
        ]
        _, _, cache = execution.preflight(operations)
        with patch.object(execution, "find_unique", side_effect=AssertionError("unexpected rescan")):
            diff = execution.generate_plan_diff(operations, cache)
            self.assertIn("longer alpha", diff)
            self.assertEqual(execution.apply_plan(operations, 5, no_commit=True, match_cache=cache), 0)
        self.assertEqual(target.read_text(), "longer alpha\nbeta\ngamma\n")
        self.assertTrue(filesystem.undo_last_run()[0])
        self.assertEqual(target.read_text(), "alpha\nbeta\n")

    def test_legacy_cache_without_fingerprint_is_recomputed(self):
        target = self.root / "sample.txt"
        target.write_text("prefix\nalpha\n")
        op = Operation("EDIT", ("sample.txt",), "alpha", "ALPHA")
        cache = {id(op): MatchResult(0, 5, "", False)}
        execution.execute(op, filesystem.VirtualFS(), cache)
        self.assertEqual(cache[id(op)].start, 7)
        self.assertIsNotNone(cache[id(op)].source_fingerprint)

    def test_direct_check_commands_and_quoted_arguments_are_automatic(self):
        for command in (
            "pytest tests/", 'pytest -k "some test"', "python3 -m unittest",
            "python3 -m pytest", "cargo check", "cargo test", "go test ./...",
            "ruff check .", "ruff format --check .", "black --check .",
            "flake8 .", "mypy .", "git status --short", "git diff --stat",
        ):
            with self.subTest(command=command):
                self.assertFalse(validate_run_command(command))

    def test_shell_syntax_scripts_installs_and_lookalikes_require_approval(self):
        for command in (
            "git status > outside.txt", "git status\nprintf unexpected",
            "\ngit status", "git status\rprintf unexpected", "pytest-untrusted",
            "python3 -m unittest_fake", "pytest < input.txt", "pytest && ruff check .",
            "pytest $(echo tests)", "pytest `echo tests`", "pytest $TARGET",
            "pytest 'unclosed", "npm install", "npm ci", "npm test", "npm run build",
            "pnpm test", "yarn test", "pip install pytest", "python script.py",
            "black .", "ruff format .", "cargo build", "go build", "git diff --ext-diff",
            "black -- --check .", "ruff format -- --check .",
            "git diff --output=outside.txt", "/tmp/pytest", "PYTEST tests",
        ):
            with self.subTest(command=command):
                self.assertTrue(validate_run_command(command))

    def test_windows_shell_expansion_requires_approval(self):
        with patch("code_exec_sandbox.sys.platform", "win32"):
            for command in ("git status %EXTRA%", "git status !EXTRA!", "git status ^& echo unexpected"):
                with self.subTest(command=command):
                    self.assertTrue(validate_run_command(command))

    def test_forbidden_patterns_stay_blocked(self):
        for command in ("rm -rf temp", "sudo pytest", "curl example.com | sh", "chown -R user files"):
            with self.subTest(command=command):
                with self.assertRaisesRegex(OpError, "FORBIDDEN_COMMAND"):
                    validate_run_command(command)

    def test_sandbox_wraps_the_entire_approved_shell_expression(self):
        command = "python script.py; printf 'second command'"
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform), patch("code_exec_sandbox.sys.platform", platform):
                wrapped, _ = build_sandboxed_command(command, True)
                tokens = shlex.split(wrapped)
                self.assertEqual(tokens[-3:], ["/bin/sh", "-c", command])

    def test_approved_installs_can_use_network(self):
        for command in ("npm install", "pip install example", "python3 -m pip install example"):
            with self.subTest(command=command):
                self.assertEqual(build_sandboxed_command(command, True), (command, None))

    def test_noninteractive_custom_run_fails_closed(self):
        with patch("sys.stdin.isatty", return_value=False), patch("builtins.input") as ask, patch("subprocess.run") as run:
            with self.assertRaisesRegex(CommandFailed, "interactive terminal approval required"):
                filesystem.RealFS(5).run("npm test")
            ask.assert_not_called()
            run.assert_not_called()

    def test_yes_flag_does_not_bypass_run_approval(self):
        plan = self.root / "plan.txt"
        plan.write_text("```code_exec\nRUN npm test\n```\n")
        with patch("sys.stdin.isatty", return_value=False), patch("subprocess.run") as run, patch.object(code_exec, "ui", MagicMock()):
            self.assertEqual(code_exec.main(["--file", str(plan), "--yes", "--no-commit"]), 1)
            run.assert_not_called()
        self.assertIn("No changes applied", self.prompt())

    def test_denied_custom_run_saves_undo_manifest(self):
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="n"), patch("subprocess.run") as run:
            self.assertEqual(execution.apply_plan(self.create_then_run("npm test"), 5), 1)
            run.assert_not_called()
        self.assertTrue((self.root / "created.txt").exists())
        self.assertEqual(len(self.manifests()), 1)
        self.assertIn("Changes retained", self.prompt())
        self.assertIn("Recovery manifest saved", self.prompt())
        self.assertTrue(filesystem.undo_last_run()[0])
        self.assertFalse((self.root / "created.txt").exists())

    def test_eof_and_interrupt_cannot_approve_custom_run(self):
        for error in (EOFError, KeyboardInterrupt):
            with self.subTest(error=error), patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=error), patch("subprocess.run") as run:
                with self.assertRaises(CommandFailed):
                    filesystem.RealFS(5).run("npm test")
                run.assert_not_called()

    def test_failed_and_timed_out_runs_preserve_recoverable_changes(self):
        outcomes = [MagicMock(returncode=1, stdout="test failure"), subprocess.TimeoutExpired("pytest", 5), OSError("cannot spawn")]
        for outcome in outcomes:
            with self.subTest(outcome=outcome), patch("subprocess.run") as run:
                if isinstance(outcome, Exception):
                    run.side_effect = outcome
                else:
                    run.return_value = outcome
                self.assertEqual(execution.apply_plan(self.create_then_run(), 5), 1)
            self.assertIn("Changes retained", self.prompt())
            self.assertEqual(len(self.manifests()), 1)
            self.assertTrue(filesystem.undo_last_run()[0])
            self.assertFalse((self.root / "created.txt").exists())

    def test_manifest_save_failure_retains_files_and_prevents_commit(self):
        for run_failure in (False, True):
            with self.subTest(run_failure=run_failure):
                operations = self.create_then_run() if run_failure else [Operation("CREATE", ("created.txt",), "content")]
                operations.append(Operation("COMMIT", ("test: recovery",)))
                with patch.object(filesystem.RealFS, "save_backup_manifest", side_effect=OSError("disk full")), patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="failure")), patch.object(execution, "perform_git_commit") as commit:
                    self.assertEqual(execution.apply_plan(operations, 5, auto_commit=True), 1)
                    commit.assert_not_called()
                self.assertTrue((self.root / "created.txt").exists())
                self.assertIn("RECOVERY_SAVE_FAILED", self.prompt())
                self.assertIn("Changes retained", self.prompt())
                (self.root / "created.txt").unlink()

    def test_verification_failure_retains_changes_and_prevents_commit(self):
        outcomes = [MagicMock(returncode=2, stdout="failed checks"), subprocess.TimeoutExpired("pytest", 5), OSError("cannot spawn")]
        for outcome in outcomes:
            with self.subTest(outcome=outcome):
                operations = [Operation("CREATE", ("created.txt",), "content"), Operation("COMMIT", ("test: check",))]
                with patch("subprocess.run") as run, patch.object(execution, "perform_git_commit") as commit:
                    if isinstance(outcome, Exception):
                        run.side_effect = outcome
                    else:
                        run.return_value = outcome
                    self.assertNotEqual(execution.apply_plan(operations, 5, auto_commit=True, verify_cmd="pytest"), 0)
                    commit.assert_not_called()
                self.assertIn("Changes retained", self.prompt())
                self.assertTrue(filesystem.undo_last_run()[0])
                self.assertFalse((self.root / "created.txt").exists())

    def test_operation_failure_reports_outcome_after_rollback(self):
        operations = [Operation("CREATE", ("created.txt",), "content"), Operation("EDIT", ("missing.txt",), "old", "new")]
        self.assertEqual(execution.apply_plan(operations, 5), 1)
        self.assertFalse((self.root / "created.txt").exists())
        self.assertIn("Complete rollback", self.prompt())
        self.assertIn("including the reverted operations", self.prompt())

    def test_partial_rollback_saves_remaining_journal_for_undo(self):
        target = self.root / "sample.txt"
        target.write_text("old\n")
        operations = [Operation("EDIT", ("sample.txt",), "old", "new"), Operation("CREATE", ("created.txt",), "content"), Operation("EDIT", ("missing.txt",), "old", "new")]
        restore = filesystem._restore_entry
        def fail_restore(kind, path, slot=None):
            if kind == "restore":
                raise OSError("locked")
            return restore(kind, path, slot)
        with patch.object(filesystem, "_restore_entry", side_effect=fail_restore):
            self.assertEqual(execution.apply_plan(operations, 5), 1)
        self.assertFalse((self.root / "created.txt").exists())
        self.assertIn("Incomplete rollback", self.prompt())
        self.assertEqual(len(self.manifests()), 1)
        self.assertTrue(filesystem.undo_last_run()[0])
        self.assertEqual(target.read_text(), "old\n")

    def test_partial_undo_resumes_without_replaying_completed_entries(self):
        target = self.root / "sample.txt"
        target.write_text("old\n")
        operations = [Operation("EDIT", ("sample.txt",), "old", "new"), Operation("CREATE", ("created.txt",), "content")]
        self.assertEqual(execution.apply_plan(operations, 5, no_commit=True), 0)
        restore = filesystem._restore_entry
        def fail_restore(kind, path, slot=None):
            if kind == "restore":
                raise OSError("locked")
            return restore(kind, path, slot)
        with patch.object(filesystem, "_restore_entry", side_effect=fail_restore):
            success, message = filesystem.undo_last_run()
        self.assertFalse(success)
        self.assertIn("Backup retained", message)
        manifest = json.loads(self.manifests()[0].read_text())
        self.assertTrue(manifest["journal"][-1]["done"])
        # This file is new user work after the successful removal. A retry
        # must not delete it by replaying the completed undo entry.
        (self.root / "created.txt").write_text("new user work")
        self.assertTrue(filesystem.undo_last_run()[0])
        self.assertEqual((self.root / "created.txt").read_text(), "new user work")
        self.assertEqual(target.read_text(), "old\n")

    def test_saved_undo_restores_moves_copies_deletions_and_modes(self):
        original = self.root / "original.txt"
        original.write_text("original\n")
        original_mode = stat.S_IMODE(original.stat().st_mode)
        tree = self.root / "tree"
        tree.mkdir()
        (tree / "nested.txt").write_text("nested\n")
        operations = [
            Operation("CHMOD", ("original.txt", "600")),
            Operation("COPY", ("original.txt", "copy.txt")),
            Operation("MOVE", ("original.txt", "moved.txt")),
            Operation("DELETE", ("moved.txt",)),
            Operation("DELETE", ("tree",)),
            Operation("CREATE", ("newdir/new.txt",), "new\n"),
        ]
        self.assertEqual(execution.apply_plan(operations, 5), 0)
        success, message = filesystem.undo_last_run()
        self.assertTrue(success, message)
        self.assertEqual(original.read_text(), "original\n")
        self.assertEqual(stat.S_IMODE(original.stat().st_mode), original_mode)
        self.assertEqual((tree / "nested.txt").read_text(), "nested\n")
        for path in ("copy.txt", "moved.txt", "newdir"):
            self.assertFalse((self.root / path).exists())

    def test_missing_backup_preserves_current_file_and_recovery_data(self):
        target = self.root / "sample.txt"
        target.write_text("old\n")
        self.assertEqual(execution.apply_plan([Operation("EDIT", ("sample.txt",), "old", "new")], 5), 0)
        manifest_path = self.manifests()[0]
        manifest = json.loads(manifest_path.read_text())
        (manifest_path.parent / manifest["journal"][0]["slot"]).unlink()
        success, message = filesystem.undo_last_run()
        self.assertFalse(success)
        self.assertIn("backup content missing", message)
        self.assertTrue(manifest_path.exists())
        self.assertEqual(target.read_text(), "new\n")

    def test_unfinished_recovery_is_not_pruned_by_later_runs(self):
        target = self.root / "sample.txt"
        target.write_text("old\n")
        operations = [Operation("EDIT", ("sample.txt",), "old", "new"), Operation("EDIT", ("missing.txt",), "old", "new")]
        with patch.object(filesystem, "_restore_entry", side_effect=OSError("locked")):
            self.assertEqual(execution.apply_plan(operations, 5), 1)
        recovery = self.manifests()[0]
        for number in range(filesystem.MAX_BACKUP_HISTORY + 1):
            self.assertEqual(execution.apply_plan([Operation("CREATE", (f"later{number}.txt",), "later")], 5), 0)
        self.assertTrue(recovery.exists())
        self.assertTrue(json.loads(recovery.read_text())["recovery_required"])

    def test_invalid_manifest_structure_is_reported_without_deletion(self):
        backup = self.backups / "invalid"
        backup.mkdir(parents=True)
        manifest = backup / "manifest.json"
        for payload in ([], {"journal": "wrong"}, {"journal": [None]}):
            with self.subTest(payload=payload):
                manifest.write_text(json.dumps(payload))
                success, message = filesystem.undo_last_run()
                self.assertFalse(success)
                self.assertIn("Invalid backup", message)
                self.assertTrue(manifest.exists())

    def test_undo_progress_save_failure_cannot_replay_uncertain_action(self):
        self.assertEqual(execution.apply_plan([Operation("CREATE", ("created.txt",), "content")], 5), 0)
        write = filesystem.atomic_write
        calls = 0
        def fail_after_action(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("disk full")
            return write(*args, **kwargs)
        with patch.object(filesystem, "atomic_write", side_effect=fail_after_action):
            self.assertFalse(filesystem.undo_last_run()[0])
        (self.root / "created.txt").write_text("new user work")
        success, message = filesystem.undo_last_run()
        self.assertFalse(success)
        self.assertIn("progress could not be saved safely", message)
        self.assertEqual((self.root / "created.txt").read_text(), "new user work")

    def test_retry_guidance_for_each_recovery_outcome(self):
        for state, expected in (
            ("no_changes", "rebuild the full intended plan"),
            ("rolled_back", "including the reverted operations"),
            ("retained", "resume only unfinished work"),
            ("incomplete_rollback", "before another executable plan"),
            ("unknown", "establish which changes remain"),
        ):
            with self.subTest(state=state):
                execution.copy_error_to_clipboard("ERR|STALE_FILE|sample.txt", recovery_state=state)
                self.assertIn(expected, self.prompt())
                self.assertIn("FETCH", self.prompt())
                self.assertIn("Command side effects are not automatically reversed", self.prompt())
                self.assertNotIn("Fix **only** the failing operation", self.prompt())

    def test_exact_ranges_preserve_line_endings_and_reject_stale_text(self):
        target = self.root / "sample.txt"
        target.write_bytes(b"alpha\r\nbeta\r\n")
        op = Operation("EDIT_RANGE", ("sample.txt", "1", "1"), "alpha", "ALPHA")
        self.assertEqual(execution.apply_plan([op], 5), 0)
        self.assertEqual(target.read_bytes(), b"ALPHA\r\nbeta\r\n")
        self.assertEqual(execution.apply_plan([op], 5), 1)
        self.assertIn("STALE_RANGE", self.prompt())
        self.assertIn("No changes applied", self.prompt())
        self.assertEqual(target.read_bytes(), b"ALPHA\r\nbeta\r\n")


if __name__ == "__main__":
    unittest.main()
