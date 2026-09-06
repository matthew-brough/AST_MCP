"""The ``outline`` profile — document structure, not definitions.

Markdown and HTML have no functions to list. They have a heading tree, code
blocks, tables, and landmarks. That is what this module returns.

SPEC §V.13: kinds here are document structures (``heading``, ``code_block``,
``table``, ``landmark``, ``element``). Nothing claims to be a symbol.

SPEC §V.8 exception: like ``extract_schema``, this walks by node type through
a string table rather than through ``.scm`` patterns.

Link targets are found with a bounded regex over the block grammar's ``inline``
text rather than by loading ``markdown_inline`` — see SPEC §B.1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from tree_sitter import Node

from ast_mcp import Error
from ast_mcp.extract import Import
from ast_mcp.parser import ParsedFile

#: HTML elements that structure a page without being headings.
LANDMARKS = frozenset({
    "main", "nav", "header", "footer", "section", "article", "aside", "form",
})
HEADING_TAGS = {f"h{n}": n for n in range(1, 7)}

#: tag -> (attribute carrying the target, import kind)
HTML_LINK_ATTRS = {
    "script": ("src", "script"),
    "link": ("href", "stylesheet"),
    "img": ("src", "image"),
    "a": ("href", "link"),
    "iframe": ("src", "frame"),
}

_MD_LINK = re.compile(r"(!?)\[[^\]]*\]\(\s*<?([^)\s>]+)")
_SLUG_STRIP = re.compile(r"[^a-z0-9\s-]")
_SLUG_SPACE = re.compile(r"[\s-]+")


@dataclass(frozen=True, slots=True)
class DocNode:
    title: str
    slug: str
    level: int
    kind: str
    lang: str
    path: str
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int
    info: str | None
    parent: str | None


@dataclass(slots=True)
class DocExtraction:
    nodes: list[DocNode] = field(default_factory=list)
    links: list[Import] = field(default_factory=list)
    #: slug -> first paragraph under that heading (SPEC §I.docstring, `lede`).
    ledes: dict[str, str] = field(default_factory=dict)
    errors: list[Error] = field(default_factory=list)


class _Slugger:
    """GitHub-style anchors, deduplicated with a numeric suffix."""

    def __init__(self) -> None:
        self._seen: dict[str, int] = {}

    def __call__(self, title: str) -> str:
        base = _SLUG_SPACE.sub("-", _SLUG_STRIP.sub("", title.strip().lower())).strip("-")
        base = base or "section"
        count = self._seen.get(base, 0)
        self._seen[base] = count + 1
        return base if count == 0 else f"{base}-{count}"


def extract_doc(parsed: ParsedFile) -> DocExtraction:
    """Walk a document into its outline. Never raises (SPEC §V.5)."""
    out = DocExtraction()
    try:
        if parsed.spec.lang == "markdown":
            _walk_markdown(parsed, parsed.tree.root_node, None, 0, _Slugger(), out)
        elif parsed.spec.lang == "html":
            _walk_html(parsed, parsed.tree.root_node, None, 0, _Slugger(), out)
        else:
            out.errors.append(
                Error("unsupported_lang", str(parsed.path),
                      f"{parsed.spec.lang} has no outline walker")
            )
    except Exception as exc:  # noqa: BLE001 — §V.5
        out.errors.append(
            Error("outline_failed", str(parsed.path), f"{type(exc).__name__}: {exc}")
        )
    return out


# --- markdown ----------------------------------------------------------------

def _walk_markdown(
    parsed: ParsedFile,
    node: Node,
    parent: str | None,
    level: int,
    slug: _Slugger,
    out: DocExtraction,
) -> None:
    current_parent, current_level, current_slug = parent, level, None

    for child in node.named_children:
        if child.type in {"atx_heading", "setext_heading"}:
            title = _heading_title(parsed, child)
            heading_level = _heading_level(child) or level + 1
            current_slug = slug(title)
            out.nodes.append(
                _doc_node(parsed, child, title, current_slug, heading_level,
                          "heading", None, parent)
            )
            current_parent, current_level = current_slug, heading_level

        elif child.type == "section":
            _walk_markdown(parsed, child, current_parent, current_level, slug, out)

        elif child.type == "fenced_code_block":
            info = _first_text(parsed, child, "info_string")
            title = info or "code"
            out.nodes.append(
                _doc_node(parsed, child, title, slug(f"code-{title}"), current_level,
                          "code_block", info, current_parent)
            )

        elif child.type in {"pipe_table", "table"}:
            title = _first_text(parsed, child, "pipe_table_header") or "table"
            title = " ".join(title.split())
            out.nodes.append(
                _doc_node(parsed, child, title, slug("table"), current_level,
                          "table", None, current_parent)
            )

        elif child.type == "paragraph":
            text = " ".join(parsed.slice(child.start_byte, child.end_byte).split())
            if current_slug is not None and current_slug not in out.ledes and text:
                out.ledes[current_slug] = text
            _collect_markdown_links(parsed, child, out)

        else:
            _collect_markdown_links(parsed, child, out)


def _collect_markdown_links(parsed: ParsedFile, node: Node, out: DocExtraction) -> None:
    text = parsed.slice(node.start_byte, node.end_byte)
    line = node.start_point[0] + 1
    for bang, target in _MD_LINK.findall(text):
        out.links.append(
            Import(module=target, names=(), alias=None, line=line,
                   kind="image" if bang else "link")
        )


def _heading_level(node: Node) -> int | None:
    for child in node.children:
        if child.type.startswith("atx_h") and child.type.endswith("_marker"):
            return int(child.type[5])
        if child.type == "setext_h1_underline":
            return 1
        if child.type == "setext_h2_underline":
            return 2
    return None


def _heading_title(parsed: ParsedFile, node: Node) -> str:
    for child in node.named_children:
        if child.type in {"inline", "heading_content", "paragraph"}:
            return " ".join(parsed.slice(child.start_byte, child.end_byte).split())
    text = parsed.slice(node.start_byte, node.end_byte).splitlines()[0]
    return text.strip("#= \t") or "section"


def _first_text(parsed: ParsedFile, node: Node, kind: str) -> str | None:
    for child in node.named_children:
        if child.type == kind:
            return parsed.slice(child.start_byte, child.end_byte).strip()
    return None


# --- html --------------------------------------------------------------------

def _walk_html(
    parsed: ParsedFile,
    node: Node,
    parent: str | None,
    level: int,
    slug: _Slugger,
    out: DocExtraction,
) -> None:
    for child in node.named_children:
        if not child.type.endswith("element"):
            _walk_html(parsed, child, parent, level, slug, out)
            continue

        tag = _tag_name(parsed, child)
        attrs = _attributes(parsed, child)
        _collect_html_links(tag, attrs, child, out)

        emitted = _html_node(parsed, child, tag, attrs, level, slug, parent)
        if emitted is None:
            _walk_html(parsed, child, parent, level, slug, out)
            continue

        out.nodes.append(emitted)
        if emitted.kind == "heading":
            # The lede is the text *under* the heading, not the heading itself.
            lede = _following_text(parsed, node, child)
            if lede:
                out.ledes.setdefault(emitted.slug, lede)
            _walk_html(parsed, child, parent, emitted.level, slug, out)
        else:
            _walk_html(parsed, child, emitted.slug, emitted.level, slug, out)


def _html_node(
    parsed: ParsedFile,
    element: Node,
    tag: str | None,
    attrs: dict[str, str],
    level: int,
    slug: _Slugger,
    parent: str | None,
) -> DocNode | None:
    if tag in HEADING_TAGS:
        title = _element_text(parsed, element) or tag
        return _doc_node(parsed, element, title, slug(title), HEADING_TAGS[tag],
                         "heading", tag, parent)
    if tag in LANDMARKS:
        title = attrs.get("aria-label") or attrs.get("id") or tag
        return _doc_node(parsed, element, title, slug(f"{tag}-{title}"), level + 1,
                         "landmark", tag, parent)
    if "id" in attrs:
        title = f"{tag}#{attrs['id']}"
        return _doc_node(parsed, element, title, slug(title), level + 1,
                         "element", tag, parent)
    return None


def _collect_html_links(
    tag: str | None, attrs: dict[str, str], element: Node, out: DocExtraction
) -> None:
    entry = HTML_LINK_ATTRS.get(tag or "")
    if entry is None:
        return
    attribute, kind = entry
    target = attrs.get(attribute)
    if target:
        out.links.append(
            Import(module=target, names=(), alias=None,
                   line=element.start_point[0] + 1, kind=kind)
        )


def _start_tag(element: Node) -> Node | None:
    return next(
        (c for c in element.named_children
         if c.type in {"start_tag", "self_closing_tag"}), None
    )


def _tag_name(parsed: ParsedFile, element: Node) -> str | None:
    tag = _start_tag(element)
    if tag is None:
        return None
    name = next((c for c in tag.named_children if c.type == "tag_name"), None)
    return parsed.slice(name.start_byte, name.end_byte).lower() if name else None


def _attributes(parsed: ParsedFile, element: Node) -> dict[str, str]:
    tag = _start_tag(element)
    if tag is None:
        return {}
    found: dict[str, str] = {}
    for attribute in tag.named_children:
        if attribute.type != "attribute":
            continue
        name = next(
            (c for c in attribute.named_children if c.type == "attribute_name"), None
        )
        value = next(
            (c for c in attribute.named_children
             if c.type in {"quoted_attribute_value", "attribute_value"}), None
        )
        if name is None:
            continue
        text = ""
        if value is not None:
            inner = next(
                (c for c in value.named_children if c.type == "attribute_value"), value
            )
            text = parsed.slice(inner.start_byte, inner.end_byte).strip("\"'")
        found[parsed.slice(name.start_byte, name.end_byte).lower()] = text
    return found


def _following_text(parsed: ParsedFile, container: Node, after: Node) -> str:
    """Text of the first sibling element following ``after`` that has any."""
    siblings = list(container.named_children)
    try:
        start = siblings.index(after) + 1
    except ValueError:
        return ""
    for sibling in siblings[start:]:
        if _tag_name(parsed, sibling) in HEADING_TAGS:
            break
        text = _element_text(parsed, sibling)
        if text:
            return text
    return ""


def _element_text(parsed: ParsedFile, element: Node) -> str:
    parts = [
        parsed.slice(c.start_byte, c.end_byte)
        for c in element.named_children
        if c.type == "text"
    ]
    return " ".join(" ".join(parts).split())


# --- shared ------------------------------------------------------------------

def _doc_node(
    parsed: ParsedFile,
    node: Node,
    title: str,
    slug: str,
    level: int,
    kind: str,
    info: str | None,
    parent: str | None,
) -> DocNode:
    return DocNode(
        title=title,
        slug=slug,
        level=level,
        kind=kind,
        lang=parsed.spec.lang,
        path=str(parsed.path),
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        info=info,
        parent=parent,
    )
