"""SPEC §T.2 / §T.4 — registry resolution, grammar loading, query compilation."""

import unittest

from tree_sitter import Parser, Query, QueryCursor

from ast_mcp.languages import (
    BY_EXT,
    GROUPS,
    LANGS,
    PROFILES,
    load_language,
    spec_for_path,
)

CORE_SAMPLES = {
    "python": b'import os\nfrom a.b import c as d\n\ndef f(x: int) -> str:\n    """doc"""\n    return ""\n\nclass C:\n    def m(self): pass\n',
    "javascript": b'import x from "m";\nconst {a} = require("n");\nexport function f(a, b) {}\nexport const g = (a) => a;\nexport default class C { m() {} }\n',
    "typescript": b'import type {T} from "m";\nexport interface I { a: number }\ntype A = string;\nexport function f(a: number): string { return ""; }\nexport abstract class C { m(): void {} }\n',
    "tsx": b'import React from "react";\nexport const App = () => <div/>;\nexport interface P { a: number }\n',
    "go": b'package main\nimport ("fmt")\ntype S struct { A int }\nfunc F(a int) error { return nil }\nfunc (s *S) M() {}\n',
    "lua": b"local M = {}\nfunction M.f(a) end\nfunction M:g() end\nlocal function h() end\nreturn M\n",
}


class TestRegistry(unittest.TestCase):
    def test_rows_are_well_formed(self):
        for spec in LANGS:
            with self.subTest(lang=spec.lang):
                self.assertIn(spec.group, GROUPS)
                self.assertIn(spec.profile, PROFILES)
                self.assertTrue(spec.exts or spec.filenames)

    def test_no_duplicate_extensions(self):
        seen: dict[str, str] = {}
        for spec in LANGS:
            for ext in spec.exts:
                with self.subTest(ext=ext):
                    self.assertNotIn(ext, seen, f"{ext} also claimed by {seen.get(ext)}")
                    seen[ext] = spec.lang
        self.assertEqual(len(seen), len(BY_EXT))

    def test_spec_for_path_by_extension(self):
        cases = [
            ("a/b/mod.py", "python"),
            ("stub.pyi", "python"),
            ("app.mjs", "javascript"),
            ("app.tsx", "tsx"),
            ("app.ts", "typescript"),
            ("main.go", "go"),
            ("init.lua", "lua"),
        ]
        for path, lang in cases:
            with self.subTest(path=path):
                spec = spec_for_path(path)
                self.assertIsNotNone(spec)
                self.assertEqual(spec.lang, lang)

    def test_unsupported_path_is_none_not_raise(self):
        """SPEC §V.5 — a miss is None, never an exception."""
        self.assertIsNone(spec_for_path("notes.xyz"))
        self.assertIsNone(spec_for_path("noextension"))

    def test_unknown_language_loads_as_none(self):
        self.assertIsNone(load_language("klingon"))


class TestGrammars(unittest.TestCase):
    def test_every_registered_grammar_loads(self):
        for spec in LANGS:
            with self.subTest(lang=spec.lang):
                lang = load_language(spec.lang)
                self.assertIsNotNone(lang, f"{spec.lang} from {spec.source}")
                self.assertGreaterEqual(lang.abi_version, 14)


class TestQueries(unittest.TestCase):
    """SPEC §T.4 / §V.8 — every query file exists and compiles."""

    def test_query_file_exists_and_compiles(self):
        for spec in LANGS:
            qp = spec.query_path
            if qp is None:
                continue
            with self.subTest(lang=spec.lang):
                self.assertTrue(qp.exists(), f"missing {qp}")
                lang = load_language(spec.lang)
                Query(lang, qp.read_text())

    def test_core_queries_capture_definitions(self):
        for spec in LANGS:
            if spec.profile != "symbols":
                continue
            with self.subTest(lang=spec.lang):
                lang = load_language(spec.lang)
                query = Query(lang, spec.query_path.read_text())
                tree = Parser(lang).parse(CORE_SAMPLES[spec.lang])
                self.assertFalse(tree.root_node.has_error, "fixture must parse clean")
                kinds = set()
                for _index, caps in QueryCursor(query).matches(tree.root_node):
                    kinds.update(k for k in caps if k.startswith("def."))
                self.assertTrue(kinds, f"{spec.lang} captured no definitions")


if __name__ == "__main__":
    unittest.main()
