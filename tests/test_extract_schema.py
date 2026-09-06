"""SPEC §T.14 — the `schema` profile: key paths, types, and §V.11 collapse."""

import unittest
from pathlib import Path

from ast_mcp.extract_schema import SCAN_LIMIT, extract_schema
from ast_mcp.parser import parse_file

FIXTURES = Path(__file__).parent / "fixtures" / "data"


def run(name: str, **kwargs):
    parsed, errors = parse_file(FIXTURES / name)
    assert parsed is not None, errors
    extraction = extract_schema(parsed, **kwargs)
    return parsed, extraction, {n.key_path: n for n in extraction.nodes}


class TestJson(unittest.TestCase):
    def setUp(self):
        self.parsed, self.ex, self.nodes = run("sample.json")

    def test_value_types_are_inferred(self):
        self.assertEqual(self.nodes["name"].kind, "string")
        self.assertEqual(self.nodes["port"].kind, "number")
        self.assertEqual(self.nodes["debug"].kind, "bool")
        self.assertEqual(self.nodes["missing"].kind, "null")
        self.assertEqual(self.nodes["tags"].kind, "array")
        self.assertEqual(self.nodes["server"].kind, "object")

    def test_no_fake_symbol_kinds(self):
        """SPEC §V.13 — schema kinds are value types, never definitions."""
        forbidden = {"function", "class", "method", "interface"}
        self.assertFalse({n.kind for n in self.ex.nodes} & forbidden)

    def test_nested_object_key_paths(self):
        self.assertEqual(self.nodes["server.host"].parent, "server")
        self.assertEqual(self.nodes["server"].children_count, 2)

    def test_array_of_objects_describes_element_zero_only(self):
        """SPEC §V.11 — two route objects, one described shape."""
        self.assertEqual(self.nodes["routes"].kind, "array")
        self.assertEqual(self.nodes["routes"].children_count, 2)
        self.assertIn("routes[].path", self.nodes)
        self.assertIn("routes[].method", self.nodes)
        self.assertNotIn("routes[1].path", self.nodes)

    def test_scalar_array_emits_no_element_node(self):
        self.assertEqual(self.nodes["tags"].children_count, 3)
        self.assertNotIn("tags[]", self.nodes)

    def test_byte_range_round_trips(self):
        """SPEC §V.2 — the recorded range slices back to the source."""
        node = self.nodes["server.host"]
        self.assertIn("localhost", self.parsed.slice(node.start_byte, node.end_byte))

    def test_depth_cap_marks_truncated_subtree(self):
        _parsed, _ex, nodes = run("sample.json", max_depth=1)
        self.assertTrue(nodes["server"].truncated_subtree)
        self.assertNotIn("server.host", nodes)


