"""MCP entry point — wires the six tools onto an ``MCPServer`` over stdio.

The index is created once per process, rooted at ``--root`` / ``AST_MCP_ROOT``
/ the working directory. Each tool revalidates the paths it touches, so a
long-lived process never serves a stale answer (SPEC §V.3).
"""

from __future__ import annotations

import argparse
import os
from functools import lru_cache
from pathlib import Path

from mcp.server import MCPServer

from ast_mcp import __version__, tools
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

mcp = MCPServer(
    name="ast-mcp",
    title="AST MCP",
    version=__version__,
    instructions=INSTRUCTIONS,
)


@lru_cache(maxsize=1)
def get_index(root: str | None = None) -> Index:
    resolved = root or os.environ.get("AST_MCP_ROOT") or os.getcwd()
    return Index(Path(resolved))


@mcp.tool()
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
def get_docstrings(
    path: str | None = None,
    symbols: list[str] | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict:
    """Documentation without bodies. Pass exactly one of path or symbols."""
    return tools.get_docstrings(get_index(), path, symbols, max_tokens)


@mcp.tool()
def list_imports(path: str, max_tokens: int = DEFAULT_MAX_TOKENS) -> dict:
    """Dependency edges out of one file, plus exports where the language has them."""
    return tools.list_imports(get_index(), path, max_tokens)


@mcp.tool()
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


def main() -> None:
    parser = argparse.ArgumentParser(prog="ast-mcp", description="AST MCP server")
    parser.add_argument("--root", default=None, help="repository root to index")
    args = parser.parse_args()
    if args.root:
        os.environ["AST_MCP_ROOT"] = str(Path(args.root).resolve())
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
