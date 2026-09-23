#!/usr/bin/env python3
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from code_exec_types import Operation, ROOT
from code_exec_parser import parse_operations
from code_exec import preflight, generate_plan_diff, main
from code_exec_plan_export import create_plan_bundle, create_plan_folder
from code_exec_ui import ui


class TestImprovements(unittest.TestCase):
    def setUp(self):
        self.original_cwd = os.getcwd()
        self.original_root = ROOT
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_root = Path(self.temp_dir.name).resolve()

    def tearDown(self):
        os.chdir(self.original_cwd)
        import code_exec_types
        import code_exec_fs
        import code_exec
        code_exec_types.ROOT = self.original_root
        code_exec_fs.ROOT = self.original_root
        code_exec_fs.BACKUP_ROOT = self.original_root / ".code_exec" / "backups"
        code_exec.ROOT = self.original_root
        self.temp_dir.cleanup()

    def test_version_flag(self):
        """Verify that --version and -V output the correct version string."""
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(["--version"])
        self.assertEqual(code, 0)
        self.assertEqual(buf.getvalue().strip(), "code-exec 1.3.0")

        buf_short = io.StringIO()
        with patch("sys.stdout", buf_short):
            code_short = main(["-V"])
        self.assertEqual(code_short, 0)
        self.assertEqual(buf_short.getvalue().strip(), "code-exec 1.3.0")

    def test_short_prompt_file_selection(self):
        """Verify that --short loads code_exec_instructions_short.md."""
        copied = {}

        def mock_set_clipboard(text: str):
            copied["content"] = text

        with patch("code_exec.set_clipboard", side_effect=mock_set_clipboard):
            code = main(["-p", "--short"])
            self.assertEqual(code, 0)
            self.assertIn("compact edition", copied.get("content", ""))

    def test_preflight_wildcard_and_regex_move(self):
        """Verify preflight does not crash on wildcard or regex MOVE / COPY patterns."""
        (self.test_root / "temp_test.py").write_text("a = 1\n", encoding="utf-8")
        (self.test_root / "test_sample.txt").write_text("test\n", encoding="utf-8")

        with patch("code_exec_types.ROOT", self.test_root), \
             patch("code_exec_fs.ROOT", self.test_root), \
             patch("code_exec.ROOT", self.test_root):
            plan = (
                "MOVE regex:^temp_.*\\.py -> backup/\n"
                "COPY test*.txt -> dest/\n"
            )
            ops = parse_operations(plan)
            reason, deferred, cache = preflight(ops)
            self.assertIsNotNone(ops)

    def test_generate_plan_diff_with_copy(self):
        """Verify generate_plan_diff correctly handles COPY operations and subsequent edits."""
        # Create a source file in temp directory
        src_file = self.test_root / "original.txt"
        src_file.write_text("line 1\nline 2\n", encoding="utf-8")

        # Temporarily mock ROOT to test_root for safe_path resolution
        with patch("code_exec_types.ROOT", self.test_root), \
             patch("code_exec_fs.ROOT", self.test_root):
            plan = (
                "COPY original.txt -> copied.txt\n"
                "EDIT copied.txt\n"
                "SEARCH <<<\n"
                "line 2\n"
                ">>>\n"
                "REPLACE <<<\n"
                "line 2 modified\n"
                ">>>\n"
            )
            ops = parse_operations(plan)
            diff_text = generate_plan_diff(ops)
            self.assertIn("copy from original.txt", diff_text)
            self.assertIn("+line 2 modified", diff_text)

    def test_create_plan_bundle(self):
        """Verify create_plan_bundle generates a single-file PROJECT_CONTEXT.md bundle."""
        (self.test_root / "hello.py").write_text("print('hello world')\n", encoding="utf-8")
        (self.test_root / "notes.txt").write_text("some notes\n", encoding="utf-8")

        res = create_plan_bundle(root=self.test_root, output_file="PROJECT_CONTEXT.md")
        bundle_path = self.test_root / "PROJECT_CONTEXT.md"

        self.assertTrue(bundle_path.is_file())
        content = bundle_path.read_text(encoding="utf-8")
        self.assertIn("# Project Context:", content)
        self.assertIn("File Index", content)
        self.assertIn("hello.py", content)
        self.assertIn("print('hello world')", content)
        self.assertEqual(res["included"], 2)

    def test_project_dir_flag(self):
        """Verify that -C / --project-dir sets the working project root correctly."""
        (self.test_root / "sample.txt").write_text("sample content\n", encoding="utf-8")

        # Using stdin simulation with --check and -C
        with patch("sys.stdin", io.StringIO("DELETE sample.txt\n")):
            code = main(["-C", str(self.test_root), "--check", "--file", "-"])
            self.assertEqual(code, 0)

    def test_ui_menu_option_9(self):
        """Verify that menu option 9 maps to short-prompt."""
        with patch.object(ui, "prompt_choice", return_value="9"), \
             patch.object(ui, "interactive_menu", return_value="9"):
            copied = {}
            with patch("code_exec.set_clipboard", side_effect=lambda txt: copied.update(val=txt)):
                with patch("sys.stdin.isatty", return_value=True):
                    # Passing no arguments invokes the interactive menu
                    code = main([])
                    self.assertEqual(code, 0)
                    self.assertIn("compact edition", copied.get("val", ""))

    def test_ui_menu_options_bundle_and_theme(self):
        """Verify that menu options 'b' and 't' work properly."""
        # Test 't' theme toggle
        with patch.object(ui, "interactive_menu", return_value="t"):
            with patch("sys.stdin.isatty", return_value=True):
                code = main([])
                self.assertEqual(code, 0)

        # Test 'b' bundle export
        with patch.object(ui, "interactive_menu", return_value="b"):
            with patch("sys.stdin.isatty", return_value=True):
                with patch("code_exec_plan_export.create_plan_bundle") as mock_bundle:
                    mock_bundle.return_value = {
                        "location": "PROJECT_CONTEXT.md",
                        "included": 1,
                        "ignored": 0,
                        "tokens": 10,
                        "size_kb": 0.1,
                        "ignore_file": ".gitignore",
                        "compact": False,
                    }
                    code = main([])
                    self.assertEqual(code, 0)
                    mock_bundle.assert_called_once()

    def test_pyproject_contains_all_modules(self):
        """Verify pyproject.toml lists code_exec_plan_export."""
        pyproject_path = ROOT / "pyproject.toml"
        self.assertTrue(pyproject_path.is_file())
        text = pyproject_path.read_text(encoding="utf-8")
        self.assertIn('"code_exec_plan_export"', text)

    def test_bundle_action_and_flag(self):
        """Verify that 'code-exec bundle' and '-b' export the bundle and copy to clipboard."""
        (self.test_root / "app.py").write_text("print('test')\n", encoding="utf-8")

        copied_text = {}
        with patch("code_exec.set_clipboard", side_effect=lambda txt: copied_text.update(val=txt)), \
             patch("code_exec.set_clipboard_file", return_value=True):
            code = main(["-C", str(self.test_root), "bundle"])
            self.assertEqual(code, 0)
            bundle_file = self.test_root / "PROJECT_CONTEXT.md"
            self.assertTrue(bundle_file.is_file())
            self.assertIn("app.py", copied_text.get("val", ""))

    def test_bundle_defaults_to_compact_mode(self):
        """Verify bundle defaults to compact=True unless --full is passed."""
        (self.test_root / "main.py").write_text("def hello():\n    pass\n", encoding="utf-8")
        with patch("code_exec_plan_export.create_plan_bundle") as mock_bundle:
            mock_bundle.return_value = {
                "location": "PROJECT_CONTEXT.md",
                "included": 1,
                "ignored": 0,
                "tokens": 10,
                "size_kb": 0.1,
                "ignore_file": ".gitignore",
                "compact": True,
            }
            code = main(["-C", str(self.test_root), "bundle"])
            self.assertEqual(code, 0)
            _, kwargs = mock_bundle.call_args
            self.assertTrue(kwargs.get("compact"))

        with patch("code_exec_plan_export.create_plan_bundle") as mock_bundle:
            mock_bundle.return_value = {
                "location": "PROJECT_CONTEXT.md",
                "included": 1,
                "ignored": 0,
                "tokens": 10,
                "size_kb": 0.1,
                "ignore_file": ".gitignore",
                "compact": False,
            }
            code = main(["-C", str(self.test_root), "bundle", "--full"])
            self.assertEqual(code, 0)
            _, kwargs = mock_bundle.call_args
            self.assertFalse(kwargs.get("compact"))

    def test_export_cleans_prior_context_and_bundle(self):
        """Verify that context export and bundle export delete each other's old artifacts."""
        # 1. Start with an existing context/ directory and a bundle file
        old_context_dir = self.test_root / "context"
        old_context_dir.mkdir(parents=True, exist_ok=True)
        (old_context_dir / "stale.txt").write_text("old", encoding="utf-8")

        old_bundle = self.test_root / "PROJECT_CONTEXT.md"
        old_bundle.write_text("old bundle", encoding="utf-8")

        (self.test_root / "main.py").write_text("x = 1\n", encoding="utf-8")

        # Running create_plan_bundle should wipe context/
        create_plan_bundle(root=self.test_root, output_file="PROJECT_CONTEXT.md")
        self.assertFalse(old_context_dir.exists())
        self.assertTrue(old_bundle.is_file())

        # Now running create_plan_folder should wipe PROJECT_CONTEXT.md
        create_plan_folder(root=self.test_root, output_dirname="context")
        self.assertTrue(old_context_dir.exists())
        self.assertFalse(old_bundle.exists())

    def test_large_bundle_copies_file_to_clipboard(self):
        """Verify that when the context bundle exceeds the token/size threshold, the file is copied to clipboard."""
        for i in range(120):
            (self.test_root / f"file_{i}.txt").write_text("a very long line of code and context for testing\n" * 35, encoding="utf-8")

        copied_files = []
        copied_texts = []
        with patch("code_exec.set_clipboard_file", side_effect=lambda p: (copied_files.append(p) or True)), \
             patch("code_exec.set_clipboard", side_effect=lambda txt: copied_texts.append(txt)):
            code = main(["-C", str(self.test_root), "bundle"])
            self.assertEqual(code, 0)
            self.assertEqual(len(copied_files), 1)
            self.assertEqual(copied_files[0].name, "PROJECT_CONTEXT.md")
            # Raw text shouldn't be dumped into clipboard because file copy succeeded for large payload
            self.assertEqual(len(copied_texts), 0)


if __name__ == "__main__":
    unittest.main()


