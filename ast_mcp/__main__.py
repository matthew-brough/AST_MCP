"""`python -m ast_mcp` — same entry as the `ast-mcp` console script."""

from __future__ import annotations

import sys

from ast_mcp.cli import main

if __name__ == "__main__":
    sys.exit(main())
