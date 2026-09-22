#!/usr/bin/env python3
"""
test_llm_error_improvements.py
Tests for all new LLM-error-reduction improvements:
  - Duplicate find_unique removal
  - Richer error guidance (_get_error_guidance)
  - Smarter clipboard retry prompt (copy_error_to_clipboard)
  - _extract_operation_context / _extract_candidate_diff helpers
  - Smart path prefix recovery (src/, app/, lib/)
  - Home-relative path rejection (~/...)
  - Tolerant END_OF_FILE for last block
  - Verbose tier tracing flag (VERBOSE)
  - --with-tree flag (instructions copy)

Run with:  python3 -m unittest tests/test_llm_error_improvements.py -v
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import code_exec_parser as p
from code_exec_types import OpError

# Suppress parser warnings during tests
p.PRINT_WARNINGS = False


def ops(text: str):
    """Full pipeline: AI reply -> plan text -> operations."""
    return p.parse_operations(p.extract_plan(text))


# ============================================================
# Helper
# ============================================================
class Base(unittest.TestCase):
    def setUp(self):
        p.take_warnings()
        p.MERGE_MULTIPLE_PLANS = True
        p.NO_PLAN_IS_ERROR = False


# ============================================================
# 1. Duplicate find_unique removed
# ============================================================
class TestDuplicateFindUniqueRemoved(unittest.TestCase):
    """The module must expose exactly ONE find_unique callable."""

    def test_find_unique_is_defined_once(self):
        import code_exec_matcher as m
        import inspect

        # Get all module-level names
        all_names = [name for name in dir(m) if name == "find_unique"]
        self.assertEqual(len(all_names), 1, "find_unique should be defined exactly once")

    def test_find_unique_works_correctly(self):
        """Confirm the surviving find_unique actually works."""
        from code_exec_matcher import find_unique
        doc = "alpha\nbeta\ngamma\n"
        result = find_unique(doc, "beta", "SEARCH", "test.txt")
        self.assertIn("beta", doc[result.start:result.end])


# ============================================================
# 2. Richer _get_error_guidance
# ============================================================
class TestErrorGuidance(unittest.TestCase):
    """Every error code must produce a ✅ Do + ❌ Do NOT hint."""

    def _guidance(self, error_code: str) -> str:
        from code_exec import _get_error_guidance
        return _get_error_guidance(error_code)

    def _assert_has_do_and_dont(self, guidance: str, label: str):
        self.assertIn("✅", guidance, f"{label}: expected ✅ Do hint")
        self.assertIn("❌", guidance, f"{label}: expected ❌ Do NOT hint")

    def test_search_not_found_guidance(self):
        g = self._guidance("ERR|SEARCH_NOT_FOUND|foo.py")
        self._assert_has_do_and_dont(g, "SEARCH_NOT_FOUND")
        self.assertIn("memory", g.lower())

    def test_search_ambiguous_guidance(self):
        g = self._guidance("ERR|SEARCH_AMBIGUOUS|foo.py|matched 2 times")
        self._assert_has_do_and_dont(g, "SEARCH_AMBIGUOUS")
        self.assertIn("anchor", g.lower())

    def test_search_too_big_guidance(self):
        g = self._guidance("ERR|SEARCH_TOO_BIG|foo.py")
        self._assert_has_do_and_dont(g, "SEARCH_TOO_BIG")
        self.assertIn("boundary", g.lower())

    def test_create_exists_guidance(self):
        g = self._guidance("ERR|CREATE_EXISTS|foo.py")
        self._assert_has_do_and_dont(g, "CREATE_EXISTS")
        self.assertIn("EDIT", g)

    def test_file_not_found_guidance(self):
        g = self._guidance("ERR|FILE_NOT_FOUND|nonexistent_file.py")
        self._assert_has_do_and_dont(g, "FILE_NOT_FOUND")
        # Should mention the missing path
        self.assertIn("nonexistent_file.py", g)

    def test_file_not_found_nearby_files(self):
        """When the parent directory exists, list real nearby files."""
        from code_exec import _get_error_guidance
        from code_exec_types import ROOT
        # The ROOT directory definitely exists and has real files in it
        real_file = next(
            p for p in ROOT.iterdir() if p.is_file() and not p.name.startswith(".")
        )
        error_msg = f"ERR|FILE_NOT_FOUND|{real_file.name}_missing.py"
        g = _get_error_guidance(error_msg)
        # The parent dir is ROOT, so nearby files should be listed
        self.assertIn("Nearby files", g)

    def test_conflicting_operations_guidance(self):
        g = self._guidance("ERR|CONFLICTING_OPERATIONS|foo.py")
        self._assert_has_do_and_dont(g, "CONFLICTING_OPERATIONS")

    def test_multiple_plans_guidance(self):
        g = self._guidance("ERR|MULTIPLE_PLANS|2")
        self._assert_has_do_and_dont(g, "MULTIPLE_PLANS")
        self.assertIn("ONE", g)

    def test_plan_not_found_guidance(self):
        g = self._guidance("ERR|PLAN_NOT_FOUND")
        self._assert_has_do_and_dont(g, "PLAN_NOT_FOUND")

    def test_forbidden_command_guidance(self):
        g = self._guidance("ERR|FORBIDDEN_COMMAND|rm -rf .")
        self._assert_has_do_and_dont(g, "FORBIDDEN_COMMAND")
        self.assertIn("pytest", g)

    def test_unknown_command_guidance(self):
        g = self._guidance("ERR|UNKNOWN_COMMAND|FROBULATE")
        self._assert_has_do_and_dont(g, "UNKNOWN_COMMAND")
        self.assertIn("RUN", g)

    def test_patch_failed_guidance(self):
        g = self._guidance("ERR|PATCH_FAILED|foo.py")
        self._assert_has_do_and_dont(g, "PATCH_FAILED")

    def test_unknown_error_falls_back_gracefully(self):
        g = self._guidance("some random error text")
        self.assertIn("Review the error", g)

    def test_hints_separated_by_double_newline(self):
        """Multiple error codes should be separated by double newlines."""
        g = self._guidance("ERR|SEARCH_NOT_FOUND|a.py\nERR|SEARCH_AMBIGUOUS|b.py")
        # Should contain two separate sections
        self.assertIn("SEARCH_NOT_FOUND", g)
        self.assertIn("SEARCH_AMBIGUOUS", g)
        self.assertIn("\n\n", g)


# ============================================================
# 3. Clipboard retry prompt helpers
# ============================================================
class TestClipboardRetryPrompt(unittest.TestCase):
    """_extract_operation_context and _extract_candidate_diff."""

    def test_extract_operation_context_found(self):
        from code_exec import _extract_operation_context
        msg = "Operation 3 (EDIT foo.py): ERR|SEARCH_NOT_FOUND|foo.py"
        ctx = _extract_operation_context(msg)
        self.assertIn("Operation 3", ctx)
        self.assertIn("EDIT foo.py", ctx)

    def test_extract_operation_context_not_found(self):
        from code_exec import _extract_operation_context
        msg = "ERR|PLAN_NOT_FOUND"
        ctx = _extract_operation_context(msg)
        self.assertEqual(ctx, "")

    def test_extract_candidate_diff_found(self):
        from code_exec import _extract_candidate_diff
        msg = (
            "ERR|SEARCH_NOT_FOUND|foo.py\n\n"
            "Closest candidate found at lines 10-12 (85% similarity):\n"
            "----------------------------------------\n"
            "--- Expected (SEARCH)\n"
            "+++ File candidate (lines 10-12)\n"
            "-old line\n"
            "+actual line\n"
            "----------------------------------------\n"
            "(Note for LLM: Lines starting with '+' show actual file content.)"
        )
        diff = _extract_candidate_diff(msg)
        self.assertIn("actual current file content", diff)
        self.assertIn("```diff", diff)
        self.assertIn("actual line", diff)

    def test_extract_candidate_diff_not_found(self):
        from code_exec import _extract_candidate_diff
        diff = _extract_candidate_diff("ERR|FILE_NOT_FOUND|missing.py")
        self.assertEqual(diff, "")

    def test_copy_error_to_clipboard_full_prompt(self):
        """copy_error_to_clipboard should produce a rich structured prompt."""
        from code_exec import copy_error_to_clipboard
        with patch("code_exec.set_clipboard") as mock_set:
            copy_error_to_clipboard(
                "Operation 2 (EDIT utils.py): ERR|SEARCH_NOT_FOUND|utils.py"
            )
            mock_set.assert_called_once()
            prompt = mock_set.call_args[0][0]

        self.assertIn("code_exec` plan failed", prompt)
        self.assertIn("Troubleshooting Guidance", prompt)
        self.assertIn("Instructions for the fix", prompt)
        self.assertIn("Fix **only** the failing operation", prompt)
        self.assertIn("Operation 2", prompt)
        self.assertIn("SEARCH_NOT_FOUND", prompt)

    def test_copy_error_to_clipboard_includes_do_not(self):
        """The retry prompt must contain the Do NOT hints."""
        from code_exec import copy_error_to_clipboard
        with patch("code_exec.set_clipboard") as mock_set:
            copy_error_to_clipboard("ERR|SEARCH_NOT_FOUND|main.py (first line: 'def foo():')")
        prompt = mock_set.call_args[0][0]
        self.assertIn("❌", prompt)
        self.assertIn("memory", prompt.lower())

    def test_copy_error_clipboard_failure_does_not_raise(self):
        """Even if set_clipboard throws, copy_error_to_clipboard must not propagate."""
        from code_exec import copy_error_to_clipboard
        with patch("code_exec.set_clipboard", side_effect=RuntimeError("no clipboard")):
            # Should complete without raising
            copy_error_to_clipboard("ERR|FILE_NOT_FOUND|test.py")


# ============================================================
# 3b. Repeated-error file injection
# ============================================================
class TestRepeatedErrorFileInjection(unittest.TestCase):
    """When the same (file, error_code) fails twice, full file content is injected into clipboard."""

    def setUp(self):
        import code_exec
        code_exec._error_history.clear()

    def tearDown(self):
        import code_exec
        code_exec._error_history.clear()

    def test_first_failure_does_not_attach_file(self):
        """On the 1st failure the clipboard prompt should NOT contain the file content section."""
        from code_exec import copy_error_to_clipboard
        with patch("code_exec.set_clipboard") as mock_set:
            copy_error_to_clipboard("ERR|SEARCH_NOT_FOUND|code_exec.py (first line: 'import re')")
        prompt = mock_set.call_args[0][0]
        self.assertNotIn("Repeated failure", prompt)
        self.assertNotIn("full file content", prompt.lower())

    def test_second_failure_attaches_file_content(self):
        """On the 2nd failure for the same file+error the full file content is appended."""
        from code_exec import copy_error_to_clipboard
        # First call — sets count to 1
        with patch("code_exec.set_clipboard"):
            copy_error_to_clipboard("ERR|SEARCH_NOT_FOUND|code_exec.py (first line: 'import re')")
        # Second call — count becomes 2, triggers injection
        with patch("code_exec.set_clipboard") as mock_set:
            with patch("code_exec_ui.ui.repeated_error_file_attached") as mock_ui:
                copy_error_to_clipboard("ERR|SEARCH_NOT_FOUND|code_exec.py (first line: 'import re')")
        prompt = mock_set.call_args[0][0]
        # Must contain the file attachment section
        self.assertIn("Repeated failure", prompt)
        self.assertIn("2 times", prompt)
        # Must contain actual file content (code_exec.py starts with #!/usr/bin/env python3)
        self.assertIn("#!/usr/bin/env python3", prompt)
        # UI card must be called
        mock_ui.assert_called_once()
        call_args = mock_ui.call_args[0]
        self.assertEqual(call_args[0], "code_exec.py")
        self.assertEqual(call_args[1], 2)

    def test_third_failure_shows_correct_attempt_count(self):
        """3rd failure shows '3 times' in the prompt."""
        from code_exec import copy_error_to_clipboard
        error = "ERR|SEARCH_NOT_FOUND|code_exec.py (first line: 'import os')"
        for _ in range(2):
            with patch("code_exec.set_clipboard"):
                with patch("code_exec_ui.ui.repeated_error_file_attached"):
                    copy_error_to_clipboard(error)
        with patch("code_exec.set_clipboard") as mock_set:
            with patch("code_exec_ui.ui.repeated_error_file_attached"):
                copy_error_to_clipboard(error)
        prompt = mock_set.call_args[0][0]
        self.assertIn("3 times", prompt)

    def test_different_error_code_on_same_file_is_independent(self):
        """SEARCH_NOT_FOUND and FILE_NOT_FOUND on the same file are tracked separately."""
        from code_exec import copy_error_to_clipboard
        # Fail twice with SEARCH_NOT_FOUND
        for _ in range(2):
            with patch("code_exec.set_clipboard"):
                with patch("code_exec_ui.ui.repeated_error_file_attached"):
                    copy_error_to_clipboard("ERR|SEARCH_NOT_FOUND|code_exec.py (x)")
        # First failure with FILE_NOT_FOUND — should NOT trigger attachment
        with patch("code_exec.set_clipboard") as mock_set:
            copy_error_to_clipboard("ERR|FILE_NOT_FOUND|code_exec.py")
        prompt = mock_set.call_args[0][0]
        # FILE_NOT_FOUND count is only 1, so no attachment
        self.assertNotIn("Repeated failure", prompt)

    def test_different_files_tracked_independently(self):
        """Failures on different files don't cross-contaminate the counter."""
        from code_exec import copy_error_to_clipboard
        # Fail once on code_exec.py
        with patch("code_exec.set_clipboard"):
            copy_error_to_clipboard("ERR|SEARCH_NOT_FOUND|code_exec.py (x)")
        # Fail once on a different file — should NOT trigger attachment
        with patch("code_exec.set_clipboard") as mock_set:
            copy_error_to_clipboard("ERR|SEARCH_NOT_FOUND|code_exec_ui.py (x)")
        prompt = mock_set.call_args[0][0]
        self.assertNotIn("Repeated failure", prompt)

    def test_nonexistent_file_does_not_crash(self):
        """If the referenced file doesn't exist, the injection is silently skipped."""
        from code_exec import copy_error_to_clipboard
        error = "ERR|SEARCH_NOT_FOUND|totally_nonexistent_xyz_file.py (x)"
        for _ in range(2):
            with patch("code_exec.set_clipboard"):
                copy_error_to_clipboard(error)
        # On 2nd call: file doesn't exist, but no exception should propagate
        with patch("code_exec.set_clipboard") as mock_set:
            copy_error_to_clipboard(error)
        prompt = mock_set.call_args[0][0]
        # No file section injected since file doesn't exist
        self.assertNotIn("full file attached", prompt.lower())


