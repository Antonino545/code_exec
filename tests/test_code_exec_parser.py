#!/usr/bin/env python3
"""
All plan-extraction and plan-parsing tests (no filesystem, no CLI).

Run with:  python3 -m unittest test_code_exec_parser
"""
import unittest

import code_exec_parser as p
from code_exec_types import OpError

p.PRINT_WARNINGS = False


def ops(text: str):
    """Full pipeline: AI reply -> plan text -> operations."""
    return p.parse_operations(p.extract_plan(text))


class Base(unittest.TestCase):
    def setUp(self):
        p.take_warnings()
        p.MERGE_MULTIPLE_PLANS = True
        p.NO_PLAN_IS_ERROR = False


# --------------------------------------------------------------------------- #
# Finding the plan inside an AI reply
# --------------------------------------------------------------------------- #
class PlanExtraction(Base):
    def test_fenced_plan_with_prose_around(self):
        # Text before and after the fence is ignored.
        body = "CREATE sample.txt\n<<<\nhello world\n>>>\n"
        r = ops(f"Paragraph before.\n\nMore text.\n\n```code_exec\n{body}```\n\nLet me know!")
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0].command, r[0].args[0], r[0].data), ("CREATE", "sample.txt", "hello world"))

    def test_nested_fence_in_file_content_is_preserved(self):
        # A CREATE/EDIT may write a file (e.g. a README) whose own content has a nested
        # ```/~~~ fence; that must not close the outer code_exec block early.
        r = ops(
            "```code_exec\nCREATE docs/README.md <<<\n```bash\nnpm install\n```\n\n"
            "More text after the nested fence.\n>>>\n```"
        )
        self.assertEqual(len(r), 1)
        self.assertIn("```bash", r[0].data)
        self.assertIn("More text after the nested fence.", r[0].data)

    def test_fence_variants(self):
        for info in ("code_exec", "code-exec", "CODE EXEC", "code_exec plan"):
            with self.subTest(info=info):
                self.assertEqual(len(ops(f"Here:\n```{info}\nDELETE a.py\n```")), 1)

    def test_tilde_indented_and_quadruple_fences(self):
        tri, quad = "`" * 3, "`" * 4
        cases = {
            "tilde": "Plan:\n\n~~~code_exec\nCREATE f.txt\n<<<\nx\n>>>\n~~~\n",
            "indented": "Indented:\n\n    ```code_exec\n    CREATE f.txt\n    <<<\n    x\n    >>>\n    ```\n",
            "quadruple with inner fence": f"{quad}code_exec\nCREATE f.txt\n<<<\n{tri}python\nprint(1)\n{tri}\n>>>\n{quad}\n",
        }
        for label, text in cases.items():
            with self.subTest(label):
                r = ops(text)
                self.assertEqual((len(r), r[0].command, r[0].args[0]), (1, "CREATE", "f.txt"))

    def test_nested_backticks_inside_content_block(self):
        r = ops(
            "Plan:\n```code_exec\nCREATE README.md\n<<<\n```python\nprint('nested')\n```\n>>>\n"
            "COMMIT feat: update readme\n```\nDone."
        )
        self.assertEqual([o.command for o in r], ["CREATE", "COMMIT"])
        self.assertIn("```python", r[0].data)

    def test_unrelated_code_blocks_are_ignored(self):
        r = ops(
            "# Summary\n```python\ndef unused():\n    return 1\n```\n"
            "```jsx\nexport const B = () => <button>Click</button>;\n```\n"
            "```code_exec\nCREATE config.json\n<<<\n{\"enabled\": true}\n>>>\n```\nAll done!"
        )
        self.assertEqual((len(r), r[0].args[0]), (1, "config.json"))

    def test_code_exec_plan_sentinels(self):
        r = ops("Alternative:\nCODE_EXEC_PLAN\nCREATE alt.txt\n<<<\nalternative\n>>>\nEND_CODE_EXEC_PLAN\nBye.")
        self.assertEqual((r[0].args[0], r[0].data), ("alt.txt", "alternative"))

    def test_bare_plan_without_fence(self):
        r = ops("CREATE raw.txt\n<<<\nraw content\n>>>\n")
        self.assertEqual((r[0].command, r[0].args[0]), ("CREATE", "raw.txt"))

    def test_think_keyword_is_no_longer_special(self):
        # THINK/END_THINK used to be a dedicated free-text block; the feature was removed
        # as unused. A leftover literal THINK line is now just ordinary text: tolerated
        # only as plain leading junk before a real plan (like any other stray sentence),
        # not as an envelope that can hide arbitrarily-shaped content.
        r = ops("THINK\nplanning this out\nEND_THINK\nDELETE a.py\n")
        self.assertEqual([o.command for o in r], ["DELETE"])
        self.assertTrue(any("before the plan" in w for w in p.take_warnings()))

    def test_edit_in_generic_fence_fallback(self):
        r = ops("Here is the edit:\n\n```diff\nEDIT src/utils.py\n<<<<\ntimeout = 10\n====\ntimeout = 30\n>>>>\n```\n")
        self.assertEqual((r[0].command, r[0].args[0], r[0].data, r[0].extra),
                         ("EDIT", "src/utils.py", "timeout = 10", "timeout = 30"))

    def test_prose_around_bare_plan(self):
        r = ops("Sure, here is the change.\n\nDELETE a.py\nCOMMIT chore: x\n\nLet me know if you need more!")
        self.assertEqual([o.command for o in r], ["DELETE", "COMMIT"])

    def test_thinking_tags_ignored_even_with_fake_plan_inside(self):
        r = ops("<thinking>\n```code_exec\nDELETE important.py\n```\n</thinking>\nEDIT a.py\n<<<<\na\n====\nb\n>>>>")
        self.assertEqual([o.command for o in r], ["EDIT"])

    def test_missing_closing_fence_but_complete_plan_is_accepted(self):
        r = ops("Explanation\n```code_exec\nDELETE a.py\nCOMMIT chore: x")
        self.assertEqual(len(r), 2)
        self.assertTrue(any("closing marker missing" in w for w in p.take_warnings()))

    def test_truncated_block_is_an_error(self):
        with self.assertRaises(OpError) as ctx:
            p.extract_plan("Truncated:\n```code_exec\nCREATE file.txt\n<<<\nsome content\n")
        self.assertIn("ERR|PLAN_NOT_FOUND", str(ctx.exception))
        self.assertIn("unclosed", str(ctx.exception).lower())

    def test_multiple_blocks_are_merged_in_order(self):
        r = ops("```code_exec\nDELETE a.py\n```\ntext\n```code_exec\nDELETE b.py\n```")
        self.assertEqual([o.args[0] for o in r], ["a.py", "b.py"])
        self.assertTrue(any("merged" in w for w in p.take_warnings()))

    def test_multiple_blocks_error_when_merging_disabled(self):
        p.MERGE_MULTIPLE_PLANS = False
        with self.assertRaises(OpError) as ctx:
            p.extract_plan("```code_exec\nDELETE a.py\n```\nPlan 2:\n```code_exec\nDELETE b.py\n```")
        self.assertIn("ERR|MULTIPLE_PLANS|2", str(ctx.exception))


