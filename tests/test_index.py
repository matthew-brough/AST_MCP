"""SPEC §T.6 — staleness (§V.3), write scope (§V.4), git-free walk (§V.10)."""

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ast_mcp import parser
from ast_mcp.index import DB_DIRNAME, SCHEMA_VERSION, Index, query_fingerprint

FIXTURES = Path(__file__).parent / "fixtures"


class IndexTestBase(unittest.TestCase):
    def setUp(self):
        parser.cache_clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def build(self, *, copy_fixtures: bool = True) -> Index:
        if copy_fixtures:
            shutil.copytree(FIXTURES, self.root / "src")
        index = Index(self.root)
        self.addCleanup(index.close)
        return index

    def count(self, index: Index, table: str) -> int:
        return index.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


class TestIndexing(unittest.TestCase):
    """One shared index over the fixture tree — building it is the expensive bit."""

    @classmethod
    def setUpClass(cls):
        parser.cache_clear()
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        shutil.copytree(FIXTURES, cls.root / "src")
        cls.index = Index(cls.root)
        cls.errors = cls.index.refresh_all()

    @classmethod
    def tearDownClass(cls):
        cls.index.close()
        cls._tmp.cleanup()

    def count(self, table: str) -> int:
        return self.index.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def test_every_profile_lands_in_its_own_table(self):
        self.assertGreater(self.count("symbols"), 0)
        self.assertGreater(self.count("schema_nodes"), 0)
        self.assertGreater(self.count("doc_nodes"), 0)
        self.assertGreater(self.count("imports"), 0)

    def test_indexing_the_fixture_tree_is_error_free(self):
        self.assertEqual([e.code for e in self.errors], [])

    def test_search_spans_profiles_and_tags_each_hit(self):
        rows, total = self.index.search("alpha")
        self.assertGreater(total, 0)
        self.assertTrue(all(r["profile"] for r in rows))

    def test_group_filter(self):
        rows, _total = self.index.search("port", group="data")
        self.assertTrue(rows)
        self.assertTrue(all(r["grp"] == "data" for r in rows))

    def test_kind_filter(self):
        rows, _total = self.index.search("render", kind="method")
        self.assertTrue(rows)
        self.assertTrue(all(r["kind"] == "method" for r in rows))

    def test_exact_name_outranks_substring(self):
        rows, _total = self.index.search("build")
        self.assertEqual(rows[0]["name"].lower(), "build")

    def test_path_glob_filter(self):
        rows, _total = self.index.search("render", path_glob="*/core/*")
        self.assertTrue(rows)
        self.assertTrue(all("/core/" in r["path"] for r in rows))

    def test_limit_is_applied_but_total_is_not_truncated(self):
        rows, total = self.index.search("a", limit=3)
        self.assertLessEqual(len(rows), 3)
        self.assertGreaterEqual(total, len(rows))


class TestStaleness(IndexTestBase):
    def test_edit_is_visible_on_the_next_query(self):
        """SPEC §V.3 — the index never answers for a file it has not revalidated."""
        source = self.root / "mod.py"
        source.write_text("def before():\n    pass\n")
        index = self.build(copy_fixtures=False)
        index.refresh_all()
        self.assertEqual(
            {r["name"] for r in index.conn.execute("SELECT name FROM symbols")},
            {"before"},
        )

        source.write_text("def after():\n    pass\n")
        index.refresh_file(source)
        self.assertEqual(
            {r["name"] for r in index.conn.execute("SELECT name FROM symbols")},
            {"after"},
        )

    def test_unchanged_file_is_not_rewritten(self):
        source = self.root / "mod.py"
        source.write_text("def stable():\n    pass\n")
        index = self.build(copy_fixtures=False)
        index.refresh_all()
        first = index.conn.execute("SELECT id FROM files").fetchone()["id"]
        index.refresh_file(source)
        second = index.conn.execute("SELECT id FROM files").fetchone()["id"]
        self.assertEqual(first, second, "a stable stat key must not reinsert the row")

    def test_deleted_file_drops_out_with_its_rows(self):
        source = self.root / "gone.py"
        source.write_text("def doomed():\n    pass\n")
        index = self.build(copy_fixtures=False)
        index.refresh_all()
        self.assertEqual(self.count(index, "symbols"), 1)
        source.unlink()
        index.refresh_all()
        self.assertEqual(self.count(index, "files"), 0)
        self.assertEqual(self.count(index, "symbols"), 0, "cascade must clear rows")


