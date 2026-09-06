"""SPEC §T.7 — the six tools, each profile, budget, ambiguity, degradation."""

import shutil
import tempfile
import unittest
from pathlib import Path

from ast_mcp import parser, tools
from ast_mcp.index import Index

FIXTURES = Path(__file__).parent / "fixtures"


class ToolTestBase(unittest.TestCase):
    """One indexed copy of the fixture tree, shared by every test in the class."""

    @classmethod
    def setUpClass(cls):
        parser.cache_clear()
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        shutil.copytree(FIXTURES, cls.root / "src")
        cls.index = Index(cls.root)
        cls.index.refresh_all()

    @classmethod
    def tearDownClass(cls):
        cls.index.close()
        cls._tmp.cleanup()


class TestFileOutline(ToolTestBase):
    def test_profile_and_group_are_always_declared(self):
        """SPEC §V.12 — the agent never has to guess the payload key."""
        cases = {
            "src/core/sample.py": ("symbols", "core", "symbols"),
            "src/data/sample.yaml": ("schema", "data", "schema"),
            "src/docs/sample.md": ("outline", "docs", "outline"),
            "src/web/sample.html": ("outline", "web", "outline"),
            "src/data/schema.sql": ("defs", "data", "symbols"),
        }
        for path, (profile, group, key) in cases.items():
            with self.subTest(path=path):
                result = tools.file_outline(self.index, path)
                self.assertEqual(result["profile"], profile)
                self.assertEqual(result["group"], group)
                self.assertIn(key, result)

    def test_symbols_are_nested_not_flat(self):
        result = tools.file_outline(self.index, "src/core/sample.py")
        widget = next(s for s in result["symbols"] if s["name"] == "Widget")
        self.assertEqual(
            {c["name"] for c in widget["children"]}, {"render", "_hidden"}
        )
        self.assertNotIn("parent", widget, "the tree already encodes containment")

    def test_bodies_are_excluded(self):
        result = tools.file_outline(self.index, "src/core/sample.py")
        top = next(s for s in result["symbols"] if s["name"] == "top_level")
        self.assertIn("def top_level", top["signature"])
        self.assertNotIn("return", top["signature"])
        self.assertNotIn("source", top)

    def test_docstrings_are_opt_in(self):
        without = tools.file_outline(self.index, "src/core/sample.py")
        with_docs = tools.file_outline(
            self.index, "src/core/sample.py", include_docstrings=True
        )
        self.assertNotIn("docstring", without["symbols"][0])
        self.assertIn("docstring", with_docs["symbols"][0])

    def test_max_depth_trims_the_tree(self):
        shallow = tools.file_outline(self.index, "src/core/sample.py", max_depth=1)
        widget = next(s for s in shallow["symbols"] if s["name"] == "Widget")
        self.assertEqual(widget.get("children", []), [])

    def test_schema_outline_collapses_a_huge_csv(self):
        """SPEC §V.11 — 5000 rows cost what 3 rows cost."""
        big = tools.file_outline(self.index, "src/data/big.csv")
        small = tools.file_outline(self.index, "src/data/small.csv")
        self.assertEqual(len(big["schema"]), len(small["schema"]))
        self.assertFalse(big["truncated"])

    def test_names_mode_costs_less_than_the_full_outline(self):
        """SPEC §I.tools — enumeration is what `grep -n` wins; this takes it back."""
        from ast_mcp.render import estimate_tokens

        full = tools.file_outline(self.index, "src/core/sample.py")
        names = tools.file_outline(self.index, "src/core/sample.py", mode="names")
        self.assertLess(
            estimate_tokens(names["symbols"]), estimate_tokens(full["symbols"]) / 2
        )
        self.assertNotIn("signature", names["symbols"][0])
        self.assertIn("qualified_name", names["symbols"][0])
        self.assertIn("start_line", names["symbols"][0])

    def test_names_mode_keeps_schema_counts(self):
        result = tools.file_outline(self.index, "src/data/small.csv", mode="names")
        column = result["schema"][0]
        self.assertEqual(column["children_count"], 3)
        self.assertNotIn("value_preview", column)

    def test_unknown_mode_is_reported_not_guessed(self):
        result = tools.file_outline(self.index, "src/core/sample.py", mode="brief")
        self.assertEqual([e["code"] for e in result["errors"]], ["bad_mode"])

    def test_schema_outline_declares_uniformity_and_leaks_no_rows(self):
        """SPEC §V.11, §G — the count is trustworthy, the rows stay in the file."""
        result = tools.file_outline(self.index, "src/data/mixed.json")
        by_path = {n["key_path"]: n for n in result["schema"]}
        self.assertEqual(by_path["records"]["uniformity"], "mixed")
        self.assertEqual(by_path["records"]["children_count"], 3)
        self.assertNotIn("uniformity", by_path["secret"])
        element = by_path["records"]["children"][0]
        self.assertIsNone(element["value_preview"])

    def test_budget_trims_and_names_the_narrowing_argument(self):
        """SPEC §V.1 — never silently drop."""
        result = tools.file_outline(self.index, "src/core/sample.py", max_tokens=1)
        self.assertTrue(result["truncated"])
        self.assertIn("max_depth", result["narrow_with"])

    def test_unreadable_path_returns_a_payload_not_an_exception(self):
        """SPEC §V.5."""
        result = tools.file_outline(self.index, "src/nope.py")
        self.assertEqual([e["code"] for e in result["errors"]], ["unreadable"])