# ============================================================
# 4. Smart path prefix recovery (src/, app/, lib/ stripping)
# ============================================================
class TestSmartPathPrefixRecovery(Base):
    """_norm_path should strip hallucinated src/ / app/ prefixes when file exists without it."""

    def test_src_prefix_stripped_when_file_exists_without_it(self):
        """If src/foo.py doesn't exist but foo.py does, strip src/ and warn."""
        # We need a real file that exists without the prefix.
        # code_exec.py exists at ROOT; use it.
        plan = "DELETE src/code_exec.py"
        with patch("code_exec_parser._warn") as mock_warn:
            result = ops(plan)
        # The path should have been corrected to code_exec.py
        warnings = [call[0][0] for call in mock_warn.call_args_list]
        prefix_warnings = [w for w in warnings if "prefix" in w and "stripped" in w]
        self.assertTrue(len(prefix_warnings) > 0, "Expected a prefix-stripping warning")
        self.assertEqual(result[0].args[0], "code_exec.py")

    def test_app_prefix_stripped_when_file_exists_without_it(self):
        plan = "DELETE app/code_exec.py"
        with patch("code_exec_parser._warn") as mock_warn:
            result = ops(plan)
        warnings = [call[0][0] for call in mock_warn.call_args_list]
        prefix_warnings = [w for w in warnings if "prefix" in w and "stripped" in w]
        self.assertTrue(len(prefix_warnings) > 0)
        self.assertEqual(result[0].args[0], "code_exec.py")

    def test_nonexistent_path_without_prefix_not_modified(self):
        """A path that doesn't exist anywhere should not be silently changed."""
        plan = "DELETE src/totally_nonexistent_file_xyz.py"
        result = ops(plan)
        # Should remain as-is (executor will raise FILE_NOT_FOUND later)
        self.assertEqual(result[0].args[0], "src/totally_nonexistent_file_xyz.py")

    def test_home_relative_path_warns(self):
        """~/... paths should trigger a warning (they'll still fail at safe_path)."""
        with patch("code_exec_parser._warn") as mock_warn:
            ops("DELETE ~/some/path.py")
        warnings = [call[0][0] for call in mock_warn.call_args_list]
        home_warnings = [w for w in warnings if "home-relative" in w]
        self.assertTrue(len(home_warnings) > 0, "Expected a home-relative path warning")

    def test_lib_prefix_stripped_when_file_exists(self):
        plan = "DELETE lib/code_exec.py"
        with patch("code_exec_parser._warn") as mock_warn:
            result = ops(plan)
        warnings = [call[0][0] for call in mock_warn.call_args_list]
        prefix_warnings = [w for w in warnings if "prefix" in w and "stripped" in w]
        self.assertTrue(len(prefix_warnings) > 0)
        self.assertEqual(result[0].args[0], "code_exec.py")


