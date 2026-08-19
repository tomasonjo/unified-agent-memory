#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["neo4j-mcp-server>=1.5"]
# ///
"""Launcher for the plugin's read-only Neo4j MCP mount.

``.mcp.json`` points here instead of at the official server directly for
one reason: configuration. An MCP server entry sees only the process
environment the harness hands it, and the plugin's settings live in the
canonical env file (``~/.unified-agent-memory/.env``). This script loads
that file exactly the way the hooks do (exported variables win, whitelist
only), resolves the same connection defaults the hooks use, pins
read-only mode, and then becomes the server via exec.

Read-only is pinned here, not left to configuration, because the mount is
read-only by construction: the model gets ``get-schema`` and
``read-cypher`` to pull from the graph, while the write path into the
graph belongs to the hooks and the seed-prompt skill, never to the model.
With ``NEO4J_READ_ONLY`` set the server does not announce its write tool
at all, which is stronger than a harness-side permission: a tool that is
never offered cannot be called.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parents[1] / "hooks"
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

from common import load_env, neo4j_config  # noqa: E402


def main() -> None:
    load_env()
    uri, user, password, database = neo4j_config()
    os.environ.update(
        {
            "NEO4J_URI": uri,
            "NEO4J_USERNAME": user,
            "NEO4J_PASSWORD": password,
            "NEO4J_DATABASE": database,
            "NEO4J_READ_ONLY": "true",
        }
    )
    os.execv(sys.executable, [sys.executable, "-m", "neo4j_mcp_server"])


if __name__ == "__main__":
    main()
