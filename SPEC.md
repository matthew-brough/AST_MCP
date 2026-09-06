# SPEC — AST_MCP

Structural code retrieval MCP server. tree-sitter AST → symbol-level queries
so agents stop reading whole files.

## Legend

| sec | meaning |
|---|---|
| §G | goal. why this exists. |
| §V | invariants. must hold always. `check` verifies. |
| §I | interfaces. public shape. tools, schema, config. |
| §T | tasks. status cell: `.` pending, `~` in progress, `x` done. |
| §B | backprop. failures + invariant added to stop recurrence. |

---

## §G — goal

Agent asks "what does `parse_config` do". Today: `Read` whole file, 2000 lines,
~25k tokens, 1 function needed. AST_MCP: `get_symbol("parse_config")` → 40
lines, ~500 tokens.

Server parses source w/ tree-sitter, extracts symbols (function, method,
class, struct, interface, type, const, var), their signatures, docstrings,
imports, byte rangbefes. Persists to SQLite. Exposes 6 read-only MCP tools.

**Success metric**: tokens-per-answered-question vs `Read` baseline.
Target: file outline ≈ N/10 tokens for N-line file. `get_symbol` costs the
symbol's own length, nothing more.

**Complement to CCE, not replacement.** CCE `context_search` = fuzzy semantic
retrieval over embedded chunks; good for "how does auth work". AST_MCP =
exact structural retrieval by name/kind/range; good for "show me
`TokenStore.refresh`". Different failure modes. Both stay.

Read-only v1. Server never mutates user files. Agent still uses Edit/Write.

**Not every language deserves the same treatment.** A `.py` file has functions
with signatures and docstrings. A `docker-compose.yml` has none of that — it
has a *shape*: key paths and types. A `README.md` has a heading tree. Forcing
all three through one symbol model produces garbage for two of them.

So: **4 extraction profiles**. `symbols` (full fidelity), `defs` (names +
ranges, thin or absent doc conventions), `schema` (key paths + inferred types,
no funcdefs), `outline` (document/heading tree). Profile is declared in every
response — agent never guesses fidelity (§V.12).

### Locked decisions

| topic | choice |
|---|---|
| languages | 4 extraction profiles across 6 groups. see I.langs. 26 languages total |
| grammars | 6 core langs = dedicated packages. all others = `tree-sitter-language-pack` |
| index | SQLite, mtime_ns + size invalidation |
| tools | 6, core retrieval set |
| writes | none. read-only |
| transport | stdio |
| distribution | PyPI wheel, `uv tool install ast-mcp`. console script `ast-mcp` |

### Non-goals v1

- No structural edits (`replace_symbol_body` etc). Deferred.
- No call graph / references / import graph. Name-based resolution without a
  type checker is heuristic; not shipping false positives in v1.
- No embeddings, no ranking model. CCE owns semantic.
- No file watcher daemon. Invalidation is pull-based per §V.3.
- No LSP. Grammar-only, no type inference.
- No CSV row data. `schema` profile returns columns + row count, never rows.
  Reading data is not this server's job.
- No inline markdown parsing. Headings/blocks/tables only; `markdown_inline`
  grammar stays unloaded.
- No POD (perl), no reStructuredText, no javadoc-style HTML rendering. Doc
  text is returned verbatim (§V.7); the agent reads it.
- `make`/`Makefile` excluded — pack grammar is ABI 13. Revisit if it bumps.

---

## §V — invariants

**V1 budget** — every tool takes `max_tokens` (default 4000). Response over
budget truncates, sets `truncated: true`, names the arg that narrows it
(e.g. `"narrow with: kind, path_glob"`). Never silently drop rows. Budget is
checked on the *assembled* payload, not just its rows: hint + errors + keys
cost tokens too. Envelope is the floor — a payload cannot shrink below its own
required fields (§V.5), so a tiny `max_tokens` yields empty rows,
`truncated: true`, and a response at that floor.

**V2 bytes are truth** — every source slice is `content_bytes[start:end]`
then utf-8 decode. No line-join reconstruction. Breaks otherwise on CRLF and
non-BMP chars. Line numbers 1-based, derived from byte offsets, never
authoritative.

**V3 no stale reads** — every file path appearing in a response was
`os.stat`-checked against `files.mtime_ns` + `files.size` during that same
call. Mismatch → reparse + upsert before answering. Index is a cache, never
an oracle.

**V4 read-only** — the *server* process opens no user file in a write mode.
Only writable path is `<root>/.ast_mcp/index.db` (+ SQLite sidecars). One
scoped exception, outside the server: the CLI's `init` (§I.cli) writes
`<root>/.mcp.json` and `<root>/.gitignore` — config, never source, never
during `serve`. No other subcommand writes anything but the index.

