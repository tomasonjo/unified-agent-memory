#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["neo4j>=5.26.0"]
# ///
"""Seed or update a ``(:SystemPrompt {name})`` node in Neo4j.

The prompt is static at runtime in the sense that the SessionStart hook only
reads it. It does not have to stay static between sessions: re-run this
script (or edit the node any other way) and every later session picks up the
new content. Re-seeding identical content is a no-op; a content change bumps
the version counter.

Usage:
    uv run --script hooks/seed_system_prompt.py                   # 'default' from prompts/default_system_prompt.md
    uv run --script hooks/seed_system_prompt.py NAME              # NAME from prompts/default_system_prompt.md
    uv run --script hooks/seed_system_prompt.py NAME --file FILE  # NAME from FILE
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
if str(HOOK_DIR) not in sys.path:
    sys.path.insert(0, str(HOOK_DIR))

from common import load_env, neo4j_config, plugin_root  # noqa: E402


def ensure_schema(tx) -> None:
    tx.run(
        "CREATE CONSTRAINT IF NOT EXISTS FOR (p:SystemPrompt) "
        "REQUIRE p.name IS UNIQUE"
    )


def upsert_prompt(tx, name: str, content: str, now: str) -> dict:
    record = tx.run(
        """
        MERGE (p:SystemPrompt {name: $name})
        ON CREATE SET p.created_at = datetime($now), p.version = 1
        WITH p, p.content AS old_content
        SET p.content = $content,
            p.updated_at = datetime($now),
            p.version = CASE
                WHEN old_content IS NULL OR old_content = $content
                    THEN coalesce(p.version, 1)
                ELSE coalesce(p.version, 1) + 1
            END
        RETURN
            CASE
                WHEN old_content IS NULL THEN 'created'
                WHEN old_content = $content THEN 'unchanged'
                ELSE 'updated'
            END AS action,
            p.version AS version
        """,
        name=name,
        content=content,
        now=now,
    ).single()
    if not record:
        return {"action": "created", "version": 1}
    return {"action": str(record["action"]), "version": int(record["version"])}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("name", nargs="?", default="default")
    parser.add_argument("--file", type=Path, default=None)
    args = parser.parse_args()

    source = args.file or (plugin_root() / "prompts" / "default_system_prompt.md")
    content = source.read_text()

    load_env()

    from neo4j import GraphDatabase

    uri, user, password, database = neo4j_config()
    now = datetime.now(timezone.utc).isoformat()

    with GraphDatabase.driver(uri, auth=(user, password)) as driver:
        with driver.session(database=database) as session:
            session.execute_write(ensure_schema)
            result = session.execute_write(upsert_prompt, args.name, content, now)

    print(
        f"Seeded (:SystemPrompt {{name: '{args.name}'}}) from {source.name}: "
        f"{result['action']}, v{result['version']}, {len(content)} chars"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