class NoPlanIsNotAnError(Base):
    """A plan is optional: an ordinary answer must not produce a failure."""

    def test_reply_without_plan(self):
        text = "You do not need to change anything; the bug is in your config."
        self.assertEqual(p.extract_plan(text), "")
        self.assertEqual(p.parse_operations(p.extract_plan(text)), [])
        self.assertFalse(p.has_plan(text))

    def test_reply_with_only_unrelated_code(self):
        self.assertFalse(p.has_plan("Example:\n```javascript\nconsole.log('no plan');\n```\n"))

    def test_sentence_starting_with_command_word_is_not_a_plan(self):
        self.assertEqual(ops("Sure.\nRUN the tests now and tell me what happens."), [])

    def test_no_plan_error_can_be_restored(self):
        p.NO_PLAN_IS_ERROR = True
        with self.assertRaises(OpError) as ctx:
            p.extract_plan("just some text")
        self.assertIn("ERR|PLAN_NOT_FOUND", str(ctx.exception))

    def test_broken_plan_still_counts_as_a_plan(self):
        self.assertTrue(p.has_plan("```code_exec\nCREATE a.py <<<\nprint(1)\n"))


# --------------------------------------------------------------------------- #
# Block syntax
# --------------------------------------------------------------------------- #
class BlockStyles(Base):
    def test_keyword_style_and_shortened_delimiters(self):
        for opener in ("<<<", "<", "<<"):
            with self.subTest(opener=opener):
                r = ops(f"EDIT a.py\nSEARCH {opener}\nold\n>>>\nREPLACE {opener}\nnew\n>>>")
                self.assertEqual((r[0].data, r[0].extra), ("old", "new"))

    def test_keyword_on_its_own_line_before_opener(self):
        r = ops("EDIT a.py\nSEARCH\n<<<\nold\n>>>\nREPLACE\n<<<\nnew\n>>>")
        self.assertEqual((r[0].data, r[0].extra), ("old", "new"))

    def test_divider_style(self):
        r = ops("EDIT src/App.jsx\n<<<<\nold code\n====\nnew code\n>>>>\n")
        self.assertEqual((r[0].command, r[0].args[0], r[0].data, r[0].extra),
                         ("EDIT", "src/App.jsx", "old code", "new code"))

    def test_git_labelled_markers(self):
        r = ops("EDIT config.py\n<<<<<<< SEARCH\nDEBUG = True\n=======\nDEBUG = False\n>>>>>>> REPLACE\n")
        self.assertEqual((r[0].data, r[0].extra), ("DEBUG = True", "DEBUG = False"))

    def test_replace_all_divider_style(self):
        r = ops('REPLACE_ALL strings.json\n<<<<\n"active": false\n====\n"active": true\n>>>>\n')
        self.assertEqual((r[0].command, r[0].data, r[0].extra), ("REPLACE_ALL", '"active": false', '"active": true'))

    def test_divider_style_keeps_angle_brackets_in_content(self):
        r = ops("EDIT logic.py\n<<<<\nif a < b and c > d:\n====\nif a <= b and c >= d:\n>>>>\n")
        self.assertEqual((r[0].data, r[0].extra), ("if a < b and c > d:", "if a <= b and c >= d:"))

    def test_jsx_with_lone_gt_line(self):
        plan = ('EDIT src/App.jsx\n<<<<\n<div\n  className="x"\n>\n  hi\n</div>\n====\n'
                '<div\n  className="y"\n>\n  hi\n</div>\n>>>>')
        r = ops(plan)
        self.assertIn('className="x"\n>', r[0].data)
        self.assertIn('className="y"\n>', r[0].extra)

    def test_html_create_with_lone_gt_line(self):
        r = ops('CREATE i.html <<<\n<div\n  class="x"\n>\n  hi\n</div>\n>>>')
        self.assertEqual(r[0].data.count("\n"), 4)

    def test_html_and_jsx_fixture_parsing_and_entities(self):
        from pathlib import Path
        fixtures_dir = Path(__file__).parent / "fixtures"
        html_fixture = (fixtures_dir / "sample.html").read_text(encoding="utf-8")
        jsx_fixture = (fixtures_dir / "Component.jsx").read_text(encoding="utf-8")

        # Verify editing HTML containing lone '>' lines and literal &lt; &gt; entities
        html_plan = f"CREATE mock.html <<<\n{html_fixture}\n>>>"
        parsed_html = ops(html_plan)
        self.assertEqual(parsed_html[0].command, "CREATE")
        self.assertIn("&lt; b and c &gt; d", parsed_html[0].data)
        self.assertIn('data-test="edge-case"\n  >', parsed_html[0].data)

        # Verify JSX parsing with mixed quotes and multi-line props
        jsx_plan = f"CREATE mock.jsx <<<\n{jsx_fixture}\n>>>"
        parsed_jsx = ops(jsx_plan)
        self.assertEqual(parsed_jsx[0].command, "CREATE")
        self.assertIn("className={`btn ${isActive ? 'btn-danger' : 'btn-outline'}`}", parsed_jsx[0].data)
        self.assertIn('alt="Analytics Overview"\n        className="w-12 h-12"\n      />', parsed_jsx[0].data)

    def test_nested_delimiters_in_content(self):
        r = ops("CREATE n.txt <<<\nhello\n<<<\ninner\n>>>\nbye\n>>>")
        self.assertIn("inner", r[0].data)
        self.assertTrue(r[0].data.endswith("bye"))

    def test_comments_and_blank_lines_between_parts(self):
        r = p.parse_operations(
            "# Top comment\n\nCREATE f.txt\n# Mid comment\n<<<\npayload\n>>>\n\n# Final comment\n"
        )
        self.assertEqual((len(r), r[0].data), (1, "payload"))

    def test_several_blocks_under_one_edit(self):
        cases = {
            "divider": "EDIT a.py\n<<<<\na\n====\nA\n>>>>\n<<<<\nb\n====\nB\n>>>>\nCOMMIT feat: x",
            "keyword": "EDIT a.py\nSEARCH <<<\na\n>>>\nREPLACE <<<\nA\n>>>\nSEARCH <<<\nb\n>>>\nREPLACE <<<\nB\n>>>\nCOMMIT feat: x",
        }
        for label, plan in cases.items():
            with self.subTest(label):
                r = ops(plan)
                self.assertEqual([o.command for o in r], ["EDIT", "EDIT", "COMMIT"])
                self.assertEqual([(o.data, o.extra) for o in r[:2]], [("a", "A"), ("b", "B")])
                self.assertEqual(r[1].args, ("a.py",))