**V5 degrade, never raise** — missing grammar, syntax error, unreadable file,
bad query → valid payload with `errors: [{code, path, detail}]` and whatever
symbols survived. Tool never propagates exception to agent. `ERROR` nodes in
tree do not abort extraction.

**V6 one registry** — language selected by extension via single `LANGS` table
in `ast_mcp/languages.py`. Zero per-language `if`/`match` branches in tool
bodies. Adding a language = one table row + one `.scm` file.

**V7 docstrings verbatim-or-null** — extracted text is the exact source of the
comment/string node. Never synthesized, never summarized, never the function
body. Absent → `null`, not `""`.

**V8 queries live in .scm** — zero tree-sitter S-expression string literals in
`.py` files. All patterns in `ast_mcp/queries/<lang>.scm`. Two exceptions:
`ast_query`, which takes user S-expr as an argument; and the node-type name
maps used by `schema`/`outline` profiles (`NODE_KINDS` tables) — those are
plain string tables, not patterns, and stay in `.py`.

**V9 ambiguity reported never resolved; multiplicity answered** —
`get_symbol` w/ name matching >1 symbol of *differing* `(name, kind)` returns
`{ambiguous: true, candidates: [...]}` w/ qualified names + locations.
Never picks one. Matches sharing one `(name, kind)` are NOT ambiguous
— they are N real definitions (Lua binds many listeners to one event name) — so
ALL come back in `symbols`, bodies while budget allows, overflow named in
`candidates`. `line` addresses one by its reported `start_line`; two handlers
in one file are identical under `path`, so `line` is their only address.
Confidently-wrong slice is worst failure mode this server has; silently-dropped
sibling is second.

**V10 no git dependency** — repo walk never shells to `git`. Honors
`.gitignore` if present, plus a hardcoded ignore set. Working tree may not be
a repo.

**V11 repeats collapse** — `schema` profile never emits one node per element of
a homogeneous array, nor one node per CSV row. Emits a single node for the
collection w/ `children_count` + the shape of element 0. A 5000-row JSON array
costs the same tokens as a 3-row one. Depth capped by `max_depth` (default 4);
deeper subtrees emit a leaf w/ `kind` and `truncated_subtree: true`.

**V12 profile is declared** — every tool response carries `profile` and `group`.
Agent must not assume a `symbols` key exists; `schema` profile fills `schema`,
`outline` profile fills `outline`. Payload key is a function of profile, and
profile is always visible before the payload is read.

**V13 no fake symbols** — non-`symbols` profiles never emit
`kind: "function" | "class" | "method"` for things that are not. `schema` kinds
are value types (`object`, `array`, `string`, ...). `outline` kinds are document
structures (`heading`, `code_block`, ...). Fidelity is never overstated to make
output look uniform.

---

## §I — interfaces

### I.tools — 6 MCP tools, all read-only

All tools: `max_tokens: int = 4000`. All responses carry
`errors: list[Error]` (empty on success) and `truncated: bool`.

---

`file_outline(path: str, max_depth: int | None = None, include_docstrings: bool = False, max_tokens: int = 4000)`

The `Read` replacement. Payload key depends on profile (§V.12).

```
-> { path, lang, group, profile, line_count,
     symbols?: [ { name, qualified_name, kind, signature,
                   start_line, end_line, docstring?, children: [...] } ],
     schema?:  [ { key_path, kind, value_preview, children_count,
                   start_line, end_line, comment?, children: [...] } ],
     outline?: [ { title, slug, level, kind,
                   start_line, end_line, children: [...] } ],
     truncated, errors }
```

Exactly one of `symbols` / `schema` / `outline` present, per I.profiles.
`max_depth` defaults to 2 for `symbols`/`defs`/`outline`, 4 for `schema`.
`signature` is the declaration header w/ params and return type, body excluded.

---

`get_symbol(name: str, path: str | None = None, line: int | None = None, mode: "source" | "signature" | "doc" = "source", max_tokens: int = 4000)`

```
-> { found: bool, ambiguous: bool, total_matches?: int,
     symbol?:  { name, qualified_name, kind, lang, path,
                 start_line, end_line, start_byte, end_byte,
                 signature, docstring, source? },
     symbols?: [ <same shape>, ... ],
     candidates?: [ { qualified_name, kind, profile, path, start_line } ],
     truncated, errors }
```

`name` accepts bare (`refresh`) or qualified (`TokenStore.refresh`).
`path` narrows to one file; `line` narrows to one definition by `start_line`.
Per §V.9: >1 match differing in `(name, kind)` → `ambiguous: true`
+ `candidates`, `symbol` absent. >1 match sharing one `(name, kind)`
→ `found: true`, `ambiguous: false`, `symbols` + `total_matches`. Exactly one
match → `symbol`, singular, as before.
`mode="source"` includes body; `"signature"` and `"doc"` omit it.

