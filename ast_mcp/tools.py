"""The six read-only tools.

Every tool follows the same contract:

* it declares ``profile`` and ``group`` before any payload (SPEC §V.12);
* it revalidates each path it touches against the index (SPEC §V.3);
* it fits the response to ``max_tokens`` and says so when it trims (§V.1);
* it returns a valid payload for every failure it can hit (§V.5).

Language dispatch happens once, in the registry. Nothing here branches on a
language name — only on the *profile* a language was assigned (§V.6).
"""

from __future__ import annotations

from typing import Any

from tree_sitter import Query, QueryCursor

from ast_mcp import Error
from ast_mcp.index import Index
from ast_mcp.languages import load_language
from ast_mcp.parser import ParsedFile
from ast_mcp.render import (
    DEFAULT_MAX_TOKENS,
    clip,
    enforce,
    envelope,
    estimate_tokens,
    fit,
    narrow_hint,
    nest,
    with_errors,
)

#: profile -> (payload key, table, id column, order column)
PROFILE_TABLE = {
    "symbols": ("symbols", "symbols", "qualified_name", "start_byte, end_byte DESC"),
    "defs": ("symbols", "symbols", "qualified_name", "start_byte, end_byte DESC"),
    "schema": ("schema", "schema_nodes", "key_path", "start_byte, end_byte DESC"),
    "outline": ("outline", "doc_nodes", "slug", "start_byte, end_byte DESC"),
}

#: Default nesting depth per profile (SPEC §I.tools).
DEFAULT_DEPTH = {"symbols": 2, "defs": 2, "outline": 2, "schema": 4}


# --- tool 1: file_outline ----------------------------------------------------

