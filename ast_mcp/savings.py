"""Token savings ledger — what the tools served vs. what reading cost.

Every tool response is compared against the honest alternative: opening each
file it drew from and reading the whole thing. The difference is the saving,
recorded per call so `ast-mcp savings` can report it later.

This ledger lives in its own database — `<root>/.ast_mcp/savings.db` — and not
in the symbol index. The index is a cache with a schema version that drops and
rebuilds on mismatch, and `ast-mcp index --rebuild` deletes the file outright;
history stored there would evaporate on the first grammar change. Recording is
best-effort by construction: a failure here never reaches the agent (SPEC §V.5).
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

from ast_mcp.index import DB_DIRNAME, Index
from ast_mcp.render import CHARS_PER_TOKEN, estimate_tokens

STATS_FILENAME = "savings.db"
SCHEMA_VERSION = 1

#: Opus input pricing, US dollars per million tokens. Savings are input
#: tokens — the ones a `Read` would have spent — so output price never enters.
INPUT_PRICE_PER_MTOK = 15.0

SCHEMA = """
CREATE TABLE calls (
  id       INTEGER PRIMARY KEY,
  ts       INTEGER NOT NULL,
  tool     TEXT NOT NULL,
  baseline INTEGER NOT NULL,
  served   INTEGER NOT NULL,
  files    INTEGER NOT NULL
);

CREATE INDEX idx_calls_tool ON calls(tool);
"""


def stats_path(root: Path) -> Path:
    return root / DB_DIRNAME / STATS_FILENAME


def enabled() -> bool:
    """Recording is on unless the operator turns it off."""
    return os.environ.get("AST_MCP_NO_STATS", "").strip().lower() in (
        "", "0", "false", "no",
    )


def price_per_mtok() -> float:
    """Input price, overridable for a non-Opus model."""
    raw = os.environ.get("AST_MCP_PRICE_PER_MTOK", "").strip()
    try:
        return float(raw) if raw else INPUT_PRICE_PER_MTOK
    except ValueError:
        return INPUT_PRICE_PER_MTOK


def dollars(tokens: int) -> float:
    return tokens * price_per_mtok() / 1_000_000


# --- storage -----------------------------------------------------------------


def _open(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # The server hands tool calls to worker threads; one cached connection has
    # to survive being used from more than one of them.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    has_table = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='calls'"
    ).fetchone()[0]
    if not has_table or version != SCHEMA_VERSION:
        conn.execute("DROP TABLE IF EXISTS calls")
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    return conn


@lru_cache(maxsize=8)
def _cached(path_str: str) -> sqlite3.Connection:
    return _open(Path(path_str))


def connect(root: Path) -> sqlite3.Connection:
    """A ledger connection for ``root``, reused across calls in a process."""
    return _cached(str(stats_path(root)))


def reset(root: Path) -> int:
    """Drop every recorded call. Returns how many were discarded."""
    if not stats_path(root).exists():
        return 0
    conn = connect(root)
    count = conn.execute("SELECT count(*) FROM calls").fetchone()[0]
    conn.execute("DELETE FROM calls")
    conn.commit()
    return int(count)


# --- measurement -------------------------------------------------------------


def referenced_paths(payload: Any) -> set[str]:
    """Every file path the payload names, at any depth.

    A response is only ever about files it cites, so the set of `path` values
    it carries *is* the set of files a reader would have opened instead.
    """
    found: set[str] = set()
    for value in _walk(payload):
        if isinstance(value, dict):
            path = value.get("path")
            if isinstance(path, str) and path:
                found.add(path)
    return found


def _walk(value: Any) -> Iterator[Any]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def baseline_tokens(index: Index, paths: set[str]) -> int:
    """What reading each of those files whole would have cost.

    Byte size over `CHARS_PER_TOKEN` — the same estimator the budget uses, so
    the two sides of the comparison are measured with one ruler.
    """
    total = 0
    for relative in paths:
        row = index.conn.execute(
            "SELECT size FROM files WHERE path = ?", (relative,)
        ).fetchone()
        size = row["size"] if row is not None else _stat_size(index.absolute(relative))
        total += max(1, size // CHARS_PER_TOKEN) if size else 0
    return total


def _stat_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def record(index: Index, tool: str, payload: dict) -> None:
    """Log one tool call. Never raises — a broken ledger is not a failed query."""
    if not enabled():
        return
    try:
        paths = referenced_paths(payload)
        row = (
            int(time.time()),
            tool,
            baseline_tokens(index, paths),
            estimate_tokens(payload),
            len(paths),
        )
        conn = connect(index.root)
        conn.execute(
            "INSERT INTO calls (ts, tool, baseline, served, files) VALUES (?,?,?,?,?)",
            row,
        )
        conn.commit()
    except (sqlite3.Error, OSError, TypeError, ValueError):
        return


# --- reporting ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolRow:
    tool: str
    calls: int
    baseline: int
    served: int

    @property
    def saved(self) -> int:
        return max(0, self.baseline - self.served)


@dataclass(frozen=True, slots=True)
class Report:
    root: Path
    calls: int
    baseline: int
    served: int
    last_ts: int | None
    tools: list[ToolRow]

    @property
    def saved(self) -> int:
        return max(0, self.baseline - self.served)

    @property
    def percent(self) -> int:
        return round(100 * self.saved / self.baseline) if self.baseline else 0

    @property
    def saved_dollars(self) -> float:
        return dollars(self.saved)

    def as_dict(self) -> dict:
        return {
            "root": str(self.root),
            "queries": self.calls,
            "baseline_tokens": self.baseline,
            "served_tokens": self.served,
            "saved_tokens": self.saved,
            "percent_saved": self.percent,
            "saved_usd": round(self.saved_dollars, 4),
            "price_per_mtok": price_per_mtok(),
            "last_query": self.last_ts,
            "tools": [
                {
                    "tool": row.tool,
                    "calls": row.calls,
                    "baseline_tokens": row.baseline,
                    "served_tokens": row.served,
                    "saved_tokens": row.saved,
                    "saved_usd": round(dollars(row.saved), 4),
                }
                for row in self.tools
            ],
        }


def report(root: Path) -> Report:
    """Aggregate the ledger.

    An absent ledger reports zero and stays absent — reading a report must not
    be the thing that creates a database, the same rule `status` follows.
    """
    if not stats_path(root).exists():
        return Report(root=root, calls=0, baseline=0, served=0, last_ts=None, tools=[])
    conn = connect(root)
    totals = conn.execute(
        "SELECT count(*) AS n, coalesce(sum(baseline), 0) AS b, "
        "coalesce(sum(served), 0) AS s, max(ts) AS last FROM calls"
    ).fetchone()
    rows = conn.execute(
        "SELECT tool, count(*) AS n, sum(baseline) AS b, sum(served) AS s "
        "FROM calls GROUP BY tool"
    ).fetchall()
    tools = sorted(
        (ToolRow(r["tool"], r["n"], r["b"], r["s"]) for r in rows),
        key=lambda row: row.saved,
        reverse=True,
    )
    return Report(
        root=root,
        calls=int(totals["n"]),
        baseline=int(totals["b"]),
        served=int(totals["s"]),
        last_ts=totals["last"],
        tools=tools,
    )