class TestBudget(ToolTestBase):
    def test_assembled_response_fits_the_budget(self):
        """SPEC §V.1 — `fit` spends before the hint and errors exist."""
        from ast_mcp.render import estimate_tokens

        result = tools.search_symbols(self.index, "render", limit=20, max_tokens=200)
        self.assertLessEqual(estimate_tokens(result), 200)
        self.assertTrue(result["truncated"])


class TestGetSymbol(ToolTestBase):
    def test_returns_exact_source_for_a_definition(self):
        result = tools.get_symbol(self.index, "Widget.render")
        self.assertTrue(result["found"])
        self.assertFalse(result["ambiguous"])
        self.assertTrue(result["symbol"]["source"].startswith("def render"))
        self.assertEqual(result["symbol"]["kind"], "method")

    def test_ambiguous_name_lists_candidates_and_picks_nothing(self):
        """SPEC §V.9 — a confidently wrong slice is the worst failure mode."""
        result = tools.get_symbol(self.index, "render")
        self.assertTrue(result["ambiguous"])
        self.assertNotIn("symbol", result)
        names = {c["qualified_name"] for c in result["candidates"]}
        self.assertIn("Widget.render", names)
        self.assertGreater(len(names), 1)

    def test_repeated_definitions_come_back_whole(self):
        """SPEC §V.9 — two listeners on one event are N answers, not ambiguity."""
        result = tools.get_symbol(self.index, "sample:ping")
        self.assertTrue(result["found"])
        self.assertFalse(result["ambiguous"])
        self.assertNotIn("symbol", result)
        self.assertEqual(result["total_matches"], 2)
        self.assertEqual(len(result["symbols"]), 2)
        lines = sorted(s["start_line"] for s in result["symbols"])
        self.assertEqual(len(set(lines)), 2)
        for entry in result["symbols"]:
            self.assertEqual(entry["kind"], "handler")
            self.assertTrue(entry["source"].startswith("RegisterNetEvent"))

    def test_line_addresses_one_of_several_identical_names(self):
        """`path` cannot separate two handlers in one file; `line` can."""
        both = tools.get_symbol(self.index, "sample:ping")
        wanted = max(s["start_line"] for s in both["symbols"])
        result = tools.get_symbol(self.index, "sample:ping", line=wanted)
        self.assertTrue(result["found"])
        self.assertNotIn("symbols", result)
        self.assertEqual(result["symbol"]["start_line"], wanted)

    def test_line_that_matches_nothing_is_not_found(self):
        result = tools.get_symbol(self.index, "sample:ping", line=9999)
        self.assertFalse(result["found"])
        self.assertEqual([e["code"] for e in result["errors"]], ["not_found"])

    def test_overflow_names_are_charged_to_the_budget(self):
        """SPEC §V.1 — the assembled payload fits, not just its rows."""
        from ast_mcp.render import estimate_tokens

        for budget in (4000, 900, 400):
            with self.subTest(budget=budget):
                result = tools.get_symbol(
                    self.index, "sample:ping", mode="source", max_tokens=budget
                )
                self.assertLessEqual(estimate_tokens(result), budget)

    def test_path_disambiguates(self):
        result = tools.get_symbol(self.index, "render", path="src/core/sample.py")
        self.assertTrue(result["found"])
        self.assertFalse(result["ambiguous"])

    def test_signature_and_doc_modes_omit_the_body(self):
        for mode in ("signature", "doc"):
            with self.subTest(mode=mode):
                result = tools.get_symbol(self.index, "Widget.render", mode=mode)
                self.assertNotIn("source", result["symbol"])

    def test_schema_profile_is_addressed_by_key_path(self):
        result = tools.get_symbol(self.index, "database.host")
        self.assertTrue(result["found"])
        self.assertEqual(result["symbol"]["profile"], "schema")
        self.assertIn("localhost", result["symbol"]["source"])

    def test_outline_profile_is_addressed_by_slug(self):
        result = tools.get_symbol(self.index, "deep-section")
        self.assertTrue(result["found"])
        self.assertEqual(result["symbol"]["profile"], "outline")
        self.assertIn("Deep Section", result["symbol"]["source"])

    def test_unknown_name_is_not_found_not_an_error_page(self):
        result = tools.get_symbol(self.index, "no_such_symbol_anywhere")
        self.assertFalse(result["found"])
        self.assertIn("not_found", [e["code"] for e in result["errors"]])

    def test_bad_mode_is_reported(self):
        result = tools.get_symbol(self.index, "Widget", mode="sideways")
        self.assertEqual([e["code"] for e in result["errors"]], ["bad_mode"])

    def test_oversized_source_is_clipped(self):
        result = tools.get_symbol(self.index, "Widget.render", max_tokens=30)
        self.assertTrue(result["truncated"])
        self.assertTrue(result["symbol"]["source"].endswith("…"))