def file_outline(
    index: Index,
    path: str,
    max_depth: int | None = None,
    include_docstrings: bool = False,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict:
    """The `Read` replacement: a file's shape, bodies elided."""
    parsed, record, errors, failure = _resolve(index, path)
    if failure is not None:
        return failure

    key, table, id_field, order = PROFILE_TABLE[record.profile]
    depth = max_depth if max_depth is not None else DEFAULT_DEPTH[record.profile]
    rows = index.rows_for_file(table, record.id, order)

    items = [_row_to_item(r, record.profile, include_docstrings) for r in rows]
    items = [
        item for item, own in zip(items, _depths(items, id_field)) if own <= depth
    ]

    payload = envelope(
        path=record.path,
        lang=record.lang,
        group=record.group,
        profile=record.profile,
        line_count=parsed.line_count,
    )
    kept, truncated = fit(items, max_tokens, estimate_tokens(payload))
    payload[key] = nest(kept, id_field, "parent")
    payload["truncated"] = truncated
    with_errors(payload, errors)
    hinted = narrow_hint(payload, "max_depth, include_docstrings")
    return enforce(hinted, (key,), max_tokens)


# --- tool 2: get_symbol ------------------------------------------------------

def get_symbol(
    index: Index,
    name: str,
    path: str | None = None,
    line: int | None = None,
    mode: str = "source",
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict:
    """Definitions by name or key path.

    Ambiguity is reported, multiplicity is answered (§V.9).
    """
    if mode not in {"source", "signature", "doc"}:
        return with_errors(
            envelope(found=False, ambiguous=False),
            [Error("bad_mode", None, f"mode must be source|signature|doc, got {mode}")],
        )

    errors: list[Error] = []
    scope: str | None = None
    if path is not None:
        parsed, record, file_errors, failure = _resolve(index, path)
        if failure is not None:
            return failure
        errors.extend(file_errors)
        scope = record.path
    else:
        errors.extend(index.refresh_all())

    matches = _candidates(index, name, scope)
    if line is not None:
        # The `start_line` a candidate reported. Two handlers bound to one
        # event name in one file are identical under `path`, so a line is the
        # only address that separates them.
        matches = [r for r in matches if r["start_line"] == line]
    payload = envelope(found=False, ambiguous=False)

    if not matches:
        with_errors(payload, [*errors, Error("not_found", scope, name)])
        return payload

    # Grouped on the bare name, not the qualified one: a listener nested inside
    # a function carries its parent as a prefix, and that is a fact about where
    # it sits, not about what the caller asked for.
    if len({(r["name"], r["kind"]) for r in matches}) > 1:
        # SPEC §V.9 — never guess which one they meant.
        payload["ambiguous"] = True
        candidates = [_candidate_fields(r) for r in matches]
        kept, truncated = fit(candidates, max_tokens, estimate_tokens(payload))
        payload["candidates"] = kept
        payload["truncated"] = truncated
        with_errors(payload, errors)
        hinted = narrow_hint(payload, "path, or a fully qualified name")
        return enforce(hinted, ("candidates",), max_tokens)

    # Past this point every match is the same (qualified_name, kind): not an
    # ambiguity but N real definitions. Lua binds many listeners to one event
    # name and each is a genuine answer, so all of them come back — dropping
    # the siblings silently would be its own confidently-wrong slice (§V.9).
    parsed_by_path: dict[str, ParsedFile] = {}
    for row in matches:
        if row["path"] in parsed_by_path:
            continue
        parsed, _record, file_errors, failure = _resolve(index, row["path"])
        if failure is not None:
            return failure
        errors.extend(file_errors)
        parsed_by_path[row["path"]] = parsed

    payload["found"] = True

    if len(matches) == 1:
        row = matches[0]
        symbol = _symbol_fields(row)
        if mode == "source":
            parsed = parsed_by_path[row["path"]]
            source = parsed.slice(row["start_byte"], row["end_byte"])
            budget = max_tokens - estimate_tokens({**payload, "symbol": symbol})
            symbol["source"], payload["truncated"] = clip(source, budget)
        payload["symbol"] = symbol
        with_errors(payload, errors)
        return narrow_hint(payload, 'mode="signature"')

    payload["total_matches"] = len(matches)
    # An even split, so one long definition cannot starve its siblings.
    share = max(1, max_tokens // (len(matches) + 1))
    clipped = False
    entries: list[dict] = []
    for row in matches:
        entry = _symbol_fields(row)
        if mode == "source":
            parsed = parsed_by_path[row["path"]]
            source = parsed.slice(row["start_byte"], row["end_byte"])
            entry["source"], cut = clip(source, share - estimate_tokens(entry))
            clipped = clipped or cut
        entries.append(entry)

    # Whatever does not fit as a body still gets named, so nothing vanishes
    # quietly — which means the naming has to be paid for out of the same
    # budget (§V.1). Take the most bodies that leave room for the rest.
    overflow = [_candidate_fields(r) for r in matches]
    body_cost = [estimate_tokens(e) for e in entries]
    name_cost = [estimate_tokens(c) for c in overflow]
    overhead = estimate_tokens(payload)
    kept_count = 0
    for count in range(len(entries), -1, -1):
        spent = overhead + sum(body_cost[:count]) + sum(name_cost[count:])
        if spent <= max_tokens:
            kept_count = count
            break

    payload["symbols"] = entries[:kept_count]
    payload["truncated"] = clipped or kept_count < len(entries)
    if kept_count < len(entries):
        spent = overhead + sum(body_cost[:kept_count])
        named, dropped = fit(overflow[kept_count:], max_tokens, spent)
        payload["candidates"] = named
        payload["truncated"] = payload["truncated"] or dropped
    with_errors(payload, errors)
    hinted = narrow_hint(payload, 'line, or mode="signature"')
    return enforce(hinted, ("symbols", "candidates"), max_tokens)


# --- tool 3: search_symbols --------------------------------------------------

def search_symbols(
    index: Index,
    query: str,
    kind: str | None = None,
    lang: str | None = None,
    group: str | None = None,
    path_glob: str | None = None,
    limit: int = 20,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict:
    """Ranked lookup across every profile. Each hit declares its own (§V.12)."""
    errors = index.refresh_all()
    rows, total = index.search(
        query, kind=kind, lang=lang, group=group, path_glob=path_glob, limit=limit
    )
    hits = [
        {
            "name": r["name"],
            "qualified_name": r["qualified_name"],
            "kind": r["kind"],
            "lang": r["lang"],
            "group": r["grp"],
            "profile": r["profile"],
            "path": r["path"],
            "start_line": r["start_line"],
            "end_line": r["end_line"],
            "signature": r["signature"],
        }
        for r in rows
    ]
    payload = envelope(total_matches=total)
    kept, truncated = fit(hits, max_tokens, estimate_tokens(payload))
    payload["hits"] = kept
    payload["truncated"] = truncated or len(rows) < total
    with_errors(payload, errors)
    hinted = narrow_hint(payload, "kind, lang, group, path_glob, limit")
    return enforce(hinted, ("hits",), max_tokens)


# --- tool 4: get_docstrings --------------------------------------------------

def get_docstrings(
    index: Index,
    path: str | None = None,
    symbols: list[str] | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict:
    """Docs without bodies. Symbols without docs are still listed."""
    if (path is None) == (symbols is None):
        return with_errors(
            envelope(docs=[]),
            [Error("bad_args", None, "pass exactly one of path or symbols")],
        )

    errors: list[Error] = []
    docs: list[dict] = []
    profile = group = None

    if path is not None:
        parsed, record, file_errors, failure = _resolve(index, path)
        if failure is not None:
            return failure
        errors.extend(file_errors)
        profile, group = record.profile, record.group
        _key, table, _id_field, order = PROFILE_TABLE[record.profile]
        for row in index.rows_for_file(table, record.id, order):
            docs.append(_doc_entry(row, record.profile, record.path))
    else:
        errors.extend(index.refresh_all())
        for wanted in symbols or []:
            found = _candidates(index, wanted, None)
            if not found:
                errors.append(Error("not_found", None, wanted))
                continue
            for row in found:
                docs.append(_doc_entry(row, row["profile"], row["path"]))

    payload = envelope(profile=profile, group=group)
    kept, truncated = fit(docs, max_tokens, estimate_tokens(payload))
    payload["docs"] = kept
    payload["truncated"] = truncated
    with_errors(payload, errors)
    hinted = narrow_hint(payload, "symbols, or a narrower path")
    return enforce(hinted, ("docs",), max_tokens)


# --- tool 5: list_imports ----------------------------------------------------

def list_imports(
    index: Index, path: str, max_tokens: int = DEFAULT_MAX_TOKENS
) -> dict:
    """Dependency edges out of one file. Empty lists, never nulls."""
    import json as _json

    parsed, record, errors, failure = _resolve(index, path)
    if failure is not None:
        return failure

    rows = index.rows_for_file("imports", record.id, "line")
    imports = [
        {
            "module": r["module"],
            "names": _json.loads(r["names"]),
            "alias": r["alias"],
            "line": r["line"],
            "kind": r["kind"],
        }
        for r in rows
    ]
    exports: list[dict] = []
    if record.profile in {"symbols", "defs"}:
        from ast_mcp.extract import extract

        exports = [
            {"name": e.name, "kind": e.kind, "line": e.line}
            for e in extract(parsed).exports
        ]

    payload = envelope(
        path=record.path,
        lang=record.lang,
        group=record.group,
        profile=record.profile,
        exports=exports,
    )
    kept, truncated = fit(imports, max_tokens, estimate_tokens(payload))
    payload["imports"] = kept
    payload["truncated"] = truncated
    with_errors(payload, errors)
    hinted = narrow_hint(payload, "a narrower path")
    return enforce(hinted, ("imports",), max_tokens)


# --- tool 6: ast_query -------------------------------------------------------

def ast_query(
    index: Index,
    path: str,
    query: str,
    captures: list[str] | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict:
    """Raw tree-sitter S-expression. The escape hatch, profile-independent."""
    parsed, record, errors, failure = _resolve(index, path)
    if failure is not None:
        return failure

    payload = envelope(
        path=record.path, lang=record.lang, group=record.group,
        profile=record.profile, matches=[],
    )
    language = load_language(record.lang)
    if language is None:
        return with_errors(payload, [*errors, Error("no_grammar", record.path, record.lang)])

    try:
        compiled = Query(language, query)
        results = QueryCursor(compiled).matches(parsed.tree.root_node)
    except Exception as exc:  # noqa: BLE001 — §V.5, a bad query is data, not a crash
        return with_errors(
            payload,
            [*errors, Error("bad_query", record.path, f"{type(exc).__name__}: {exc}")],
        )

    wanted = set(captures) if captures else None
    matches: list[dict] = []
    for _pattern_index, caps in results:
        for capture_name, nodes in caps.items():
            if wanted is not None and capture_name not in wanted:
                continue
            for node in nodes:
                matches.append({
                    "capture": capture_name,
                    "node_type": node.type,
                    "start_line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                    "start_byte": node.start_byte,
                    "end_byte": node.end_byte,
                    "text": parsed.slice(node.start_byte, node.end_byte),
                })

    kept, truncated = fit(matches, max_tokens, estimate_tokens(payload))
    payload["matches"] = kept
    payload["truncated"] = truncated
    with_errors(payload, errors)
    hinted = narrow_hint(payload, "captures, or a more specific query")
    return enforce(hinted, ("matches",), max_tokens)


# --- shared ------------------------------------------------------------------

def _resolve(index: Index, path: str):
    """Revalidate a path and fetch its index row (SPEC §V.3).

    Returns ``(parsed, record, errors, failure_payload)``. When
    ``failure_payload`` is not None the caller returns it unchanged.
    """
    relative = index.relative(path)
    parsed, errors = index.refresh_file(relative)
    if parsed is None:
        return None, None, errors, with_errors(
            envelope(path=relative, profile=None, group=None), errors
        )
    record = index.file_record(relative)
    if record is None:
        errors = [*errors, Error("not_indexed", relative, "file could not be indexed")]
        return None, None, errors, with_errors(
            envelope(path=relative, profile=None, group=None), errors
        )
    return parsed, record, errors, None


def _symbol_fields(row) -> dict[str, Any]:
    """The full description of one definition, body excluded."""
    return {
        "name": row["name"],
        "qualified_name": row["qualified_name"],
        "kind": row["kind"],
        "lang": row["lang"],
        "profile": row["profile"],
        "group": row["grp"],
        "path": row["path"],
        "start_line": row["start_line"],
        "end_line": row["end_line"],
        "start_byte": row["start_byte"],
        "end_byte": row["end_byte"],
        "signature": row["signature"],
        "docstring": row["docstring"],
    }


def _candidate_fields(row) -> dict[str, Any]:
    """The compact form: enough to address a definition, nothing more.

    ``start_line`` is the address — it is what ``get_symbol(line=...)`` takes.
    """
    return {
        "qualified_name": row["qualified_name"],
        "kind": row["kind"],
        "profile": row["profile"],
        "path": row["path"],
        "start_line": row["start_line"],
    }


def _row_to_item(row, profile: str, include_docstrings: bool) -> dict:
    if profile in {"symbols", "defs"}:
        item = {
            "name": row["name"],
            "qualified_name": row["qualified_name"],
            "kind": row["kind"],
            "signature": row["signature"],
            "start_line": row["start_line"],
            "end_line": row["end_line"],
            "parent": row["parent"],
        }
        if include_docstrings:
            item["docstring"] = row["docstring"]
        return item
    if profile == "schema":
        item = {
            "key_path": row["key_path"],
            "kind": row["kind"],
            "value_preview": row["value_preview"],
            "children_count": row["children_count"],
            "truncated_subtree": bool(row["truncated_subtree"]),
            "start_line": row["start_line"],
            "end_line": row["end_line"],
            "parent": row["parent"],
        }
        if include_docstrings:
            item["comment"] = row["comment"]
        return item
    return {
        "title": row["title"],
        "slug": row["slug"],
        "level": row["level"],
        "kind": row["kind"],
        "info": row["info"],
        "start_line": row["start_line"],
        "end_line": row["end_line"],
        "parent": row["parent"],
    }


def _depths(items: list[dict], id_field: str) -> list[int]:
    """Nesting depth per item, 1-based, within this file.

    Resolved by position rather than by name, because ids are not unique —
    a Lua file can register the same event name twice. Rows arrive
    container-first, so an item's parent is the nearest preceding item
    carrying that id.
    """
    latest: dict[str, int] = {}
    depths: list[int] = []
    for position, item in enumerate(items):
        parent = item.get("parent")
        parent_position = latest.get(str(parent)) if parent is not None else None
        depths.append(1 if parent_position is None else depths[parent_position] + 1)
        latest[str(item[id_field])] = position
    return depths


def _doc_entry(row, profile: str, path: str) -> dict:
    keys = row.keys()
    if profile in {"symbols", "defs"}:
        name, doc, signature = row["qualified_name"], row["docstring"], row["signature"]
    elif profile == "schema":
        name, doc, signature = row["key_path"], row["comment"], None
    else:
        name, doc, signature = row["slug"], None, row["title"]
    return {
        "qualified_name": name,
        "kind": row["kind"],
        "path": path,
        "start_line": row["start_line"],
        "signature": signature,
        "docstring": doc,
        "profile": profile,
    }


def _candidates(index: Index, name: str, scope: str | None) -> list:
    """Exact matches first; only fall back to substring when there are none."""
    rows, _total = index.search(name, path_glob=scope, limit=200)
    lowered = name.lower()
    exact = [
        r for r in rows
        if r["qualified_name"].lower() == lowered or r["name"].lower() == lowered
    ]
    return exact or rows
