"""MCP entry point — wires the six tools onto an ``MCPServer`` over stdio.

The index is created once per process, rooted at ``--root`` / ``AST_MCP_ROOT``
/ the working directory. Each tool revalidates the paths it touches, so a
long-lived process never serves a stale answer (SPEC §V.3).
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache, wraps
from pathlib import Path
from typing import Callable, TypeVar, cast

from mcp.server import MCPServer

from ast_mcp import __version__, savings, tools
from ast_mcp.index import Index
from ast_mcp.render import DEFAULT_MAX_TOKENS

INSTRUCTIONS = """\
Structural code retrieval. Prefer these tools over reading whole files.

Every response declares `profile` and `group`. The payload key follows the
profile: `symbols` for code, `schema` for data files (JSON/YAML/TOML/XML/CSV),
`outline` for documents (Markdown/HTML). Read `profile` before the payload.

Start with file_outline to see a file's shape, then get_symbol to pull just the
definition you need. search_symbols finds things by name across the repo.
ast_query is the escape hatch when the typed tools do not fit.
"""

#: Appended to ``INSTRUCTIONS`` when the operator opts in with `serve
#: --with-cce` (or ``AST_MCP_WITH_CCE``). The default is standalone: nothing
#: above assumes a semantic retriever is registered, and nothing above defers
#: to one. This paragraph is the only place CCE is named.
CCE_INSTRUCTIONS = """
A semantic retriever (CCE `context_search`) is registered alongside this
server. Route on what you can name: a symbol, key path or heading is these
tools; behaviour you can only describe is `context_search`. Its hits carry
names — hand those to get_symbol for the exact definition.
"""


def with_cce() -> bool:
    """Whether to advertise the CCE division of labour (SPEC §I.config)."""
    return os.environ.get("AST_MCP_WITH_CCE", "").strip().lower() not in (
        "", "0", "false", "no",
    )


def instructions() -> str:
    """Server instructions — standalone by default, CCE-aware on request."""
    return INSTRUCTIONS + CCE_INSTRUCTIONS if with_cce() else INSTRUCTIONS


mcp = MCPServer(
    name="ast-mcp",
    title="AST MCP",
    version=__version__,
    instructions=instructions(),
)


@lru_cache(maxsize=1)
def get_index(root: str | None = None) -> Index:
    resolved = root or os.environ.get("AST_MCP_ROOT") or os.getcwd()
    return Index(Path(resolved))


F = TypeVar("F", bound=Callable[..., dict])


def measured(func: F) -> F:
    """Log what the call served against what reading those files would cost.

    Wrapped inside `mcp.tool()` so the ledger sees the payload the agent gets.
    `savings.record` swallows its own failures — measurement never costs the
    caller an answer (SPEC §V.5).
    """

    @wraps(func)
    def wrapper(*args, **kwargs) -> dict:
        payload = func(*args, **kwargs)
        savings.record(get_index(), func.__name__, payload)
        return payload

    return cast(F, wrapper)


@mcp.tool()
@measured
def file_outline(
    path: str,
    max_depth: int | None = None,
    include_docstrings: bool = False,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict:
    """Outline one file: definitions, key paths or headings, bodies elided.

    Use this instead of reading a file to find out what is in it.
    """
    return tools.file_outline(
        get_index(), path, max_depth, include_docstrings, max_tokens
    )


@mcp.tool()
@measured
def get_symbol(
    name: str,
    path: str | None = None,
    line: int | None = None,
    mode: str = "source",
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict:
    """Fetch definitions by name, key path, or heading slug.

    mode: "source" (default) returns the body, "signature" and "doc" omit it.
    A name matching several different symbols returns candidates rather than
    guessing. A name matching several definitions of the same thing — many Lua
    listeners on one event — returns all of them in "symbols".
    line: pick one definition by the start_line a candidate reported.
    """
    return tools.get_symbol(get_index(), name, path, line, mode, max_tokens)


@mcp.tool()
@measured
def search_symbols(
    query: str,
    kind: str | None = None,
    lang: str | None = None,
    group: str | None = None,
    path_glob: str | None = None,
    limit: int = 20,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict:
    """Find definitions, key paths and headings by name across the repository.

    group is one of core, web, scripting, data, devops, docs.
    """
    return tools.search_symbols(
        get_index(), query, kind, lang, group, path_glob, limit, max_tokens
    )


@mcp.tool()
@measured
def get_docstrings(
    path: str | None = None,
    symbols: list[str] | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict:
    """Documentation without bodies. Pass exactly one of path or symbols."""
    return tools.get_docstrings(get_index(), path, symbols, max_tokens)


@mcp.tool()
@measured
def list_imports(path: str, max_tokens: int = DEFAULT_MAX_TOKENS) -> dict:
    """Dependency edges out of one file, plus exports where the language has them."""
    return tools.list_imports(get_index(), path, max_tokens)


@mcp.tool()
@measured
def ast_query(
    path: str,
    query: str,
    captures: list[str] | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict:
    """Run a raw tree-sitter S-expression query against one file.

    The escape hatch for anything the five typed tools flatten away.
    """
    return tools.ast_query(get_index(), path, query, captures, max_tokens)


def main() -> int:
    """Kept so ``python -m ast_mcp.main`` still works; the CLI owns argv now."""
    from ast_mcp.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    sys.exit(main())
