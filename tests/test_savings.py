"""SPEC §T.20 — the savings ledger and the `savings` subcommand."""

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ast_mcp import parser, savings
from ast_mcp.cli import main
from ast_mcp.index import Index
from ast_mcp.render import estimate_tokens

FIXTURES = Path(__file__).parent / "fixtures"


class SavingsTestBase(unittest.TestCase):
    def setUp(self):
        parser.cache_clear()
        savings._cached.cache_clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(savings._cached.cache_clear)
        shutil.copytree(FIXTURES / "core", self.root / "src")
        self.index = Index(self.root)
        self.addCleanup(self.index.close)
        self.index.refresh_all()

    def run_cli(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([*argv])
        return code, out.getvalue(), err.getvalue()


class TestReferencedPaths(unittest.TestCase):
    def test_collects_nested_paths(self):
        payload = {
            "path": "src/sample.py",
            "symbols": [{"path": "src/sample.py"}, {"path": "src/other.py"}],
        }
        self.assertEqual(
            savings.referenced_paths(payload), {"src/sample.py", "src/other.py"}
        )

    def test_payload_without_paths_is_empty(self):
        self.assertEqual(savings.referenced_paths({"errors": [{"code": "x"}]}), set())


class TestRecording(SavingsTestBase):
    def test_baseline_is_the_whole_file(self):
        source = self.root / "src" / "sample.py"
        expected = source.stat().st_size // 4
        measured = savings.baseline_tokens(self.index, {"src/sample.py"})
        self.assertEqual(measured, expected)

    def test_record_logs_one_call(self):
        payload = {"path": "src/sample.py", "symbols": [{"name": "f"}]}
        savings.record(self.index, "file_outline", payload)
        report = savings.report(self.root)
        self.assertEqual(report.calls, 1)
        self.assertEqual(report.served, estimate_tokens(payload))
        self.assertGreater(report.baseline, report.served)
        self.assertEqual(report.saved, report.baseline - report.served)
        self.assertEqual([row.tool for row in report.tools], ["file_outline"])

    def test_recording_can_be_turned_off(self):
        with mock.patch.dict("os.environ", {"AST_MCP_NO_STATS": "1"}):
            savings.record(self.index, "file_outline", {"path": "src/sample.py"})
        self.assertEqual(savings.report(self.root).calls, 0)

    def test_a_broken_ledger_never_raises(self):
        with mock.patch.object(savings, "connect", side_effect=OSError("locked")):
            savings.record(self.index, "file_outline", {"path": "src/sample.py"})

    def test_reset_discards_history(self):
        savings.record(self.index, "get_symbol", {"path": "src/sample.py"})
        self.assertEqual(savings.reset(self.root), 1)
        self.assertEqual(savings.report(self.root).calls, 0)

    def test_price_override(self):
        with mock.patch.dict("os.environ", {"AST_MCP_PRICE_PER_MTOK": "3"}):
            self.assertEqual(savings.price_per_mtok(), 3.0)
            self.assertAlmostEqual(savings.dollars(1_000_000), 3.0)

    def test_bad_price_falls_back(self):
        with mock.patch.dict("os.environ", {"AST_MCP_PRICE_PER_MTOK": "cheap"}):
            self.assertEqual(savings.price_per_mtok(), savings.INPUT_PRICE_PER_MTOK)


class TestServerRecordsWhatItServes(SavingsTestBase):
    """The wiring, not the arithmetic: a real tool call lands in the ledger."""

    def test_file_outline_through_the_server_is_recorded(self):
        from ast_mcp import main as server

        server.get_index.cache_clear()
        self.addCleanup(server.get_index.cache_clear)
        with mock.patch.dict("os.environ", {"AST_MCP_ROOT": str(self.root)}):
            payload = server.file_outline("src/sample.py")
        self.assertEqual(payload["path"], "src/sample.py")
        report = savings.report(self.root)
        self.assertEqual([row.tool for row in report.tools], ["file_outline"])
        self.assertEqual(report.calls, 1)
        self.assertGreater(report.baseline, 0)


class TestSavingsCommand(SavingsTestBase):
    def test_unrecorded_root_reports_and_creates_no_database(self):
        code, out, _ = self.run_cli("savings", "--root", str(self.root))
        self.assertEqual(code, 0)
        self.assertIn("no queries recorded yet", out)
        self.assertFalse(savings.stats_path(self.root).exists())

    def test_report_after_a_recorded_call(self):
        savings.record(self.index, "file_outline", {"path": "src/sample.py"})
        code, out, _ = self.run_cli("savings", "--root", str(self.root))
        self.assertEqual(code, 0)
        self.assertIn("1 query", out)
        self.assertIn("saved", out)
        self.assertIn("file_outline", out)

    def test_json_output(self):
        savings.record(self.index, "get_symbol", {"path": "src/sample.py"})
        code, out, _ = self.run_cli("savings", "--root", str(self.root), "--json")
        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(payload["queries"], 1)
        self.assertEqual(payload["tools"][0]["tool"], "get_symbol")
        self.assertEqual(
            payload["saved_tokens"],
            payload["baseline_tokens"] - payload["served_tokens"],
        )

    def test_reset_from_the_cli(self):
        savings.record(self.index, "get_symbol", {"path": "src/sample.py"})
        code, out, _ = self.run_cli("savings", "--root", str(self.root), "--reset")
        self.assertEqual(code, 0)
        self.assertIn("cleared 1", out)
        self.assertEqual(savings.report(self.root).calls, 0)


if __name__ == "__main__":
    unittest.main()