---

`search_symbols(query: str, kind: str | None = None, lang: str | None = None, group: str | None = None, path_glob: str | None = None, limit: int = 20, max_tokens: int = 4000)`

Index lookup. `query` matches `name` / `qualified_name` for symbol-bearing
profiles, `key_path` for `schema`, `title` / `slug` for `outline`
(case-insensitive substring; exact match ranks first, then prefix, then
substring, then shorter path).

`group` filters to one of `core|web|scripting|data|devops|docs`.

```
-> { hits: [ { name, qualified_name, kind, lang, group, profile, path,
               start_line, end_line, signature? } ],
     total_matches: int, truncated, errors }
```

Every hit carries `profile` so mixed-profile results stay unambiguous (§V.12).

`total_matches` may exceed `len(hits)` when `limit`/budget bites.

---

`get_docstrings(path: str | None = None, symbols: list[str] | None = None, max_tokens: int = 4000)`

Docs without bodies. Exactly one of `path` / `symbols` required.

```
-> { docs: [ { qualified_name, kind, path, start_line,
               signature, docstring } ],
     truncated, errors }
```

`docstring` is `null` where absent (§V.7). Symbols w/o docs are still listed —
absence is signal.

---

`list_imports(path: str, max_tokens: int = 4000)`

```
-> { path, lang,
     imports: [ { module, names: [str], alias, line, kind } ],
     exports: [ { name, kind, line } ],
     truncated, errors }
```

`kind` ∈ `import` | `from_import` | `require` | `dynamic`.
`exports` populated only for languages with explicit export syntax
(js/ts/tsx). Empty list elsewhere — never null.

---

`ast_query(path: str, query: str, captures: list[str] | None = None, max_tokens: int = 4000)`

Escape hatch. Raw tree-sitter S-expression against the file's tree.

```
-> { path, lang,
     matches: [ { capture, node_type, start_line, end_line,
                  start_byte, end_byte, text } ],
     truncated, errors }
```

Malformed query → `errors: [{code: "bad_query", detail: <QueryError msg>}]`,
`matches: []`. Never raises (§V.5). `captures` filters which capture names
return.

---

### I.langs — LANGS registry

`ast_mcp/languages.py`. Single source of ext→language routing (§V.6).
26 rows, one per language. Adding a language = one row + one `.scm` (or a
`NODE_KINDS` entry for schema/outline profiles).

```python
@dataclass(frozen=True, slots=True)
class LangSpec:
    lang: str
    exts: tuple[str, ...]
    group: str        # core|web|scripting|data|devops|docs
    profile: str      # symbols|defs|schema|outline
    source: str       # dotted path to grammar fn, or "pack:<name>"
```

#### group `core` — profile `symbols`

Full fidelity. Signature + docstring + parent nesting + import graph.

| lang | exts | source |
|---|---|---|
| python | `.py` `.pyi` | `tree_sitter_python.language()` |
| javascript | `.js` `.mjs` `.cjs` `.jsx` | `tree_sitter_javascript.language()` |
| typescript | `.ts` `.mts` `.cts` | `tree_sitter_typescript.language_typescript()` |
| tsx | `.tsx` | `tree_sitter_typescript.language_tsx()` |
| go | `.go` | `tree_sitter_go.language()` |
| lua | `.lua` | `tree_sitter_lua.language()` |

#### group `scripting` — profile `defs`

Real definitions, weak-to-absent doc conventions. Docstring is best-effort
`preceding_comment_block`, frequently `null`. No import graph guarantee.

| lang | exts | source | extracted |
|---|---|---|---|
| ruby | `.rb` `.rake` `.gemspec` | `pack:ruby` | `class`, `module`, `def`, `singleton_method`, attr_* |
| perl | `.pl` `.pm` `.t` | `pack:perl` | `sub`, `package` |
| r | `.R` `.r` | `pack:r` | `x <- function(...)` assignments, S4 `setClass`/`setGeneric` |
| bash | `.sh` `.bash` | `pack:bash` | `function_definition`, top-level `VAR=` exports |
| zsh | `.zsh` `.zshrc` | `pack:zsh` | same as bash |

#### group `web` — mixed

| lang | exts | source | profile | extracted |
|---|---|---|---|---|
| html | `.html` `.htm` | `pack:html` | `outline` | headings h1-h6, landmarks (`main`/`nav`/`header`/`footer`/`section`/`article`), any element w/ `id` |
| css | `.css` | `pack:css` | `defs` | one entry per `rule_set`; `name` = selector text, `kind` = `rule`. Also `@media`, `@keyframes`, custom props |
| scss | `.scss` | `pack:scss` | `defs` | as css, plus `@mixin`, `@function` |

#### group `data` — profile `schema`

