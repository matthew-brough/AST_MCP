"""SQLite symbol index — a cache in front of the parser, never an oracle.

SPEC §V.3 is the rule that shapes this module: every path that appears in a
response was ``stat``-checked during the same call. The index makes cross-file
lookup fast; it never gets to answer for a file it has not just revalidated.

SPEC §V.4 — this database is the only thing the server writes.
SPEC §V.10 — the walk never shells to ``git``.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from ast_mcp import Error
from ast_mcp.extract import extract
from ast_mcp.extract_doc import extract_doc
from ast_mcp.extract_schema import extract_schema
from ast_mcp.languages import spec_for_path
from ast_mcp.parser import ParsedFile, parse_file, size_limit, stat_key

SCHEMA_VERSION = 2
DB_DIRNAME = ".ast_mcp"
DB_FILENAME = "index.db"

_QUERY_FINGERPRINT: int | None = None


def query_fingerprint() -> int:
    """Hash of every ``.scm``, stored alongside the schema version.

    The index caches extraction, and extraction is defined by the query files.
    Without this, editing a query serves symbols from the old rules until the
    source file happens to change — the one way this cache could lie that
    ``stat`` cannot catch (SPEC §V.3). A mismatch rebuilds, like §I.db.
    """
    global _QUERY_FINGERPRINT
    if _QUERY_FINGERPRINT is None:
        digest = hashlib.sha256()
        for path in sorted((Path(__file__).parent / "queries").rglob("*.scm")):
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
        _QUERY_FINGERPRINT = int.from_bytes(digest.digest()[:4], "big") & 0x7FFFFFFF
    return _QUERY_FINGERPRINT

#: Never descended into. `.gitignore` patterns are honoured on top of this.
IGNORE_DIRS = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", DB_DIRNAME, "target",
    ".tox", ".next", ".svelte-kit", "vendor",
})

SCHEMA = """
CREATE TABLE files (
  id         INTEGER PRIMARY KEY,
  path       TEXT NOT NULL UNIQUE,
  lang       TEXT NOT NULL,
  grp        TEXT NOT NULL,
  profile    TEXT NOT NULL,
  mtime_ns   INTEGER NOT NULL,
  size       INTEGER NOT NULL,
  indexed_at INTEGER NOT NULL
);

CREATE TABLE symbols (
  id             INTEGER PRIMARY KEY,
  file_id        INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  name           TEXT NOT NULL,
  qualified_name TEXT NOT NULL,
  kind           TEXT NOT NULL,
  parent         TEXT,
  start_byte     INTEGER NOT NULL,
  end_byte       INTEGER NOT NULL,
  start_line     INTEGER NOT NULL,
  end_line       INTEGER NOT NULL,
  signature      TEXT NOT NULL,
  docstring      TEXT
);

CREATE TABLE imports (
  id      INTEGER PRIMARY KEY,
  file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  module  TEXT NOT NULL,
  names   TEXT NOT NULL,
  alias   TEXT,
  line    INTEGER NOT NULL,
  kind    TEXT NOT NULL
);

CREATE TABLE schema_nodes (
  id                INTEGER PRIMARY KEY,
  file_id           INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  key_path          TEXT NOT NULL,
  kind              TEXT NOT NULL,
  parent            TEXT,
  start_byte        INTEGER NOT NULL,
  end_byte          INTEGER NOT NULL,
  start_line        INTEGER NOT NULL,
  end_line          INTEGER NOT NULL,
  value_preview     TEXT,
  comment           TEXT,
  children_count    INTEGER NOT NULL DEFAULT 0,
  truncated_subtree INTEGER NOT NULL DEFAULT 0,
  uniformity        TEXT
);

CREATE TABLE doc_nodes (
  id         INTEGER PRIMARY KEY,
  file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  title      TEXT NOT NULL,
  slug       TEXT NOT NULL,
  level      INTEGER NOT NULL,
  kind       TEXT NOT NULL,
  parent     TEXT,
  start_byte INTEGER NOT NULL,
  end_byte   INTEGER NOT NULL,
  start_line INTEGER NOT NULL,
  end_line   INTEGER NOT NULL,
  info       TEXT
);

