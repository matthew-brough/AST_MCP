"""Definitions, docstrings and imports for the ``symbols`` and ``defs`` profiles.

Everything here is driven by the ``.scm`` files (SPEC §V.8). The only
language-shaped knowledge in this module is a handful of node-type *name*
tables — wrappers, comment kinds, module-literal kinds — which are string
tables, not patterns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from tree_sitter import Node, Query, QueryCursor

from ast_mcp import Error
from ast_mcp.languages import load_language
from ast_mcp.parser import ParsedFile

MAX_SIGNATURE = 500

#: Nodes that wrap a definition without being one. The symbol's range extends
#: over these so decorators, ``export`` and ``type``/``const`` keywords stay
#: attached to the source — and so a docstring sitting above the wrapper is
#: still found (SPEC §I.docstring).
WRAPPERS = frozenset({
    "decorated_definition", "export_statement",
    "type_declaration", "const_declaration", "var_declaration",
    "lexical_declaration", "variable_declaration",
})

#: Wrappers whose text belongs in the signature. Decorators do not — they can
#: run to many lines and are not part of the declaration header.
SIGNATURE_WRAPPERS = WRAPPERS - {"decorated_definition"}

#: Comment node types across the registry's grammars.
COMMENT_TYPES = frozenset({"comment", "line_comment", "block_comment"})

#: Nodes that carry a module path inside an import statement.
MODULE_TYPES = frozenset({
    "string", "string_literal", "interpreted_string_literal", "dotted_name",
    "raw_string_literal", "quoted_template", "string_content",
})

#: Specificity ranking. Two patterns can match the same byte range (Go's
#: ``type_spec`` matches both ``type`` and ``struct``); the higher rank wins.
KIND_RANK = {
    "var": 0, "const": 1, "module": 2, "type": 2, "rule": 3, "index": 3,
    "function": 4, "class": 5, "interface": 5, "struct": 5, "enum": 5,
    "message": 5, "service": 5, "table": 5, "view": 5, "resource": 5,
    "stage": 5, "block": 5, "method": 6, "rpc": 6,
}

#: Call/command names that mean "pull in another file". Anything else captured
#: as ``@import.callee`` is not an import and the match is discarded.
IMPORT_CALLEES = frozenset({
    "require", "require_relative", "import", "library", "source", "include", ".",
})

#: Container kinds whose direct function children are methods, not functions.
METHOD_PARENTS = frozenset({"class", "interface", "struct", "message", "service"})


@dataclass(frozen=True, slots=True)
class Symbol:
    name: str
    qualified_name: str
    kind: str
    lang: str
    path: str
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int
    signature: str
    docstring: str | None
    parent: str | None


@dataclass(frozen=True, slots=True)
class Import:
    module: str
    names: tuple[str, ...]
    alias: str | None
    line: int
    kind: str


@dataclass(frozen=True, slots=True)
class Export:
    name: str
    kind: str
    line: int


@dataclass(slots=True)
class Extraction:
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[Import] = field(default_factory=list)
    exports: list[Export] = field(default_factory=list)
    errors: list[Error] = field(default_factory=list)


# --- docstring helpers (SPEC §I.docstring) ----------------------------------

def preceding_comment_block(node: Node, src: bytes) -> str | None:
    """The contiguous comment block directly above ``node``, verbatim.

    Contiguous means no blank line between the block's last line and the
    declaration's first line — one blank line breaks the association.
    Returns None when there is no such block (SPEC §V.7: never invent).
    """
    block: list[Node] = []
    anchor = node
    # A grammar may hang the comment on an ancestor rather than on the
    # definition itself (Ruby does). Climb while we are the first child.
    while anchor.prev_sibling is None and anchor.parent is not None:
        anchor = anchor.parent
    cursor = anchor.prev_sibling
    while cursor is not None and cursor.type in COMMENT_TYPES:
        if cursor.end_point[0] != anchor.start_point[0] - 1:
            break
        block.append(cursor)
        anchor = cursor
        cursor = cursor.prev_sibling
    if not block:
        # Some grammars park a comment at the tail of the *previous* sibling
        # rather than at the head of this one (YAML does between mappings).
        trailing = _trailing_comment(anchor.prev_sibling)
        if trailing is None or trailing.end_point[0] != anchor.start_point[0] - 1:
            return None
        block = [trailing]
        cursor = trailing.prev_sibling
        while cursor is not None and cursor.type in COMMENT_TYPES:
            if cursor.end_point[0] != block[-1].start_point[0] - 1:
                break
            block.append(cursor)
            cursor = cursor.prev_sibling
    block.reverse()
    return src[block[0].start_byte : block[-1].end_byte].decode("utf-8", errors="replace")


def _trailing_comment(node: Node | None) -> Node | None:
    """The comment closing out a subtree, if the subtree ends in one."""
    seen = 0
    while node is not None and seen < 32:
        if node.type in COMMENT_TYPES:
            return node
        if node.named_child_count == 0:
            return None
        node, seen = node.named_children[-1], seen + 1
    return None


def python_docstring(body: Node | None, src: bytes) -> str | None:
    """First string literal statement of a Python body, verbatim (SPEC §V.7)."""
    if body is None:
        return None
    for child in body.named_children:
        if child.type != "expression_statement":
            return None
        inner = child.named_children[0] if child.named_children else None
        if inner is not None and inner.type == "string":
            return src[inner.start_byte : inner.end_byte].decode("utf-8", errors="replace")
        return None
    return None


# --- extraction --------------------------------------------------------------

@lru_cache(maxsize=64)
def _compiled(lang: str, source: str) -> Query | None:
    language = load_language(lang)
    if language is None:
        return None
    try:
        return Query(language, source)
    except Exception:  # noqa: BLE001 — §V.5
        return None


def extract(parsed: ParsedFile) -> Extraction:
    """Run the language's query file over a parsed tree.

    Never raises (SPEC §V.5): every failure becomes an ``Error`` and whatever
    was recovered is still returned.
    """
    out = Extraction()
    query_path = parsed.spec.query_path
    if query_path is None or not query_path.exists():
        out.errors.append(Error("no_query", str(parsed.path), f"{parsed.spec.lang} has no query file"))
        return out

    query = _compiled(parsed.spec.lang, query_path.read_text())
    if query is None:
        out.errors.append(Error("bad_query", str(parsed.path), f"{query_path.name} failed to compile"))
        return out

    src = parsed.source
    try:
        matches = QueryCursor(query).matches(parsed.tree.root_node)
    except Exception as exc:  # noqa: BLE001 — §V.5
        out.errors.append(Error("query_failed", str(parsed.path), f"{type(exc).__name__}: {exc}"))
        return out

    raw: dict[tuple[int, int], tuple[str, Node, list[Node]]] = {}
    for _index, caps in matches:
        def_key = next((k for k in caps if k.startswith("def.")), None)
        if def_key is not None:
            _collect_definition(def_key, caps, raw)
            continue
        imported = _build_import(caps, src)
        if imported is not None:
            out.imports.append(imported)

    out.symbols.extend(_nest(raw, parsed))
    out.exports.extend(_collect_exports(out.symbols, raw))
    return out


def _collect_definition(
    def_key: str,
    caps: dict[str, list[Node]],
    raw: dict[tuple[int, int], tuple[str, Node, list[Node]]],
) -> None:
    nodes = caps.get(def_key) or []
    names = caps.get("name") or []
    if not nodes or not names:
        return
    node, name_node = nodes[0], names
    outer = _outer(node)
    key = (outer.start_byte, outer.end_byte)
    kind = def_key.removeprefix("def.")
    previous = raw.get(key)
    if previous is None or KIND_RANK.get(kind, 0) > KIND_RANK.get(previous[0], 0):
        raw[key] = (kind, node, name_node)
    elif KIND_RANK.get(kind, 0) == KIND_RANK.get(previous[0], 0) and len(name_node) > len(previous[2]):
        # Same kind, more name parts — the fuller composite name wins.
        raw[key] = (kind, node, name_node)


def _outer(node: Node) -> Node:
    """Climb through wrappers so decorators and ``export`` stay in range."""
    return _climb(node, WRAPPERS)


def _climb(node: Node, through: frozenset[str]) -> Node:
    current = node
    while current.parent is not None and current.parent.type in through:
        current = current.parent
    return current


def _signature(node: Node, src: bytes) -> str:
    """Declaration header, body excluded.

    Keywords carried by wrappers (``export``, ``type``, ``const``) are
    prefixed back on, so the signature reads as it does in the file.
    """
    prefix = src[_climb(node, SIGNATURE_WRAPPERS).start_byte : node.start_byte]
    body = node.child_by_field_name("body")
    if body is None:
        value = node.child_by_field_name("value")
        if value is not None:
            body = value.child_by_field_name("body")
    if body is not None and body.start_byte > node.start_byte:
        text = src[node.start_byte : _header_end(node, body)]
    else:
        text = src[node.start_byte : node.end_byte].split(b"\n", 1)[0]
    flat = " ".join((prefix + text).decode("utf-8", errors="replace").split())
    return flat[: MAX_SIGNATURE - 1] + "…" if len(flat) > MAX_SIGNATURE else flat


def _header_end(node: Node, body: Node) -> int:
    """Where the declaration header stops.

    Normally the body's first byte, but a grammar can place a comment between
    the name and the body (Ruby); the header ends before that comment rather
    than swallowing it into the signature.
    """
    for child in node.children:
        if child.start_byte >= body.start_byte:
            break
        if child.type in COMMENT_TYPES:
            return child.start_byte
    return body.start_byte


def _docstring(parsed: ParsedFile, node: Node, outer: Node) -> str | None:
    if parsed.spec.lang == "python":
        found = python_docstring(node.child_by_field_name("body"), parsed.source)
        if found is not None:
            return found
    return preceding_comment_block(outer, parsed.source)


def _nest(
    raw: dict[tuple[int, int], tuple[str, Node, list[Node]]],
    parsed: ParsedFile,
) -> list[Symbol]:
    """Resolve containment by byte range, then build qualified names."""
    ordered = sorted(raw.items(), key=lambda kv: (kv[0][0], -kv[0][1]))
    symbols: list[Symbol] = []
    stack: list[tuple[tuple[int, int], Symbol]] = []

    for (start, end), (kind, node, name_nodes) in ordered:
        while stack and not (stack[-1][0][0] <= start and end <= stack[-1][0][1]):
            stack.pop()
        parent = stack[-1][1] if stack else None
        if kind == "function" and parent is not None and parent.kind in METHOD_PARENTS:
            kind = "method"

        # Several @name captures in one match compose a dotted name — that is
        # how Terraform's `resource "aws_s3_bucket" "b"` becomes one symbol.
        name = ".".join(_text(n, parsed.source) for n in name_nodes)
        qualified = f"{parent.qualified_name}.{name}" if parent else name
        outer = _outer(node)
        symbol = Symbol(
            name=name,
            qualified_name=qualified,
            kind=kind,
            lang=parsed.spec.lang,
            path=str(parsed.path),
            start_byte=start,
            end_byte=end,
            start_line=outer.start_point[0] + 1,
            end_line=outer.end_point[0] + 1,
            signature=_signature(node, parsed.source),
            docstring=_docstring(parsed, node, outer),
            parent=parent.qualified_name if parent else None,
        )
        symbols.append(symbol)
        stack.append(((start, end), symbol))
    return symbols


def _collect_exports(
    symbols: list[Symbol],
    raw: dict[tuple[int, int], tuple[str, Node, list[Node]]],
) -> list[Export]:
    """A symbol is exported when a wrapper ``export_statement`` covers it."""
    exports: list[Export] = []
    for symbol in symbols:
        entry = raw.get((symbol.start_byte, symbol.end_byte))
        if entry is None:
            continue
        node = entry[1]
        if _outer(node).type == "export_statement":
            exports.append(Export(symbol.name, symbol.kind, symbol.start_line))
    return exports


def _build_import(caps: dict[str, list[Node]], src: bytes) -> Import | None:
    key = next((k for k in caps if k.startswith("import.")), None)
    if key is None:
        return None
    kinds = [k.removeprefix("import.") for k in caps if k.startswith("import.")]
    kind = next((k for k in kinds if k not in {"callee", "module", "alias"}), "import")

    callee = caps.get("import.callee")
    callee_node = callee[0] if callee else None
    if callee_node is not None and _text(callee_node, src) not in IMPORT_CALLEES:
        return None

    node = (caps.get(f"import.{kind}") or [None])[0]
    module_node = (caps.get("import.module") or [None])[0]
    if module_node is None and node is not None:
        module_node = _first_of_type(node, MODULE_TYPES)
    if module_node is None:
        return None
    module = _text(module_node, src).strip("\"'`")

    alias = _alias_for(node, module, src, callee_node) if node is not None else None

    names: list[str] = []
    if node is not None:
        for descendant in _walk(node):
            if descendant.id == getattr(callee_node, "id", None):
                continue
            if descendant.type in {"identifier", "dotted_name", "type_identifier"}:
                text = _text(descendant, src)
                if text != module and text != alias and text not in names:
                    names.append(text)

    line = (node or module_node).start_point[0] + 1
    return Import(module=module, names=tuple(names), alias=alias, line=line, kind=kind)


def _alias_for(
    node: Node, module: str, src: bytes, callee: Node | None = None
) -> str | None:
    """Local rebinding of an import, if the statement has one.

    An ``alias`` field wins. Failing that, a ``name`` field counts only when it
    is a bare identifier that differs from the module path — which rules out
    Python's ``import os`` (name == module) while keeping Go's ``alias "x"``.
    """
    direct = node.child_by_field_name("alias")
    if direct is not None:
        return _text(direct, src)
    named = node.child_by_field_name("name")
    if named is not None and named.id != getattr(callee, "id", None):
        text = _text(named, src)
        if text != module and text.isidentifier():
            return text
    for descendant in _walk(node):
        nested = descendant.child_by_field_name("alias")
        if nested is not None:
            return _text(nested, src)
    return None


def _walk(node: Node):
    for child in node.named_children:
        yield child
        yield from _walk(child)


def _first_of_type(node: Node, types: frozenset[str]) -> Node | None:
    for child in _walk(node):
        if child.type in types:
            return child
    return None


def _text(node: Node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", errors="replace")
