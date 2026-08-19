#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["neo4j>=5.26.0"]
# ///
"""Hook: capture every Claude Code lifecycle event to Neo4j.

Wired in hooks/hooks.json for SessionStart, UserPromptSubmit, PreToolUse,
PostToolUse, PostToolUseFailure, Notification, Stop, SubagentStart,
SubagentStop, PreCompact, and SessionEnd. Reads the hook payload from stdin
and appends one :SessionEvent to the per-session chain, the same episodic
shape the meta-knowledge-graph sister project uses::

    (:Session)-[:FIRST_EVENT]->(:SessionEvent)-[:NEXT]->(:SessionEvent)...
    (:Session)-[:HAS_EVENT]->(every :SessionEvent)
    (:Session)-[:LATEST_EVENT]->(the newest :SessionEvent)

Storage decisions:
- Tool results are NOT stored. They are the bulk of a session and they are
  regenerable packaging; the record keeps that the tool ran, what it was
  asked, and how many characters came back (``tool_response_chars``).
- Inputs are stored: prompts and tool inputs (bounded at 8,000 chars), and
  the injection hook records what it injected on the SessionStart event,
  so a session can be reproduced from its record.
- Every :Session and :SessionEvent is stamped with a ``user_id`` (an email
  address, resolved from the harness's logged-in account, then the
  machine's git configuration), so sessions have owners and later
  user-scoped memory has a stable key.
- Every :Session is also stamped with the ``harness`` it came from
  (``claude-code`` here; UAM_HARNESS overrides for ports), so a store
  collecting sessions from several harnesses keeps their origins apart,
  and with the ``model`` that did the work: the SessionStart payload
  when it announces one, else the last assistant message in the
  harness's own transcript.

Graph properties are flat, so ``tool_input`` is serialized to a JSON
string before storage. Connection details come from the NEO4J_* settings
in ``~/.unified-agent-memory/.env``; when the graph is unreachable the
hook reports to stderr, drops the event, and exits 0, because losing one
record is always better than blocking the session.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
if str(HOOK_DIR) not in sys.path:
    sys.path.insert(0, str(HOOK_DIR))

from common import append_session_event, in_llm_subprocess, load_env  # noqa: E402

MAX_FIELD_CHARS = 8000
TRUNCATED_FIELDS = ("tool_input", "prompt", "last_assistant_message")


def _text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, default=str)


def _truncate(value) -> str:
    text = _text(value)
    if len(text) <= MAX_FIELD_CHARS:
        return text
    return text[:MAX_FIELD_CHARS] + f"...[truncated {len(text) - MAX_FIELD_CHARS} chars]"


def build_event_props(data: dict) -> dict:
    """Flatten the hook payload into :SessionEvent properties."""
    response = data.get("tool_response")
    props = {
        "cwd": data.get("cwd"),
        "source": data.get("source"),
        "model": data.get("model"),
        "prompt": data.get("prompt"),
        "tool_name": data.get("tool_name"),
        "tool_use_id": data.get("tool_use_id"),
        "tool_input": data.get("tool_input"),
        "tool_error": data.get("tool_error"),
        "is_interrupt": data.get("is_interrupt"),
        "last_assistant_message": data.get("last_assistant_message"),
        "stop_hook_active": data.get("stop_hook_active"),
        "agent_id": data.get("agent_id"),
        "agent_type": data.get("agent_type"),
        "transcript_path": data.get("transcript_path"),
    }
    props = {k: v for k, v in props.items() if v is not None}
    for field in TRUNCATED_FIELDS:
        if field in props:
            props[field] = _truncate(props[field])
    if response is not None:
        props["tool_response_chars"] = len(_text(response))
    return props


def main() -> int:
    try:
        # A headless helper call spawned by hooks/llm.py is plumbing, not
        # a session; capturing it would spam the graph with one pseudo
        # session per LLM call.
        if in_llm_subprocess():
            return 0
        load_env()
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        append_session_event(
            str(data.get("session_id") or "unknown"),
            str(data.get("hook_event_name") or "unknown"),
            build_event_props(data),
        )
    except Exception as exc:  # hook must never crash the session
        print(f"[log_event] error: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
