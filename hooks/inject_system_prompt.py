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
   default rather than falling back, so the graph is authoritative from
   the first session and the SessionStart event always has a node to
   link to. Creation only: an existing node is never modified here.
2. The bundled ``hooks/default_system_prompt.md`` file, when the graph
   is unreachable.

If neither source yields a prompt, the hook injects nothing but the
warning described below, when there is one.

The SessionStart event records the prompt's name and source. When the
prompt came from the graph, the event is also linked to the node,
``(event)-[:INJECTED_PROMPT]->(prompt)``, which holds the text, so the
event never copies it.

The Neo4j lookup goes through the hooks' shared driver, which checks
connectivity first and bounds retries, so an unreachable database delays
session start by a moment instead of stalling it. The hook never fails the
session: any error falls through to the bundled default.

A failure is still reported to the user. When the graph cannot be read, or
the SessionStart event cannot be written to it, the hook's output carries a
``systemMessage``, which Claude Code shows the user as a warning and never
passes to the model. Not every client displays it. The terminal prints it,
but the desktop app's Code tab (Claude Code 2.1.317) only records it in the
transcript. The hook therefore appends the same warning to the additional
context, with a request that the agent pass it on, so the user hears about
it in either client. The other hooks stay quiet, so a broken connection
produces one warning per session instead of one per event.
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
    env_file_path,
    graph_driver,
    in_llm_subprocess,
    link_event_to_prompt,
    load_env,
    neo4j_config,
    read_payload,
    set_event_props,
)
from log_event import build_event_props  # noqa: E402

import os  # noqa: E402

# The error the last Neo4j lookup ended with, or None when it reached the
# graph. main() turns it into the warning the user sees.
lookup_error: Exception | None = None


# Creation only: ON CREATE seeds the bundled default, and there is no SET
# outside it, so a node that already exists comes back untouched.
BOOTSTRAP_QUERY = """\
MERGE (p:SystemPrompt {name: $name})
ON CREATE SET p.content = $content,
    p.created_at = datetime($now)
RETURN p.content AS content
"""


def fetch_prompt_from_neo4j(name: str) -> str | None:
    global lookup_error
    lookup_error = None
    database = neo4j_config()[3]
    try:
        # The shared driver fails fast on an unreachable graph. A driver of
        # its own here retried the first query for about 30 seconds.
        with graph_driver() as driver:
            records, _, _ = driver.execute_query(
                "MATCH (p:SystemPrompt {name: $name}) "
                "RETURN p.content AS content LIMIT 1",
                name=name,
                database_=database,
            )
            if not records:
                seed = read_bundled_prompt()
                if not seed:
                    return None
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
                    f"{{name: {name!r}}}) from bundled default "
                    f"({len(seed)} chars)",
                    file=sys.stderr,
                )
        if records and records[0].get("content"):
            content = str(records[0]["content"])
            if content.strip():
                return content
    except Exception as exc:  # hook must never crash the session
        lookup_error = exc
        print(f"[inject_system_prompt] Neo4j lookup failed: {exc}", file=sys.stderr)
    return None


def describe_graph_error(exc: Exception, database: str) -> str:
    """A short reason for a Neo4j failure, in words a reader can act on."""
    if isinstance(exc, ImportError):
        return "the neo4j Python driver is not installed"
    code = str(getattr(exc, "code", "") or "")
    try:
        from neo4j.exceptions import AuthError, ServiceUnavailable
    except ImportError:
        AuthError = ServiceUnavailable = ()  # type: ignore[assignment]
    if isinstance(exc, AuthError) or code.endswith("Security.Unauthorized"):
        return "the username or password was rejected"
    if code.endswith("Database.DatabaseNotFound"):
        return f"there is no database named {database!r}"
    if code.endswith("Security.Forbidden"):
        return "the user is not allowed to write to the graph"
    if isinstance(exc, ServiceUnavailable):
        return "the server could not be reached"
    lines = str(exc).strip().splitlines()
    return lines[0][:200] if lines else type(exc).__name__