# --------------------------------------------------------------------------- #
# Forgiven slips
# --------------------------------------------------------------------------- #
class Normalisation(Base):
    def test_html_escaped_delimiters(self):
        r = ops("EDIT a.py\nSEARCH &lt;&lt;&lt;\nold\n&gt;&gt;&gt;\nREPLACE &lt;&lt;&lt;\nnew\n&gt;&gt;&gt;")
        self.assertEqual((r[0].data, r[0].extra), ("old", "new"))
        self.assertTrue(any("restored" in w for w in p.take_warnings()))

    def test_html_entities_in_file_content_are_untouched(self):
        r = ops("CREATE a.html <<<\n<p>a &lt; b</p>\n>>>")
        self.assertIn("&lt;", r[0].data)

    def test_indented_plan(self):
        r = ops("    EDIT a.py\n    <<<<\n    a\n    ====\n    b\n    >>>>")
        self.assertEqual((r[0].data, r[0].extra), ("a", "b"))

    def test_paths_are_normalised(self):
        r = ops("DELETE ./src/a.js\nDELETE **src/b.js**\nDELETE src\\c.js")
        self.assertEqual([o.args[0] for o in r], ["src/a.js", "src/b.js", "src/c.js"])

    def test_aliases_and_lowercase_commands(self):
        r = ops("```code_exec\nupdate a.py\n<<<<\na\n====\nb\n>>>>\nmv a.py -> b.py\n```")
        self.assertEqual([o.command for o in r], ["EDIT", "MOVE"])

    def test_move_copy_rename_accept_to(self):
        r = ops("MOVE old.txt to new.txt")
        self.assertEqual((r[0].command, r[0].args), ("MOVE", ("old.txt", "new.txt")))

    def test_two_char_diff_markers(self):
        # The bare conflict-marker style's minimum was inconsistent with the closer's
        # (which already allowed 2+); opener/separator/closer all now accept 2+ chars.
        r = ops("EDIT a.py\n<<\nold\n====\nnew\n>>>>\n")
        self.assertEqual((r[0].data, r[0].extra), ("old", "new"))

    def test_trailing_inline_comment_is_stripped(self):
        r = ops("DELETE old/file.py  # no longer needed")
        self.assertEqual(r[0].args[0], "old/file.py")
        self.assertTrue(any("trailing comment" in w for w in p.take_warnings()))

    def test_trailing_lowercase_sentence_is_not_run_as_a_command(self):
        # RUN/COMMIT take free text and always "succeed" to parse, so a stray lowercase
        # sentence after a real plan must be dropped as prose, not executed.
        r = ops("DELETE a.py\nrun the tests manually after this lands")
        self.assertEqual([o.command for o in r], ["DELETE"])
        self.assertTrue(any("trailing text" in w for w in p.take_warnings()))

    def test_short_lowercase_run_still_works(self):
        r = ops("DELETE a.py\nrun pytest -x")
        self.assertEqual([o.command for o in r], ["DELETE", "RUN"])
        self.assertEqual(r[1].args[0], "pytest -x")


