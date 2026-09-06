# AST_MCP

Structural code retrieval over MCP. Parses source with tree-sitter and serves
**symbols** instead of files, so an agent asking "what does `parse_config` do"
gets 40 lines rather than 2000.

Read-only. The server never writes to your source — the only file it writes is
its own index at `.ast_mcp/index.db`.

## Install

```bash
uv tool install ast-mcp
```

Or run it without installing anything:

```bash
uvx ast-mcp --version
```

Requires Python 3.11 or newer.

## Wire it into Claude Code

From the repository you want indexed:

```bash
ast-mcp init
```

That writes an `ast-mcp` stanza into `<repo>/.mcp.json`, adds `.ast_mcp/` to
`.gitignore`, writes a short retrieval-routing block into `CLAUDE.md`, and
builds the index. Claude Code reads `.mcp.json` at startup, so the next
session in that directory has the tools already connected — no per-session
step.

The `CLAUDE.md` block is what makes the agent actually reach for the tools;
registration alone tends to leave them idle. It is about thirty lines and says
one thing: name a symbol, key path or heading → these tools; describe
behaviour you cannot name → your semantic retriever; about to edit a file, or
need it byte-for-byte → `Read`. `--no-claude-md` skips it.

`init` merges. Every other server in `.mcp.json` is left exactly as it was,
and a file it cannot parse is reported rather than overwritten. Re-running it
is a no-op. Pass `--dry-run` to see the change first.

The stanza it writes carries no absolute paths, so it is safe to commit:

```json
{
  "mcpServers": {
    "ast-mcp": { "command": "ast-mcp", "args": ["serve"] }
  }
}
```

If `ast-mcp` is not permanently on `PATH` it records `uvx ast-mcp serve`
instead. Override either with `--command "uv run ast-mcp serve"`.

## CLI

| command | what it does |
|---|---|
| `ast-mcp init` | register in `.mcp.json`, write the `CLAUDE.md` routing block, ignore the index dir, build the index |
| `ast-mcp index [--rebuild]` | build or refresh the index; `--rebuild` discards it first |
| `ast-mcp status [--json]` | file/symbol counts, index size, freshness, registration |
| `ast-mcp savings [--json] [--reset]` | tokens served vs. what reading those files whole would have cost |
| `ast-mcp languages [--group G]` | the language registry — 26 rows, their extensions and profiles |
| `ast-mcp serve` | the MCP server over stdio; what Claude Code launches |

Every command takes `--root PATH`. Root resolution is `--root`, else
`AST_MCP_ROOT`, else the working directory. Bare `ast-mcp` means `ast-mcp
serve`.

Only `init` writes anything outside `.ast_mcp/`, and only `.mcp.json`,
`.gitignore` and `CLAUDE.md`. The server itself never writes to your source.

## Not every language gets the same treatment

A `.py` file has functions with signatures and docstrings. A
`docker-compose.yml` has none of that — it has a *shape*. A `README.md` has a
heading tree. Forcing all three through one symbol model produces garbage for
two of them, so there are four **extraction profiles**:

| profile | payload key | what you get | languages |
|---|---|---|---|
| `symbols` | `symbols` | signature, docstring, nesting, imports | python, javascript, typescript, tsx, go, lua |
| `defs` | `symbols` | same shape, weaker guarantees — docstrings often `null` | ruby, perl, r, bash, zsh, css, scss, sql, graphql, proto, terraform, dockerfile |
| `schema` | `schema` | key paths + inferred value types, **not** funcdefs | json, json5, yaml, toml, xml, csv |
| `outline` | `outline` | heading / section tree | markdown, html |

26 languages across six groups: `core`, `web`, `scripting`, `data`, `devops`,
`docs`. Every response declares its `profile` and `group` **before** the
payload — read that field, don't assume `symbols` exists.

Kinds never lie about fidelity. A `schema` node's kind is a value type
(`object`, `array`, `string`); an `outline` node's kind is a document structure
(`heading`, `code_block`). Nothing outside the `symbols`/`defs` profiles ever
claims to be a function or a class.

## Tools

| tool | use it for |
|---|---|
| `file_outline(path, max_depth, include_docstrings)` | the `Read` replacement — a file's shape, bodies elided |
| `get_symbol(name, path, mode)` | one definition. `name` is a symbol name, a key path (`services.web.ports`), or a heading slug |
| `search_symbols(query, kind, lang, group, path_glob, limit)` | find things by name across the repo |
| `get_docstrings(path \| symbols)` | docs without bodies |
| `list_imports(path)` | dependency edges out of a file, plus exports where the language has them |
| `ast_query(path, query, captures)` | raw tree-sitter S-expression — the escape hatch |