def graph_warning(exc: Exception, *, writing: bool) -> str:
    """The systemMessage for a graph the hook could not use."""
    uri, _, _, database = neo4j_config()
    path = env_file_path()
    try:
        shown = "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        shown = str(path)
    action = "write to" if writing else "use"
    effect = (
        "this session's events are not being recorded"
        if writing
        else "the session starts from the bundled prompt, and its events "
        "are not being recorded"
    )
    return (
        f"unified-agent-memory could not {action} Neo4j at {uri}: "
        f"{describe_graph_error(exc, database)}. Until that is fixed, "
        f"{effect}. Check the NEO4J_ settings in {shown} or your "
        "environment."
    )


def context_notice(warning: str) -> str:
    """The warning as the model reads it, for clients that hide systemMessage."""
    return (
        f"Session-start warning: {warning} "
        "Not every Claude Code client displays hook warnings, so the user "
        "may not have seen this one. Tell them about it in your first reply."
    )


def read_bundled_prompt() -> str | None:
    path = HOOK_DIR / "default_system_prompt.md"
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return content if content.strip() else None


def resolve_prompt(name: str) -> tuple[str, str] | None:
    """Return (content, source); source in {neo4j, bundled}."""
    fetched = fetch_prompt_from_neo4j(name)
    if fetched:
        return fetched, "neo4j"
    bundled = read_bundled_prompt()
    if bundled:
        return bundled, "bundled"
    return None


def main() -> int:
    global lookup_error
    lookup_error = None
    try:
        # A headless helper call spawned by hooks/llm.py needs no persona;
        # injecting one would only steer the helper prompt off course.
        if in_llm_subprocess():
            return 0
        load_env()

        try:
            payload = read_payload()
        except Exception:
            payload = {}

        name = os.getenv("UAM_AGENT_NAME", "default")
        resolved = resolve_prompt(name)
        failure = lookup_error
        warning = graph_warning(failure, writing=False) if failure else None
        if resolved is None:
            print(
                f"[inject_system_prompt] no prompt for {name!r}; injecting nothing",
                file=sys.stderr,
            )
            if warning:
                print(json.dumps({
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": context_notice(warning),
                    },
                    "systemMessage": warning,
                }))
            return 0
        prompt, source = resolved
        session_id = str(payload.get("session_id") or "unknown")
        print(
            f"[inject_system_prompt] injecting {name!r} from {source} "
            f"({len(prompt)} chars) for session {session_id}",
            file=sys.stderr,
        )

        # Record the injection on the SessionStart event itself: which
        # prompt, and where it came from. The injection is not a lifecycle
        # event, so nothing new enters the chain: this hook appends the
        # same SessionStart event the capture hook does (the shared content
        # hash collapses the two writes into one node, whichever hook lands
        # first) and then sets the injection's properties on that node.
        # A graph the lookup could not use would fail here too, so the
        # write is skipped. Its timeouts are the shared driver's, so the
        # output below waits a few seconds at most.
        if failure is None:
            try:
                event_id = append_session_event(
                    session_id,
                    str(payload.get("hook_event_name") or "SessionStart"),
                    build_event_props(payload),
                )
                set_event_props(
                    event_id,
                    {"prompt_name": name, "prompt_source": source},
                )
                # When the prompt came from the graph there is a node to
                # point at, and the node holds the text. A bundled prompt
                # ships with the plugin, so it needs no link.
                if source == "neo4j":
                    link_event_to_prompt(event_id, name)
            except Exception as exc:
                print(f"[inject_system_prompt] record failed: {exc}", file=sys.stderr)
                warning = graph_warning(exc, writing=True)

        context = prompt
        if warning:
            context = f"{prompt.rstrip()}\n\n{context_notice(warning)}"
        output: dict = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        }
        if warning:
            output["systemMessage"] = warning
        print(json.dumps(output))
    except Exception as exc:  # hook must never crash the session
        print(f"[inject_system_prompt] error: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