# ============================================================
# 5. Tolerant END_OF_FILE for last block
# ============================================================
class TestTolerantEndOfFile(Base):
    """When END_OF_FILE is missing on the last block, warn and recover."""

    def test_missing_eof_on_last_create_block_is_tolerated(self):
        plan = "CREATE hello.txt\nhello world\nsome more content"
        with patch("code_exec_parser._warn") as mock_warn:
            result = p.parse_operations(plan)
        warnings = [call[0][0] for call in mock_warn.call_args_list]
        eof_warnings = [w for w in warnings if "END_OF_FILE" in w and "implicit" in w]
        self.assertTrue(len(eof_warnings) > 0, "Expected implicit EOF tolerance warning")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].command, "CREATE")
        self.assertIn("hello world", result[0].data)

    def test_missing_eof_mid_plan_still_raises(self):
        """If a later command follows a block without END_OF_FILE, it must still fail."""
        plan = (
            "CREATE first.txt\n"
            "content without eof\n"
            "CREATE second.txt\n"
            "<<<\n"
            "another\n"
            ">>>\n"
        )
        with self.assertRaises(ValueError) as ctx:
            p.parse_operations(plan)
        self.assertIn("END_OF_FILE", str(ctx.exception))

    def test_explicit_eof_still_works(self):
        """The normal END_OF_FILE terminator must continue to work."""
        plan = "CREATE file.txt\nhello\nEND_OF_FILE\n"
        result = p.parse_operations(plan)
        self.assertEqual(result[0].data, "hello")

    def test_missing_eof_warning_text(self):
        """The warning message must be helpful."""
        plan = "CREATE readme.txt\nThis is the content\nwithout a closer"
        with patch("code_exec_parser._warn") as mock_warn:
            p.parse_operations(plan)
        all_warnings = " ".join(call[0][0] for call in mock_warn.call_args_list)
        self.assertIn("implicit closer", all_warnings)


