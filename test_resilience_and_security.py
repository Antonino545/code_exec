#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from code_exec_types import Operation, OpError, ROOT
from code_exec_fs import (
    BACKUP_ROOT,
    RealFS,
    safe_path,
    undo_last_run,
)
from code_exec_matcher import find_unique, _adjust_indentation
from code_exec_sandbox import validate_run_command
from code_exec import apply_plan, execute


class TestResilienceAndSecurity(unittest.TestCase):
    def setUp(self):
        self.scratch = ROOT / "_test_scratch"
        self.scratch_rel = "_test_scratch"
        if self.scratch.exists():
            shutil.rmtree(self.scratch, ignore_errors=True)
        self.scratch.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        if self.scratch.exists():
            shutil.rmtree(self.scratch, ignore_errors=True)

    def test_realfs_operations_and_rollback(self):
        fs = RealFS(timeout=10)

        # 1. MKDIR
        op_mkdir = Operation("MKDIR", (f"{self.scratch_rel}/subdir",))
        execute(op_mkdir, fs)
        self.assertTrue((self.scratch / "subdir").is_dir())

        # 2. CREATE
        op_create = Operation("CREATE", (f"{self.scratch_rel}/subdir/sample.txt",), "base line\n")
        execute(op_create, fs)
        self.assertEqual((self.scratch / "subdir/sample.txt").read_text(encoding="utf-8"), "base line\n")

        # 3. APPEND
        op_append = Operation("APPEND", (f"{self.scratch_rel}/subdir/sample.txt",), "appended line\n")
        execute(op_append, fs)
        self.assertEqual(
            (self.scratch / "subdir/sample.txt").read_text(encoding="utf-8"),
            "base line\nappended line\n",
        )

        # 4. PREPEND
        op_prepend = Operation("PREPEND", (f"{self.scratch_rel}/subdir/sample.txt",), "prepended line\n")
        execute(op_prepend, fs)
        self.assertEqual(
            (self.scratch / "subdir/sample.txt").read_text(encoding="utf-8"),
            "prepended line\nbase line\nappended line\n",
        )

        # 5. INSERT_BEFORE
        op_ins_b = Operation("INSERT_BEFORE", (f"{self.scratch_rel}/subdir/sample.txt",), "base line", "before base")
        execute(op_ins_b, fs)
        self.assertIn("before base\nbase line", (self.scratch / "subdir/sample.txt").read_text(encoding="utf-8"))

        # 6. INSERT_AFTER
        op_ins_a = Operation("INSERT_AFTER", (f"{self.scratch_rel}/subdir/sample.txt",), "base line", "after base")
        execute(op_ins_a, fs)
        self.assertIn("base line\nafter base", (self.scratch / "subdir/sample.txt").read_text(encoding="utf-8"))

        # 7. COPY
        op_copy = Operation("COPY", (f"{self.scratch_rel}/subdir/sample.txt", f"{self.scratch_rel}/subdir/copied.txt"))
        execute(op_copy, fs)
        self.assertTrue((self.scratch / "subdir/copied.txt").is_file())

        # 8. RENAME
        op_rename = Operation("RENAME", (f"{self.scratch_rel}/subdir/copied.txt", f"{self.scratch_rel}/subdir/renamed.txt"))
        execute(op_rename, fs)
        self.assertFalse((self.scratch / "subdir/copied.txt").exists())
        self.assertTrue((self.scratch / "subdir/renamed.txt").is_file())

        # 9. MOVE
        op_move = Operation("MOVE", (f"{self.scratch_rel}/subdir/renamed.txt", f"{self.scratch_rel}/moved.txt"))
        execute(op_move, fs)
        self.assertFalse((self.scratch / "subdir/renamed.txt").exists())
        self.assertTrue((self.scratch / "moved.txt").is_file())
        # 10. PATCH
        patch_txt = (
            "@@ -1,3 +1,3 @@\n"
            " prepended line\n"
            "-base line\n"
            "+base line patched\n"
            " after base\n"
        )
        op_patch = Operation("PATCH", (f"{self.scratch_rel}/moved.txt",), patch_txt)
        execute(op_patch, fs)
        self.assertIn("base line patched", (self.scratch / "moved.txt").read_text(encoding="utf-8"))
        # Rollback all disk modifications
        errors = fs.rollback()
        self.assertEqual(errors, [])
        self.assertFalse((self.scratch / "moved.txt").exists())
        self.assertFalse((self.scratch / "subdir").exists())
        fs.cleanup()

    def test_mid_execution_failure_automatic_rollback(self):
        target = self.scratch / "rollback_target.txt"
        target.write_text("initial content\n", encoding="utf-8")

        # Plan with 3 operations: 1 and 2 succeed, 3 fails on missing anchor
        ops = [
            Operation("EDIT", (f"{self.scratch_rel}/rollback_target.txt",), "initial content\n", "modified step 1\n"),
            Operation("CREATE", (f"{self.scratch_rel}/step2.txt",), "step 2 content\n"),
            Operation("EDIT", (f"{self.scratch_rel}/rollback_target.txt",), "nonexistent pattern\n", "fail\n"),
        ]

        exit_code = apply_plan(ops, timeout=60, no_commit=True, auto_commit=True)
        self.assertEqual(exit_code, 1)
        # Verify rollback restored modified file and removed created file
        self.assertEqual(target.read_text(encoding="utf-8"), "initial content\n")
        self.assertFalse((self.scratch / "step2.txt").exists())

    def test_file_permission_preservation(self):
        if sys.platform == "win32":
            self.skipTest("POSIX file permission testing not applicable on Windows")

        script_file = self.scratch / "runnable.sh"
        script_file.write_text("#!/bin/sh\necho 1\n", encoding="utf-8")
        os.chmod(script_file, 0o755)

        fs = RealFS(timeout=10)
        op = Operation("EDIT", (f"{self.scratch_rel}/runnable.sh",), "echo 1\n", "echo 2\n")
        execute(op, fs)
        fs.cleanup()

        current_mode = stat.S_IMODE(script_file.stat().st_mode)
        self.assertEqual(current_mode & 0o777, 0o755)
        self.assertIn("echo 2", script_file.read_text(encoding="utf-8"))

    def test_crlf_preservation(self):
        crlf_file = self.scratch / "crlf_test.txt"
        crlf_file.write_bytes(b"header\r\nvalue = old\r\nfooter\r\n")

        fs = RealFS(timeout=10)
        op = Operation("EDIT", (f"{self.scratch_rel}/crlf_test.txt",), "value = old\r\n", "value = new\r\n")
        execute(op, fs)
        fs.cleanup()

        raw_bytes = crlf_file.read_bytes()
        self.assertEqual(raw_bytes, b"header\r\nvalue = new\r\nfooter\r\n")

    def test_security_path_traversal_and_case_insensitive_protections(self):
        with self.assertRaises(OpError):
            safe_path("../../outside.txt")
        with self.assertRaises(OpError):
            safe_path(".ENV")
        with self.assertRaises(OpError):
            safe_path(".env.LOCAL")
        with self.assertRaises(OpError):
            safe_path(".git/config")
        with self.assertRaises(OpError):
            safe_path("nested/.git/config")
        with self.assertRaises(OpError):
            safe_path(".GitLab-CI.yml")
        with self.assertRaises(OpError):
            safe_path("deploy_key.PEM")
        with self.assertRaises(OpError):
            safe_path("private.KEY")
        with self.assertRaises(OpError):
            safe_path("id_RSA")

    def test_security_symlink_escape(self):
        if sys.platform == "win32" or not hasattr(os, "symlink"):
            self.skipTest("Symlink testing skipped on this platform")

        outside_temp = Path(tempfile.mkdtemp(prefix="code_exec_outside_"))
        try:
            symlink_target = self.scratch / "symlink_outside"
            os.symlink(str(outside_temp), str(symlink_target))
            with self.assertRaises(OpError) as ctx:
                safe_path(f"{self.scratch_rel}/symlink_outside/evil.txt")
            self.assertIn("ERR|INVALID_PATH", str(ctx.exception))
        finally:
            shutil.rmtree(outside_temp, ignore_errors=True)

    def test_fuzzy_matching_strict_threshold_rejection(self):
        doc = "def calculate_price(quantity, discount=0.10):\n    return quantity * 10 * (1 - discount)\n"
        # Heavily modified needle (< 90% similarity) must be rejected
        needle = "def compute_price(amount, disc=0.20):\n    return amount * 20 * (1 - disc)\n"
        with self.assertRaises(OpError) as ctx:
            find_unique(doc, needle, "SEARCH", "calc.py")
        self.assertIn("ERR|SEARCH_NOT_FOUND", str(ctx.exception))

    def test_adjust_indentation_matrix(self):
        doc = "class Service:\n        def execute(self):\n            pass\n"
        search_snippet = "  def execute(self):\n      pass\n"
        replace_snippet = "  def execute(self):\n      return True\n"
        # Adjust replacement to match doc line indentation (8 spaces)
        adjusted = _adjust_indentation(doc, (1, 2), search_snippet, replace_snippet, "\n")
        self.assertIn("        def execute(self):\n", adjusted)
        self.assertIn("            return True", adjusted)

    def test_undo_corrupted_manifest_handled_safely(self):
        corrupt_backup = BACKUP_ROOT / "99999999_999999_corrupt"
        corrupt_backup.mkdir(parents=True, exist_ok=True)
        try:
            manifest_file = corrupt_backup / "manifest.json"
            manifest_file.write_text("{malformed_json: true", encoding="utf-8")
            success, msg = undo_last_run()
            self.assertFalse(success)
            self.assertIn("Failed to read backup manifest", msg)
        finally:
            shutil.rmtree(corrupt_backup, ignore_errors=True)

    def test_replace_all_command(self):
        target = self.scratch / "multi_replace.txt"
        target.write_text("apple banana apple cherry apple\n", encoding="utf-8")
        fs = RealFS(timeout=10)
        op = Operation("REPLACE_ALL", (f"{self.scratch_rel}/multi_replace.txt",), "apple", "orange")
        msg = execute(op, fs)
        self.assertIn("3 occurrence", msg)
        self.assertEqual(target.read_text(encoding="utf-8"), "orange banana orange cherry orange\n")
        fs.cleanup()

    def test_touch_and_chmod_commands(self):
        target = self.scratch / "touched.sh"
        fs = RealFS(timeout=10)
        op_touch = Operation("TOUCH", (f"{self.scratch_rel}/touched.sh",))
        execute(op_touch, fs)
        self.assertTrue(target.is_file())

        if sys.platform != "win32":
            op_chmod = Operation("CHMOD", (f"{self.scratch_rel}/touched.sh", "+x"))
            execute(op_chmod, fs)
            mode = stat.S_IMODE(target.stat().st_mode)
            self.assertTrue(bool(mode & 0o111))
        fs.cleanup()

    def test_clipboard_error_feedback_integration(self):
        from unittest.mock import patch
        from code_exec import copy_error_to_clipboard
        with patch("code_exec.set_clipboard") as mock_set:
            copy_error_to_clipboard("ERR|SEARCH_NOT_FOUND|foo.py")
            mock_set.assert_called_once()
            clipboard_content = mock_set.call_args[0][0]
            self.assertIn("ERR|SEARCH_NOT_FOUND|foo.py", clipboard_content)
            self.assertIn("`code_exec` plan failed", clipboard_content)
            self.assertIn("Troubleshooting Guidance", clipboard_content)
            self.assertIn("SEARCH_NOT_FOUND", clipboard_content)

    def test_generate_commit_prompt(self):
        from unittest.mock import patch, MagicMock
        from code_exec import generate_commit_prompt

        fake_diff = MagicMock(stdout="diff --git a/test.py b/test.py\n+new_feature = True\n")
        fake_status = MagicMock(stdout="?? untracked_file.txt\n")

        with patch("subprocess.run", side_effect=[fake_diff, fake_status]):
            prompt = generate_commit_prompt()
            self.assertIn("Generate a concise, scoped conventional commit message", prompt)
            self.assertIn("+new_feature = True", prompt)
            self.assertIn("untracked_file.txt", prompt)
            self.assertIn("COMMIT type(scope):", prompt)

    def test_perform_git_commit_standalone_success(self):
        from unittest.mock import patch, MagicMock
        from code_exec import perform_git_commit
        status_mock = MagicMock(stdout=" M file.py\n")
        cached_mock = MagicMock(stdout="file.py\n")
        commit_mock = MagicMock(stdout="[main abc1234] feat: standalone\n")
        with patch("subprocess.run", side_effect=[status_mock, MagicMock(), cached_mock, commit_mock]):
            success, msg = perform_git_commit("feat: standalone", [])
            self.assertTrue(success)
            self.assertIn("feat: standalone", msg)

    def test_perform_git_commit_standalone_clean(self):
        from unittest.mock import patch, MagicMock
        from code_exec import perform_git_commit
        status_mock = MagicMock(stdout="")
        with patch("subprocess.run", return_value=status_mock):
            success, msg = perform_git_commit("feat: standalone", [])
            self.assertFalse(success)
            self.assertIn("No changes detected in git repository to commit", msg)

    def test_complex_five_stage_rollback(self):
        fs = RealFS(timeout=10)
        # Stage 1: Create directory
        op1 = Operation("MKDIR", (f"{self.scratch_rel}/stage_dir",))
        execute(op1, fs)
        # Stage 2: Create base file
        op2 = Operation("CREATE", (f"{self.scratch_rel}/stage_dir/base.txt",), "line 1\nline 2\n")
        execute(op2, fs)
        # Stage 3: Edit base file
        op3 = Operation("EDIT", (f"{self.scratch_rel}/stage_dir/base.txt",), "line 2\n", "line 2 edited\n")
        execute(op3, fs)
        # Stage 4: Patch base file
        patch_text = "@@ -1,2 +1,2 @@\n line 1\n-line 2 edited\n+line 2 patched\n"
        op4 = Operation("PATCH", (f"{self.scratch_rel}/stage_dir/base.txt",), patch_text)
        execute(op4, fs)
        self.assertIn("line 2 patched", (self.scratch / "stage_dir/base.txt").read_text(encoding="utf-8"))

        # Rollback all 4 operations
        errs = fs.rollback()
        self.assertEqual(errs, [])
        self.assertFalse((self.scratch / "stage_dir/base.txt").exists())
        self.assertFalse((self.scratch / "stage_dir").exists())
        fs.cleanup()

    def test_chmod_rollback(self):
        if sys.platform == "win32":
            self.skipTest("POSIX file permission testing not applicable on Windows")
        script = self.scratch / "chmod_test.sh"
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(script, 0o644)
        fs = RealFS(timeout=10)

        op = Operation("CHMOD", (f"{self.scratch_rel}/chmod_test.sh", "+x"))
        execute(op, fs)
        current_mode = stat.S_IMODE(script.stat().st_mode)
        self.assertTrue(bool(current_mode & 0o111))

        # Rollback
        errs = fs.rollback()
        self.assertEqual(errs, [])
        restored_mode = stat.S_IMODE(script.stat().st_mode)
        self.assertEqual(restored_mode & 0o777, 0o644)
        fs.cleanup()

    def test_safe_path_extended_traversal_attempts(self):
        traversals = [
            f"{self.scratch_rel}/././../../outside.py",
            "subdir/../../outside.txt",
            ".git",
            ".code_exec",
            "nested/.code_exec",
            ".ENV.staging",
            "id_ed25519",
            "cert.CRT",
            ".github/workflows/test.yml",
            ".GitLab-CI.yml",
        ]
        for bad_path in traversals:
            with self.assertRaises(OpError) as ctx:
                safe_path(bad_path)
            self.assertTrue(
                "ERR|INVALID_PATH" in str(ctx.exception)
                or "ERR|PROTECTED_PATH" in str(ctx.exception)
                or "ERR|FILE_PROTECTED" in str(ctx.exception)
            )


if __name__ == "__main__":
    unittest.main()
