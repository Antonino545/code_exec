#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from code_exec_types import Operation, OpError, ROOT
import code_exec_parser as p
from code_exec_plan_export import generate_skeleton, create_plan_folder
from code_exec import handle_fetch_operations, apply_plan, main

p.PRINT_WARNINGS = False


class TestLazyContextAndFetch(unittest.TestCase):
    def setUp(self):
        self.scratch = ROOT / "_test_scratch_fetch"
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.sample_py = self.scratch / "sample.py"
        self.sample_py.write_text(
            '"""Sample module docstring."""\n'
            "def foo(x, y):\n"
            '    """Foo helper function."""\n'
            "    return x + y\n\n"
            "class Bar:\n"
            "    def baz(self, val):\n"
            "        return val * 2\n",
            encoding="utf-8",
        )

    def tearDown(self):
        import shutil
        if self.scratch.exists():
            shutil.rmtree(self.scratch, ignore_errors=True)

    def test_parse_fetch_instruction_formats(self):
        ops = p.parse_operations("FETCH src/app.py\nFETCH src/logic.py:10-50\nFETCH utils.py 20-30\nFETCH src/core.py:my_function\nFETCH_FUNCTION api.py:process_data")
        self.assertEqual(len(ops), 5)
        self.assertEqual(ops[0].command, "FETCH")
        self.assertEqual(ops[0].args, ("src/app.py",))
        self.assertEqual(ops[1].args, ("src/logic.py", "10-50"))
        self.assertEqual(ops[2].args, ("utils.py", "20-30"))
        self.assertEqual(ops[3].args, ("src/core.py", "my_function"))
        self.assertEqual(ops[4].args, ("api.py", "process_data"))

    def test_parse_fetch_aliases(self):
        ops = p.parse_operations("READ src/app.py\nGET src/utils.py:5-15\nFETCH_FUNC logic.py:compute")
        self.assertEqual(len(ops), 3)
        self.assertEqual(ops[0].command, "FETCH")
        self.assertEqual(ops[0].args, ("src/app.py",))
        self.assertEqual(ops[1].command, "FETCH")
        self.assertEqual(ops[1].args, ("src/utils.py", "5-15"))
        self.assertEqual(ops[2].command, "FETCH")
        self.assertEqual(ops[2].args, ("logic.py", "compute"))

    def test_handle_fetch_operations_symbol(self):
        rel_path = f"_test_scratch_fetch/sample.py"
        op = Operation("FETCH", (rel_path, "foo"))
        with patch("code_exec.set_clipboard"):
            body, count, lines, toks = handle_fetch_operations([op])
            self.assertEqual(count, 1)
            self.assertIn("def foo(x, y):", body)
            self.assertNotIn("class Bar", body)

    def test_handle_fetch_operations_class_method(self):
        rel_path = f"_test_scratch_fetch/sample.py"
        op = Operation("FETCH", (rel_path, "Bar.baz"))
        with patch("code_exec.set_clipboard"):
            body, count, lines, toks = handle_fetch_operations([op])
            self.assertEqual(count, 1)
            self.assertIn("def baz(self", body)
            self.assertIn("Bar.baz", body)
            self.assertNotIn("def foo(", body)

    def test_handle_fetch_operations_react_functional_component(self):
        op = Operation("FETCH", ("tests/fixtures/Component.jsx", "DashboardCard"))
        with patch("code_exec.set_clipboard"):
            body, count, lines, toks = handle_fetch_operations([op])
            self.assertEqual(count, 1)
            self.assertIn("export function DashboardCard(", body)
            self.assertIn("DashboardCard", body)
            self.assertIn("lines 7-46", body)
            self.assertNotIn("export default DashboardCard;", body)

    def test_handle_fetch_operations_js_arrow_and_class_components(self):
        sample_js = self.scratch / "components.jsx"
        sample_js.write_text(
            "import React from 'react';\n\n"
            "export const Button = ({ onClick, children }) => {\n"
            "  return (\n"
            "    <button onClick={onClick}>\n"
            "      {children}\n"
            "    </button>\n"
            "  );\n"
            "};\n\n"
            "export class Header extends React.Component {\n"
            "  render() {\n"
            "    return <h1>Title</h1>;\n"
            "  }\n"
            "}\n",
            encoding="utf-8",
        )
        rel_path = "_test_scratch_fetch/components.jsx"

        # Arrow function component
        op1 = Operation("FETCH", (rel_path, "Button"))
        with patch("code_exec.set_clipboard"):
            body1, count, lines, toks = handle_fetch_operations([op1])
            self.assertEqual(count, 1)
            self.assertIn("export const Button = ({ onClick, children }) => {", body1)
            self.assertNotIn("class Header", body1)

        # Class component
        op2 = Operation("FETCH", (rel_path, "Header"))
        with patch("code_exec.set_clipboard"):
            body2, count, lines, toks = handle_fetch_operations([op2])
            self.assertEqual(count, 1)
            self.assertIn("export class Header extends React.Component {", body2)
            self.assertNotIn("const Button", body2)

    def test_handle_fetch_operations_symbol_not_found_fallback(self):
        rel_path = f"_test_scratch_fetch/sample.py"
        op = Operation("FETCH", (rel_path, "nonexistent_fn"))
        with patch("code_exec.set_clipboard"):
            body, count, lines, toks = handle_fetch_operations([op])
            self.assertEqual(count, 1)
            self.assertIn("symbol 'nonexistent_fn' not located", body)
            self.assertIn("def foo(x, y):", body)

    def test_handle_fetch_operations_full_file(self):
        rel_path = f"_test_scratch_fetch/sample.py"
        op = Operation("FETCH", (rel_path,))
        with patch("code_exec.set_clipboard") as mock_set:
            body, count, lines, toks = handle_fetch_operations([op])
            mock_set.assert_called_once()
            self.assertEqual(count, 1)
            self.assertIn("Sample module docstring", body)
            self.assertIn("class Bar", body)

    def test_handle_fetch_operations_line_range(self):
        rel_path = f"_test_scratch_fetch/sample.py"
        op = Operation("FETCH", (rel_path, "2-4"))
        with patch("code_exec.set_clipboard"):
            body, count, lines, toks = handle_fetch_operations([op])
            self.assertEqual(count, 1)
            self.assertEqual(lines, 3)
            self.assertIn("def foo(x, y):", body)
            self.assertNotIn("class Bar", body)

    def test_apply_plan_pure_fetch_exits_cleanly(self):
        rel_path = f"_test_scratch_fetch/sample.py"
        ops = [Operation("FETCH", (rel_path, "1-3"))]
        with patch("code_exec.set_clipboard"):
            exit_code = apply_plan(ops, timeout=10)
            self.assertEqual(exit_code, 0)

    def test_python_skeleton_generation(self):
        skel = generate_skeleton(self.sample_py, max_lines=4)
        self.assertIn("def foo(x, y): ...", skel)
        self.assertIn("class Bar: ...", skel)
        self.assertIn("def baz(self, val): ...", skel)

    def test_create_plan_folder_compact_mode(self):
        out_dir = "_test_scratch_fetch/context_out"
        res = create_plan_folder(
            root=self.scratch,
            output_dirname=out_dir,
            export_all=True,
            compact=True,
        )
        self.assertTrue(res["compact"])
        index_file = self.scratch / out_dir / "PROJECT_INDEX.md"
        self.assertTrue(index_file.is_file())
        self.assertIn("Project Skeleton Index", index_file.read_text(encoding="utf-8"))

    def test_context_folder_automatically_added_to_gitignore(self):
        gitignore = self.scratch / ".gitignore"
        gitignore.write_text("node_modules/\n", encoding="utf-8")
        create_plan_folder(
            root=self.scratch,
            output_dirname="context",
            export_all=True,
        )
        content = gitignore.read_text(encoding="utf-8")
        self.assertIn("context/", content)
        self.assertIn(".code_exec/", content)

    def test_cli_fetch_action(self):
        rel_path = f"_test_scratch_fetch/sample.py:2-4"
        with patch("code_exec.set_clipboard"):
            ret = main(["fetch", rel_path])
            self.assertEqual(ret, 0)


if __name__ == "__main__":
    unittest.main()
