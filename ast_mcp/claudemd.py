"""`CLAUDE.md` routing block — one block that directs every retriever present.

CCE writes its own instructions into `CLAUDE.md` and they order
`context_search` for work this server answers better. Rather than fight for
the slot, `init` writes a single block that routes both, inside CCE's own
delimiters and stamped with the version tag CCE last used. CCE's
`_ensure_claude_md` tests that tag for equality and returns early on a match,
so the block survives a later `cce init`.
"""

from __future__ import annotations

from pathlib import Path

CLAUDE_MD = "CLAUDE.md"

#: CCE's delimiters, reused verbatim. The tag value is never invented — only
#: ever copied from whatever CCE already stamped.
CCE_TAG_PREFIX = "<!-- cce-block-version: "
CCE_END_MARKER = "<!-- /cce-block -->"

#: Ours, nested inside CCE's. Presence inside a CCE span is how `status` tells
#: a block we wrote from one CCE has since replaced.
AST_MARKER = "<!-- ast-mcp-routing: 1 -->"
AST_END_MARKER = "<!-- /ast-mcp-routing -->"

_ROUTING = """\
## Code retrieval

Route on what you can name.

- **Name it** — a symbol, key path, or heading: the `ast-mcp` tools.
  `file_outline` for a file's shape, `get_symbol` for one definition,
  `search_symbols` to find a name across the repo, `list_imports`,
  `get_docstrings`, `ast_query` as the escape hatch. Every response declares
  a `profile`: `symbols` for code, `schema` for JSON/YAML/TOML/XML/CSV,
  `outline` for Markdown/HTML. Read the profile before the payload.
{describe}- **Read it** — a path you are about to edit, or content you need
  verbatim: `Read`. No retriever replaces it.

Do not `cat`, `grep -r`, or `sed -n` a file these tools cover, and do not read
a whole file to answer a question about its shape. A tool that reports a size
cap or finds nothing is an answer — say so rather than falling back to a bulk
read.
"""

_DESCRIBE = """\
- **Describe it** — behaviour you can characterise but not name:
  `context_search`. Its hits carry names; hand those to `get_symbol` for the
  exact definition.
"""

_MEMORY = """\

## Project memory (CCE)

- `session_recall("<topic phrase>")` before answering a design question or a
  "why did we" — the decision may already be recorded. Pass a phrase, not a
  single word; matching is by vector similarity.
- `record_decision(decision=..., reason=...)` after a choice you would not
  want re-litigated next session.
- `record_code_area(file_path=..., description=...)` after substantive work in
  a file.

Skip all three for trivial lookups. `session_timeline(session_id=...)` and
`session_event(event_id=...)` drill into a recall hit.
"""


def render(tag: str | None, *, with_cce: bool) -> str:
    """The block as written to disk, wrapped in whichever markers apply."""
    body = _ROUTING.format(describe=_DESCRIBE if with_cce else "")
    if with_cce:
        body += _MEMORY
    if tag is None:
        return f"{AST_MARKER}\n{body}{AST_END_MARKER}\n"
    return (
        f"{CCE_TAG_PREFIX}{tag} -->\n{AST_MARKER}\n{body}{AST_END_MARKER}\n"
        f"{CCE_END_MARKER}\n"
    )


def _span(text: str, start_marker: str, end_marker: str) -> tuple[int, int] | None:
    start = text.find(start_marker)
    if start == -1:
        return None
    end = text.find(end_marker, start)
    if end == -1:
        return None
    return start, end + len(end_marker)


def cce_tag(text: str) -> str | None:
    """The version value CCE stamped, or None when it has not written here."""
    found = _span(text, CCE_TAG_PREFIX, CCE_END_MARKER)
    if found is None:
        return None
    close = text.find("-->", found[0])
    if close == -1 or close > found[1]:
        return None
    return text[found[0] + len(CCE_TAG_PREFIX):close].strip() or None


def update(root: Path, *, with_cce: bool, dry_run: bool) -> str:
    """Write the routing block into `<root>/CLAUDE.md`. Returns a status line."""
    path = root / CLAUDE_MD
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else None
    except OSError as exc:
        return f"{CLAUDE_MD} unreadable ({exc}) — left alone"

    if existing is None:
        block = render(None, with_cce=with_cce)
        verb = "would create" if dry_run else "created"
        detail = f"{verb} {CLAUDE_MD} with the routing block"
    else:
        tag = cce_tag(existing)
        block = render(tag, with_cce=with_cce)
        target = _span(existing, CCE_TAG_PREFIX, CCE_END_MARKER) if tag else None
        if target is None:
            target = _span(existing, AST_MARKER, AST_END_MARKER)
            replacing = "own block" if target else None
        else:
            replacing = f"CCE block (version {tag}, tag kept)"
        if target is None:
            head = existing.rstrip()
            new_text = f"{head}\n\n{block}" if head else block
            verb = "would append" if dry_run else "appended"
            detail = f"{verb} the routing block to {CLAUDE_MD}"
        else:
            start, end = target
            new_text = existing[:start] + block.rstrip() + existing[end:]
            if new_text == existing:
                return f"{CLAUDE_MD} routing block unchanged"
            verb = "would replace" if dry_run else "replaced"
            detail = f"{verb} the {replacing} in {CLAUDE_MD}"
        block = new_text

    if not dry_run:
        try:
            path.write_text(block, encoding="utf-8")
        except OSError as exc:
            return f"{CLAUDE_MD} not writable ({exc}) — left alone"
    return detail


def drift(root: Path) -> str | None:
    """Whether CCE has since rewritten a block we own."""
    path = root / CLAUDE_MD
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError:
        return None
    if AST_MARKER in existing:
        return None
    if cce_tag(existing) is None:
        return None
    return "CLAUDE.md carries CCE's own block — `ast-mcp init` unifies it"