# --------------------------------------------------------------------------- #
# Things that must still be rejected
# --------------------------------------------------------------------------- #
class StillStrict(Base):
    def test_unknown_command_suggests_the_right_one(self):
        for bad, expected in (("EDITT file.txt", "EDIT"), ("COMMITT msg", "COMMIT")):
            with self.subTest(bad):
                with self.assertRaises(OpError) as ctx:
                    p.parse_operations(f"{bad}\n<<<\ncontent\n>>>\n")
                self.assertIn("ERR|UNKNOWN_COMMAND", str(ctx.exception))
                self.assertIn(f"Did you mean '{expected}'?", str(ctx.exception))

    def test_shell_command_without_run(self):
        for cmd in ("npm test", "pytest", "python script.py", "git status",
                    "git commit -m 'update'", "pip install -r requirements.txt"):
            with self.subTest(cmd):
                with self.assertRaises(OpError) as ctx:
                    p.parse_operations(cmd)
                self.assertIn("ERR|UNKNOWN_COMMAND", str(ctx.exception))
                self.assertIn("Shell commands must be prefixed with RUN", str(ctx.exception))

    def test_shell_command_after_a_valid_operation(self):
        with self.assertRaises(OpError) as ctx:
            p.parse_operations("DELETE a.py\npytest -q")
        self.assertIn("RUN", str(ctx.exception))

    def test_rm_rf_is_not_an_alias_for_delete(self):
        with self.assertRaises(OpError):
            p.parse_operations("rm -rf build")

    def test_conversational_text_between_operations(self):
        plan = (
            "CREATE test.txt\n<<<\nhello\n>>>\n"
            "Here is conversational text in the middle of a plan:\n"
            "CREATE test2.txt\n<<<\nworld\n>>>\n"
        )
        with self.assertRaises(OpError) as ctx:
            p.parse_operations(plan)
        self.assertIn("ERR|UNKNOWN_COMMAND", str(ctx.exception))

    def test_wrong_edit_keywords(self):
        cases = {
            "FIND instead of SEARCH": ("EDIT file.txt\nFIND\n<<<\nold\n>>>\nREPLACE\n<<<\nnew\n>>>\n", "EDIT requires SEARCH"),
            "WITH instead of REPLACE": ("EDIT file.txt\nSEARCH\n<<<\nold\n>>>\nWITH\n<<<\nnew\n>>>\n", "EDIT requires REPLACE"),
        }
        for label, (plan, message) in cases.items():
            with self.subTest(label):
                with self.assertRaises(ValueError) as ctx:
                    p.parse_operations(plan)
                self.assertIn(message, str(ctx.exception))

    def test_bad_move_copy_syntax(self):
        for bad in ("MOVE old.txt new.txt", "COPY old.txt destination/"):
            with self.subTest(bad):
                with self.assertRaises(ValueError) as ctx:
                    p.parse_operations(bad)
                self.assertIn("requires 'source -> destination'", str(ctx.exception))

    def test_unclosed_blocks(self):
        cases = {
            "keyword style": ("CREATE unclosed.txt\n<<<\nline 1\nline 2\n", "Missing >>>"),
            "divider, no separator": ("EDIT broken.txt\n<<<<\ncontent without divider\n>>>>\n", "missing ===="),
            "divider, no closer": ("EDIT unclosed.txt\n<<<<\nsearch part\n====\nreplace part\n", "missing >>>>"),
        }
        for label, (plan, message) in cases.items():
            with self.subTest(label):
                with self.assertRaises(ValueError) as ctx:
                    p.parse_operations(plan)
                self.assertIn(message.lower(), str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()