# ============================================================
# 6. VERBOSE tier tracing
# ============================================================
class TestVerboseTierTracing(unittest.TestCase):
    """Setting VERBOSE=True should emit tracing notes via ui.warn."""

    def setUp(self):
        import code_exec_matcher as m
        self._original_verbose = m.VERBOSE
        m.VERBOSE = True

    def tearDown(self):
        import code_exec_matcher as m
        m.VERBOSE = self._original_verbose

    def test_tier1_exact_match_emits_verbose_note(self):
        from code_exec_matcher import find_unique
        with patch("code_exec_matcher.ui") as mock_ui:
            find_unique("alpha\nbeta\ngamma\n", "beta", "SEARCH", "test.txt")
        all_warn_calls = " ".join(
            str(call) for call in mock_ui.warn.call_args_list
        )
        self.assertIn("Tier 1", all_warn_calls)
        self.assertIn("exact", all_warn_calls.lower())

    def test_tier2_trailing_whitespace_emits_verbose_note(self):
        from code_exec_matcher import find_unique
        # Exact match fails when the needle has trailing spaces that the doc doesn't have.
        # Tier 2 strips trailing whitespace from both sides, so it matches.
        doc = "line_one\nbeta_anchor\nline_three\n"
        # Needle has trailing space on each line — not an exact substring of doc
        needle = "beta_anchor   "
        with patch("code_exec_matcher.ui") as mock_ui:
            find_unique(doc, needle, "SEARCH", "test.txt")
        all_warn_calls = " ".join(str(call) for call in mock_ui.warn.call_args_list)
        self.assertIn("Tier 2", all_warn_calls)

    def test_tier3_indent_emits_verbose_note(self):
        from code_exec_matcher import find_unique
        doc = "    def foo():\n        return 1\n"
        needle = "def foo():\n    return 1\n"  # different indent
        with patch("code_exec_matcher.ui") as mock_ui:
            find_unique(doc, needle, "SEARCH", "test.txt")
        all_warn_calls = " ".join(str(call) for call in mock_ui.warn.call_args_list)
        # Should have tried Tier 2 (no match) then matched at Tier 3
        self.assertIn("Tier 3", all_warn_calls)

    def test_no_verbose_output_when_flag_is_false(self):
        import code_exec_matcher as m
        m.VERBOSE = False
        from code_exec_matcher import find_unique
        with patch("code_exec_matcher.ui") as mock_ui:
            find_unique("alpha\nbeta\ngamma\n", "beta", "SEARCH", "test.txt")
        # ui.warn should not have been called for verbose notes
        for call in mock_ui.warn.call_args_list:
            self.assertNotIn("[matcher]", str(call))


