"""Output shaping and the token budget.

SPEC §V.1 — every tool takes ``max_tokens``. A response that would exceed it is
trimmed, flagged ``truncated: true``, and told the caller which argument
narrows it. Nothing is ever dropped silently.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

#: Rough bytes-per-token for source text. Deliberately conservative.
CHARS_PER_TOKEN = 4
DEFAULT_MAX_TOKENS = 4000
#: Always leave room for the envelope even at an absurd budget.
MIN_ITEMS = 1


def estimate_tokens(value: Any) -> int:
    """Cheap token estimate. Fast and slightly pessimistic beats exact here."""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, default=str)
    return max(1, len(text) // CHARS_PER_TOKEN)


def fit(items: list[dict], budget: int, overhead: int = 0) -> tuple[list[dict], bool]:
    """Longest prefix of ``items`` that fits the budget (SPEC §V.1)."""
    kept: list[dict] = []
    used = overhead
    for item in items:
        cost = estimate_tokens(item)
        if kept and used + cost > budget:
            return kept, True
        kept.append(item)
        used += cost
    return kept, False


def clip(text: str, budget: int) -> tuple[str, bool]:
    """Trim a single string to the budget, marking it if anything was cut."""
    limit = max(1, budget) * CHARS_PER_TOKEN
    if len(text) <= limit:
        return text, False
    return text[: limit - 1] + "…", True


def nest(items: list[dict], id_field: str, parent_field: str) -> list[dict]:
    """Turn a flat, ordered list into a tree.

    The tree encodes containment, so the ``parent`` field is dropped and an
    empty ``children`` list is omitted entirely — on a file with thirty
    symbols that redundancy is a fifth of the response (SPEC §G, §B.2).

    An item whose parent was trimmed away stays at the root rather than
    vanishing; §V.1 forbids dropping content without saying so.
    """
    roots: list[dict] = []
    latest: dict[str, dict] = {}
    for item in items:
        node = {k: v for k, v in item.items() if k != parent_field}
        parent_id = item.get(parent_field)
        parent = latest.get(str(parent_id)) if parent_id is not None else None
        if parent is None or parent is node:
            roots.append(node)
        else:
            parent.setdefault("children", []).append(node)
        # Ids are not unique: one Lua file registers the same event name
        # twice. Rows arrive container-first, so the nearest preceding node
        # with that id is the real container.
        latest[str(item[id_field])] = node
    return roots


def enforce(payload: dict, keys: tuple[str, ...], max_tokens: int) -> dict:
    """Final guard: the assembled response must fit, not just its items.

    ``fit`` spends its budget before the list key, the errors and the narrowing
    hint exist, so a payload can pass item-wise and still overshoot §V.1 by a
    constant. Drop from the tail of the last populated list until the real
    thing fits, and mark it trimmed.
    """
    for key in reversed(keys):
        items = payload.get(key)
        if not isinstance(items, list):
            continue
        while items and estimate_tokens(payload) > max_tokens:
            items.pop()
            payload["truncated"] = True
    return payload


def envelope(**fields: Any) -> dict:
    """Base response: errors and truncated are always present (SPEC §V.5, §V.1)."""
    payload: dict[str, Any] = {"errors": [], "truncated": False}
    payload.update(fields)
    return payload


def with_errors(payload: dict, errors: Iterable) -> dict:
    payload["errors"] = [
        e.as_dict() if hasattr(e, "as_dict") else dict(e) for e in errors
    ]
    return payload


def narrow_hint(payload: dict, hint: str) -> dict:
    """Name the argument that shrinks an over-budget response (SPEC §V.1)."""
    if payload.get("truncated"):
        payload["narrow_with"] = hint
    return payload
