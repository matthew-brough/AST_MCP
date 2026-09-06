"""Command line surface — `ast-mcp`.

Five subcommands (SPEC §I.cli). Four are read-only; `init` is the single
exception to §V.4 and touches exactly two files, both outside the source tree
it indexes: `<root>/.mcp.json` and `<root>/.gitignore`.

Invoking with no subcommand, or with flags only, runs `serve` — so the
pre-CLI form `ast-mcp --root /path` keeps working.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

from ast_mcp import __version__
from ast_mcp.index import DB_DIRNAME, DB_FILENAME, Index
from ast_mcp.languages import GROUPS, LANGS

#: Key this tool owns inside `.mcp.json`. `init` rewrites this key and no other.
MCP_SERVER_KEY = "ast-mcp"

SUBCOMMANDS = ("serve", "init", "index", "status", "languages")

GITIGNORE_ENTRY = f"{DB_DIRNAME}/"

_COUNT_TABLES = ("files", "symbols", "imports", "schema_nodes", "doc_nodes")


def resolve_root(root: str | None) -> Path:
    """`--root`, else `AST_MCP_ROOT`, else cwd (SPEC §I.config)."""
    return Path(root or os.environ.get("AST_MCP_ROOT") or Path.cwd()).resolve()


def db_path(root: Path) -> Path:
    return root / DB_DIRNAME / DB_FILENAME


# --- serve -------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    # The server resolves its own root from the environment, so a `--root`
    # passed here has to reach it the same way an operator-set one would.
    os.environ["AST_MCP_ROOT"] = str(root)
    from ast_mcp.main import mcp

    mcp.run(transport="stdio")
    return 0


# --- index -------------------------------------------------------------------


def _counts(index: Index) -> dict[str, int]:
    return {
        table: index.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in _COUNT_TABLES
    }


def _by_group(index: Index) -> list[tuple[str, int]]:
    rows = index.conn.execute(
        "SELECT grp, count(*) AS n FROM files GROUP BY grp ORDER BY n DESC"
    ).fetchall()
    return [(row["grp"], row["n"]) for row in rows]


def cmd_index(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    database = db_path(root)
    if args.rebuild:
        for sidecar in ("", "-wal", "-shm"):
            target = Path(str(database) + sidecar)
            try:
                target.unlink(missing_ok=True)
            except OSError as exc:
                # Windows refuses to unlink an open file, and a running server
                # holds this one. Say so instead of raising at the operator.
                print(f"error: cannot discard {target} ({exc})", file=sys.stderr)
                print(
                    "       another process has the index open — stop it, or "
                    "run `ast-mcp index` without --rebuild",
                    file=sys.stderr,
                )
                return 1

    started = time.monotonic()
    with Index(root) as index:
        errors = index.refresh_all()
        counts = _counts(index)
        groups = _by_group(index)
    elapsed = time.monotonic() - started

    if args.quiet:
        return 0

    verb = "rebuilt" if args.rebuild else "indexed"
    print(f"{verb} {root}")
    print(
        f"  {counts['files']} files · {counts['symbols']} symbols · "
        f"{counts['schema_nodes']} schema nodes · {counts['doc_nodes']} doc nodes "
        f"· {counts['imports']} imports"
    )
    if groups:
        print("  " + "  ".join(f"{name}={n}" for name, n in groups))
    print(f"  {elapsed:.2f}s · {_human_bytes(_db_size(database))} at {database}")
    _report_errors(errors, verbose=args.verbose)
    return 0


def _report_errors(errors, *, verbose: bool) -> None:
    if not errors:
        return
    tally = Counter(error.code for error in errors)
    summary = ", ".join(f"{code}={n}" for code, n in tally.most_common())
    print(f"  skipped {len(errors)}: {summary}", file=sys.stderr)
    if verbose:
        for error in errors:
            detail = f" — {error.detail}" if error.detail else ""
            print(f"    {error.code} {error.path}{detail}", file=sys.stderr)


# --- status ------------------------------------------------------------------


def _db_size(database: Path) -> int:
    total = 0
    for sidecar in ("", "-wal", "-shm"):
        path = Path(str(database) + sidecar)
        if path.exists():
            total += path.stat().st_size
    return total


def _human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


def cmd_status(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    database = db_path(root)
    if not database.exists():
        payload = {"root": str(root), "indexed": False, "db": str(database)}
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"root {root}")
            print("  no index — run `ast-mcp index`")
        return 0

    with Index(root) as index:
        counts = _counts(index)
        groups = _by_group(index)
        newest = index.conn.execute("SELECT max(indexed_at) FROM files").fetchone()[0]

    registered = _registered_command(root)
    if args.json:
        print(json.dumps({
            "root": str(root),
            "indexed": True,
            "db": str(database),
            "db_bytes": _db_size(database),
            "last_indexed": newest,
            "counts": counts,
            "groups": dict(groups),
            "mcp_registered": registered,
        }, indent=2))
        return 0

    print(f"root {root}")
    print(
        f"  {counts['files']} files · {counts['symbols']} symbols · "
        f"{counts['schema_nodes']} schema nodes · {counts['doc_nodes']} doc nodes "
        f"· {counts['imports']} imports"
    )
    if groups:
        print("  " + "  ".join(f"{name}={n}" for name, n in groups))
    if newest:
        age = max(0, int(time.time()) - int(newest))
        print(f"  last write {_human_age(age)} ago · {_human_bytes(_db_size(database))}")
    print(f"  .mcp.json: {registered or 'not registered — run `ast-mcp init`'}")
    return 0


def _human_age(seconds: int) -> str:
    for limit, unit, div in ((60, "s", 1), (3600, "m", 60), (86400, "h", 3600)):
        if seconds < limit:
            return f"{seconds // div}{unit}"
    return f"{seconds // 86400}d"


def _registered_command(root: Path) -> str | None:
    """The `ast-mcp` invocation recorded in `<root>/.mcp.json`, if any."""
    try:
        data = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    stanza = (data.get("mcpServers") or {}).get(MCP_SERVER_KEY)
    if not isinstance(stanza, dict):
        return None
    return shlex.join([str(stanza.get("command", "")), *stanza.get("args", [])]).strip()


# --- init --------------------------------------------------------------------


def _uv_cache_roots() -> list[Path]:
    """Directories uv uses for throwaway environments."""
    roots = []
    from_env = os.environ.get("UV_CACHE_DIR")
    if from_env:
        roots.append(Path(from_env))
    home = Path.home()
    roots += [
        home / ".cache" / "uv",                       # linux
        home / "Library" / "Caches" / "uv",           # macos
        home / "AppData" / "Local" / "uv" / "cache",  # windows
    ]
    return roots


def _is_ephemeral(executable: Path) -> bool:
    """True for a console script that will not exist after this process ends.

    `uvx ast-mcp init` runs from a cache-backed environment, so `ast-mcp` is on
    PATH for exactly as long as that command runs. Recording the bare name in
    `.mcp.json` from there would register a launcher that is already gone by
    the time Claude Code starts.
    """
    resolved = executable.resolve()
    if any(part.startswith(("archive-v", "environments-v")) for part in resolved.parts):
        return True
    for root in _uv_cache_roots():
        # Both sides have to be resolved: macOS puts temp dirs behind the
        # /var -> /private/var symlink and Windows hands back 8.3 short names,
        # so an unresolved root never matches a resolved executable.
        try:
            candidate = root.expanduser().resolve()
        except OSError:
            continue
        if resolved.is_relative_to(candidate):
            return True
    return False


def server_invocation(override: str | None = None) -> tuple[str, list[str]]:
    """How `.mcp.json` should spawn the server.

    Prefers the installed console script — a `uv tool install` puts `ast-mcp`
    on PATH and that is the cheapest launch. Falls back to `uvx`, which needs
    no install at all. Neither form hardcodes an absolute path, so the written
    stanza stays committable and portable across machines.
    """
    if override:
        parts = shlex.split(override)
        if not parts:
            raise ValueError("--command is empty")
        return parts[0], parts[1:]
    found = shutil.which(MCP_SERVER_KEY)
    if found and not _is_ephemeral(Path(found)):
        return MCP_SERVER_KEY, ["serve"]
    return "uvx", ["ast-mcp", "serve"]


def _update_gitignore(root: Path, *, dry_run: bool) -> str | None:
    """Add the index directory to `.gitignore`. Returns a status line."""
    path = root / ".gitignore"
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError as exc:
        return f".gitignore unreadable ({exc}) — add `{GITIGNORE_ENTRY}` yourself"
    if any(line.strip() in (GITIGNORE_ENTRY, DB_DIRNAME) for line in existing.splitlines()):
        return None
    if dry_run:
        return f"would add `{GITIGNORE_ENTRY}` to .gitignore"
    prefix = "" if existing == "" or existing.endswith("\n") else "\n"
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{prefix}\n# AST_MCP symbol index\n{GITIGNORE_ENTRY}\n")
    except OSError as exc:
        return f".gitignore not writable ({exc}) — add `{GITIGNORE_ENTRY}` yourself"
    return f"added `{GITIGNORE_ENTRY}` to .gitignore"


def cmd_init(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    try:
        command, command_args = server_invocation(args.command)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    mcp_path = root / ".mcp.json"
    if mcp_path.exists():
        try:
            data = json.loads(mcp_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # Never clobber a file we cannot parse — a bad merge here silently
            # unregisters every other server the user depends on.
            print(f"error: {mcp_path} is not readable JSON ({exc})", file=sys.stderr)
            print("       fix or move it, then re-run `ast-mcp init`", file=sys.stderr)
            return 1
        if not isinstance(data, dict):
            print(f"error: {mcp_path} is not a JSON object", file=sys.stderr)
            return 1
    else:
        data = {}

    servers = data.get("mcpServers")
    if servers is None:
        servers = data["mcpServers"] = {}
    elif not isinstance(servers, dict):
        print(f"error: {mcp_path} has a non-object `mcpServers`", file=sys.stderr)
        return 1

    stanza = {"command": command, "args": command_args}
    previous = servers.get(MCP_SERVER_KEY)
    servers[MCP_SERVER_KEY] = stanza
    rendered = json.dumps(data, indent=2) + "\n"

    if args.dry_run:
        print(f"would write {mcp_path}:")
        print(rendered, end="")
    else:
        mcp_path.write_text(rendered, encoding="utf-8")

    verb = "would register" if args.dry_run else (
        "unchanged" if previous == stanza else
        "updated" if previous is not None else "registered"
    )
    print(f"{verb} `{MCP_SERVER_KEY}` in {mcp_path}")
    print(f"  {shlex.join([command, *command_args])}")
    others = [name for name in servers if name != MCP_SERVER_KEY]
    if others:
        print(f"  left alone: {', '.join(sorted(others))}")

    note = _update_gitignore(root, dry_run=args.dry_run)
    if note:
        print(f"  {note}")

    if args.no_index:
        print("  index skipped — run `ast-mcp index` before the first session")
    else:
        cmd_index(argparse.Namespace(
            root=str(root), rebuild=False, quiet=False, verbose=False
        ))

    print("  Claude Code picks this up on its next start in this directory.")
    return 0


# --- languages ---------------------------------------------------------------


def cmd_languages(args: argparse.Namespace) -> int:
    rows = [spec for spec in LANGS if args.group is None or spec.group == args.group]
    if args.json:
        print(json.dumps([{
            "lang": spec.lang,
            "group": spec.group,
            "profile": spec.profile,
            "extensions": list(spec.exts),
            "filenames": list(spec.filenames),
        } for spec in rows], indent=2))
        return 0

    width = max((len(spec.lang) for spec in rows), default=4)
    print(f"{'lang'.ljust(width)}  {'group':<9}  {'profile':<8}  matches")
    for spec in sorted(rows, key=lambda s: (GROUPS.index(s.group), s.lang)):
        matches = ", ".join([*spec.exts, *spec.filenames])
        print(f"{spec.lang.ljust(width)}  {spec.group:<9}  {spec.profile:<8}  {matches}")
    print(f"\n{len(rows)} languages · profiles: symbols, defs, schema, outline")
    return 0


# --- wiring ------------------------------------------------------------------


def _add_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--root", default=None,
        help="repository root (default: $AST_MCP_ROOT, else the working directory)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ast-mcp",
        description="Structural code retrieval over MCP — symbols instead of files.",
    )
    parser.add_argument("-V", "--version", action="version", version=f"ast-mcp {__version__}")
    subparsers = parser.add_subparsers(dest="command_name")

    serve = subparsers.add_parser("serve", help="run the MCP server over stdio")
    _add_root(serve)
    serve.set_defaults(func=cmd_serve)

    init = subparsers.add_parser(
        "init", help="register the server in .mcp.json and build the index"
    )
    _add_root(init)
    init.add_argument(
        "--command", default=None,
        help="override how .mcp.json launches the server, e.g. \"uv run ast-mcp serve\"",
    )
    init.add_argument("--no-index", action="store_true", help="register only, skip indexing")
    init.add_argument("--dry-run", action="store_true", help="print changes, write nothing")
    init.set_defaults(func=cmd_init)

    index = subparsers.add_parser("index", help="build or refresh the symbol index")
    _add_root(index)
    index.add_argument("--rebuild", action="store_true", help="discard the index first")
    index.add_argument("--verbose", action="store_true", help="list every skipped file")
    index.add_argument("--quiet", action="store_true", help="print nothing on success")
    index.set_defaults(func=cmd_index)

    status = subparsers.add_parser("status", help="index size, freshness, registration")
    _add_root(status)
    status.add_argument("--json", action="store_true", help="machine-readable output")
    status.set_defaults(func=cmd_status)

    languages = subparsers.add_parser("languages", help="the language registry")
    languages.add_argument("--group", choices=GROUPS, default=None)
    languages.add_argument("--json", action="store_true", help="machine-readable output")
    languages.set_defaults(func=cmd_languages)

    return parser


def normalise(argv: list[str]) -> list[str]:
    """Default to `serve` so the pre-CLI invocation keeps working."""
    if not argv:
        return ["serve"]
    if argv[0].startswith("-") and argv[0] not in ("-h", "--help", "-V", "--version"):
        return ["serve", *argv]
    return argv


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(normalise(list(sys.argv[1:] if argv is None else argv)))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