class TestWalk(IndexTestBase):
    def test_ignored_directories_are_skipped(self):
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "dep.js").write_text("function x() {}\n")
        (self.root / "keep.js").write_text("function y() {}\n")
        index = self.build(copy_fixtures=False)
        walked = {index.relative(p) for p in index.walk()}
        self.assertIn("keep.js", walked)
        self.assertNotIn("node_modules/dep.js", walked)

    def test_gitignore_is_honoured_without_running_git(self):
        """SPEC §V.10 — patterns are read from the file, never from a subprocess."""
        (self.root / ".gitignore").write_text("secret.py\ngenerated/\n")
        (self.root / "secret.py").write_text("x = 1\n")
        (self.root / "generated").mkdir()
        (self.root / "generated" / "out.py").write_text("y = 2\n")
        (self.root / "kept.py").write_text("z = 3\n")
        index = self.build(copy_fixtures=False)
        walked = {index.relative(p) for p in index.walk()}
        self.assertEqual(walked, {"kept.py"})

    def test_unregistered_extensions_are_skipped(self):
        (self.root / "notes.xyz").write_text("hello\n")
        (self.root / "a.py").write_text("x = 1\n")
        index = self.build(copy_fixtures=False)
        self.assertEqual({index.relative(p) for p in index.walk()}, {"a.py"})


class TestWriteScope(IndexTestBase):
    def test_only_the_database_is_written(self):
        """SPEC §V.4 — indexing must not touch a single source byte."""
        source = self.root / "mod.py"
        source.write_text("def f():\n    pass\n")
        before = source.read_bytes()
        before_stat = source.stat().st_mtime_ns
        index = self.build(copy_fixtures=False)
        index.refresh_all()
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(source.stat().st_mtime_ns, before_stat)

    def test_database_lives_under_the_dot_directory(self):
        index = self.build(copy_fixtures=False)
        self.assertTrue(index.db_path.exists())
        self.assertEqual(index.db_path.parent.name, DB_DIRNAME)


class TestSchemaVersioning(IndexTestBase):
    def test_version_mismatch_rebuilds_rather_than_migrates(self):
        index = self.build(copy_fixtures=False)
        (self.root / "a.py").write_text("def f():\n    pass\n")
        index.refresh_all()
        self.assertEqual(self.count(index, "symbols"), 1)
        db_path = index.db_path
        index.close()

        connection = sqlite3.connect(db_path)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 99}")
        connection.commit()
        connection.close()

        rebuilt = Index(self.root)
        self.addCleanup(rebuilt.close)
        self.assertEqual(self.count(rebuilt, "symbols"), 0)
        self.assertEqual(
            rebuilt.conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION
        )

    def test_edited_query_files_rebuild_the_index(self):
        """A cached symbol set outlives its rules unless the queries are versioned."""
        index = self.build(copy_fixtures=False)
        (self.root / "a.py").write_text("def f():\n    pass\n")
        index.refresh_all()
        self.assertEqual(self.count(index, "symbols"), 1)
        db_path = index.db_path
        index.close()

        connection = sqlite3.connect(db_path)
        connection.execute("PRAGMA application_id = 1234")
        connection.commit()
        connection.close()

        rebuilt = Index(self.root)
        self.addCleanup(rebuilt.close)
        self.assertEqual(self.count(rebuilt, "symbols"), 0)
        self.assertEqual(
            rebuilt.conn.execute("PRAGMA application_id").fetchone()[0],
            query_fingerprint(),
        )


if __name__ == "__main__":
    unittest.main()
