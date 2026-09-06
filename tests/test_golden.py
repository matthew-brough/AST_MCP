"""SPEC §T.9 — golden outlines and the §G token-economics claim.

These are the tests that fail loudly if extraction quietly changes shape.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from ast_mcp import parser, tools
from ast_mcp.index import Index
from ast_mcp.render import estimate_tokens

FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).resolve().parents[1]

#: path -> ordered (kind, qualified_name) pairs. Update deliberately, never
#: to make a failing test pass.
GOLDEN = {
    "src/core/sample.py": [
        ("function", "top_level"),
        ("class", "Widget"),
        ("method", "Widget.render"),
        ("method", "Widget._hidden"),
        ("function", "undocumented"),
    ],
    "src/core/sample.go": [
        ("struct", "Widget"),
        ("interface", "Renderer"),
        ("const", "Version"),
        ("method", "Render"),
        ("function", "Build"),
    ],
    "src/core/sample.ts": [
        ("interface", "Named"),
        ("type", "Id"),
        ("function", "build"),
        ("class", "Service"),
        ("method", "Service.run"),
    ],
    "src/core/sample.lua": [
        ("function", "M.add"),
        ("method", "M:render"),
        ("function", "helper"),
        ("handler", "sample:ping"),
        ("handler", "sample:ping"),
        ("handler", "getWidget"),
    ],
    "src/data/schema.proto": [
        ("message", "User"),
        ("enum", "Role"),
        ("service", "UserService"),
        ("rpc", "UserService.GetUser"),
    ],
}


def flatten(nodes, key="qualified_name"):
    out = []
    for node in nodes:
        out.append((node["kind"], node[key]))
        out.extend(flatten(node.get("children", []), key))
    return out


class GoldenTestBase(unittest.TestCase):
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


class TestGoldenOutlines(GoldenTestBase):
    def test_outlines_match_the_golden_records(self):
        for path, expected in GOLDEN.items():
            with self.subTest(path=path):
                result = tools.file_outline(self.index, path, max_depth=8)
                self.assertEqual(flatten(result["symbols"]), expected)

    def test_schema_outline_is_stable(self):
        result = tools.file_outline(self.index, "src/data/sample.toml", max_depth=8)
        self.assertEqual(
            flatten(result["schema"], key="key_path"),
            [
                ("string", "title"),
                ("table", "server"),
                ("string", "server.host"),
                ("number", "server.port"),
                ("array", "items[]"),
                ("number", "items[].id"),
            ],
        )

    def test_document_outline_is_stable(self):
        result = tools.file_outline(self.index, "src/docs/sample.md", max_depth=8)
        self.assertEqual(
            flatten(result["outline"], key="slug"),
            [
                ("heading", "project-title"),
                ("heading", "alpha"),
                ("heading", "deep-section"),
                ("code_block", "code-python"),
                ("table", "table"),
                ("heading", "alpha-1"),
                ("heading", "second-top"),
                ("heading", "skipped-level"),
            ],
        )


class TestTokenEconomics(unittest.TestCase):
    """SPEC §G — the reason the server exists. Outlines must be far cheaper."""

    @classmethod
    def setUpClass(cls):
        parser.cache_clear()
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        shutil.copytree(REPO / "ast_mcp", cls.root / "ast_mcp")
        cls.index = Index(cls.root)
        cls.index.refresh_all()

    @classmethod
    def tearDownClass(cls):
        cls.index.close()
        cls._tmp.cleanup()

    def test_outline_is_much_cheaper_than_reading_the_file(self):
        """SPEC §B.2 — the measured floor is 4x, not the §G "N/10" figure."""
        target = "ast_mcp/index.py"
        whole_file = estimate_tokens((self.root / target).read_text())
        outline = estimate_tokens(json.dumps(tools.file_outline(self.index, target)))
        self.assertLess(
            outline * 4, whole_file,
            f"outline {outline} tokens vs full read {whole_file} — not worth it",
        )

    def test_one_symbol_costs_about_the_symbol(self):
        target = "ast_mcp/render.py"
        whole_file = estimate_tokens((self.root / target).read_text())
        result = tools.get_symbol(self.index, "estimate_tokens", path=target)
        self.assertTrue(result["found"])
        self.assertLess(estimate_tokens(json.dumps(result)) * 3, whole_file)

    def test_server_can_index_its_own_source_without_errors(self):
        errors = self.index.refresh_all()
        self.assertEqual([e.as_dict() for e in errors], [])
        indexed = self.index.conn.execute("SELECT count(*) FROM files").fetchone()[0]
        self.assertGreaterEqual(indexed, 9)


if __name__ == "__main__":
    unittest.main()
