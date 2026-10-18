#!/usr/bin/env python
"""dsh-everything-mcp entry point (stdio transport).

Usage::

    python server.py                # run straight from a checkout
    python -m everything_mcp        # run as a module
    dsh-everything-mcp              # console script after pip install -e .

Configuration is read from environment variables (see README.md and
``everything_mcp/config.py``). An MCP client registration example lives in
``examples/cordis.patch.yml``.
"""

from __future__ import annotations

import pathlib
import sys

# 允许直接以脚本方式运行（把 everything-mcp/ 加入 sys.path）
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from everything_mcp.server import main  # noqa: E402

if __name__ == "__main__":
    main()
