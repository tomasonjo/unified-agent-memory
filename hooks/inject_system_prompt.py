#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["neo4j>=5.26.0"]
# ///
"""SessionStart hook: inject the agent's system prompt as additional context.

Resolution order:

1. Neo4j ``(:SystemPrompt {name})`` node, when a graph is reachable. The
   active name comes from ``UAM_AGENT_NAME`` (default ``default``). When
   the node does not exist yet, the hook creates it from the bundled
   default (v1) rather than falling back, so the graph is authoritative
   from the first session and the SessionStart event always has a node
   to link to. Creation only: an existing node is never modified here.
2. The bundled ``hooks/default_system_prompt.md`` file, when the graph
   is unreachable.
3. A minimal embedded constant, so the hook always has something to inject.

When the prompt came from the graph, the SessionStart event is linked to
the node, ``(event)-[:INJECTED_PROMPT {version}]->(prompt)``, with the
injected version pinned on the relationship.

The Neo4j lookup uses a short connection timeout so an unreachable database
delays session start by a moment instead of stalling it. The hook never
fails the session: any error falls through to the bundled default.

Seed or update the graph-backed prompt with
``skills/seed-prompt/scripts/seed_system_prompt.py``.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
if str(HOOK_DIR) not in sys.path:
    sys.path.insert(0, str(HOOK_DIR))

from common import (  # noqa: E402
    append_session_event,
    in_llm_subprocess,
    link_event_to_prompt,
    load_env,
    neo4j_config,
    set_event_props,
)
from log_event import build_event_props  # noqa: E402

import os  # noqa: E402

MINIMAL_FALLBACK_PROMPT = (
    "You are a general-purpose AI agent with persistent memory. Treat "
    "injected memory context as your starting state, lead with the user's "
    "goal, and record durable facts and corrections so future sessions can "
    "reuse them."
)


# Creation only: ON CREATE seeds the bundled default as v1, and there is
# no SET outside it, so a node that already exists comes back untouched.
# Content changes stay with the seed-prompt skill, behind an explicit
# request.
BOOTSTRAP_QUERY = """\
MERGE (p:SystemPrompt {name: $name})
ON CREATE SET p.content = $content,
    p.version = 1,
    p.created_at = datetime($now),
    p.updated_at = datetime($now)
RETURN p.content AS content, p.version AS version
"""


def fetch_prompt_from_neo4j(name: str) -> tuple[str, int | None] | None:
    try:
        from neo4j import GraphDatabase
    except ImportError:
        return None

    uri, user, password, database = neo4j_config()
    try:
        with GraphDatabase.driver(
            uri, auth=(user, password), connection_timeout=2.0
        ) as driver:
            records, _, _ = driver.execute_query(
                "MATCH (p:SystemPrompt {name: $name}) "
                "RETURN p.content AS content, p.version AS version LIMIT 1",
                name=name,
                database_=database,
            )
            if not records:
                seed = read_bundled_prompt() or MINIMAL_FALLBACK_PROMPT
                driver.execute_query(
                    "CREATE CONSTRAINT IF NOT EXISTS FOR (p:SystemPrompt) "
                    "REQUIRE p.name IS UNIQUE",
                    database_=database,
                )
                records, _, _ = driver.execute_query(
                    BOOTSTRAP_QUERY,
                    name=name,
                    content=seed,
                    now=datetime.now(timezone.utc).isoformat(),
                    database_=database,
                )
                print(
                    f"[inject_system_prompt] seeded (:SystemPrompt "
                    f"{{name: {name!r}}}) v1 from bundled default "
                    f"({len(seed)} chars)",
                    file=sys.stderr,
                )
        if records and records[0].get("content"):
            content = str(records[0]["content"])
            if content.strip():
                version = records[0].get("version")
                return content, int(version) if version is not None else None
    except Exception as exc:  # hook must never crash the session
        print(f"[inject_system_prompt] Neo4j lookup failed: {exc}", file=sys.stderr)
    return None


def read_bundled_prompt() -> str | None:
    path = HOOK_DIR / "default_system_prompt.md"
    try:
        content = path.read_text()
    except OSError:
        return None
    return content if content.strip() else None


def resolve_prompt(name: str) -> tuple[str, str, int | None]:
    """Return (content, source, version); source in {neo4j, bundled, embedded}."""
    fetched = fetch_prompt_from_neo4j(name)
    if fetched:
        content, version = fetched
        return content, "neo4j", version
    bundled = read_bundled_prompt()
    if bundled:
        return bundled, "bundled", None
    return MINIMAL_FALLBACK_PROMPT, "embedded", None


def main() -> int:
    try:
        # A headless helper call spawned by hooks/llm.py needs no persona;
        # injecting one would only steer the helper prompt off course.
        if in_llm_subprocess():
            return 0
        load_env()

        try:
            raw = sys.stdin.read()
            payload = json.loads(raw) if raw.strip() else {}
        except Exception:
            payload = {}

        name = os.getenv("UAM_AGENT_NAME", "default")
        prompt, source, version = resolve_prompt(name)
        session_id = str(payload.get("session_id") or "unknown")
        print(
            f"[inject_system_prompt] injecting {name!r} from {source} "
            f"({len(prompt)} chars) for session {session_id}",
            file=sys.stderr,
        )

        output = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": prompt,
            }
        }
        print(json.dumps(output))

        # Record the injection on the SessionStart event itself, full
        # content included, so the session can be reproduced from its
        # record. The injection is not a lifecycle event, so nothing new
        # enters the chain: this hook appends the same SessionStart event
        # the capture hook does (the shared content hash collapses the two
        # writes into one node, whichever hook lands first) and then sets
        # the injection's properties on that node. Recording must never
        # block the injection itself.
        try:
            event_id = append_session_event(
                session_id,
                str(payload.get("hook_event_name") or "SessionStart"),
                build_event_props(payload),
            )
            set_event_props(
                event_id,
                {
                    "prompt_name": name,
                    "prompt_source": source,
                    "prompt_version": version,
                    "prompt_content": prompt,
                },
            )
            # When the prompt came from the graph there is a node to
            # point at, so the event is also linked to it, with the
            # injected version pinned on the relationship.
            if source == "neo4j":
                link_event_to_prompt(event_id, name, version)
        except Exception as exc:
            print(f"[inject_system_prompt] record failed: {exc}", file=sys.stderr)
    except Exception as exc:  # hook must never crash the session
        print(f"[inject_system_prompt] error: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
