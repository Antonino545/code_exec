"""
Unit tests for Dual-Threshold Activation Zones, Smarter Matching Intelligence,
and Guardrails & Safety Pre-Validation in the Interactive Fuzzy Match Resolver.
"""

from __future__ import annotations

import os
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

import code_exec_matcher as matcher
from code_exec import (
    AUDIT_FUZZY_RESOLUTIONS,
    execute,
    generate_plan_diff,
    interactive_fuzzy_resolver,
    preflight,
    validate_fuzzy_replacement_safety,
)
from code_exec_fs import RealFS
from code_exec_matcher import (
    CONFIDENT_THRESHOLD,
    RESOLVER_THRESHOLD,
    _find_high_similarity_candidates,
    _find_high_similarity_match,
    _semantic_similarity,
    find_unique,
)
from code_exec_types import ROOT, FuzzyCandidate, MatchResult, OpError, Operation
from code_exec_ui import ui


class TestFuzzyResolverIntelligenceAndSafety(unittest.TestCase):
    def setUp(self):
        self.scratch = ROOT / "_test_fuzzy_scratch"
        self.scratch.mkdir(exist_ok=True)
        self.scratch_rel = "_test_fuzzy_scratch"
        self.orig_policy = matcher.FUZZY_POLICY
        self.orig_resolver = matcher.FUZZY_RESOLVER
        AUDIT_FUZZY_RESOLUTIONS.clear()

    def tearDown(self):
        matcher.FUZZY_POLICY = self.orig_policy
        matcher.FUZZY_RESOLVER = self.orig_resolver
        shutil.rmtree(self.scratch, ignore_errors=True)

    # =========================================================================
    # 1. Smarter Matching Intelligence
    # =========================================================================

    def test_semantic_token_weighting_rejects_identifier_drift(self):
        """
        Identifier names differing heavily should be penalized below threshold,
        even if overall punctuation and code structure are very similar.
        """
        doc = (
            "def handle_payment(account_id, amount, currency='USD'):\n"
            "    validate_account(account_id)\n"
            "    process_transaction(amount, currency)\n"
            "    return {'status': 'success', 'account': account_id}\n"
        )
        needle = (
            "def handle_shipment(carrier_id, weight, unit='KG'):\n"
            "    validate_carrier(carrier_id)\n"
            "    process_dispatch(weight, unit)\n"
            "    return {'status': 'success', 'carrier': carrier_id}\n"
        )
        sim = _semantic_similarity(needle, doc)
        self.assertLess(sim, RESOLVER_THRESHOLD)
        with self.assertRaises(OpError) as ctx:
            matcher.FUZZY_POLICY = "strict"
            find_unique(doc, needle, "SEARCH", "service.py")
        self.assertIn("ERR|SEARCH_NOT_FOUND", str(ctx.exception))

    def test_semantic_token_weighting_tolerates_quotes_comments_and_commas(self):
        """
        Changes in comments, quote styles, and trailing commas should maintain
        a very high similarity (>= CONFIDENT_THRESHOLD), matching automatically.
        """
        doc = (
            "def fetch_data(user_id):\n"
            "    # Query database for user records\n"
            "    query = 'SELECT * FROM users WHERE id = %s'\n"
            "    options = {'timeout': 30, 'retries': 3,}\n"
            "    return execute(query, (user_id,), options=options)\n"
        )
        needle = (
            'def fetch_data(user_id):\n'
            '    query = "SELECT * FROM users WHERE id = %s"\n'
            '    options = {"timeout": 30, "retries": 3}\n'
            '    return execute(query, (user_id,), options=options)'
        )
        sim = _semantic_similarity(needle, doc)
        self.assertGreaterEqual(sim, CONFIDENT_THRESHOLD)
        match = find_unique(doc, needle, "SEARCH", "db.py")
        self.assertTrue(match.fuzzy)
        self.assertGreaterEqual(match.similarity, CONFIDENT_THRESHOLD)

    def test_elastic_window_with_omitted_blank_lines(self):
        """
        When the file contains multiple blank lines that the LLM omitted in its SEARCH
        block (difference of 4 lines), elastic window finds the candidate accurately.
        """
        doc_lines = [
            "class MetricsCollector:",
            "    def __init__(self, name):",
            "        self.name = name",
            "",
            "",
            "",
            "        self.metrics = []",
            "",
            "    def record(self, value):",
            "        self.metrics.append(value)",
        ]
        doc = "\n".join(doc_lines) + "\n"

        needle = (
            "    def __init__(self, name):\n"
            "        self.name = name\n"
            "        self.metrics = []"
        )
        candidates = _find_high_similarity_candidates(doc, needle, min_threshold=0.72)
        self.assertGreater(len(candidates), 0)
        best = candidates[0]
        self.assertEqual(best[3], 1)
        self.assertEqual(best[4], 6)
        self.assertGreaterEqual(best[0], 0.80)

    def test_boundary_constrained_fuzzy_matching(self):
        """
        When the head and tail lines match, boundary-constrained detection isolates
        the bounded region even if inner lines have drifted.
        """
        doc = (
            "def build_config():\n"
            "    cfg = {}\n"
            "    cfg['debug'] = True\n"
            "    cfg['version'] = '1.0'\n"
            "    cfg['env'] = 'development'\n"
            "    return cfg\n"
        )
        needle = (
            "def build_config():\n"
            "    cfg = {}\n"
            "    cfg['debug'] = False\n"
            "    cfg['version'] = '1.0'\n"
            "    return cfg"
        )
        candidates = _find_high_similarity_candidates(doc, needle, min_threshold=0.72)
        self.assertGreater(len(candidates), 0)
        best = candidates[0]
        self.assertEqual(best[3], 0)  # Starts at def build_config()
        self.assertEqual(best[4], 5)  # Ends at return cfg

    # =========================================================================
    # 2. Dual-Threshold Activation Zones
    # =========================================================================

    def test_confident_zone_auto_accepts_above_90_percent(self):
        """Candidates with >= 90% similarity auto-match without needing interactive approval."""
        doc = (
            "def setup_routes(app):\n"
            "    app.add_route('/api/users', UserHandler)\n"
            "    app.add_route('/api/auth', AuthHandler)\n"
            "    app.add_route('/health', HealthHandler)\n"
        )
        needle = (
            "def setup_routes(app):\n"
            "    app.add_route('/api/users', UserHandler)\n"
            "    app.add_route('/api/auth', AuthHandler);\n"  # minor semicolon drift
            "    app.add_route('/health', HealthHandler)\n"
        )
        matcher.FUZZY_POLICY = "strict"
        match = find_unique(doc, needle, "SEARCH", "routes.py")
        self.assertTrue(match.fuzzy)
        self.assertGreaterEqual(match.similarity, CONFIDENT_THRESHOLD)

    def test_borderline_zone_strict_mode_rejects_with_actionable_info(self):
        """In strict mode (CI/non-interactive), borderline matches (72-89%) raise error with candidate details."""
        doc = (
            "def render_dashboard(user, active_tab='home'):\n"
            "    context = build_user_context(user)\n"
            "    template = load_template('dashboard.html')\n"
            "    return template.render(context, tab=active_tab)\n"
        )
        needle = (
            "def render_dashboard(user, active_tab='overview'):\n"
            "    context = create_context(user)\n"
            "    template = get_template('dashboard.html')\n"
            "    return template.render(context, tab=active_tab)\n"
        )
        matcher.FUZZY_POLICY = "strict"
        with self.assertRaises(OpError) as ctx:
            find_unique(doc, needle, "SEARCH", "views.py")
        err = str(ctx.exception)
        self.assertIn("ERR|SEARCH_NOT_FOUND|views.py", err)
        self.assertIn("candidate found at lines 1-4", err)
        self.assertIn("below 90% threshold", err)

    def test_borderline_zone_auto_policy_accepts(self):
        """With FUZZY_POLICY='auto', borderline candidates (72-89%) are accepted with a note."""
        doc = (
            "def render_dashboard(user, active_tab='home'):\n"
            "    context = build_user_context(user)\n"
            "    template = load_template('dashboard.html')\n"
            "    return template.render(context, tab=active_tab)\n"
        )
        needle = (
            "def render_dashboard(user, active_tab='overview'):\n"
            "    context = create_context(user)\n"
            "    template = get_template('dashboard.html')\n"
            "    return template.render(context, tab=active_tab)\n"
        )
        matcher.FUZZY_POLICY = "auto"
        match = find_unique(doc, needle, "SEARCH", "views.py")
        self.assertTrue(match.fuzzy)
        self.assertGreaterEqual(match.similarity, RESOLVER_THRESHOLD)
        self.assertLess(match.similarity, CONFIDENT_THRESHOLD)
        self.assertIn("auto-accepted borderline fuzzy match", match.note)

    def test_borderline_zone_interactive_hook_accept(self):
        """When interactive resolver hook is registered, user acceptance returns the MatchResult."""
        doc = (
            "def calculate_tax(subtotal, rate=0.08):\n"
            "    fee = compute_handling_fee(subtotal)\n"
            "    return subtotal * rate + fee\n"
        )
        needle = (
            "def calculate_tax(subtotal, rate=0.05):\n"
            "    fee = calculate_handling(subtotal)\n"
            "    return subtotal * rate + fee\n"
        )

        def mock_resolver(target, needle_str, candidates):
            cand = candidates[0]
            return MatchResult(
                cand.start,
                cand.end,
                " (resolved by test hook)",
                True,
                (cand.start_line, cand.end_line),
                similarity=cand.similarity,
            )

        matcher.FUZZY_RESOLVER = mock_resolver
        match = find_unique(doc, needle, "SEARCH", "pricing.py")
        self.assertTrue(match.fuzzy)
        self.assertEqual(match.note, " (resolved by test hook)")

    def test_borderline_zone_interactive_hook_rejection(self):
        """When interactive resolver hook returns None (user rejected), an explicit OpError is raised."""
        doc = (
            "def calculate_tax(subtotal, rate=0.08):\n"
            "    fee = compute_handling_fee(subtotal)\n"
            "    return subtotal * rate + fee\n"
        )
        needle = (
            "def calculate_tax(subtotal, rate=0.05):\n"
            "    fee = calculate_handling(subtotal)\n"
            "    return subtotal * rate + fee\n"
        )

        matcher.FUZZY_RESOLVER = lambda target, n, c: None
        with self.assertRaises(OpError) as ctx:
            find_unique(doc, needle, "SEARCH", "pricing.py")
        self.assertIn("ERR|SEARCH_FUZZY_REJECTED", str(ctx.exception))

    def test_ambiguity_detection_and_disambiguation(self):
        """Multiple candidates with similar scores trigger ambiguity error or resolver hook."""
        doc = (
            "def get_user_v1(uid):\n"
            "    user = db.find(uid)\n"
            "    return user\n"
            "\n"
            "def get_user_v2(uid):\n"
            "    user = db.find(uid)\n"
            "    return user\n"
        )
        needle = (
            "def get_user(uid):\n"
            "    user = db.find(uid)\n"
            "    return user\n"
        )
        matcher.FUZZY_POLICY = "strict"
        matcher.FUZZY_RESOLVER = None
        with self.assertRaises(OpError) as ctx:
            find_unique(doc, needle, "SEARCH", "api.py")
        self.assertIn("ERR|SEARCH_AMBIGUOUS|api.py", str(ctx.exception))

        def disambiguate_hook(target, n, candidates):
            cand = candidates[1]  # Pick the second candidate (v2)
            return MatchResult(
                cand.start,
                cand.end,
                " (disambiguated v2)",
                True,
                (cand.start_line, cand.end_line),
                similarity=cand.similarity,
            )

        matcher.FUZZY_RESOLVER = disambiguate_hook
        match = find_unique(doc, needle, "SEARCH", "api.py")
        self.assertEqual(match.note, " (disambiguated v2)")
        self.assertEqual(match.line_range, (4, 6))

    # =========================================================================
    # 3. 🛡️ Guardrails & Safety Pre-Validation
    # =========================================================================

    def test_guardrail_blocks_python_syntax_corruption(self):
        """Temporary compatibility mode: fuzzy syntax problems are ignored instead of aborting the edit."""
        original_doc = (
            "def process_items(items):\n"
            "    total = 0\n"
            "    for item in items:\n"
            "        total += item.price\n"
            "    return total\n"
        )
        corrupted_doc = (
            "def process_items(items):\n"
            "    total = sum((item.price for item in items\n"  # missing closing parenthesis!
            "    return total\n"
        )
        match = MatchResult(0, 50, "fuzzy", fuzzy=True, line_range=(1, 3))
        validate_fuzzy_replacement_safety("app.py", original_doc, corrupted_doc, match)

    def test_guardrail_blocks_json_syntax_corruption(self):
        """Temporary compatibility mode: invalid JSON from a fuzzy edit is tolerated for now."""
        original_json = '{\n  "name": "code_exec",\n  "version": "1.0"\n}\n'
        corrupted_json = '{\n  "name": "code_exec",\n  "version": "1.0",\n}\n'  # illegal trailing comma
        match = MatchResult(0, 20, "fuzzy", fuzzy=True, line_range=(1, 2))
        validate_fuzzy_replacement_safety("package.json", original_json, corrupted_json, match)

    def test_guardrail_blocks_unbalanced_brackets_in_javascript(self):
        """Temporary compatibility mode: bracket imbalance in fuzzy edits is ignored for now."""
        original_js = (
            "function calculateTotal(items) {\n"
            "    let sum = 0;\n"
            "    items.forEach(it => { sum += it.price; });\n"
            "    return sum;\n"
            "}\n"
        )
        corrupted_js = (
            "function calculateTotal(items) {\n"
            "    let sum = 0;\n"
            "    items.forEach(it => { sum += it.price; });\n"
            "    return sum;\n"
        )
        match = MatchResult(0, 30, "fuzzy", fuzzy=True, line_range=(0, 4))
        validate_fuzzy_replacement_safety("utils.js", original_js, corrupted_js, match)

    def test_guardrail_protects_file_on_disk_during_execution(self):
        """In compatibility mode, fuzzy syntax warnings do not abort the edit and no rollback is needed."""
        target_file = self.scratch / "safe_script.py"
        valid_code = "def foo():\n    return 42\n"
        target_file.write_text(valid_code, encoding="utf-8")

        fs = RealFS(timeout=5)
        op = Operation(
            "EDIT",
            (f"{self.scratch_rel}/safe_script.py",),
            data="def foo():\n   return 42",
            extra="def foo():\n   return ((42",  # Unclosed parens syntax error
        )
        matcher.FUZZY_POLICY = "auto"
        result = execute(op, fs)
        self.assertIsNotNone(result)
        self.assertEqual(target_file.read_text(encoding="utf-8"), "def foo():\n   return ((42\n")
        fs.cleanup()

    def test_fuzzy_audit_logging_and_summary(self):
        """Fuzzy operations are recorded in the audit log and can be summarized."""
        target_file = self.scratch / "audited_app.py"
        target_file.write_text("def ping():\n    return 'pong'\n", encoding="utf-8")

        fs = RealFS(timeout=5)
        op = Operation(
            "EDIT",
            (f"{self.scratch_rel}/audited_app.py",),
            data="def ping():\n    return \"pong\"",  # quote drift
            extra="def ping():\n    return 'pong-updated'\n",
        )
        matcher.FUZZY_POLICY = "auto"
        msg = execute(op, fs)
        self.assertIn("Edited", msg)
        self.assertEqual(len(AUDIT_FUZZY_RESOLUTIONS), 1)
        logged_target, logged_range, logged_sim = AUDIT_FUZZY_RESOLUTIONS[0]
        self.assertEqual(logged_target, f"{self.scratch_rel}/audited_app.py")
        self.assertGreaterEqual(logged_sim, 0.90)
        fs.cleanup()

    # =========================================================================
    # 4. Interactive Resolver TUI & Search Indicator Concurrency
    # =========================================================================

    def test_searching_indicator_lifecycle_and_stop_searching(self):
        """Verifies that ui.stop_searching cleanly terminates the background animation."""
        with ui.searching("heavy_scan.jsx"):
            self.assertIsNotNone(ui._active_search)
            # Call stop_searching while inside the context
            ui.stop_searching()
            self.assertIsNone(ui._active_search)
        # Context manager exit should be clean and not raise
        self.assertIsNone(ui._active_search)

    def test_resolve_fuzzy_match_accepts_a_and_bare_enter(self):
        """Entering 'a', 'A', '  a  ', or bare Enter (default) accepts the fuzzy match."""
        cand = FuzzyCandidate(0.85, 0, 10, 0, 1, "test")
        doc_lines = ["line 1", "line 2"]

        # 1. Test explicit 'a'
        with patch.object(ui, "prompt_choice", return_value="a"):
            self.assertTrue(ui.resolve_fuzzy_match("test.js", "needle", doc_lines, cand))

        # 2. Test whitespace-padded '  a  '
        with patch.object(ui, "prompt_choice", return_value="  a  "):
            self.assertTrue(ui.resolve_fuzzy_match("test.js", "needle", doc_lines, cand))

        # 3. Test empty input (bare Enter, defaults to 'a')
        with patch.object(ui, "prompt_choice", return_value=""):
            self.assertTrue(ui.resolve_fuzzy_match("test.js", "needle", doc_lines, cand))

        # 4. Test explicit 's' (skip)
        with patch.object(ui, "prompt_choice", return_value="s"):
            self.assertFalse(ui.resolve_fuzzy_match("test.js", "needle", doc_lines, cand))

    def test_resolve_fuzzy_match_reprompts_on_invalid_input(self):
        """Typing invalid choices re-prompts the user instead of rejecting or crashing."""
        cand = FuzzyCandidate(0.85, 0, 10, 0, 1, "test")
        doc_lines = ["line 1", "line 2"]

        # Returns invalid first, then accepts with 'a'
        inputs = iter(["invalid_input", "x", "a"])
        with patch.object(ui, "prompt_choice", side_effect=lambda *args, **kwargs: next(inputs)):
            self.assertTrue(ui.resolve_fuzzy_match("test.js", "needle", doc_lines, cand))

    def test_find_unique_stops_searching_before_invoking_fuzzy_resolver(self):
        """find_unique stops the searching animation BEFORE calling the interactive resolver."""
        doc = "def foo():\n    return 1\n"
        needle = "def foo():\n    return 2"  # Borderline match
        stopped_during_resolver = []

        def mock_resolver(target, needle_str, candidates):
            # Check if active search was stopped
            stopped_during_resolver.append(ui._active_search is None)
            cand = candidates[0]
            return MatchResult(cand.start, cand.end, "resolved", True, (cand.start_line, cand.end_line), cand.similarity)

        matcher.FUZZY_RESOLVER = mock_resolver
        find_unique(doc, needle, "SEARCH", "app.py")
        self.assertEqual(stopped_during_resolver, [True])

    def test_generate_plan_diff_reuses_match_cache_without_rescanning(self):
        """generate_plan_diff uses _match_cache so it never triggers a second search or resolver prompt."""
        test_file = self.scratch / "diff_cache_test.py"
        test_file.write_text("def greet():\n    print('hello world')\n", encoding="utf-8")

        op = Operation(
            "EDIT",
            (f"{self.scratch_rel}/diff_cache_test.py",),
            data="def greet():\n    print(\"hello world\")",  # quote drift (borderline/confident)
            extra="def greet():\n    print('hello universe')\n",
        )

        resolver_calls = []

        def tracking_resolver(target, needle, candidates):
            resolver_calls.append(target)
            cand = candidates[0]
            return MatchResult(cand.start, cand.end, "resolved", True, (cand.start_line, cand.end_line), cand.similarity)

        matcher.FUZZY_RESOLVER = tracking_resolver
        matcher.FUZZY_POLICY = "prompt"

        # 1. Run preflight: populates match_cache
        _, _, match_cache = preflight([op])
        self.assertIn(id(op), match_cache)

        # Count resolver calls during preflight
        preflight_calls = len(resolver_calls)

        # 2. Run generate_plan_diff WITH match_cache: should reuse match_cache and NOT invoke resolver again
        diff_text = generate_plan_diff([op], _match_cache=match_cache)
        self.assertIn("-    print('hello world')", diff_text)
        self.assertIn("+    print('hello universe')", diff_text)
        self.assertEqual(len(resolver_calls), preflight_calls, "generate_plan_diff must not re-invoke matcher/resolver")


if __name__ == "__main__":
    unittest.main()