Not funcdefs. Key paths + inferred types. §V.11 collapse applies.

| lang | exts | source | key path form |
|---|---|---|---|
| json | `.json` | `pack:json` | `a.b[].c` |
| json5 | `.json5` | `pack:json5` | `a.b[].c` |
| yaml | `.yaml` `.yml` | `pack:yaml` | `a.b[].c`, multi-document → `[0].a` |
| toml | `.toml` | `pack:toml` | `table.key`, array-of-tables → `table[].key` |
| xml | `.xml` `.xsd` `.svg` | `pack:xml` | `root/child@attr` |
| csv | `.csv` | `pack:csv` | column name; one node per column, never per row |

#### group `data` — profile `defs` (schema *languages*, not schema *files*)

These declare types; they have real named definitions. `defs`, not `schema`.

| lang | exts | source | extracted |
|---|---|---|---|
| sql | `.sql` | `pack:sql` | `CREATE TABLE/VIEW/INDEX/FUNCTION/TRIGGER`; `signature` = column list for tables |
| graphql | `.graphql` `.gql` | `pack:graphql` | `type`, `input`, `enum`, `interface`, `union`, `scalar`, operations |
| proto | `.proto` | `pack:proto` | `message`, `enum`, `service`, `rpc`; `signature` = field list |

#### group `devops` — profile `defs`

| lang | exts | source | extracted |
|---|---|---|---|
| terraform | `.tf` `.tfvars` `.hcl` | `pack:terraform` | `resource`/`data`/`module`/`variable`/`output`/`provider` blocks; `name` = `<type>.<label>` |
| dockerfile | `Dockerfile` `.dockerfile` `Containerfile` | `pack:dockerfile` | stages (`FROM ... AS x`); `imports` = base images + `COPY --from` |

Dockerfile matches by **filename**, not extension — registry supports exact
filename keys alongside extensions.

#### group `docs` — profile `outline`

| lang | exts | source | extracted |
|---|---|---|---|
| markdown | `.md` `.markdown` | `pack:markdown` | ATX+setext headings → nested tree; fenced code blocks w/ info string; tables |

`markdown_inline` grammar is **not** loaded — inline spans are not indexed.

---

#### Loader

```python
def load_language(lang: str) -> Language | None
```

Order: dedicated package (core group) → `tree_sitter_language_pack.get_language(name)`
→ `None`. `None` surfaces as `errors: [{code: "no_grammar"}]`, never an
exception (§V.5). Moving a core lang onto the pack is a `source` string edit.

Unknown extension → `errors: [{code: "unsupported_lang"}]`. Not an error the
agent must handle specially; the payload is still valid (§V.5).

**Grammar availability verified 2026-09-06** under tree-sitter 0.26 — all 26
load and parse clean. See §T.11 for the probe. Pack ABI is 14 or 15 except
`make` (13), which is why make/`Makefile` is *not* in the registry.

---

### I.profiles — what each profile emits

| profile | payload key | record | fields that exist |
|---|---|---|---|
| `symbols` | `symbols` | `Symbol` | name, qualified_name, kind, signature, docstring, parent, ranges |
| `defs` | `symbols` | `Symbol` | same shape; `docstring` often `null`, `parent` often `null` |
| `schema` | `schema` | `SchemaNode` | key_path, kind, value_preview, comment, children_count, ranges |
| `outline` | `outline` | `DocNode` | title, slug, level, kind, parent, ranges |

`defs` reuses the `Symbol` record deliberately — same shape, weaker guarantees,
so `search_symbols` spans core + scripting + devops + sql/graphql/proto in one
index table. `schema` and `outline` get their own tables.

### I.tool_profile — tool behavior per profile

| tool | `symbols` / `defs` | `schema` | `outline` |
|---|---|---|---|
| `file_outline` | nested symbol tree | key-path tree, `max_depth` default 4, §V.11 collapse | heading tree by level |
| `get_symbol` | by name or qualified name | `name` = key path (`services.web.ports`); returns that subtree's source slice | `name` = heading text or slug; returns that section's source slice |
| `search_symbols` | index lookup over symbols | matches key paths; `kind` filter = value type | matches heading titles/slugs |
| `get_docstrings` | docstrings | leading comments on keys (yaml/toml/xml/proto). json/csv → all `null`, not an error | first paragraph under each heading |
| `list_imports` | language imports | `[]` — no import concept | markdown: link + image targets. html: `<script src>`, `<link href>`, `<img src>` |
| `ast_query` | raw S-expr | raw S-expr | raw S-expr |

`ast_query` is profile-independent by design — it is the escape hatch for
anything the profile model flattens away.

`search_symbols` gains a `group` filter arg: `group="data"` restricts to
data-group files. Union queries across profiles return each hit tagged with its
`profile` (§V.12).

