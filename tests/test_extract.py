"""SPEC §T.5 — symbols, docstrings and imports for the core fixtures."""

import unittest
from pathlib import Path

from ast_mcp.extract import extract, preceding_comment_block, python_docstring
from ast_mcp.parser import parse_file

FIXTURES = Path(__file__).parent / "fixtures" / "core"


def run(name: str):
    parsed, errors = parse_file(FIXTURES / name)
    assert parsed is not None, errors
    return parsed, extract(parsed)


def by_qname(extraction):
    return {s.qualified_name: s for s in extraction.symbols}


class TestPython(unittest.TestCase):
    def setUp(self):
        self.parsed, self.ex = run("sample.py")
        self.symbols = by_qname(self.ex)

    def test_symbol_set(self):
        self.assertEqual(
            set(self.symbols),
            {"top_level", "Widget", "Widget.render", "Widget._hidden", "undocumented"},
        )

    def test_nested_function_becomes_method(self):
        self.assertEqual(self.symbols["Widget.render"].kind, "method")
        self.assertEqual(self.symbols["Widget.render"].parent, "Widget")
        self.assertEqual(self.symbols["top_level"].kind, "function")
        self.assertIsNone(self.symbols["top_level"].parent)

    def test_docstring_is_verbatim(self):
        """SPEC §V.7 — exact source text, quotes included."""
        self.assertEqual(
            self.symbols["top_level"].docstring, '"""Top-level function doc."""'
        )

    def test_absent_docstring_is_none_not_empty(self):
        """SPEC §V.7 — absent means null."""
        self.assertIsNone(self.symbols["undocumented"].docstring)

    def test_signature_excludes_body(self):
        sig = self.symbols["top_level"].signature
        self.assertEqual(sig, 'def top_level(a: int, b: str = "x") -> bool:')
        self.assertNotIn("return", sig)

    def test_byte_range_round_trips(self):
        """SPEC §V.2 — the recorded range slices back to the definition."""
        sym = self.symbols["Widget.render"]
        text = self.parsed.slice(sym.start_byte, sym.end_byte)
        self.assertTrue(text.startswith("def render"))
        self.assertIn("return", text)

    def test_imports(self):
        found = {i.module: i for i in self.ex.imports}
        self.assertEqual(set(found), {"os", "pathlib"})
        self.assertIsNone(found["os"].alias)
        self.assertEqual(found["pathlib"].kind, "from_import")
        self.assertEqual(found["pathlib"].alias, "P")
        self.assertIn("Path", found["pathlib"].names)


class TestGo(unittest.TestCase):
    def setUp(self):
        self.parsed, self.ex = run("sample.go")
        self.symbols = by_qname(self.ex)

    def test_specific_kind_wins_over_general(self):
        """Go's type_spec matches both `type` and `struct`; struct is kept."""
        self.assertEqual(self.symbols["Widget"].kind, "struct")
        self.assertEqual(self.symbols["Renderer"].kind, "interface")

    def test_line_comment_block_is_the_docstring(self):
        self.assertEqual(self.symbols["Build"].docstring, "// Build makes a widget.")

    def test_signature_carries_wrapper_keyword(self):
        self.assertEqual(self.symbols["Version"].signature, 'const Version = "1.0"')
        self.assertTrue(self.symbols["Widget"].signature.startswith("type Widget struct"))

    def test_import_alias(self):
        found = {i.module: i for i in self.ex.imports}
        self.assertEqual(found["strings"].alias, "alias")
        self.assertIsNone(found["fmt"].alias)


class TestJavaScriptFamily(unittest.TestCase):
    def test_exports_include_wrapped_declarations(self):
        _, ex = run("sample.js")
        self.assertEqual({e.name for e in ex.exports}, {"add", "scale", "Box"})

    def test_require_call_is_an_import_without_the_callee(self):
        _, ex = run("sample.js")
        found = {i.module: i for i in ex.imports}
        self.assertEqual(found["lodash"].kind, "require")
        self.assertNotIn("require", found["lodash"].names)

    def test_jsdoc_block(self):
        _, ex = run("sample.js")
        self.assertEqual(
            by_qname(ex)["add"].docstring, "/**\n * Adds two numbers.\n */"
        )

    def test_typescript_kinds(self):
        _, ex = run("sample.ts")
        kinds = {s.qualified_name: s.kind for s in ex.symbols}
        self.assertEqual(kinds["Named"], "interface")
        self.assertEqual(kinds["Id"], "type")
        self.assertEqual(kinds["Service.run"], "method")

    def test_tsx_arrow_component(self):
        _, ex = run("sample.tsx")
        symbols = by_qname(ex)
        self.assertEqual(symbols["App"].kind, "function")
        self.assertEqual(symbols["App"].docstring, "/** The app root. */")


class TestLua(unittest.TestCase):
    def test_dot_and_colon_definitions(self):
        _, ex = run("sample.lua")
        symbols = by_qname(ex)
        self.assertEqual(symbols["M.add"].kind, "function")
        self.assertEqual(symbols["M:render"].kind, "method")
        self.assertIn("helper", symbols)

    def test_luals_comment_block(self):
        _, ex = run("sample.lua")
        doc = by_qname(ex)["M.add"].docstring
        self.assertTrue(doc.startswith("--- Adds two numbers."))
        self.assertIn("@param", doc)


class TestDocstringHelpers(unittest.TestCase):
    def test_blank_line_breaks_the_association(self):
        """SPEC §I.docstring — one blank line detaches the comment block."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.go"
            path.write_text("package m\n\n// detached\n\nfunc F() {}\n")
            _, ex = parse_file(path), None
            parsed, _errors = parse_file(path)
            ex = extract(parsed)
            self.assertIsNone(by_qname(ex)["F"].docstring)

    def test_python_docstring_returns_none_for_non_string_body(self):
        parsed, _ = parse_file(FIXTURES / "sample.py")
        node = parsed.tree.root_node
        self.assertIsNone(python_docstring(None, parsed.source))
        self.assertIsNone(preceding_comment_block(node, parsed.source))


if __name__ == "__main__":
    unittest.main()
