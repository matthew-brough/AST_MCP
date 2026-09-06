"""Module docstring."""

import os
from pathlib import Path as P


def top_level(a: int, b: str = "x") -> bool:
    """Top-level function doc."""
    return True


class Widget:
    """A widget."""

    def render(self, depth: int) -> str:
        """Render the widget."""
        return "w"

    def _hidden(self):
        return None


def undocumented(x):
    return x