---

### I.symbol — record shape

Produced by `ast_mcp/extract.py`, stored by `ast_mcp/index.py`.

```python
@dataclass(frozen=True, slots=True)
class Symbol:
    name: str
    qualified_name: str     # dotted: module.Class.method
    kind: str               # function|method|class|struct|interface|type|const|var|module
    lang: str
    path: str               # repo-relative, posix separators
    start_byte: int
    end_byte: int
    start_line: int         # 1-based
    end_line: int
    signature: str          # decl header, body excluded
    docstring: str | None
    parent: str | None      # qualified_name of enclosing symbol
```

```python
@dataclass(frozen=True, slots=True)
class Import:
    module: str
    names: tuple[str, ...]
    alias: str | None
    line: int
    kind: str               # import|from_import|require|dynamic|base_image|link|module
```

`schema` profile record — `ast_mcp/extract_schema.py`:

```python
@dataclass(frozen=True, slots=True)
class SchemaNode:
    key_path: str           # services.web.ports[] | table[].key | root/child@attr
    kind: str               # object|array|string|number|bool|null|table|element|attr|column
    lang: str
    path: str
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int
    value_preview: str | None    # scalars only, <= 80 chars, elided w/ "…"
    comment: str | None          # leading comment on the key, verbatim (§V.7)
    children_count: int          # array len / object key count / csv row count
    truncated_subtree: bool      # depth cap hit (§V.11)
    parent: str | None           # parent key_path
```

`outline` profile record — `ast_mcp/extract_doc.py`:

```python
@dataclass(frozen=True, slots=True)
class DocNode:
    title: str
    slug: str               # github-style anchor, deduped w/ -1 -2 suffix
    level: int              # h1 = 1; non-heading kinds inherit enclosing level
    kind: str               # heading|code_block|table|landmark|element
    lang: str
    path: str
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int
    info: str | None        # fenced-code info string, or html tag+id
    parent: str | None      # parent slug
```

---

### I.docstring — extraction helper

Docstring rules are per-language and non-uniform. Two named helpers, both in
`extract.py`, both verifiable by `check`:

```python
def preceding_comment_block(node: Node, src: bytes) -> str | None
def python_docstring(body: Node, src: bytes) -> str | None
```

| lang | rule |
|---|---|
| python | `python_docstring`: first `expression_statement > string` in body |
| javascript / typescript / tsx | `preceding_comment_block`: contiguous leading `comment` nodes ending on line before decl; JSDoc `/** */` preferred |
| go | `preceding_comment_block`: contiguous `//` line-comment block directly above decl |
| lua | `preceding_comment_block`: contiguous `---` block (LuaLS annotations) |
| ruby | `preceding_comment_block`: `#` block. YARD `@param` kept verbatim |
| perl | `preceding_comment_block`: `#` block. POD (`=head1`…`=cut`) **not** parsed in v1 → `null` |
| r | `preceding_comment_block`: `#'` roxygen block; falls back to plain `#` |
| bash / zsh | `preceding_comment_block`: `#` block. Shebang line excluded |
| css / scss | `preceding_comment_block`: `/* */` immediately above the rule |
| sql | `preceding_comment_block`: `--` block, or `/* */` |
| graphql | native `description` string (`"""…"""`) if present, else `preceding_comment_block` on `#` |
| proto | `preceding_comment_block`: `//` block |
| terraform | `preceding_comment_block`: `#` or `//` block |
| dockerfile | `preceding_comment_block`: `#` block above the stage's `FROM` |
| yaml / toml / xml / proto (schema) | comment on the key → `SchemaNode.comment`, same walker |
| json / json5 / csv | no comment syntax carried → always `null` |
| markdown / html | not applicable. `get_docstrings` returns the first paragraph under each heading, `kind: "lede"` |

"Contiguous" = no blank line between the comment block's last line and the
declaration's first line. One blank line breaks the association.

Returned text is verbatim node source including comment markers (§V.7).

---

### I.db — SQLite schema

Location `<root>/.ast_mcp/index.db`. Gitignored.

```sql
CREATE TABLE files (
  id         INTEGER PRIMARY KEY,
  path       TEXT NOT NULL UNIQUE,   -- repo-relative, posix
  lang       TEXT NOT NULL,
  grp        TEXT NOT NULL,          -- core|web|scripting|data|devops|docs
  profile    TEXT NOT NULL,          -- symbols|defs|schema|outline
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
  names   TEXT NOT NULL,             -- json array
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
  truncated_subtree INTEGER NOT NULL DEFAULT 0
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

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
```

`grp`, not `group` — `GROUP` is a SQL reserved word.

Upsert is delete-then-insert per file: `DELETE FROM files WHERE path = ?`
cascades symbols + imports + schema_nodes + doc_nodes, then reinsert. No
partial-update path.

