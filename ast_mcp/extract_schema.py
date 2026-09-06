"""The ``schema`` profile — key paths and value types, not definitions.

A ``docker-compose.yml`` has no functions to list; it has a *shape*. This
module produces that shape: one :class:`SchemaNode` per key path, with the
inferred value type, a short preview, and any comment sitting above the key.

SPEC §V.11 is the load-bearing rule here. Homogeneous arrays and CSV rows
collapse to a single node carrying ``children_count``, so a 5000-row file
costs the same as a 3-row one. Without that the profile would defeat the
purpose of the server.

SPEC §V.13: kinds are *value types* (``object``, ``array``, ``string``, …).
Nothing in here ever claims to be a function or a class.

SPEC §V.8 exception: grammars name their nodes differently, so this module is
driven by the ``NODE_KINDS`` string table rather than by ``.scm`` patterns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from tree_sitter import Node

from ast_mcp import Error
from ast_mcp.extract import preceding_comment_block
from ast_mcp.parser import ParsedFile

DEFAULT_MAX_DEPTH = 4
PREVIEW_CHARS = 80
#: How many data rows a CSV column's type is inferred from.
CSV_SAMPLE_ROWS = 50

#: Per-grammar node names for the roles the walker needs (SPEC §I.layout).
NODE_KINDS: dict[str, dict[str, frozenset[str] | str]] = {
    "json": {
        "mapping": frozenset({"object"}),
        "sequence": frozenset({"array"}),
        "pair": frozenset({"pair"}),
        "unwrap": frozenset({"document"}),
        "key_field": "key",
        "value_field": "value",
    },
    "json5": {
        "mapping": frozenset({"object"}),
        "sequence": frozenset({"array"}),
        "pair": frozenset({"pair", "member"}),
        "unwrap": frozenset({"document"}),
        "key_field": "key",
        "value_field": "value",
    },
    "yaml": {
        "mapping": frozenset({"block_mapping", "flow_mapping"}),
        "sequence": frozenset({"block_sequence", "flow_sequence"}),
        "pair": frozenset({"block_mapping_pair", "flow_pair"}),
        "unwrap": frozenset({
            "stream", "document", "block_node", "flow_node", "block_sequence_item",
        }),
        "key_field": "key",
        "value_field": "value",
    },
}

#: Node type -> value kind. Anything unlisted falls back to text inference.
VALUE_KINDS = {
    "object": "object", "inline_table": "object", "block_mapping": "object",
    "flow_mapping": "object", "table": "object",
    "array": "array", "block_sequence": "array", "flow_sequence": "array",
    "string": "string", "string_scalar": "string",
    "number": "number", "integer": "number", "float": "number",
    "true": "bool", "false": "bool", "boolean": "bool", "boolean_scalar": "bool",
    "null": "null", "null_scalar": "null",
}

_NUMBER = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
_BOOL = frozenset({"true", "false", "yes", "no", "on", "off"})
_NULL = frozenset({"null", "~", "nil", ""})


@dataclass(frozen=True, slots=True)
class SchemaNode:
    key_path: str
    kind: str
    lang: str
    path: str
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int
    value_preview: str | None
    comment: str | None
    children_count: int
    truncated_subtree: bool
    parent: str | None


@dataclass(slots=True)
class SchemaExtraction:
    nodes: list[SchemaNode] = field(default_factory=list)
    errors: list[Error] = field(default_factory=list)


def extract_schema(
    parsed: ParsedFile, max_depth: int = DEFAULT_MAX_DEPTH
) -> SchemaExtraction:
    """Walk a data file into key paths. Never raises (SPEC §V.5)."""
    out = SchemaExtraction()
    lang = parsed.spec.lang
    try:
        if lang == "csv":
            _walk_csv(parsed, out)
        elif lang == "toml":
            _walk_toml(parsed, parsed.tree.root_node, "", None, 1, max_depth, out)
        elif lang == "xml":
            _walk_xml(parsed, parsed.tree.root_node, "", None, 1, max_depth, out)
        elif lang in NODE_KINDS:
            _walk_tree(parsed, parsed.tree.root_node, "", None, 1, max_depth, out)
        else:
            out.errors.append(
                Error("unsupported_lang", str(parsed.path), f"{lang} has no schema walker")
            )
    except Exception as exc:  # noqa: BLE001 — §V.5
        out.errors.append(
            Error("schema_failed", str(parsed.path), f"{type(exc).__name__}: {exc}")
        )
    return out


# --- json / json5 / yaml -----------------------------------------------------

def _walk_tree(
    parsed: ParsedFile,
    node: Node,
    prefix: str,
    parent: str | None,
    depth: int,
    max_depth: int,
    out: SchemaExtraction,
) -> None:
    table = NODE_KINDS[parsed.spec.lang]
    node = _unwrap(node, table)
    if node is None:
        return

    if node.type in table["mapping"]:
        for pair in node.named_children:
            if pair.type not in table["pair"]:
                continue
            key_node = pair.child_by_field_name(table["key_field"])
            value_node = pair.child_by_field_name(table["value_field"])
            if key_node is None:
                continue
            key = _scalar_text(parsed, key_node)
            path = f"{prefix}.{key}" if prefix else key
            _emit_value(parsed, pair, value_node, path, parent, depth, max_depth, out)
        return

    if node.type in table["sequence"]:
        _emit_value(parsed, node, node, prefix, parent, depth, max_depth, out)
        return

    # Degraded parse (SPEC §V.5): a truncated file puts an ERROR node where the
    # mapping should be. Recover the pairs that did parse instead of giving up.
    if node.type == "ERROR" or node.has_error:
        for child in node.named_children:
            if child.type in table["pair"]:
                key_node = child.child_by_field_name(table["key_field"])
                value_node = child.child_by_field_name(table["value_field"])
                if key_node is None:
                    continue
                key = _scalar_text(parsed, key_node)
                path = f"{prefix}.{key}" if prefix else key
                _emit_value(parsed, child, value_node, path, parent, depth, max_depth, out)
            elif child.type not in {"comment"} and child.named_child_count:
                _walk_tree(parsed, child, prefix, parent, depth, max_depth, out)


def _emit_value(
    parsed: ParsedFile,
    owner: Node,
    value: Node | None,
    path: str,
    parent: str | None,
    depth: int,
    max_depth: int,
    out: SchemaExtraction,
) -> None:
    table = NODE_KINDS[parsed.spec.lang]
    value = _unwrap(value, table) if value is not None else None
    kind = _kind_of(parsed, value)
    is_mapping = value is not None and value.type in table["mapping"]
    is_sequence = value is not None and value.type in table["sequence"]

    if is_sequence:
        elements = [
            e for e in (_unwrap(c, table) for c in value.named_children)
            if e is not None and e.type != "comment"
        ]
        count = len(elements)
    elif is_mapping:
        count = sum(1 for c in value.named_children if c.type in table["pair"])
    else:
        count = 0

    # A container at max_depth does not expand: its children would sit one
    # level deeper than the cap allows.
    over_depth = depth >= max_depth and (is_mapping or is_sequence)
    out.nodes.append(
        _node(parsed, owner, path, kind, parent, value, count, over_depth)
    )
    if over_depth or value is None:
        return

    if is_mapping:
        _walk_tree(parsed, value, path, path, depth + 1, max_depth, out)
    elif is_sequence and elements:
        # SPEC §V.11 — describe element 0's shape, never every element.
        first = elements[0]
        if first.type in table["mapping"] or first.type in table["sequence"]:
            _walk_tree(parsed, first, f"{path}[]", path, depth + 1, max_depth, out)


def _unwrap(node: Node | None, table) -> Node | None:
    """Skip pass-through nodes (`document`, `block_node`, …)."""
    seen = 0
    while node is not None and node.type in table["unwrap"] and seen < 16:
        nxt = next((c for c in node.named_children if c.type != "comment"), None)
        if nxt is None:
            return None
        node, seen = nxt, seen + 1
    return node


# --- toml --------------------------------------------------------------------

def _walk_toml(
    parsed: ParsedFile,
    node: Node,
    prefix: str,
    parent: str | None,
    depth: int,
    max_depth: int,
    out: SchemaExtraction,
) -> None:
    seen_arrays: set[str] = set()
    repeats: dict[str, int] = {}
    for child in node.named_children:
        if child.type == "table_array_element" and child.named_child_count:
            name = _scalar_text(parsed, child.named_children[0])
            repeats[name] = repeats.get(name, 0) + 1
    for child in node.named_children:
        if child.type == "pair":
            key_node = child.named_children[0]
            value_node = child.named_children[-1] if child.named_child_count > 1 else None
            key = _scalar_text(parsed, key_node)
            path = f"{prefix}.{key}" if prefix else key
            out.nodes.append(
                _node(parsed, child, path, _kind_of(parsed, value_node), parent,
                      value_node, 0, False)
            )
        elif child.type in {"table", "table_array_element"}:
            key_node = child.named_children[0] if child.named_child_count else None
            if key_node is None:
                continue
            name = _scalar_text(parsed, key_node)
            is_array = child.type == "table_array_element"
            path = f"{name}[]" if is_array else name
            if is_array and path in seen_arrays:
                # SPEC §V.11 — repeated [[table]] entries are one node.
                continue
            seen_arrays.add(path)
            count = (
                repeats[name] if is_array
                else sum(1 for c in child.named_children if c.type == "pair")
            )
            over = depth >= max_depth
            out.nodes.append(
                _node(parsed, child, path, "array" if is_array else "table",
                      parent, None, count, over)
            )
            if not over:
                _walk_toml(parsed, child, path, path, depth + 1, max_depth, out)


# --- xml ---------------------------------------------------------------------

def _walk_xml(
    parsed: ParsedFile,
    node: Node,
    prefix: str,
    parent: str | None,
    depth: int,
    max_depth: int,
    out: SchemaExtraction,
) -> None:
    elements = [c for c in node.named_children if c.type == "element"]
    if not elements and node.type == "document":
        root = node.child_by_field_name("root")
        elements = [root] if root is not None else []

    grouped: dict[str, list[Node]] = {}
    for element in elements:
        grouped.setdefault(_xml_name(parsed, element) or "?", []).append(element)

    for name, siblings in grouped.items():
        path = f"{prefix}/{name}" if prefix else name
        first = siblings[0]
        over = depth >= max_depth
        out.nodes.append(
            _node(parsed, first, path, "element", parent, None, len(siblings), over)
        )
        if over:
            continue
        for attr_name, attr_node in _xml_attributes(parsed, first):
            value = next(
                (c for c in attr_node.named_children if c.type == "AttValue"), attr_node
            )
            out.nodes.append(
                _node(parsed, attr_node, f"{path}@{attr_name}", "attr", path,
                      value, 0, False)
            )
        content = next((c for c in first.named_children if c.type == "content"), None)
        if content is not None:
            _walk_xml(parsed, content, path, path, depth + 1, max_depth, out)


def _xml_tag(element: Node) -> Node | None:
    return next(
        (c for c in element.named_children if c.type in {"STag", "EmptyElemTag"}), None
    )


def _xml_name(parsed: ParsedFile, element: Node) -> str | None:
    tag = _xml_tag(element)
    if tag is None:
        return None
    name = next((c for c in tag.named_children if c.type == "Name"), None)
    return _scalar_text(parsed, name) if name is not None else None


def _xml_attributes(parsed: ParsedFile, element: Node):
    tag = _xml_tag(element)
    if tag is None:
        return
    for attribute in tag.named_children:
        if attribute.type != "Attribute":
            continue
        name = next((c for c in attribute.named_children if c.type == "Name"), None)
        if name is not None:
            yield _scalar_text(parsed, name), attribute


# --- csv ---------------------------------------------------------------------

def _walk_csv(parsed: ParsedFile, out: SchemaExtraction) -> None:
    """Columns, never rows (SPEC §V.11).

    The byte range of a column is its *header cell* — a column is not a
    contiguous span, and §V.2 forbids inventing one.
    """
    rows = [r for r in parsed.tree.root_node.named_children if r.type == "row"]
    if not rows:
        return
    header, data = rows[0], rows[1:]
    header_fields = [f for f in header.named_children if f.type == "field"]

    for index, field_node in enumerate(header_fields):
        name = _scalar_text(parsed, field_node)
        samples: list[Node] = []
        for row in data[:CSV_SAMPLE_ROWS]:
            cells = [c for c in row.named_children if c.type == "field"]
            if index < len(cells):
                samples.append(cells[index])
        kind = _csv_column_kind(parsed, samples)
        preview = _preview(parsed, samples[0]) if samples else None
        out.nodes.append(
            SchemaNode(
                key_path=name,
                kind=kind,
                lang=parsed.spec.lang,
                path=str(parsed.path),
                start_byte=field_node.start_byte,
                end_byte=field_node.end_byte,
                start_line=field_node.start_point[0] + 1,
                end_line=field_node.end_point[0] + 1,
                value_preview=preview,
                comment=None,
                children_count=len(data),
                truncated_subtree=False,
                parent=None,
            )
        )


def _csv_column_kind(parsed: ParsedFile, samples: list[Node]) -> str:
    if not samples:
        return "null"
    kinds = {_infer_from_text(_scalar_text(parsed, s)) for s in samples}
    kinds.discard("null")
    if len(kinds) == 1:
        return kinds.pop()
    return "string"


# --- shared helpers ----------------------------------------------------------

def _node(
    parsed: ParsedFile,
    owner: Node,
    path: str,
    kind: str,
    parent: str | None,
    value: Node | None,
    children_count: int,
    truncated: bool,
) -> SchemaNode:
    scalar = value is not None and kind not in {"object", "array", "table", "element"}
    return SchemaNode(
        key_path=path,
        kind=kind,
        lang=parsed.spec.lang,
        path=str(parsed.path),
        start_byte=owner.start_byte,
        end_byte=owner.end_byte,
        start_line=owner.start_point[0] + 1,
        end_line=owner.end_point[0] + 1,
        value_preview=_preview(parsed, value) if scalar else None,
        comment=preceding_comment_block(owner, parsed.source),
        children_count=children_count,
        truncated_subtree=truncated,
        parent=parent,
    )


def _kind_of(parsed: ParsedFile, value: Node | None) -> str:
    if value is None:
        return "null"
    mapped = VALUE_KINDS.get(value.type)
    if mapped is not None:
        return mapped
    inner = next((c for c in value.named_children if c.type != "comment"), None)
    if inner is not None and inner.type in VALUE_KINDS:
        return VALUE_KINDS[inner.type]
    return _infer_from_text(_scalar_text(parsed, value))


def _infer_from_text(text: str) -> str:
    lowered = text.strip().strip("\"'").lower()
    if lowered in _NULL:
        return "null"
    if lowered in _BOOL:
        return "bool"
    if _NUMBER.match(lowered):
        return "number"
    return "string"


def _scalar_text(parsed: ParsedFile, node: Node) -> str:
    return parsed.slice(node.start_byte, node.end_byte).strip().strip("\"'")


def _preview(parsed: ParsedFile, node: Node | None) -> str | None:
    if node is None:
        return None
    text = " ".join(parsed.slice(node.start_byte, node.end_byte).split())
    if len(text) > PREVIEW_CHARS:
        return text[: PREVIEW_CHARS - 1] + "…"
    return text or None
