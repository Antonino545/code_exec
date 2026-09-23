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

    def test_completions_script_generation(self):
        """Verify that bash, zsh, and fish completion scripts generate valid content."""
        from code_exec_completions import generate_bash_completions, generate_zsh_completions, generate_fish_completions

        zsh = generate_zsh_completions()
        self.assertIn("#compdef code-exec", zsh)
        self.assertIn("subcommands=(", zsh)
        self.assertIn("themes=(", zsh)

        bash = generate_bash_completions()
        self.assertIn("_code_exec_completions()", bash)
        self.assertIn("complete -F _code_exec_completions code-exec", bash)

        fish = generate_fish_completions()
        self.assertIn("complete -c code-exec", fish)
        self.assertIn("apply", fish)

    def test_install_completions_fish_and_zsh(self):
        """Verify install_completions writes proper files in mock home directory."""
        from code_exec_completions import install_completions

        fake_home = self.test_root / "fake_home"
        fake_home.mkdir(parents=True, exist_ok=True)
        with patch("pathlib.Path.home", return_value=fake_home):
            # Test Fish install
            ok, sh, path = install_completions("fish")
            self.assertTrue(ok)
            self.assertEqual(sh, "fish")
            self.assertTrue(Path(path).is_file())

            # Test Zsh install
            ok, sh, path = install_completions("zsh")
            self.assertTrue(ok)
            self.assertEqual(sh, "zsh")
            self.assertTrue(Path(path).is_file())
            zshrc = fake_home / ".zshrc"
            self.assertTrue(zshrc.is_file())
            self.assertIn(".zsh/completions", zshrc.read_text(encoding="utf-8"))

    def test_completions_cli_actions(self):
        """Verify code-exec completions prints to stdout and --install-completions works."""
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            code = main(["completions", "bash"])
            self.assertEqual(code, 0)
            self.assertIn("_code_exec_completions", mock_out.getvalue())

        with patch("code_exec_completions.install_completions", return_value=(True, "zsh", "/mock/path")):
            code = main(["--install-completions"])
            self.assertEqual(code, 0)

    def test_watch_mode_detects_and_applies_plan(self):
        """Verify watch mode polls clipboard, detects plan, and applies when confirmed."""
        from code_exec_watch import watch_clipboard
        from unittest.mock import MagicMock

        mock_args = MagicMock()
        mock_args.diff = False
        mock_args.yes = False
        mock_args.timeout = 10
        mock_args.no_commit = False
        mock_args.dry_run = False
        mock_args.verify = None

        sample_plan = "```code_exec\nCREATE watch_test.txt\n<<<\nhello from watch\n>>>\n```"
        clips = ["Initial unrelated text", sample_plan]

        def get_clip_side_effect():
            if clips:
                return clips.pop(0)
            return sample_plan

        mock_ui = MagicMock()
        mock_ui.palette = ui.palette
        mock_ui.prompt_choice.return_value = "y"

        applied_ops = []

        with patch("code_exec_watch.get_clipboard", side_effect=get_clip_side_effect), \
             patch("code_exec_watch.time.sleep", return_value=None):
            code = watch_clipboard(
                mock_args,
                self.test_root,
                mock_ui,
                preflight_fn=lambda ops: ("OK", False, {}),
                apply_plan_fn=lambda ops, *a, **k: applied_ops.extend(ops),
                generate_diff_fn=lambda ops: "mock diff",
                poll_interval=0.01,
                max_loops=2,
            )
            self.assertEqual(code, 0)
            self.assertEqual(len(applied_ops), 1)
            mock_ui.watch_plan_detected.assert_called_once()

    def test_watch_mode_auto_apply_yes(self):
        """Verify watch mode with --yes applies immediately without confirmation prompt."""
        from code_exec_watch import watch_clipboard
        from unittest.mock import MagicMock

        mock_args = MagicMock()
        mock_args.diff = False
        mock_args.yes = True
        mock_args.timeout = 10
        mock_args.no_commit = False
        mock_args.dry_run = False
        mock_args.verify = None

        sample_plan = "```code_exec\nCREATE watch_yes.txt\n<<<\nauto applied\n>>>\n```"
        clips = ["Initial text", sample_plan]

        mock_ui = MagicMock()
        mock_ui.palette = ui.palette
        applied_ops = []

        with patch("code_exec_watch.get_clipboard", side_effect=lambda: clips.pop(0) if clips else sample_plan), \
             patch("code_exec_watch.time.sleep", return_value=None):
            code = watch_clipboard(
                mock_args,
                self.test_root,
                mock_ui,
                preflight_fn=lambda ops: ("OK", False, {}),
                apply_plan_fn=lambda ops, *a, **k: applied_ops.extend(ops),
                generate_diff_fn=lambda ops: "mock diff",
                poll_interval=0.01,
                max_loops=2,
            )
            self.assertEqual(code, 0)
            self.assertEqual(len(applied_ops), 1)
            # prompt_choice shouldn't be called because --yes is active
            mock_ui.prompt_choice.assert_not_called()

    def test_ui_menu_option_w_launches_watch(self):
        """Verify that selecting 'w' from the interactive menu launches watch mode."""
        with patch.object(ui, "interactive_menu", return_value="w"):
            with patch("sys.stdin.isatty", return_value=True):
                with patch("code_exec_watch.watch_clipboard", return_value=0) as mock_watch:
                    code = main([])
                    self.assertEqual(code, 0)
                    mock_watch.assert_called_once()

    def test_generate_skeleton_go(self):
        """Verify Go skeleton extracts package, structs, interfaces, and methods."""
        from code_exec_plan_export import generate_skeleton

        go_code = (
            "package main\n\n"
            "import (\n\t\"fmt\"\n\t\"strings\"\n)\n\n"
            "// User represents a system account\n"
            "type User struct {\n\tID int\n\tName string\n}\n\n"
            "type Reader interface {\n\tRead(p []byte) (n int, err error)\n}\n\n"
            "func (u *User) Greet() string {\n"
            + "\treturn fmt.Sprintf(\"hello %s\", u.Name)\n" * 20
            + "}\n\n"
            "func CalculateTotal(prices []float64) float64 {\n"
            + "\tvar sum float64 = 0\n" * 15
            + "\treturn sum\n}\n"
        )
        go_file = self.test_root / "service.go"
        go_file.write_text(go_code, encoding="utf-8")

        skel = generate_skeleton(go_file, max_lines=20)
        self.assertIn("// Skeleton: service.go", skel)
        self.assertIn("package main", skel)
        self.assertIn("type User struct ...", skel)
        self.assertIn("type Reader interface ...", skel)
        self.assertIn("func (u *User) Greet() string ...", skel)
        self.assertIn("func CalculateTotal(prices []float64) float64 ...", skel)

    def test_generate_skeleton_rust(self):
        """Verify Rust skeleton extracts structs, enums, traits, impls, and functions."""
        from code_exec_plan_export import generate_skeleton

        rs_code = (
            "use std::collections::HashMap;\n\n"
            "/// Account record\n"
            "pub struct Account {\n    pub id: u64,\n}\n\n"
            "pub enum Status {\n    Active,\n    Inactive,\n}\n\n"
            "pub trait Repository {\n    fn find_by_id(&self, id: u64) -> Option<Account>;\n}\n\n"
            "impl Account {\n"
            + "    pub fn is_valid(&self) -> bool {\n        true\n    }\n" * 10
            + "}\n\n"
            "pub async fn fetch_remote_data(url: &str) -> Result<String, String> {\n"
            + "    let mut data = String::new();\n" * 15
            + "    Ok(data)\n}\n"
        )
        rs_file = self.test_root / "lib.rs"
        rs_file.write_text(rs_code, encoding="utf-8")

        skel = generate_skeleton(rs_file, max_lines=20)
        self.assertIn("// Skeleton: lib.rs", skel)
        self.assertIn("pub struct Account ...", skel)
        self.assertIn("pub enum Status ...", skel)
        self.assertIn("pub trait Repository ...", skel)
        self.assertIn("pub async fn fetch_remote_data(url: &str) -> Result<String, String> ...", skel)

    def test_generate_skeleton_java_and_kotlin(self):
        """Verify Java and Kotlin skeletons extract classes, interfaces, and methods."""
        from code_exec_plan_export import generate_skeleton

        java_code = (
            "package com.example.app;\n\n"
            "import java.util.List;\n\n"
            "/** Order processing service */\n"
            "public class OrderService implements IService {\n"
            + "    public void processOrder(int id) {\n        // logic\n    }\n" * 15
            + "}\n"
        )
        java_file = self.test_root / "OrderService.java"
        java_file.write_text(java_code, encoding="utf-8")

        skel_java = generate_skeleton(java_file, max_lines=20)
        self.assertIn("public class OrderService implements IService ...", skel_java)
        self.assertIn("public void processOrder(int id) ...", skel_java)

        kt_code = (
            "package com.example.mobile\n\n"
            "import android.os.Bundle\n\n"
            "class MainActivity : AppCompatActivity() {\n"
            + "    fun onCreate(savedInstanceState: Bundle?) {\n        super.onCreate(savedInstanceState)\n    }\n" * 12
            + "}\n"
        )
        kt_file = self.test_root / "MainActivity.kt"
        kt_file.write_text(kt_code, encoding="utf-8")

        skel_kt = generate_skeleton(kt_file, max_lines=20)
        self.assertIn("class MainActivity : AppCompatActivity() ...", skel_kt)
        self.assertIn("fun onCreate(savedInstanceState: Bundle?) ...", skel_kt)

    def test_generate_skeleton_cpp_ruby_php(self):
        """Verify C++, Ruby, and PHP skeleton extractions."""
        from code_exec_plan_export import generate_skeleton

        # C++
        cpp_code = (
            "#include <iostream>\n#include <vector>\n\n"
            "struct EngineConfig {\n    int rpm;\n};\n\n"
            "class Vehicle {\n"
            + "    void drive() {\n        // internal\n    }\n" * 15
            + "};\n"
        )
        cpp_file = self.test_root / "engine.cpp"
        cpp_file.write_text(cpp_code, encoding="utf-8")
        skel_cpp = generate_skeleton(cpp_file, max_lines=20)
        self.assertIn("struct EngineConfig ...", skel_cpp)
        self.assertIn("class Vehicle ...", skel_cpp)

        # Ruby
        rb_code = (
            "require 'json'\n\n"
            "module PaymentGateway\n"
            "  class StripeProcessor < BaseProcessor\n"
            + "    def charge(amount)\n      puts amount\n    end\n" * 10
            + "  end\n"
            "end\n"
        )
        rb_file = self.test_root / "stripe.rb"
        rb_file.write_text(rb_code, encoding="utf-8")
        skel_rb = generate_skeleton(rb_file, max_lines=20)
        self.assertIn("module PaymentGateway ...", skel_rb)
        self.assertIn("class StripeProcessor < BaseProcessor ...", skel_rb)
        self.assertIn("def charge(amount) ...", skel_rb)

        # PHP
        php_code = (
            "<?php\n\nnamespace App\\Controllers;\n\nuse App\\Models\\User;\n\n"
            "class UserController extends BaseController {\n"
            + "    public function index() {\n        return view('users');\n    }\n" * 10
            + "}\n"
        )
        php_file = self.test_root / "UserController.php"
        php_file.write_text(php_code, encoding="utf-8")
        skel_php = generate_skeleton(php_file, max_lines=20)
        self.assertIn("class UserController extends BaseController ...", skel_php)
        self.assertIn("public function index() ...", skel_php)


if __name__ == "__main__":
    unittest.main()