Schema version pinned in `PRAGMA user_version`. Mismatch → drop + rebuild,
never migrate in v1.

---

### I.walk — repo walk

`index.py`. No git (§V.10).

Hard ignore set: `.git`, `.venv`, `venv`, `node_modules`, `__pycache__`,
`dist`, `build`, `.mypy_cache`, `.pytest_cache`, `.ast_mcp`, `target`.
Plus `.gitignore` patterns if the file exists.

Skip files > 2 MB → `errors: [{code: "too_large"}]`. Exception: `schema`
profile files up to 16 MB are still indexed — §V.11 collapse means a huge
JSON/CSV costs bounded tokens, and huge data files are exactly where whole-file
`Read` hurts most. Their *source slices* still honor `max_tokens`.

Match order per file: exact filename (`Dockerfile`, `Containerfile`) → longest
extension (`.tar.gz`-style compound, unused today) → extension. Miss → skip.

---

### I.config — server config + registration

Root dir: `--root` CLI arg, else `AST_MCP_ROOT` env, else cwd. Every
subcommand resolves it the same way; `serve` exports the resolved value back
into `AST_MCP_ROOT` so the server picks it up through one path only.

`.mcp.json` stanza, written by `ast-mcp init` alongside whatever servers are
already there:

```json
"ast-mcp": {
  "command": "ast-mcp",
  "args": ["serve"]
}
```

No absolute paths — the file is meant to be committed and to work on a
teammate's machine. When `ast-mcp` is not permanently on `PATH` the stanza
becomes `uvx ast-mcp serve` instead.

Entry: `mcp.run(transport="stdio")`.

Packaging: hatchling, `requires-python >= 3.11`, version read from
`ast_mcp.__version__`. The wheel must carry `ast_mcp/queries/**/*.scm` —
§V.8 puts every pattern in those files, so a wheel without them imports
cleanly and extracts nothing. CI asserts the count.

---

### I.cli — `ast-mcp` command surface

Five subcommands. Bare `ast-mcp`, or `ast-mcp` followed only by flags, means
`serve` — the pre-CLI invocation stays valid.

| command | writes | does |
|---|---|---|
| `serve [--root]` | index only | stdio MCP server. what `.mcp.json` launches |
| `init [--root] [--command CMD] [--no-index] [--dry-run]` | `.mcp.json`, `.gitignore`, index | register + ignore + build |
| `index [--root] [--rebuild] [--verbose] [--quiet]` | index only | `refresh_all`, then counts + skips |
| `status [--root] [--json]` | nothing | counts, size, age, registration |
| `languages [--group G] [--json]` | nothing | the §I.langs registry |

`init` is a **merge, not a template**. It rewrites exactly one key —
`mcpServers["ast-mcp"]` — and leaves every sibling server untouched. A
`.mcp.json` that does not parse as a JSON object is reported and left on disk
unmodified: silently dropping a user's other MCP servers is a worse failure
than refusing to run. Re-running is a no-op and says so.

`init` never records the console script it can see if that script is
ephemeral. `uvx ast-mcp init` runs from a uv cache environment where `ast-mcp`
is on `PATH` for the length of that one command; writing the bare name from
there registers a launcher that is already gone. Detection is by path — a uv
cache root, or an `archive-v*` / `environments-v*` component — and the
fallback is `uvx`.

`status` on an unindexed root reports that and creates no database. Reading
status must not be the thing that writes one.

---

### I.layout — files

```
ast_mcp/
  __init__.py
  __main__.py        `python -m ast_mcp` -> cli.main
  cli.py             argv routing, init/index/status/languages, serve
  main.py            entry, MCPServer wiring, run(transport="stdio")
  languages.py       LangSpec registry (26 rows), load_language + fallback
  parser.py          bytes -> Tree, LRU cache keyed (path, mtime_ns, size)
  extract.py         dispatch on profile; Symbol/Import for symbols+defs
  extract_schema.py  SchemaNode for data group; NODE_KINDS map; §V.11 collapse
  extract_doc.py     DocNode for markdown + html; slug generation
  index.py           SQLite schema, upsert, staleness, walk
  tools.py           6 tool impls, profile dispatch
  render.py          output shaping, max_tokens trimming
  queries/
    core/     python.scm javascript.scm typescript.scm tsx.scm go.scm lua.scm
    defs/     ruby.scm perl.scm r.scm bash.scm zsh.scm
              css.scm scss.scm sql.scm graphql.scm proto.scm
              terraform.scm dockerfile.scm
tests/
  fixtures/
    core/       sample.py sample.js sample.ts sample.tsx sample.go sample.lua
    scripting/  sample.rb sample.pl sample.R sample.sh sample.zsh
    web/        sample.html sample.css sample.scss
    data/       sample.json sample.yaml sample.toml sample.xml sample.csv
                schema.sql schema.graphql schema.proto
    devops/     main.tf Dockerfile
    docs/       sample.md
  test_languages.py test_extract.py test_extract_schema.py
  test_extract_doc.py test_index.py test_tools.py test_cli.py
```