class TestUniformity(unittest.TestCase):
    """SPEC §V.11 — the collapse reports whether element 0 was representative."""

    def test_uniform_array_says_so(self):
        _p, _ex, nodes = run("sample.json")
        self.assertEqual(nodes["routes"].uniformity, "uniform")

    def test_differing_key_sets_are_mixed(self):
        _p, _ex, nodes = run("mixed.json")
        self.assertEqual(nodes["records"].uniformity, "mixed")

    def test_differing_scalar_kinds_are_mixed(self):
        _p, _ex, nodes = run("mixed.json")
        self.assertEqual(nodes["tokens"].uniformity, "mixed")

    def test_scalars_carry_no_uniformity(self):
        _p, _ex, nodes = run("sample.json")
        self.assertIsNone(nodes["name"].uniformity)
        self.assertIsNone(nodes["server"].uniformity)

    def test_oversized_collection_is_unverified_not_guessed(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "huge.json"
            body = ",".join('{"id": 1}' for _ in range(SCAN_LIMIT + 1))
            path.write_text('{"rows": [' + body + "]}")
            parsed, _errors = parse_file(path)
            assert parsed is not None
            nodes = {n.key_path: n for n in extract_schema(parsed).nodes}
            self.assertEqual(nodes["rows"].children_count, SCAN_LIMIT + 1)
            self.assertEqual(nodes["rows"].uniformity, "unverified")


class TestPreviewsStopAtCollections(unittest.TestCase):
    """SPEC §G — shape, never rows. One element's data is still data."""

    def test_top_level_scalars_keep_their_preview(self):
        _p, _ex, nodes = run("sample.json")
        self.assertEqual(nodes["port"].value_preview, "8080")

    def test_scalars_inside_a_collapsed_array_have_none(self):
        _p, _ex, nodes = run("sample.json")
        self.assertIsNone(nodes["routes[].path"].value_preview)
        self.assertIsNone(nodes["routes[].method"].value_preview)

    def test_repeated_toml_tables_suppress_previews(self):
        _p, _ex, nodes = run("sample.toml")
        self.assertIsNotNone(nodes["server.host"].value_preview)
        self.assertIsNone(nodes["items[].id"].value_preview)


class TestYaml(unittest.TestCase):
    def test_comments_attach_to_the_key_below_them(self):
        _p, _ex, nodes = run("sample.yaml")
        self.assertEqual(nodes["name"].comment, "# Service configuration.")
        self.assertEqual(nodes["database"].comment, "# Database settings.")
        self.assertIsNone(nodes["port"].comment)

    def test_block_structures(self):
        _p, _ex, nodes = run("sample.yaml")
        self.assertEqual(nodes["tags"].kind, "array")
        self.assertEqual(nodes["database"].kind, "object")
        self.assertEqual(nodes["database.pool"].kind, "number")


class TestToml(unittest.TestCase):
    def test_tables_and_array_of_tables(self):
        _p, _ex, nodes = run("sample.toml")
        self.assertEqual(nodes["title"].comment, "# Project title.")
        self.assertEqual(nodes["server"].kind, "table")
        self.assertEqual(nodes["server.port"].kind, "number")

    def test_repeated_table_arrays_collapse_to_one_node(self):
        """SPEC §V.11 — two [[items]] blocks, one node, count of two."""
        _p, _ex, nodes = run("sample.toml")
        self.assertEqual(nodes["items[]"].children_count, 2)
        self.assertEqual(nodes["items[].id"].kind, "number")


class TestXml(unittest.TestCase):
    def test_paths_use_slashes_and_at_signs(self):
        _p, _ex, nodes = run("sample.xml")
        self.assertIn("catalog", nodes)
        self.assertIn("catalog/book", nodes)
        self.assertIn("catalog/book@isbn", nodes)
        self.assertEqual(nodes["catalog/book@isbn"].kind, "attr")

    def test_repeated_elements_collapse(self):
        """SPEC §V.11 — two <book> elements, one node, count of two."""
        _p, _ex, nodes = run("sample.xml")
        self.assertEqual(nodes["catalog/book"].children_count, 2)

    def test_attribute_preview_is_the_value(self):
        _p, _ex, nodes = run("sample.xml")
        self.assertIn("root", nodes["catalog@id"].value_preview)
        self.assertNotIn("id=", nodes["catalog@id"].value_preview)


class TestCsv(unittest.TestCase):
    def test_columns_not_rows(self):
        _p, _ex, nodes = run("small.csv")
        self.assertEqual(set(nodes), {"id", "name", "active", "score"})

    def test_column_types_inferred_from_data_rows(self):
        _p, _ex, nodes = run("small.csv")
        self.assertEqual(nodes["id"].kind, "number")
        self.assertEqual(nodes["name"].kind, "string")
        self.assertEqual(nodes["active"].kind, "bool")
        self.assertEqual(nodes["score"].kind, "number")

    def test_cost_is_flat_in_row_count(self):
        """SPEC §V.11 — the whole point. 5000 rows must cost what 3 rows cost."""
        _p, small, small_nodes = run("small.csv")
        _p2, big, big_nodes = run("big.csv")
        self.assertEqual(len(small.nodes), len(big.nodes))
        self.assertEqual(set(small_nodes), set(big_nodes))
        self.assertEqual(small_nodes["id"].children_count, 3)
        self.assertEqual(big_nodes["id"].children_count, 5000)
        self.assertEqual(
            {n.kind for n in small.nodes}, {n.kind for n in big.nodes}
        )


class TestDegradation(unittest.TestCase):
    def test_broken_json_still_returns_a_payload(self):
        """SPEC §V.5 — a malformed data file degrades, never raises."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text('{"a": 1, "b": ')
            parsed, errors = parse_file(path)
            self.assertIsNotNone(parsed)
            self.assertEqual([e.code for e in errors], ["syntax_error"])
            extraction = extract_schema(parsed)
            self.assertIn("a", {n.key_path for n in extraction.nodes})


if __name__ == "__main__":
    unittest.main()