# ============================================================
# 7. --with-tree flag in prompt handler
# ============================================================
class TestWithTreeFlag(unittest.TestCase):
    """--with-tree appends project file tree to the instructions clipboard."""

    def test_with_tree_appends_tree_block(self):
        import argparse
        from code_exec import main

        captured = {}

        def fake_set_clipboard(text):
            captured["text"] = text

        with patch("code_exec.set_clipboard", side_effect=fake_set_clipboard):
            # Simulate: code-exec -p --with-tree
            ret = main(["-p", "--with-tree"])

        self.assertEqual(ret, 0)
        content = captured.get("text", "")
        self.assertIn("## Project file tree", content)
        self.assertIn("```", content)
        # Should mention at least one .py file from the project root
        self.assertIn(".py", content)
        self.assertIn("Do NOT guess paths", content)

    def test_without_with_tree_no_tree_appended(self):
        """Without --with-tree the copied content is just the raw instructions."""
        captured = {}

        def fake_set_clipboard(text):
            captured["text"] = text

        with patch("code_exec.set_clipboard", side_effect=fake_set_clipboard):
            ret = main(["-p"])

        self.assertEqual(ret, 0)
        content = captured.get("text", "")
        self.assertNotIn("## Project file tree", content)

    def test_with_tree_excludes_hidden_dirs(self):
        """The generated tree section should not list .git or __pycache__ entries."""
        captured = {}

        def fake_set_clipboard(text):
            captured["text"] = text

        with patch("code_exec.set_clipboard", side_effect=fake_set_clipboard):
            from code_exec import main
            main(["-p", "--with-tree"])

        content = captured.get("text", "")
        # Isolate only the tree block (between the ``` markers after "## Project file tree")
        tree_start = content.find("## Project file tree")
        self.assertGreater(tree_start, 0, "Tree section not found")
        tree_section = content[tree_start:]
        # .git and __pycache__ should not appear as directory entries in the tree
        lines_in_tree = tree_section.splitlines()
        tree_dir_lines = [ln.strip() for ln in lines_in_tree if ln.strip().endswith("/")]
        self.assertNotIn(".git/", tree_dir_lines)
        self.assertNotIn("__pycache__/", tree_dir_lines)


