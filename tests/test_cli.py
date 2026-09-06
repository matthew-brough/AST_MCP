"""SPEC §T.16 — the `ast-mcp` CLI: argv routing, .mcp.json merge, index, status."""

import io
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ast_mcp import __version__, claudemd, parser
from ast_mcp.cli import MCP_SERVER_KEY, build_parser, main, normalise, server_invocation
from ast_mcp.index import DB_DIRNAME, DB_FILENAME

FIXTURES = Path(__file__).parent / "fixtures"


class CLITestBase(unittest.TestCase):
    def setUp(self):
        parser.cache_clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        shutil.copytree(FIXTURES / "core", self.root / "src")

    def run_cli(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([*argv])
        return code, out.getvalue(), err.getvalue()

    def mcp_json(self) -> dict:
        return json.loads((self.root / ".mcp.json").read_text(encoding="utf-8"))


class TestArgvRouting(unittest.TestCase):
    """No subcommand, or flags only, must still mean `serve` (SPEC §I.cli)."""

    def test_empty_argv_is_serve(self):
        self.assertEqual(normalise([]), ["serve"])

    def test_bare_flag_is_serve(self):
        self.assertEqual(normalise(["--root", "/x"]), ["serve", "--root", "/x"])

    def test_subcommand_passes_through(self):
        self.assertEqual(normalise(["index", "--rebuild"]), ["index", "--rebuild"])

    def test_help_and_version_are_not_rewritten(self):
        for flag in ("-h", "--help", "-V", "--version"):
            self.assertEqual(normalise([flag]), [flag])

    def test_every_subcommand_parses(self):
        cli = build_parser()
        for name in ("serve", "init", "index", "status", "savings", "languages"):
            self.assertTrue(callable(cli.parse_args([name]).func), name)

    def test_version_flag_prints_package_version(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            main(["--version"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn(__version__, out.getvalue())


class TestServerInvocation(unittest.TestCase):
    def test_prefers_console_script_when_on_path(self):
        with mock.patch("ast_mcp.cli.shutil.which", return_value="/usr/bin/ast-mcp"):
            self.assertEqual(server_invocation(), ("ast-mcp", ["serve"]))

    def test_falls_back_to_uvx(self):
        with mock.patch("ast_mcp.cli.shutil.which", return_value=None):
            self.assertEqual(server_invocation(), ("uvx", ["ast-mcp", "serve"]))

    def test_ephemeral_uvx_script_is_not_recorded(self):
        """`uvx ast-mcp init` must not register a launcher that dies with it."""
        cached = str(Path.home() / ".cache/uv/archive-v0/abc123/bin/ast-mcp")
        with mock.patch("ast_mcp.cli.shutil.which", return_value=cached):
            self.assertEqual(server_invocation(), ("uvx", ["ast-mcp", "serve"]))

    def test_respects_uv_cache_dir_override(self):
        with tempfile.TemporaryDirectory() as cache:
            script = Path(cache) / "somewhere" / "bin" / "ast-mcp"
            script.parent.mkdir(parents=True)
            script.touch()
            with mock.patch.dict("os.environ", {"UV_CACHE_DIR": cache}), \
                 mock.patch("ast_mcp.cli.shutil.which", return_value=str(script)):
                self.assertEqual(server_invocation(), ("uvx", ["ast-mcp", "serve"]))

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "needs symlinks")
    def test_cache_root_is_matched_through_a_symlink(self):
        """macOS reaches temp dirs through /var -> /private/var."""
        with tempfile.TemporaryDirectory() as real:
            root = Path(real)
            script = root / "cache" / "env" / "bin" / "ast-mcp"
            script.parent.mkdir(parents=True)
            script.touch()
            link = root / "link"
            try:
                link.symlink_to(root / "cache")
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            with mock.patch.dict("os.environ", {"UV_CACHE_DIR": str(link)}), \
                 mock.patch("ast_mcp.cli.shutil.which", return_value=str(script)):
                self.assertEqual(server_invocation(), ("uvx", ["ast-mcp", "serve"]))

    def test_persistent_tool_install_is_recorded(self):
        with tempfile.TemporaryDirectory() as bindir:
            script = Path(bindir) / "ast-mcp"
            script.touch()
            with mock.patch("ast_mcp.cli.shutil.which", return_value=str(script)):
                self.assertEqual(server_invocation(), ("ast-mcp", ["serve"]))

    def test_override_is_split(self):
        self.assertEqual(
            server_invocation("uv run --directory /r ast-mcp serve"),
            ("uv", ["run", "--directory", "/r", "ast-mcp", "serve"]),
        )

    def test_empty_override_rejected(self):
        with self.assertRaises(ValueError):
            server_invocation("   ")

    def test_with_cce_is_recorded_in_the_stanza(self):
        """The opt-in has to survive into the file Claude Code launches from."""
        with mock.patch("ast_mcp.cli.shutil.which", return_value=None):
            self.assertEqual(
                server_invocation(with_cce=True),
                ("uvx", ["ast-mcp", "serve", "--with-cce"]),
            )

    def test_with_cce_appends_to_an_override(self):
        self.assertEqual(
            server_invocation("uv run ast-mcp serve", with_cce=True),
            ("uv", ["run", "ast-mcp", "serve", "--with-cce"]),
        )

    def test_no_absolute_paths_in_default_stanza(self):
        """A committed .mcp.json has to work on someone else's machine."""
        command, args = server_invocation()
        self.assertFalse(Path(command).is_absolute())
        self.assertFalse(any(arg.startswith("/") for arg in args))


class TestInit(CLITestBase):
    def test_creates_mcp_json_and_indexes(self):
        code, out, _ = self.run_cli("init", "--root", str(self.root))
        self.assertEqual(code, 0)
        stanza = self.mcp_json()["mcpServers"][MCP_SERVER_KEY]
        self.assertIn("serve", stanza["args"])
        self.assertTrue((self.root / DB_DIRNAME / DB_FILENAME).exists())
        self.assertIn("registered", out)

    def test_preserves_other_servers(self):
        (self.root / ".mcp.json").write_text(json.dumps({
            "mcpServers": {"context-engine": {"command": "cce", "args": ["serve"]}},
            "otherKey": {"keep": True},
        }), encoding="utf-8")
        code, out, _ = self.run_cli("init", "--root", str(self.root), "--no-index")
        self.assertEqual(code, 0)
        data = self.mcp_json()
        self.assertEqual(data["mcpServers"]["context-engine"]["command"], "cce")
        self.assertEqual(data["otherKey"], {"keep": True})
        self.assertIn("context-engine", out)

    def test_default_stanza_is_standalone(self):
        self.run_cli("init", "--root", str(self.root), "--no-index")
        stanza = self.mcp_json()["mcpServers"][MCP_SERVER_KEY]
        self.assertNotIn("--with-cce", stanza["args"])

    def test_with_cce_opts_the_server_in(self):
        code, _, _ = self.run_cli(
            "init", "--root", str(self.root), "--no-index", "--with-cce"
        )
        self.assertEqual(code, 0)
        stanza = self.mcp_json()["mcpServers"][MCP_SERVER_KEY]
        self.assertEqual(stanza["args"][-1], "--with-cce")

    def test_registered_cce_only_hints(self):
        """Detection suggests the flag; it never flips the mode by itself."""
        (self.root / ".mcp.json").write_text(json.dumps({
            "mcpServers": {"context-engine": {"command": "cce", "args": ["serve"]}},
        }), encoding="utf-8")
        code, out, _ = self.run_cli("init", "--root", str(self.root), "--no-index")
        self.assertEqual(code, 0)
        self.assertIn("--with-cce", out)
        stanza = self.mcp_json()["mcpServers"][MCP_SERVER_KEY]
        self.assertNotIn("--with-cce", stanza["args"])

    def test_hint_is_silent_once_opted_in(self):
        (self.root / ".mcp.json").write_text(json.dumps({
            "mcpServers": {"context-engine": {"command": "cce", "args": ["serve"]}},
        }), encoding="utf-8")
        _, out, _ = self.run_cli(
            "init", "--root", str(self.root), "--no-index", "--with-cce"
        )
        self.assertNotIn("adds the routing guidance", out)

    def test_rerun_is_idempotent(self):
        self.run_cli("init", "--root", str(self.root), "--no-index")
        first = (self.root / ".mcp.json").read_text(encoding="utf-8")
        code, out, _ = self.run_cli("init", "--root", str(self.root), "--no-index")
        self.assertEqual(code, 0)
        self.assertEqual(first, (self.root / ".mcp.json").read_text(encoding="utf-8"))
        self.assertIn("unchanged", out)

    def test_refuses_to_clobber_unparseable_mcp_json(self):
        broken = "{not json"
        (self.root / ".mcp.json").write_text(broken, encoding="utf-8")
        code, _, err = self.run_cli("init", "--root", str(self.root), "--no-index")
        self.assertEqual(code, 1)
        self.assertEqual((self.root / ".mcp.json").read_text(encoding="utf-8"), broken)
        self.assertIn("not readable JSON", err)

    def test_dry_run_writes_nothing(self):
        code, out, _ = self.run_cli("init", "--root", str(self.root), "--dry-run", "--no-index")
        self.assertEqual(code, 0)
        self.assertFalse((self.root / ".mcp.json").exists())
        self.assertFalse((self.root / ".gitignore").exists())
        self.assertIn("would write", out)

    def test_adds_index_dir_to_gitignore_once(self):
        self.run_cli("init", "--root", str(self.root), "--no-index")
        self.run_cli("init", "--root", str(self.root), "--no-index")
        body = (self.root / ".gitignore").read_text(encoding="utf-8")
        self.assertEqual(body.count(f"{DB_DIRNAME}/"), 1)

    def test_command_override_is_recorded(self):
        self.run_cli(
            "init", "--root", str(self.root), "--no-index",
            "--command", "uv run ast-mcp serve",
        )
        stanza = self.mcp_json()["mcpServers"][MCP_SERVER_KEY]
        self.assertEqual(stanza, {"command": "uv", "args": ["run", "ast-mcp", "serve"]})


class TestIndexCommand(CLITestBase):
    def test_reports_counts(self):
        code, out, _ = self.run_cli("index", "--root", str(self.root))
        self.assertEqual(code, 0)
        self.assertIn("files", out)
        self.assertIn("symbols", out)

    def test_rebuild_discards_the_database(self):
        """A marker table survives a refresh and dies with a rebuild."""
        self.run_cli("index", "--root", str(self.root))
        database = self.root / DB_DIRNAME / DB_FILENAME
        self._query(database, "CREATE TABLE probe (x INTEGER)")

        self.run_cli("index", "--root", str(self.root))
        self.assertTrue(self._has_probe(database), "refresh should reuse the database")

        code, out, _ = self.run_cli("index", "--root", str(self.root), "--rebuild")
        self.assertEqual(code, 0)
        self.assertIn("rebuilt", out)
        self.assertFalse(self._has_probe(database))
        self.assertGreater(
            self._query(database, "SELECT count(*) FROM symbols")[0][0], 0
        )

    def test_rebuild_reports_a_locked_index_instead_of_raising(self):
        """Windows refuses to unlink an open database; say so, do not traceback."""
        self.run_cli("index", "--root", str(self.root))
        locked = PermissionError(32, "used by another process")
        with mock.patch("pathlib.Path.unlink", side_effect=locked):
            code, _, err = self.run_cli("index", "--root", str(self.root), "--rebuild")
        self.assertEqual(code, 1)
        self.assertIn("cannot discard", err)
        self.assertIn("without --rebuild", err)

    @staticmethod
    def _query(database: Path, sql: str) -> list:
        """Run one statement and close the handle.

        `sqlite3.connect` as a context manager scopes the transaction, not the
        connection — leaving it open blocks the tempdir cleanup on Windows.
        """
        conn = sqlite3.connect(database)
        try:
            rows = conn.execute(sql).fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()

    def _has_probe(self, database: Path) -> bool:
        return bool(self._query(
            database, "SELECT count(*) FROM sqlite_master WHERE name = 'probe'"
        )[0][0])

    def test_quiet_prints_nothing(self):
        code, out, _ = self.run_cli("index", "--root", str(self.root), "--quiet")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")


class TestStatus(CLITestBase):
    def test_unindexed_root_reports_and_creates_no_database(self):
        code, out, _ = self.run_cli("status", "--root", str(self.root))
        self.assertEqual(code, 0)
        self.assertIn("no index", out)
        self.assertFalse((self.root / DB_DIRNAME / DB_FILENAME).exists())

    def test_json_output_after_indexing(self):
        self.run_cli("init", "--root", str(self.root))
        code, out, _ = self.run_cli("status", "--root", str(self.root), "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["indexed"])
        self.assertGreater(payload["counts"]["symbols"], 0)
        self.assertIn("serve", payload["mcp_registered"])


class TestLanguages(unittest.TestCase):
    def test_lists_all_registry_rows(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["languages"]), 0)
        body = out.getvalue()
        self.assertIn("python", body)
        self.assertIn("26 languages", body)

    def test_group_filter_and_json(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["languages", "--group", "core", "--json"]), 0)
        rows = json.loads(out.getvalue())
        self.assertEqual({row["group"] for row in rows}, {"core"})
        self.assertEqual({row["profile"] for row in rows}, {"symbols"})


class TestServe(CLITestBase):
    def test_root_reaches_the_server_through_the_environment(self):
        with mock.patch.dict("os.environ", {}, clear=False), \
             mock.patch("ast_mcp.main.mcp.run") as run:
            self.assertEqual(main(["serve", "--root", str(self.root)]), 0)
            import os

            self.assertEqual(os.environ["AST_MCP_ROOT"], str(self.root.resolve()))
        run.assert_called_once_with(transport="stdio")


if __name__ == "__main__":
    unittest.main()


CCE_BLOCK = """<!-- cce-block-version: 4 -->
## Context Engine (CCE)

**You MUST use `context_search` instead of reading files directly.**
<!-- /cce-block -->
"""


class TestClaudeMd(CLITestBase):
    """SPEC §I.claudemd — one routing block, written only when asked."""

    def claude_md(self) -> str:
        return (self.root / "CLAUDE.md").read_text(encoding="utf-8")

    def test_written_by_default(self):
        self.run_cli("init", "--root", str(self.root), "--no-index")
        self.assertIn(claudemd.AST_MARKER, self.claude_md())

    def test_no_claude_md_leaves_the_file_alone(self):
        self.run_cli("init", "--root", str(self.root), "--no-index", "--no-claude-md")
        self.assertFalse((self.root / "CLAUDE.md").exists())

    def test_creates_the_file_when_absent(self):
        self.run_cli("init", "--root", str(self.root), "--no-index")
        text = self.claude_md()
        self.assertIn(claudemd.AST_MARKER, text)
        self.assertIn("Route on what you can name", text)
        self.assertNotIn(claudemd.CCE_TAG_PREFIX, text)

    def test_replaces_the_cce_block_and_keeps_its_version_tag(self):
        (self.root / "CLAUDE.md").write_text(
            f"# House rules\n\nKeep it terse.\n\n{CCE_BLOCK}", encoding="utf-8"
        )
        self.run_cli("init", "--root", str(self.root), "--no-index")
        text = self.claude_md()
        self.assertIn("# House rules", text)
        self.assertIn("Keep it terse.", text)
        self.assertNotIn("You MUST use `context_search` instead", text)
        self.assertIn(claudemd.AST_MARKER, text)
        # The exact string `cce init` tests for equality before rewriting.
        self.assertIn("<!-- cce-block-version: 4 -->", text)
        self.assertIn(claudemd.CCE_END_MARKER, text)

    def test_an_unknown_cce_version_is_carried_over_not_invented(self):
        (self.root / "CLAUDE.md").write_text(
            CCE_BLOCK.replace("version: 4", "version: 9"), encoding="utf-8"
        )
        self.run_cli("init", "--root", str(self.root), "--no-index")
        self.assertIn("<!-- cce-block-version: 9 -->", self.claude_md())

    def test_rerun_reports_unchanged(self):
        args = ("init", "--root", str(self.root), "--no-index")
        self.run_cli(*args)
        first = self.claude_md()
        _, out, _ = self.run_cli(*args)
        self.assertEqual(first, self.claude_md())
        self.assertIn("unchanged", out)

    def test_with_cce_adds_the_semantic_route_and_memory(self):
        self.run_cli("init", "--root", str(self.root), "--no-index")
        standalone = self.claude_md()
        self.assertNotIn("context_search", standalone)
        self.assertNotIn("record_decision", standalone)
        self.run_cli(
            "init", "--root", str(self.root), "--no-index", "--with-cce"
        )
        paired = self.claude_md()
        self.assertIn("context_search", paired)
        self.assertIn("record_decision", paired)

    def test_dry_run_writes_nothing(self):
        self.run_cli(
            "init", "--root", str(self.root), "--no-index", "--dry-run"
        )
        self.assertFalse((self.root / "CLAUDE.md").exists())

    def test_a_cce_only_block_is_reported_as_drift(self):
        (self.root / "CLAUDE.md").write_text(CCE_BLOCK, encoding="utf-8")
        _, out, _ = self.run_cli(
            "init", "--root", str(self.root), "--no-index", "--no-claude-md")
        self.assertIn("`ast-mcp init` unifies it", out)

    def test_no_drift_once_the_block_is_ours(self):
        (self.root / "CLAUDE.md").write_text(CCE_BLOCK, encoding="utf-8")
        self.run_cli("init", "--root", str(self.root), "--no-index")
        self.assertIsNone(claudemd.drift(self.root))

    def test_status_surfaces_drift(self):
        (self.root / "CLAUDE.md").write_text(CCE_BLOCK, encoding="utf-8")
        self.run_cli("init", "--root", str(self.root), "--no-claude-md")
        _, out, _ = self.run_cli("status", "--root", str(self.root))
        self.assertIn("`ast-mcp init` unifies it", out)
        _, raw, _ = self.run_cli("status", "--root", str(self.root), "--json")
        self.assertIsNotNone(json.loads(raw)["claude_md_drift"])