`schema` and `outline` profiles have **no `.scm` files** — they walk the tree
by node type via `NODE_KINDS` tables (§V.8 exception). Grammar node names
differ per language (json `pair`, yaml `block_mapping_pair`, toml `pair`,
xml `element`), so the table maps
`lang -> {container, pair, key, value, comment}`.

Existing `AST_MCP/main.py` (uppercase dir) is a stub — renamed to `ast_mcp/`,
stray `import httpx2` deleted.

---

## §T — tasks

| # | st | task | verify |
|---|---|---|---|
| T1 | x | add 5 grammar deps + language-pack. prove every grammar loads under tree-sitter 0.26 (ABI gate). **blocks all** | gate script below exits 0, prints abi per lang |
| T2 | x | `languages.py` — `LangSpec` table for the 6 **core** rows, `load_language` w/ fallback chain, ext→lang | `test_languages.py`: all 6 core load, unknown ext → None, no exception |
| T3 | x | `parser.py` — read bytes, parse, LRU cache keyed `(path, mtime_ns, size)` | cache hit on repeat, miss after touch |
| T4 | x | `queries/core/*.scm` — tag queries. python first, then js/ts/tsx/go/lua | each `.scm` compiles via `Query(lang, src)` |
| T5 | x | `extract.py` — captures→`Symbol`, `preceding_comment_block`, `python_docstring`, imports | `test_extract.py` golden symbol lists per fixture |
| T6 | x | `index.py` — schema, upsert, staleness check, walk | `test_index.py`: edit fixture → next query reflects it (§V.3) |
| T7 | x | `tools.py` + `render.py` — 6 tools, **profile dispatch** (§V.12), budget trim, error payloads | `test_tools.py`: each tool × each profile, incl. ambiguity (§V.9) + bad query (§V.5) |
| T8 | x | `main.py` — MCPServer wiring, stdio, `.mcp.json` entry | server starts, `tools/list` returns 6 |
| T9 | x | tests — core fixtures, golden outlines, staleness, budget | full suite green |
| T10 | x | `README.md` + agent usage guidance (AST_MCP vs CCE `context_search`, and which profile each group gets) | doc exists, names both, documents 4 profiles |
| T11 | x | probe language-pack for all 20 non-core grammars under ts 0.26 | every name loads + parses w/o `has_error` |
| T12 | x | extend registry to 26 rows: `LangSpec` w/ `group` + `profile`, filename matching for Dockerfile | `test_languages.py`: all 26 resolve, every `source` loads, unknown ext → `None` |
| T13 | x | `queries/defs/*.scm` ×12 — ruby, perl, r, bash, zsh, css, scss, sql, graphql, proto, terraform, dockerfile | each compiles via `Query(lang, src)`; golden symbol list per fixture |
| T14 | x | `extract_schema.py` — `NODE_KINDS` for json/json5/yaml/toml/xml/csv, key-path building, type inference, §V.11 collapse + depth cap | 5000-row array fixture emits 1 array node; token count flat vs 3-row fixture |
| T15 | x | `extract_doc.py` — markdown heading tree + fenced blocks + tables; html headings/landmarks/ids; slug dedup | `test_extract_doc.py`: nesting correct across skipped levels (h1→h3), dup titles get `-1` |
| T16 | x | `cli.py` — 5 subcommands, argv routing, `.mcp.json` merge, `.gitignore` entry, ephemeral-launcher detection | `test_cli.py`: bare argv -> serve, sibling servers survive, unparseable `.mcp.json` refused, rerun idempotent, `uvx` launcher not recorded |
| T17 | x | packaging — hatchling, console script `ast-mcp`, py3.11 floor, wheel carries every `.scm`, LICENSE | `uv build` then handshake the wheel: `tools/list` returns 6, `file_outline` extracts |
| T18 | x | CI + release workflows — test matrix 3.11–3.14, wheel query-file gate, tag/version check, PyPI trusted publishing | workflows present; release job refuses a tag that disagrees with `__version__` |

### T1 gate script

