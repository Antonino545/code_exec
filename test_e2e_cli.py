#!/usr/bin/env python3
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class TestCodeExecCLI(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.test_dir.name).resolve()
        self.script_path = Path(__file__).resolve().parent / "code_exec.py"

    def tearDown(self):
        self.test_dir.cleanup()

    def run_cli(self, plan_text: str, extra_args: list[str] = None):
        plan_file = self.root / "ai_response.md"
        plan_file.write_text(plan_text, encoding="utf-8")
        cmd = [sys.executable, str(self.script_path), "--file", str(plan_file), "--yes"]
        if extra_args:
            cmd.extend(extra_args)
        return subprocess.run(
            cmd,
            cwd=self.root,
            capture_output=True,
            text=True,
        )

    def test_e2e_successful_extraction_and_execution(self):
        response = (
            "Here is the change requested to configure the app:\n\n"
            "```python\n"
            "# Reference snippet (should be ignored)\n"
            "DEBUG = True\n"
            "```\n\n"
            "Now here is the executable plan:\n\n"
            "```code_exec\n"
            "THINK\n"
            "Create settings file\n"
            "END_THINK\n"
            "CREATE settings.conf\n"
            "<<<\n"
            "ENVIRONMENT=production\n"
            "PORT=8080\n"
            ">>>\n"
            "```\n\n"
            "Let me know if you have any questions!"
        )
        result = self.run_cli(response)
        self.assertEqual(result.returncode, 0, msg=f"Process failed: {result.stderr}")
        created = self.root / "settings.conf"
        self.assertTrue(created.is_file())
        self.assertEqual(created.read_text(encoding="utf-8"), "ENVIRONMENT=production\nPORT=8080\n")

    def test_e2e_multiple_plans_refusal(self):
        response = (
            "Option 1:\n"
            "```code_exec\n"
            "THINK\n"
            "First option\n"
            "END_THINK\n"
            "CREATE file1.txt\n"
            "<<<\n"
            "one\n"
            ">>>\n"
            "```\n\n"
            "Option 2:\n"
            "```code_exec\n"
            "THINK\n"
            "Second option\n"
            "END_THINK\n"
            "CREATE file2.txt\n"
            "<<<\n"
            "two\n"
            ">>>\n"
            "```\n"
        )
        result = self.run_cli(response)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ERR|MULTIPLE_PLANS|2", result.stderr)
        self.assertFalse((self.root / "file1.txt").exists())
        self.assertFalse((self.root / "file2.txt").exists())

    def test_e2e_no_plan_found(self):
        response = (
            "I checked your project. Here is an example of what to change:\n\n"
            "```javascript\n"
            "console.log('No plan block provided');\n"
            "```\n"
        )
        result = self.run_cli(response)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ERR|PLAN_NOT_FOUND", result.stderr)

    def test_e2e_search_not_found_reporting(self):
        target = self.root / "index.js"
        target.write_text("function init() {\n  return 0;\n}\n", encoding="utf-8")
        response = (
            "Here is the update plan:\n"
            "```code_exec\n"
            "THINK\n"
            "Attempt to edit non-existent function\n"
            "END_THINK\n"
            "EDIT index.js\n"
            "SEARCH\n"
            "<<<\n"
            "function nonExistent() {\n"
            ">>>\n"
            "REPLACE\n"
            "<<<\n"
            "function replaced() {\n"
            ">>>\n"
            "```\n"
        )
        result = self.run_cli(response)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ERR|SEARCH_NOT_FOUND|index.js", result.stderr)

    def test_e2e_hallucinated_unknown_command_rejection(self):
        fence = chr(96) * 3
        response = (
            "Here is the update plan:\n\n"
            f"{fence}code_exec\n"
            "UPDATE config.ini\n"
            "<<<\n"
            "setting = 1\n"
            ">>>\n"
            f"{fence}\n"
        )
        result = self.run_cli(response)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ERR|UNKNOWN_COMMAND|UPDATE", result.stderr)
        self.assertIn("Did you mean 'EDIT'?", result.stderr)

    def test_e2e_hallucinated_bare_shell_command_rejection(self):
        fence = chr(96) * 3
        response = (
            "Let's run the test command:\n\n"
            f"{fence}code_exec\n"
            "npm test\n"
            f"{fence}\n"
        )
        result = self.run_cli(response)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ERR|UNKNOWN_COMMAND|npm", result.stderr)
        self.assertIn("Shell commands must be prefixed with RUN", result.stderr)

    def test_e2e_patch_execution(self):
        target = self.root / "app.py"
        target.write_text("def run():\n    return False\n", encoding="utf-8")
        fence = chr(96) * 3
        response = (
            "Here is the patch:\n\n"
            f"{fence}code_exec\n"
            "PATCH app.py\n"
            "<<<\n"
            "@@ -1,2 +1,2 @@\n"
            " def run():\n"
            "-    return False\n"
            "+    return True\n"
            ">>>\n"
            f"{fence}\n"
        )
        result = self.run_cli(response)
        self.assertEqual(result.returncode, 0, msg=f"CLI failed: {result.stderr}")
        self.assertEqual(target.read_text(encoding="utf-8"), "def run():\n    return True\n")


if __name__ == "__main__":
    unittest.main()