class TestSearchSymbols(ToolTestBase):
    def test_hits_carry_their_profile(self):
        """SPEC §V.12 — mixed-profile results stay unambiguous."""
        result = tools.search_symbols(self.index, "alpha")
        self.assertTrue(result["hits"])
        self.assertTrue(all("profile" in h for h in result["hits"]))

    def test_group_filter(self):
        result = tools.search_symbols(self.index, "port", group="data")
        self.assertTrue(result["hits"])
        self.assertTrue(all(h["group"] == "data" for h in result["hits"]))

    def test_kind_and_lang_filters(self):
        by_kind = tools.search_symbols(self.index, "render", kind="method")
        self.assertTrue(all(h["kind"] == "method" for h in by_kind["hits"]))
        by_lang = tools.search_symbols(self.index, "render", lang="go")
        self.assertTrue(all(h["lang"] == "go" for h in by_lang["hits"]))

    def test_limit_reports_the_untruncated_total(self):
        result = tools.search_symbols(self.index, "a", limit=2)
        self.assertLessEqual(len(result["hits"]), 2)
        self.assertGreater(result["total_matches"], len(result["hits"]))
        self.assertTrue(result["truncated"])

    def test_no_match_is_an_empty_hit_list(self):
        result = tools.search_symbols(self.index, "zzz_no_such_thing")
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["errors"], [])


class TestGetDocstrings(ToolTestBase):
    def test_by_path_lists_undocumented_symbols_too(self):
        """Absence of a docstring is signal, so it is reported as null."""
        result = tools.get_docstrings(self.index, path="src/core/sample.py")
        docs = {d["qualified_name"]: d["docstring"] for d in result["docs"]}
        self.assertEqual(docs["top_level"], '"""Top-level function doc."""')
        self.assertIsNone(docs["undocumented"])

    def test_bodies_are_never_included(self):
        result = tools.get_docstrings(self.index, path="src/core/sample.py")
        self.assertTrue(all("source" not in d for d in result["docs"]))

    def test_by_symbol_names(self):
        result = tools.get_docstrings(self.index, symbols=["Widget.render"])
        self.assertEqual(len(result["docs"]), 1)
        self.assertIn("Render the widget", result["docs"][0]["docstring"])

    def test_schema_profile_returns_key_comments(self):
        result = tools.get_docstrings(self.index, path="src/data/sample.yaml")
        docs = {d["qualified_name"]: d["docstring"] for d in result["docs"]}
        self.assertEqual(docs["database"], "# Database settings.")

    def test_json_has_no_comment_syntax_so_all_docs_are_null(self):
        result = tools.get_docstrings(self.index, path="src/data/sample.json")
        self.assertTrue(result["docs"])
        self.assertTrue(all(d["docstring"] is None for d in result["docs"]))
        self.assertEqual(result["errors"], [])

    def test_both_or_neither_argument_is_rejected(self):
        for kwargs in ({}, {"path": "src/core/sample.py", "symbols": ["Widget"]}):
            with self.subTest(kwargs=kwargs):
                result = tools.get_docstrings(self.index, **kwargs)
                self.assertEqual([e["code"] for e in result["errors"]], ["bad_args"])


