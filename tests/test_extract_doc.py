"""SPEC §T.15 — the `outline` profile: heading trees, blocks, slugs, links."""

import unittest
from pathlib import Path

from ast_mcp.extract_doc import extract_doc
from ast_mcp.parser import parse_file

FIXTURES = Path(__file__).parent / "fixtures"


def run(relative: str):
    parsed, errors = parse_file(FIXTURES / relative)
    assert parsed is not None, errors
    extraction = extract_doc(parsed)
    return parsed, extraction, {n.slug: n for n in extraction.nodes}


class TestMarkdown(unittest.TestCase):
    def setUp(self):
        self.parsed, self.ex, self.nodes = run("docs/sample.md")

    def test_heading_levels_and_nesting(self):
        self.assertEqual(self.nodes["project-title"].level, 1)
        self.assertIsNone(self.nodes["project-title"].parent)
        self.assertEqual(self.nodes["alpha"].level, 2)
        self.assertEqual(self.nodes["alpha"].parent, "project-title")
        self.assertEqual(self.nodes["deep-section"].parent, "alpha")

    def test_skipped_level_still_nests_under_its_ancestor(self):
        """SPEC §T.15 — h1 followed directly by h3."""
        skipped = self.nodes["skipped-level"]
        self.assertEqual(skipped.level, 3)
        self.assertEqual(skipped.parent, "second-top")

    def test_duplicate_titles_get_a_numeric_suffix(self):
        self.assertIn("alpha", self.nodes)
        self.assertIn("alpha-1", self.nodes)
        self.assertEqual(self.nodes["alpha"].title, self.nodes["alpha-1"].title)

    def test_fenced_code_block_carries_its_info_string(self):
        block = next(n for n in self.ex.nodes if n.kind == "code_block")
        self.assertEqual(block.info, "python")
        self.assertEqual(block.parent, "deep-section")

    def test_table_is_captured(self):
        self.assertTrue(any(n.kind == "table" for n in self.ex.nodes))

    def test_no_fake_symbol_kinds(self):
        """SPEC §V.13 — outline kinds are document structures."""
        self.assertTrue(
            {n.kind for n in self.ex.nodes} <= {"heading", "code_block", "table"}
        )

    def test_link_and_image_targets(self):
        targets = {(link.kind, link.module) for link in self.ex.links}
        self.assertIn(("link", "https://example.com/a"), targets)
        self.assertIn(("image", "img/b.png"), targets)

    def test_lede_is_the_paragraph_under_the_heading(self):
        self.assertEqual(
            self.ex.ledes["project-title"], "The lede paragraph for the title."
        )

    def test_byte_range_round_trips(self):
        """SPEC §V.2 — the range slices back to the heading source."""
        node = self.nodes["deep-section"]
        self.assertIn(
            "Deep Section", self.parsed.slice(node.start_byte, node.end_byte)
        )


class TestHtml(unittest.TestCase):
    def setUp(self):
        self.parsed, self.ex, self.nodes = run("web/sample.html")

    def test_headings_landmarks_and_ids(self):
        kinds = {n.kind for n in self.ex.nodes}
        self.assertEqual(kinds, {"heading", "landmark"})
        self.assertEqual(self.nodes["page-title"].level, 1)
        self.assertEqual(self.nodes["alpha"].level, 2)

    def test_landmark_prefers_aria_label_then_id(self):
        self.assertEqual(self.nodes["nav-primary"].title, "Primary")
        self.assertEqual(self.nodes["main-root"].title, "root")

    def test_nesting_follows_the_landmark_tree(self):
        self.assertEqual(self.nodes["page-title"].parent, "main-root")
        self.assertEqual(self.nodes["alpha"].parent, "section-alpha")

    def test_asset_links(self):
        targets = {(link.kind, link.module) for link in self.ex.links}
        self.assertIn(("stylesheet", "styles/app.css"), targets)
        self.assertIn(("script", "app.js"), targets)
        self.assertIn(("image", "pic.png"), targets)

    def test_heading_lede_is_not_the_heading_text(self):
        self.assertNotEqual(self.ex.ledes.get("page-title"), "Page Title")


class TestDegradation(unittest.TestCase):
    def test_wrong_profile_is_an_error_not_a_raise(self):
        """SPEC §V.5 — asking for an outline of Python degrades cleanly."""
        parsed, _ = parse_file(FIXTURES / "core" / "sample.py")
        extraction = extract_doc(parsed)
        self.assertEqual([e.code for e in extraction.errors], ["unsupported_lang"])
        self.assertEqual(extraction.nodes, [])


if __name__ == "__main__":
    unittest.main()
