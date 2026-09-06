"""Source reading and parsing, with a stat-keyed cache.

SPEC §V.2 — the byte string is the source of truth; every range downstream
indexes into ``ParsedFile.source``.
SPEC §V.3 — the cache is keyed on ``(mtime_ns, size)`` and revalidated by a
``stat`` on every lookup, so a cached tree can never outlive its file.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Parser, Tree

from ast_mcp import Error
from ast_mcp.languages import LangSpec, load_language, spec_for_path

#: Ordinary source files above this are skipped (SPEC §I.walk).
MAX_BYTES = 2 * 1024 * 1024
#: ``schema`` profile files get a larger cap — §V.11 collapse bounds their cost.
MAX_BYTES_SCHEMA = 16 * 1024 * 1024

_CACHE_SIZE = 64
_cache: OrderedDict[str, "ParsedFile"] = OrderedDict()


@dataclass(frozen=True, slots=True)
class ParsedFile:
    path: Path
    spec: LangSpec
    source: bytes
    tree: Tree
    mtime_ns: int
    size: int

    @property
    def line_count(self) -> int:
        return self.source.count(b"\n") + (0 if self.source.endswith(b"\n") else 1)

    def slice(self, start_byte: int, end_byte: int) -> str:
        """Exact source text for a byte range (SPEC §V.2)."""
        return self.source[start_byte:end_byte].decode("utf-8", errors="replace")


def size_limit(spec: LangSpec) -> int:
    return MAX_BYTES_SCHEMA if spec.profile == "schema" else MAX_BYTES


def stat_key(path: Path) -> tuple[int, int] | None:
    """``(mtime_ns, size)`` for a readable regular file, else None."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def parse_file(path: str | Path) -> tuple[ParsedFile | None, list[Error]]:
    """Parse a file, reusing the cached tree when the file has not changed.

    Never raises (SPEC §V.5). Returns ``(None, errors)`` when the file cannot
    be parsed at all, and ``(parsed, errors)`` — with a possibly non-empty
    error list — when it can.
    """
    p = Path(path)
    key = str(p)

    spec = spec_for_path(p)
    if spec is None:
        return None, [Error("unsupported_lang", key, f"no registry row for {p.name}")]

    st = stat_key(p)
    if st is None:
        return None, [Error("unreadable", key, "stat failed")]
    mtime_ns, size = st

    cached = _cache.get(key)
    if cached is not None and cached.mtime_ns == mtime_ns and cached.size == size:
        _cache.move_to_end(key)
        return cached, _parse_errors(cached)

    limit = size_limit(spec)
    if size > limit:
        return None, [Error("too_large", key, f"{size} bytes > {limit} limit")]

    language = load_language(spec.lang)
    if language is None:
        return None, [Error("no_grammar", key, f"could not load {spec.source}")]

    try:
        source = p.read_bytes()
        tree = Parser(language).parse(source)
    except Exception as exc:  # noqa: BLE001 — §V.5, degrade never raise
        return None, [Error("parse_failed", key, f"{type(exc).__name__}: {exc}")]

    parsed = ParsedFile(p, spec, source, tree, mtime_ns, size)
    _cache[key] = parsed
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_SIZE:
        _cache.popitem(last=False)
    return parsed, _parse_errors(parsed)


def _parse_errors(parsed: ParsedFile) -> list[Error]:
    """Syntax errors are reported, not fatal — extraction still runs (§V.5)."""
    if not parsed.tree.root_node.has_error:
        return []
    return [Error("syntax_error", str(parsed.path), "tree contains ERROR nodes")]


def cache_clear() -> None:
    _cache.clear()


def cache_size() -> int:
    return len(_cache)
