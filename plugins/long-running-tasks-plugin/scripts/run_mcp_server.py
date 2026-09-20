#!/usr/bin/env python3
"""Launch the plugin MCP server through the locked uv environment."""

from __future__ import annotations

import os
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    os.execvp("uv", ["uv", "run", "--locked", "--project", str(root), "python", "-m", "long_running_tasks.mcp_server"])


if __name__ == "__main__":
    main()
