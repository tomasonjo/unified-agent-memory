#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["neo4j>=5.26.0"]
# ///
"""Skill script: seed or update a ``(:SystemPrompt {name})`` node in Neo4j.

Bundled with the seed-prompt skill and executed by the agent on explicit
request, not by a hook. The prompt is static at runtime in the sense that
the SessionStart hook only reads it. It does not have to stay static
between sessions: re-run this script (or edit the node any other way) and
every later session picks up the new content. Re-seeding identical content
is a no-op; a content change bumps the version counter.

Usage (paths relative to the plugin root):
    uv run --script skills/seed-prompt/scripts/seed_system_prompt.py                   # 'default' from hooks/default_system_prompt.md
    uv run --script skills/seed-prompt/scripts/seed_system_prompt.py NAME              # NAME from hooks/default_system_prompt.md
    uv run --script skills/seed-prompt/scripts/seed_system_prompt.py NAME --file FILE  # NAME from FILE
    uv run --script skills/seed-prompt/scripts/seed_system_prompt.py --status          # read-only: report what seeding would do

``--status`` writes nothing. It reports whether the node exists, its
version, and whether the source content differs, in one deterministic
line, so a caller can look before writing and put a human decision
between the two.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parents[3] / "hooks"
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

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


def fetch_current(driver, database: str, name: str) -> dict | None:
    records, _, _ = driver.execute_query(
        "MATCH (p:SystemPrompt {name: $name}) "
        "RETURN p.content AS content, p.version AS version LIMIT 1",
        name=name,
        database_=database,
    )
    if not records or records[0].get("content") is None:
        return None
    version = records[0].get("version")
    return {
        "content": str(records[0]["content"]),
        "version": int(version) if version is not None else 1,
    }


def status_line(name: str, source_name: str, content: str, current: dict | None) -> str:
    label = f"(:SystemPrompt {{name: '{name}'}})"
    if current is None:
        return (
            f"No {label} in the graph; seeding from {source_name} "
            f"would create v1 ({len(content)} chars)."
        )
    version = current["version"]
    if current["content"] == content:
        return (
            f"{label} is at v{version}; {source_name} is identical "
            f"({len(content)} chars), re-seeding would change nothing."
        )
    return (
        f"{label} is at v{version} ({len(current['content'])} chars); "
        f"{source_name} differs ({len(content)} chars), "
        f"seeding would bump to v{version + 1}."
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("name", nargs="?", default="default")
    parser.add_argument("--file", type=Path, default=None)
    parser.add_argument(
        "--status",
        action="store_true",
        help="read-only: report what seeding would do, writing nothing",
    )
    args = parser.parse_args()

    source = args.file or (plugin_root() / "hooks" / "default_system_prompt.md")
    content = source.read_text()

    load_env()

    from neo4j import GraphDatabase

    uri, user, password, database = neo4j_config()

    if args.status:
        with GraphDatabase.driver(uri, auth=(user, password)) as driver:
            current = fetch_current(driver, database, args.name)
        print(status_line(args.name, source.name, content, current))
        return 0

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
