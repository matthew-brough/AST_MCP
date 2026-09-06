"""AST_MCP — structural code retrieval over tree-sitter.

Package-level payload records shared by every layer.
"""

from __future__ import annotations

from dataclasses import dataclass

__version__ = "0.4.1"


@dataclass(frozen=True, slots=True)
class Error:
    """A non-fatal problem, carried in a response instead of raised.

    SPEC §V.5: a tool never propagates an exception to the agent. Every
    failure mode becomes one of these and the payload stays valid.
    """

    code: str
    path: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {"code": self.code, "path": self.path, "detail": self.detail}