class TestListImports(ToolTestBase):
    def test_javascript_imports_and_exports(self):
        result = tools.list_imports(self.index, "src/core/sample.js")
        modules = {(i["kind"], i["module"]) for i in result["imports"]}
        self.assertIn(("import", "node:fs"), modules)
        self.assertIn(("require", "lodash"), modules)
        self.assertEqual({e["name"] for e in result["exports"]}, {"add", "scale", "Box"})

    def test_languages_without_exports_get_an_empty_list_not_null(self):
        result = tools.list_imports(self.index, "src/core/sample.py")
        self.assertEqual(result["exports"], [])

    def test_markdown_returns_link_and_image_targets(self):
        """SPEC §B.1 — delivered without loading markdown_inline."""
        result = tools.list_imports(self.index, "src/docs/sample.md")
        modules = {(i["kind"], i["module"]) for i in result["imports"]}
        self.assertIn(("link", "https://example.com/a"), modules)
        self.assertIn(("image", "img/b.png"), modules)

    def test_html_asset_references(self):
        result = tools.list_imports(self.index, "src/web/sample.html")
        modules = {(i["kind"], i["module"]) for i in result["imports"]}
        self.assertIn(("script", "app.js"), modules)
        self.assertIn(("stylesheet", "styles/app.css"), modules)

    def test_schema_profile_has_no_import_concept(self):
        result = tools.list_imports(self.index, "src/data/sample.json")
        self.assertEqual(result["imports"], [])
        self.assertEqual(result["exports"], [])


class TestAstQuery(ToolTestBase):
    def test_captures_are_returned_with_ranges_and_text(self):
        result = tools.ast_query(
            self.index,
            "src/core/sample.py",
            "(function_definition name: (identifier) @fn)",
        )
        self.assertTrue(result["matches"])
        names = {m["text"] for m in result["matches"] if m["capture"] == "fn"}
        self.assertIn("top_level", names)

    def test_capture_filter(self):
        query = "(function_definition name: (identifier) @fn body: (block) @body)"
        result = tools.ast_query(
            self.index, "src/core/sample.py", query, captures=["fn"]
        )
        self.assertTrue(result["matches"])
        self.assertEqual({m["capture"] for m in result["matches"]}, {"fn"})

    def test_malformed_query_is_data_not_a_crash(self):
        """SPEC §V.5."""
        result = tools.ast_query(self.index, "src/core/sample.py", "(nonsense")
        self.assertEqual(result["matches"], [])
        self.assertEqual([e["code"] for e in result["errors"]], ["bad_query"])

    def test_works_on_a_schema_profile_file(self):
        """The escape hatch is profile-independent by design."""
        result = tools.ast_query(self.index, "src/data/sample.json", "(pair) @p")
        self.assertTrue(result["matches"])

    def test_budget_applies(self):
        result = tools.ast_query(
            self.index, "src/core/sample.py", "(_) @all", max_tokens=20
        )
        self.assertTrue(result["truncated"])
        self.assertIn("captures", result["narrow_with"])


class TestFreshness(unittest.TestCase):
    def test_tools_see_an_edit_without_an_explicit_reindex(self):
        """SPEC §V.3 — every path in a response was stat-checked in that call."""
        parser.cache_clear()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "mod.py"
            source.write_text("def before():\n    pass\n")
            with Index(root) as index:
                index.refresh_all()
                first = tools.file_outline(index, "mod.py")
                self.assertEqual(
                    [s["name"] for s in first["symbols"]], ["before"]
                )
                source.write_text("def after():\n    pass\n")
                second = tools.file_outline(index, "mod.py")
                self.assertEqual([s["name"] for s in second["symbols"]], ["after"])


if __name__ == "__main__":
    unittest.main()