```bash
uv add tree-sitter-python tree-sitter-javascript tree-sitter-typescript \
       tree-sitter-go tree-sitter-lua tree-sitter-language-pack

uv run python - <<'PY'
import tree_sitter_python as tsp, tree_sitter_javascript as tsjs
import tree_sitter_typescript as tsts, tree_sitter_go as tsgo, tree_sitter_lua as tslua
from tree_sitter import Language, Parser
grammars = [
    ("python", tsp.language()),
    ("javascript", tsjs.language()),
    ("typescript", tsts.language_typescript()),
    ("tsx", tsts.language_tsx()),
    ("go", tsgo.language()),
    ("lua", tslua.language()),
]
for name, cap in grammars:
    L = Language(cap)
    root = Parser(L).parse(b"x").root_node
    print(f"{name:12} abi={L.abi_version} root={root.type}")
PY
```

Any grammar failing to load → record in §B, flip that row of I.langs to
`tree_sitter_language_pack.get_language(<lang>)`. Not a redesign.

**Gate result 2026-09-06 — all pass, no fallback needed:**

```
python       OK   abi=15 root=module
javascript   OK   abi=15 root=program
typescript   OK   abi=14 root=program
tsx          OK   abi=14 root=program
go           OK   abi=15 root=source_file
lua          OK   abi=15 root=chunk
```

tree-sitter 0.26 accepts grammar ABI 14 and 15. The `tree-sitter~=0.24` pin in
the grammar packages' `core` extra is advisory only — irrelevant at runtime.
`tree-sitter-language-pack 1.16.2` installed but unused by the 6 first-class
languages; it serves I.langs fallback for anything else.

---

### T11 probe result — 2026-09-06

`tree_sitter_language_pack 1.16.2` under `tree-sitter 0.26.0`. All load, all
parse their smoke sample with `has_error = False`:

```
html abi=14   css abi=14   scss abi=14   ruby abi=14   perl abi=15
r abi=14      bash abi=14  zsh abi=15    json abi=14   json5 abi=14
yaml abi=14   toml abi=14  xml abi=14    sql abi=14    graphql abi=14
proto abi=14  dockerfile abi=14          terraform abi=14
markdown abi=14             csv abi=14
```

Root node types confirmed (needed by `NODE_KINDS`):
`html document`, `css stylesheet`, `ruby program`, `perl source_file`,
`r program`, `bash program`, `json document`, `yaml stream`, `toml document`,
`xml document`, `sql program`, `graphql source_file`, `proto source_file`,
`dockerfile source_file`, `terraform config_file`, `markdown document`,
`csv document`.

Rejected: `make` (grammar ABI 13), `protobuf` and `jsonc` (not in pack
manifest — use `proto` and `json`).

---

## §B — backprop

**B.1 — markdown link targets vs the "no inline parsing" non-goal** (T15).

§I.tool_profile promises `list_imports` returns "markdown: link + image
targets". §G non-goals say `markdown_inline` stays unloaded. The block grammar
does not decompose `[text](target)` — it hands back one opaque `inline` node —
so the two clauses could not both be honoured as written.

Resolved in favour of the non-goal: `markdown_inline` stays unloaded, and link
targets are pulled from the `inline` node's text with one bounded regex
(`extract_doc._MD_LINK`). No second grammar, no inline AST, and `list_imports`
still returns what §I.tool_profile promises.

No new §V invariant — the failure was an internal contradiction in §I, not a
class of bug that can recur silently. Fixed by narrowing the §I wording rather
than by adding a rule.

**B.2 — the §G "N/10 tokens" figure is wrong** (T9).

§G claims a file outline costs about `N/10` tokens for an N-line file. Measured
on real source (`ast_mcp/index.py`, 430 lines): the outline costs ~900 tokens,
roughly `N/0.5`. Signature-heavy code does not compress anywhere near 10:1 —
signatures, names and line numbers are most of the payload and none of them are
optional.

Two outcomes:

1. Real waste found and removed: nested payloads were repeating `parent` on
   every node and emitting `"children": []` on every leaf, both already implied
   by the tree. `render.nest` now drops them — about a fifth of the response on
   a thirty-symbol file.
2. The claim is restated as a measured range rather than a formula, and the
   saving **grows with file size** — the envelope is fixed cost, so small files
   benefit least. Measured on this codebase after the fix above:

   | file | lines | full read | outline | saving |
   |---|---|---|---|---|
   | `ast_mcp/render.py` | 94 | 789 tok | 352 tok | 2.2x |
   | `ast_mcp/extract.py` | 427 | 3906 tok | 1064 tok | 3.7x |
   | `ast_mcp/index.py` | 428 | 3994 tok | 967 tok | 4.1x |
   | `ast_mcp/tools.py` | 457 | 3882 tok | 690 tok | 5.6x |

   `tests/test_golden.py::TestTokenEconomics` pins the floor: a 400+ line file
   outlines for under a quarter of a full read, and a single `get_symbol` costs
   under a third. Below ~100 lines the tool is roughly break-even against
   `Read`, which is the honest answer.

No new §V invariant: §V.1 already governs response size. The defect was an
unmeasured claim in §G, now replaced by an executable one.
