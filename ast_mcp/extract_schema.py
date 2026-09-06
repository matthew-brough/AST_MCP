"""The ``schema`` profile — key paths and value types, not definitions.

A ``docker-compose.yml`` has no functions to list; it has a *shape*. This
module produces that shape: one :class:`SchemaNode` per key path, with the
inferred value type, a short preview, and any comment sitting above the key.

SPEC §V.11 is the load-bearing rule here. Homogeneous arrays and CSV rows
collapse to a single node carrying ``children_count``, so a 5000-row file
costs the same as a 3-row one. Without that the profile would defeat the
purpose of the server. The collapse says so: every collapsed collection
carries ``uniformity``, checked across the element *key sets*, so a caller
never has to read the file to find out whether element 0 was representative.

SPEC §G — this profile returns shape, never rows. A scalar inside a collapsed
collection therefore gets no ``value_preview``: one element's data presented
as the collection's shape is exactly the leak the profile exists to avoid.

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
#: Elements scanned when checking a collapsed collection for a single shape.
#: Beyond this the collection reports ``unverified`` rather than guessing.
SCAN_LIMIT = 5000
UNIFORM, MIXED, UNVERIFIED = "uniform", "mixed", "unverified"

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
    uniformity: str | None = None


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
    in_collection: bool = False,
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
            _emit_value(
                parsed, pair, value_node, path, parent, depth, max_depth, out,
                in_collection,
            )
        return

    if node.type in table["sequence"]:
        _emit_value(
            parsed, node, node, prefix, parent, depth, max_depth, out, in_collection
        )
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
                _emit_value(
                    parsed, child, value_node, path, parent, depth, max_depth, out,
                    in_collection,
                )
            elif child.type not in {"comment"} and child.named_child_count:
                _walk_tree(
                    parsed, child, prefix, parent, depth, max_depth, out, in_collection
                )


def _emit_value(
    parsed: ParsedFile,
    owner: Node,
    value: Node | None,
    path: str,
    parent: str | None,
    depth: int,
    max_depth: int,
    out: SchemaExtraction,
    in_collection: bool = False,
) -> None:
    table = NODE_KINDS[parsed.spec.lang]
    value = _unwrap(value, table) if value is not None else None
    kind = _kind_of(parsed, value)
    is_mapping = value is not None and value.type in table["mapping"]
    is_sequence = value is not None and value.type in table["sequence"]
    uniformity: str | None = None

    if is_sequence:
        elements = [
            e for e in (_unwrap(c, table) for c in value.named_children)
            if e is not None and e.type != "comment"
        ]
        count = len(elements)
        uniformity = _sequence_uniformity(parsed, elements, table)
    elif is_mapping:
        count = sum(1 for c in value.named_children if c.type in table["pair"])
    else:
        count = 0

    # A container at max_depth does not expand: its children would sit one
    # level deeper than the cap allows.
    over_depth = depth >= max_depth and (is_mapping or is_sequence)
    out.nodes.append(
        _node(parsed, owner, path, kind, parent, value, count, over_depth,
              preview=not in_collection, uniformity=uniformity)
    )
    if over_depth or value is None:
        return

    if is_mapping:
        _walk_tree(parsed, value, path, path, depth + 1, max_depth, out, in_collection)
    elif is_sequence and elements:
        # SPEC §V.11 — describe element 0's shape, never every element.
        first = elements[0]
        if first.type in table["mapping"] or first.type in table["sequence"]:
            _walk_tree(parsed, first, f"{path}[]", path, depth + 1, max_depth, out, True)


def _sequence_uniformity(parsed: ParsedFile, elements: list[Node], table) -> str | None:
    """Do all elements share element 0's shape? (SPEC §V.11.)

    Key *names* only — values are never read, and the tree is already parsed,
    so the check costs one pass and adds no nodes.
    """
    if not elements:
        return None
    if len(elements) > SCAN_LIMIT:
        return UNVERIFIED
    shapes = {_shape_signature(parsed, e, table) for e in elements}
    return UNIFORM if len(shapes) == 1 else MIXED


def _shape_signature(parsed: ParsedFile, node: Node, table) -> str:
    if node.type in table["mapping"]:
        keys = sorted(
            _scalar_text(parsed, key)
            for key in (
                pair.child_by_field_name(table["key_field"])
                for pair in node.named_children
                if pair.type in table["pair"]
            )
            if key is not None
        )
        return "object:" + ",".join(keys)
    if node.type in table["sequence"]:
        return "array"
    return _kind_of(parsed, node)


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
    in_collection: bool = False,
) -> None:
    seen_arrays: set[str] = set()
    repeats: dict[str, list[Node]] = {}
    for child in node.named_children:
        if child.type == "table_array_element" and child.named_child_count:
            name = _scalar_text(parsed, child.named_children[0])
            repeats.setdefault(name, []).append(child)
    for child in node.named_children:
        if child.type == "pair":
            key_node = child.named_children[0]
            value_node = child.named_children[-1] if child.named_child_count > 1 else None
            key = _scalar_text(parsed, key_node)
            path = f"{prefix}.{key}" if prefix else key
            out.nodes.append(
                _node(parsed, child, path, _kind_of(parsed, value_node), parent,
                      value_node, 0, False, preview=not in_collection)
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
            siblings = repeats[name] if is_array else []
            count = (
                len(siblings) if is_array
                else sum(1 for c in child.named_children if c.type == "pair")
            )
            over = depth >= max_depth
            out.nodes.append(
                _node(parsed, child, path, "array" if is_array else "table",
                      parent, None, count, over,
                      uniformity=_toml_uniformity(parsed, siblings) if is_array else None)
            )
            if not over:
                _walk_toml(parsed, child, path, path, depth + 1, max_depth, out,
                           in_collection or is_array)


def _toml_uniformity(parsed: ParsedFile, siblings: list[Node]) -> str | None:
    """Do all `[[table]]` entries of one name declare the same keys? (§V.11.)"""
    if not siblings:
        return None
    if len(siblings) > SCAN_LIMIT:
        return UNVERIFIED
    shapes = {
        ",".join(sorted(
            _scalar_text(parsed, c.named_children[0])
            for c in element.named_children
            if c.type == "pair" and c.named_child_count
        ))
        for element in siblings
    }
    return UNIFORM if len(shapes) == 1 else MIXED


# --- xml ---------------------------------------------------------------------

def _walk_xml(
    parsed: ParsedFile,
    node: Node,
    prefix: str,
    parent: str | None,
    depth: int,
    max_depth: int,
    out: SchemaExtraction,
    in_collection: bool = False,
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
        # Repetition is a sibling count in XML — there is no array construct.
        collapsed = in_collection or len(siblings) > 1
        out.nodes.append(
            _node(parsed, first, path, "element", parent, None, len(siblings), over,
                  uniformity=_xml_uniformity(parsed, siblings))
        )
        if over:
            continue
        for attr_name, attr_node in _xml_attributes(parsed, first):
            value = next(
                (c for c in attr_node.named_children if c.type == "AttValue"), attr_node
            )
            out.nodes.append(
                _node(parsed, attr_node, f"{path}@{attr_name}", "attr", path,
                      value, 0, False, preview=not collapsed)
            )
        content = next((c for c in first.named_children if c.type == "content"), None)
        if content is not None:
            _walk_xml(parsed, content, path, path, depth + 1, max_depth, out, collapsed)


def _xml_uniformity(parsed: ParsedFile, siblings: list[Node]) -> str | None:
    """Do repeated sibling elements carry the same attributes and children?"""
    if len(siblings) < 2:
        return None
    if len(siblings) > SCAN_LIMIT:
        return UNVERIFIED
    shapes = {_xml_shape(parsed, element) for element in siblings}
    return UNIFORM if len(shapes) == 1 else MIXED


def _xml_shape(parsed: ParsedFile, element: Node) -> str:
    attrs = sorted(name for name, _ in _xml_attributes(parsed, element))
    content = next((c for c in element.named_children if c.type == "content"), None)
    children = sorted(
        {
            _xml_name(parsed, c) or "?"
            for c in (content.named_children if content is not None else [])
            if c.type == "element"
        }
    )
    return f"@{','.join(attrs)}|{','.join(children)}"


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

    scanned = data[:SCAN_LIMIT]
    for index, field_node in enumerate(header_fields):
        name = _scalar_text(parsed, field_node)
        samples: list[Node] = []
        for row in scanned:
            cells = [c for c in row.named_children if c.type == "field"]
            if index < len(cells):
                samples.append(cells[index])
        kinds = {_infer_from_text(_scalar_text(parsed, s)) for s in samples}
        kinds.discard("null")
        mixed = len(kinds) > 1
        out.nodes.append(
            SchemaNode(
                key_path=name,
                kind=next(iter(kinds)) if len(kinds) == 1 else "string" if mixed else "null",
                lang=parsed.spec.lang,
                path=str(parsed.path),
                start_byte=field_node.start_byte,
                end_byte=field_node.end_byte,
                start_line=field_node.start_point[0] + 1,
                end_line=field_node.end_point[0] + 1,
                # §G — a cell is a row value; columns never carry one.
                value_preview=None,
                comment=None,
                children_count=len(data),
                truncated_subtree=False,
                parent=None,
                uniformity=(
                    UNVERIFIED if len(data) > SCAN_LIMIT
                    else MIXED if mixed else UNIFORM
                ),
            )
        )


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
    preview: bool = True,
    uniformity: str | None = None,
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
        value_preview=_preview(parsed, value) if scalar and preview else None,
        comment=preceding_comment_block(owner, parsed.source),
        children_count=children_count,
        truncated_subtree=truncated,
        parent=parent,
        uniformity=uniformity,
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
