"""SPEC §T.8 — the server exposes exactly the six tools from §I.tools."""

import asyncio
import unittest

from ast_mcp.main import mcp

EXPECTED = {
    "file_outline",
    "get_symbol",
    "search_symbols",
    "get_docstrings",
    "list_imports",
    "ast_query",
}


class TestServer(unittest.TestCase):
    def setUp(self):
        self.listed = asyncio.run(mcp.list_tools())

    def test_exactly_six_tools(self):
        self.assertEqual({t.name for t in self.listed}, EXPECTED)
        self.assertEqual(len(self.listed), 6)

    def test_every_tool_is_described(self):
        for tool in self.listed:
            with self.subTest(tool=tool.name):
                self.assertTrue(tool.description)
                self.assertIn("properties", tool.input_schema)

    def test_every_tool_takes_a_token_budget(self):
        """SPEC §V.1 — the budget is part of the contract, not an extra."""
        for tool in self.listed:
            with self.subTest(tool=tool.name):
                self.assertIn("max_tokens", tool.input_schema["properties"])

    def test_required_arguments_match_the_spec(self):
        required = {t.name: set(t.input_schema.get("required", [])) for t in self.listed}
        self.assertEqual(required["file_outline"], {"path"})
        self.assertEqual(required["get_symbol"], {"name"})
        self.assertEqual(required["search_symbols"], {"query"})
        self.assertEqual(required["list_imports"], {"path"})
        self.assertEqual(required["ast_query"], {"path", "query"})
        self.assertEqual(required["get_docstrings"], set())

    def test_search_symbols_exposes_the_group_filter(self):
        search = next(t for t in self.listed if t.name == "search_symbols")
        self.assertIn("group", search.input_schema["properties"])

    def test_instructions_tell_the_agent_about_profiles(self):
        self.assertIn("profile", mcp._lowlevel_server.instructions)


if __name__ == "__main__":
    unittest.main()