CREATE INDEX idx_symbols_name  ON symbols(name);
CREATE INDEX idx_symbols_qname ON symbols(qualified_name);
CREATE INDEX idx_symbols_file  ON symbols(file_id);
CREATE INDEX idx_imports_file  ON imports(file_id);
CREATE INDEX idx_schema_path   ON schema_nodes(key_path);
CREATE INDEX idx_schema_file   ON schema_nodes(file_id);
CREATE INDEX idx_doc_slug      ON doc_nodes(slug);
CREATE INDEX idx_doc_file      ON doc_nodes(file_id);
"""


@dataclass(frozen=True, slots=True)
class FileRecord:
    id: int
    path: str
    lang: str
    group: str
    profile: str


class Index:
    """The symbol index for one repository root."""

    def __init__(self, root: str | Path, db_path: str | Path | None = None) -> None:
        self.root = Path(root).resolve()
        self.db_path = (
            Path(db_path) if db_path is not None
            else self.root / DB_DIRNAME / DB_FILENAME
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._ensure_schema()

    # --- lifecycle -----------------------------------------------------------

    def _ensure_schema(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        queries = self.conn.execute("PRAGMA application_id").fetchone()[0]
        has_tables = self.conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='files'"
        ).fetchone()[0]
        if has_tables and version == SCHEMA_VERSION and queries == query_fingerprint():
            return
        # SPEC §I.db — a version mismatch drops and rebuilds; v1 does not migrate.
        for table in ("doc_nodes", "schema_nodes", "imports", "symbols", "files"):
            self.conn.execute(f"DROP TABLE IF EXISTS {table}")
        self.conn.executescript(SCHEMA)
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.execute(f"PRAGMA application_id = {query_fingerprint()}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Index":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- paths ---------------------------------------------------------------

    def relative(self, path: str | Path) -> str:
        p = Path(path)
        absolute = p if p.is_absolute() else (self.root / p)
        try:
            return absolute.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return absolute.as_posix()

    def absolute(self, relative_path: str) -> Path:
        p = Path(relative_path)
        return p if p.is_absolute() else self.root / p

    # --- freshness -----------------------------------------------------------

    def refresh_file(self, path: str | Path) -> tuple[ParsedFile | None, list[Error]]:
        """Reindex ``path`` if its stat key moved. SPEC §V.3.

        Returns the parsed file so the caller can slice source without a second
        read, plus any non-fatal errors.
        """
        relative = self.relative(path)
        absolute = self.absolute(relative)
        spec = spec_for_path(absolute)
        if spec is None:
            return None, [Error("unsupported_lang", relative, absolute.name)]

        current = stat_key(absolute)
        if current is None:
            self._delete_file(relative)
            return None, [Error("unreadable", relative, "stat failed")]

        row = self.conn.execute(
            "SELECT mtime_ns, size FROM files WHERE path = ?", (relative,)
        ).fetchone()
        parsed, errors = parse_file(absolute)
        if parsed is None:
            self._delete_file(relative)
            return None, errors
        if row is not None and (row["mtime_ns"], row["size"]) == current:
            return parsed, errors

        errors.extend(self._store(relative, parsed))
        return parsed, errors

    def refresh_all(self) -> list[Error]:
        """Walk the root and reindex everything stale. SPEC §V.3, §V.10."""
        errors: list[Error] = []
        seen: set[str] = set()
        for path in self.walk():
            relative = self.relative(path)
            seen.add(relative)
            _parsed, file_errors = self.refresh_file(path)
            errors.extend(file_errors)
        for row in self.conn.execute("SELECT path FROM files").fetchall():
            if row["path"] not in seen:
                self._delete_file(row["path"])
        self.conn.commit()
        return errors

    def walk(self):
        """Yield indexable files under the root. Never shells to git (§V.10)."""
        ignored = _gitignore_patterns(self.root)
        stack = [self.root]
        while stack:
            directory = stack.pop()
            try:
                entries = sorted(directory.iterdir())
            except OSError:
                continue
            for entry in entries:
                relative = entry.relative_to(self.root).as_posix()
                if entry.name in IGNORE_DIRS or _is_ignored(relative, entry, ignored):
                    continue
                if entry.is_dir():
                    stack.append(entry)
                    continue
                spec = spec_for_path(entry)
                if spec is None:
                    continue
                stat = stat_key(entry)
                if stat is None or stat[1] > size_limit(spec):
                    continue
                yield entry

    # --- writes --------------------------------------------------------------

    def _delete_file(self, relative: str) -> None:
        self.conn.execute("DELETE FROM files WHERE path = ?", (relative,))

    def _store(self, relative: str, parsed: ParsedFile) -> list[Error]:
        spec = parsed.spec
        self._delete_file(relative)
        cursor = self.conn.execute(
            "INSERT INTO files (path, lang, grp, profile, mtime_ns, size, indexed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (relative, spec.lang, spec.group, spec.profile,
             parsed.mtime_ns, parsed.size, int(time.time())),
        )
        file_id = cursor.lastrowid
        errors: list[Error] = []

        if spec.profile in {"symbols", "defs"}:
            extraction = extract(parsed)
            errors.extend(extraction.errors)
            self.conn.executemany(
                "INSERT INTO symbols (file_id, name, qualified_name, kind, parent,"
                " start_byte, end_byte, start_line, end_line, signature, docstring)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(file_id, s.name, s.qualified_name, s.kind, s.parent, s.start_byte,
                  s.end_byte, s.start_line, s.end_line, s.signature, s.docstring)
                 for s in extraction.symbols],
            )
            self._store_imports(file_id, extraction.imports)

        elif spec.profile == "schema":
            extraction = extract_schema(parsed)
            errors.extend(extraction.errors)
            self.conn.executemany(
                "INSERT INTO schema_nodes (file_id, key_path, kind, parent,"
                " start_byte, end_byte, start_line, end_line, value_preview,"
                " comment, children_count, truncated_subtree, uniformity)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(file_id, n.key_path, n.kind, n.parent, n.start_byte, n.end_byte,
                  n.start_line, n.end_line, n.value_preview, n.comment,
                  n.children_count, int(n.truncated_subtree), n.uniformity)
                 for n in extraction.nodes],
            )

        elif spec.profile == "outline":
            extraction = extract_doc(parsed)
            errors.extend(extraction.errors)
            self.conn.executemany(
                "INSERT INTO doc_nodes (file_id, title, slug, level, kind, parent,"
                " start_byte, end_byte, start_line, end_line, info)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(file_id, n.title, n.slug, n.level, n.kind, n.parent, n.start_byte,
                  n.end_byte, n.start_line, n.end_line, n.info)
                 for n in extraction.nodes],
            )
            self._store_imports(file_id, extraction.links)

        self.conn.commit()
        return errors

    def _store_imports(self, file_id: int, imports) -> None:
        self.conn.executemany(
            "INSERT INTO imports (file_id, module, names, alias, line, kind)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [(file_id, i.module, json.dumps(list(i.names)), i.alias, i.line, i.kind)
             for i in imports],
        )

    # --- reads ---------------------------------------------------------------

    def file_record(self, relative: str) -> FileRecord | None:
        row = self.conn.execute(
            "SELECT id, path, lang, grp, profile FROM files WHERE path = ?",
            (relative,),
        ).fetchone()
        if row is None:
            return None
        return FileRecord(row["id"], row["path"], row["lang"], row["grp"], row["profile"])

    def rows_for_file(self, table: str, file_id: int, order: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            f"SELECT * FROM {table} WHERE file_id = ? ORDER BY {order}", (file_id,)
        ).fetchall()

    def search(
        self,
        query: str,
        *,
        kind: str | None = None,
        lang: str | None = None,
        group: str | None = None,
        path_glob: str | None = None,
        limit: int = 20,
    ) -> tuple[list[sqlite3.Row], int]:
        """Ranked lookup across all three payload tables.

        Rank: exact name, then prefix, then substring; shorter paths first.
        """
        needle = query.lower()
        # (table, name column, qualified-name column, signature expr, doc expr)
        selects = [
            ("symbols", "name", "qualified_name", "t.signature", "t.docstring"),
            ("schema_nodes", "key_path", "key_path", "t.value_preview", "t.comment"),
            ("doc_nodes", "title", "slug", "t.title", "NULL"),
        ]
        unions = []
        params: list[object] = []
        for table, name_col, qname_col, signature, docstring in selects:
            unions.append(
                f"SELECT f.path AS path, f.lang AS lang, f.grp AS grp,"
                f" f.profile AS profile, t.{name_col} AS name,"
                f" t.{qname_col} AS qualified_name, t.kind AS kind,"
                f" {signature} AS signature, {docstring} AS docstring,"
                f" t.start_line AS start_line, t.end_line AS end_line,"
                f" t.start_byte AS start_byte, t.end_byte AS end_byte"
                f" FROM {table} t JOIN files f ON f.id = t.file_id"
                f" WHERE (lower(t.{name_col}) LIKE ? OR lower(t.{qname_col}) LIKE ?)"
            )
            params.extend([f"%{needle}%", f"%{needle}%"])

        sql = " UNION ALL ".join(unions)
        filters, filter_params = [], []
        if kind:
            filters.append("kind = ?")
            filter_params.append(kind)
        if lang:
            filters.append("lang = ?")
            filter_params.append(lang)
        if group:
            filters.append("grp = ?")
            filter_params.append(group)
        where = f" WHERE {' AND '.join(filters)}" if filters else ""

        rank = (
            "CASE WHEN lower(name) = ? THEN 0"
            " WHEN lower(name) LIKE ? THEN 1"
            " WHEN lower(qualified_name) = ? THEN 2"
            " ELSE 3 END"
        )
        rank_params = [needle, f"{needle}%", needle]
        full = (
            f"SELECT * FROM ({sql}){where}"
            f" ORDER BY {rank}, length(path), path, start_line"
        )
        rows = self.conn.execute(full, [*params, *filter_params, *rank_params]).fetchall()

        if path_glob:
            rows = [r for r in rows if fnmatch.fnmatch(r["path"], path_glob)]
        return rows[:limit], len(rows)


# --- ignore handling ---------------------------------------------------------

def _gitignore_patterns(root: Path) -> list[str]:
    """Read `.gitignore` if present. Reading the file is not shelling to git."""
    path = root / ".gitignore"
    if not path.exists():
        return []
    patterns = []
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and not line.startswith("!"):
                patterns.append(line.rstrip("/"))
    except OSError:
        return []
    return patterns


def _is_ignored(relative: str, entry: Path, patterns: list[str]) -> bool:
    for pattern in patterns:
        if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(entry.name, pattern):
            return True
        if fnmatch.fnmatch(relative, f"{pattern}/*"):
            return True
    return False