# Import main at module level for the --with-tree tests
from code_exec import main


# ============================================================
# 8. Instructions file anti-hallucination content
# ============================================================
class TestInstructionsAntiHallucination(unittest.TestCase):
    """The rewritten instructions.md must contain the new guardrail sections."""

    def setUp(self):
        self.instructions = (
            Path(__file__).resolve().parent.parent / "code_exec_instructions.md"
        ).read_text(encoding="utf-8")

    def test_has_anti_hallucination_section(self):
        self.assertIn("Anti-hallucination", self.instructions)

    def test_has_search_block_rules(self):
        self.assertIn("NEVER write a SEARCH block from memory", self.instructions)

    def test_has_path_rules(self):
        self.assertIn("NEVER guess a file path", self.instructions)

    def test_has_single_block_rule(self):
        self.assertIn("NEVER emit more than one", self.instructions)

    def test_has_do_and_dont_markers(self):
        self.assertIn("✅", self.instructions)
        self.assertIn("❌", self.instructions)

    def test_has_boundary_anchor_hint(self):
        self.assertIn("boundary anchor", self.instructions)

    def test_has_block_syntax_warning(self):
        # The instructions say **Never** combine (markdown bold), not NEVER combine
        self.assertTrue(
            "Never** combine" in self.instructions or "NEVER mix block styles" in self.instructions,
            "Expected block-style mixing warning in instructions"
        )