Every tool takes `max_tokens` (default 4000). An over-budget response is
trimmed, flagged `truncated: true`, and tells you which argument narrows it.
Nothing is dropped silently.

`get_symbol` **never guesses**. A name matching several symbols returns
`ambiguous: true` with candidates; pass `path` or a qualified name to resolve.

## Alongside a semantic retriever (optional)

AST_MCP stands alone. Nothing it tells the agent assumes another retrieval
tool is registered, and a repo running only this server is a supported setup.

If you also run CCE, `--with-cce` adds one paragraph to the server's
instructions so the agent knows how to split the work:

```bash
ast-mcp init --with-cce      # records the flag in .mcp.json
```

- **`context_search` (CCE)** — fuzzy semantic retrieval over embedded chunks.
  For *"how does auth work?"*, *"where is rate limiting handled?"* — you know
  the concept but not the name.
- **AST_MCP** — exact structural retrieval by name, kind and range. For
  *"show me `TokenStore.refresh`"*, *"what's in this config file?"*, *"list
  every `CREATE TABLE` in the repo"* — you know the name but not the location.

Rough rule: describing behaviour → `context_search`. Naming a thing →
AST_MCP. A `context_search` hit hands you a name; `get_symbol` turns it into
the exact definition.

Without the flag nothing changes and no CCE mention reaches the agent. Plain
`ast-mcp init` prints a hint if it notices `context-engine` in `.mcp.json`;
it never switches modes for you.

### One block, not two

CCE writes its own `CLAUDE.md` instructions, and they say to use
`context_search` instead of reading files — with no mention of this server, so
a named symbol gets routed to the semantic retriever too. Two blocks means two
contradictory rules, and the loud one wins.

So `init` replaces that block rather than adding a second one. It reuses CCE's
own `<!-- cce-block-version: N -->` markers and keeps the value of `N` it finds
on disk. CCE decides whether to rewrite by testing for its current tag, so a
later `cce init` sees a match and leaves the file alone. Run `cce init` first,
then `ast-mcp init`.

If CCE later bumps that version and reclaims the slot, `ast-mcp status` says
so — re-run `ast-mcp init` to take it back. Nothing outside the marked block
is touched, at any point.

## What it costs

The saving grows with file size — the response envelope is fixed cost, so
small files benefit least. Measured on this codebase:

| file | lines | full read | outline | saving |
|---|---|---|---|---|
| `ast_mcp/render.py` | 94 | 789 tok | 352 tok | 2.2x |
| `ast_mcp/index.py` | 428 | 3994 tok | 967 tok | 4.1x |
| `ast_mcp/tools.py` | 457 | 3882 tok | 690 tok | 5.6x |

Below roughly 100 lines it is about break-even against `Read`. Above that it
pays, and `get_symbol` on a single definition pays regardless.

A 5000-row CSV or JSON array costs the same as a 3-row one: homogeneous
repeats collapse to one node carrying `children_count`.

### Measure it on your own repo

Every tool response is logged to `.ast_mcp/savings.db` — what it served, and
what a whole-file read of every file it cited would have cost.

```console
$ ast-mcp savings
root /home/you/project · 5 queries · last query 2m ago

  ▰▰▰▰▰▰▰▰▰▱  90% of a whole-file read saved

  whole-file reads     38.5k tokens
  served by ast-mcp     3.9k tokens
  ──────────────────────────────────────
  saved                34.6k tokens   $0.52
  ~6.9k tokens / query  ~$0.10 / query

  by tool:
    search_symbols   62%  ▰▰▰▰▰▰▱▱▱▱    21.5k   $0.32 · 1 call
    file_outline     21%  ▰▰▱▱▱▱▱▱▱▱     7.3k   $0.11 · 2 calls
```

`--json` for the same numbers machine-readably, `--reset` to start the count
over. Pricing is Opus input ($15/1M); set `AST_MCP_PRICE_PER_MTOK` for another
model, or `AST_MCP_NO_STATS=1` to record nothing at all.

## Freshness

The SQLite index is a cache, never an oracle. Every path in a response was
`stat`-checked against its recorded `(mtime_ns, size)` during that same call,
and reparsed if it moved. There is no watcher daemon and no stale window.

## Development

```bash
git clone https://github.com/matthew-brough/AST_MCP && cd AST_MCP
uv sync --all-groups
uv run --group dev pytest -q
```

The suite runs on 3.11 through 3.14. `uv build` produces the wheel; the
`.scm` query files ship inside it, and CI fails the build if any are missing.

`SPEC.md` is the contract: §V invariants, §I interfaces, §T tasks, §B the log
of what went wrong and what changed because of it.