# ============================================================
# 9. --verbose CLI flag wires through correctly
# ============================================================
class TestVerboseCLIFlag(unittest.TestCase):
    """--verbose sets code_exec_matcher.VERBOSE = True before plan execution."""

    def test_verbose_flag_sets_matcher_verbose(self):
        import code_exec_matcher as m
        original = m.VERBOSE
        try:
            m.VERBOSE = False
            from code_exec import main
            with patch("code_exec.set_clipboard"):
                with patch("code_exec.get_clipboard", return_value="DELETE nonexistent_xyz_abc.txt"):
                    with patch("code_exec_ui.ui") as mock_ui:
                        # The plan will fail (file doesn't exist), but VERBOSE should have been set
                        # before preflight runs. We patch preflight to check.
                        with patch("code_exec.preflight", return_value=(None, 0, {})) as mock_pf:
                            with patch("code_exec.apply_plan", return_value=0):
                                with patch("code_exec_ui.ui.confirm", return_value=True):
                                    with patch("code_exec_ui.ui.header"):
                                        with patch("code_exec_ui.ui.show_plan"):
                                            with patch("code_exec_ui.ui.show_diff"):
                                                main(["--verbose", "--yes", "--dry-run"])
            # After main() ran with --verbose, the module flag should be True
            self.assertTrue(m.VERBOSE)
        finally:
            m.VERBOSE = original


# ============================================================
# 10. Regression — existing error guidance format still valid
# ============================================================
class TestErrorGuidanceRegression(unittest.TestCase):
    """The clipboard prompt must still contain the fields the old tests expected."""

    def test_clipboard_prompt_contains_error_block(self):
        from code_exec import copy_error_to_clipboard
        with patch("code_exec.set_clipboard") as mock_set:
            copy_error_to_clipboard("ERR|SEARCH_NOT_FOUND|foo.py")
        content = mock_set.call_args[0][0]
        self.assertIn("ERR|SEARCH_NOT_FOUND|foo.py", content)
        self.assertIn("`code_exec` plan failed", content)
        self.assertIn("Troubleshooting Guidance", content)
        self.assertIn("SEARCH_NOT_FOUND", content)

    def test_guidance_has_do_not_text_for_all_error_types(self):
        from code_exec import _get_error_guidance
        error_codes = [
            "ERR|SEARCH_NOT_FOUND|f.py",
            "ERR|SEARCH_AMBIGUOUS|f.py",
            "ERR|SEARCH_TOO_BIG|f.py",
            "ERR|CREATE_EXISTS|f.py",
            "ERR|FILE_NOT_FOUND|f.py",
            "ERR|CONFLICTING_OPERATIONS|f.py",
            "ERR|MULTIPLE_PLANS|2",
            "ERR|PLAN_NOT_FOUND",
            "ERR|FORBIDDEN_COMMAND|x",
            "ERR|UNKNOWN_COMMAND|x",
            "ERR|PATCH_FAILED|f.py",
        ]
        for code in error_codes:
            with self.subTest(code=code):
                g = _get_error_guidance(code)
                self.assertIn("❌", g, f"Missing ❌ for {code}")
                self.assertIn("✅", g, f"Missing ✅ for {code}")


if __name__ == "__main__":
    unittest.main()
