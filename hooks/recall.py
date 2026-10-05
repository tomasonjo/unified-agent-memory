#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["neo4j>=5.26.0"]
# ///
"""Recall: push episodes into a session, and record what it received.

One script, two entry points, chosen by the hook event:

- ``SessionStart`` (every source): a recap of recent project activity, so
  the agent starts aware of the team's work. Selection is code, by recency;
  no model call. The block names the project, labels itself as history, and
  says how to open more, because it lands beside the standing instructions
  from another hook and must stand alone.
- ``PostToolUse`` on the memory server's ``search_episodic`` and
  ``expand_episodic``: records what those tools returned, so the delivery
  record covers what the agent opened itself, not only what hooks pushed.

Every delivery is recorded on the event that carried it: the exact block on
the event, and ``INJECTED_AT`` from each memory it names. Each
``SessionStart`` (startup, resume, clear, compact) is its own event and gets
its own recap, so nothing tracks what an earlier context saw. Delivery
establishes exposure, not influence.

Each entry point works within a time budget and returns nothing when the
store is slow or unavailable: a recap that cannot be built is omitted.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
if str(HOOK_DIR) not in sys.path:
    sys.path.insert(0, str(HOOK_DIR))

import episodes  # noqa: E402
from common import (  # noqa: E402
    append_event,
    graph_driver,
    in_llm_subprocess,
    load_env,
    neo4j_config,
)
from log_event import build_event_props  # noqa: E402

# Seconds each entry point may take before it gives up and delivers
# nothing. The hook timeouts in hooks.json are only a backstop.
BUDGETS = {"SessionStart": 5.0, "PostToolUse": 5.0}
# mcp__plugin_unified-agent-memory_memory__search_episodic as a plugin
# server; mcp__memory__search_episodic when the same server is configured
# directly. The delivery channel is the verb: search or expand.
MEMORY_TOOL = re.compile(r"_memory__(search|expand)_episodic$")


def recap(db, payload: dict) -> str | None:
    """Recent sessions and activity in the project, as a session-start block.

    The block is recorded on its ``SessionStart`` event before it is
    returned for the harness.
    """
    session_id = str(payload.get("session_id") or "")
    event_id = append_event(db, session_id, "SessionStart", build_event_props(payload))
    run = episodes.reader(db)
    state = episodes.session_state(run, session_id)
    if not state or not state["project"]:
        return None
    rows = episodes.recap_rows(run, state["project"], session_id, state["user"])
    block = episodes.render_recap(
        state["project"], rows["sessions"], rows["observations"], rows["own"]
    )
    if not block:
        return None
    shown = rows["sessions"] + rows["observations"] + ([rows["own"]] if rows["own"] else [])
    keys = list(dict.fromkeys(row["key"] for row in shown))
    db.execute_write(episodes.record_delivery, event_id, block, "recap", keys)
    return block


def response_text(response) -> str:
    """The text a tool returned, whatever envelope the harness wraps it in."""
    if response is None:
        return ""
    if isinstance(response, str):
        return response
    texts: list[str] = []

    def walk(value) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("text"), str):
                texts.append(value["text"])
            else:
                for item in value.values():
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(response)
    return "\n".join(texts) if texts else json.dumps(response, default=str)


def tool_delivery(db, payload: dict) -> None:
    """Record what ``search_episodic`` or ``expand_episodic`` returned.

    Every memory the response names is linked to its ``PostToolUse`` event.
    A subagent's call is recorded the same way; its event carries the
    ``agent_id``.
    """
    match = MEMORY_TOOL.search(str(payload.get("tool_name") or ""))
    if not match:
        return None
    channel = match.group(1)
    text = response_text(payload.get("tool_response"))
    if not text.strip():
        return None
    session_id = str(payload.get("session_id") or "")
    event_id = append_event(db, session_id, "PostToolUse", build_event_props(payload))
    refs = episodes.resolve_display_ids(episodes.reader(db), episodes.display_ids(text))
    db.execute_write(
        episodes.record_delivery, event_id, text, channel, list(refs.values())
    )
    return None


HANDLERS = {
    "SessionStart": recap,
    "PostToolUse": tool_delivery,
}


def within(seconds: float, fn, *args):
    """``fn(*args)``, or None when it fails or runs past ``seconds``."""
    result: dict = {}

    def target() -> None:
        try:
            result["value"] = fn(*args)
        except Exception as exc:  # the session never waits on a broken store
            result["error"] = exc

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        print(f"[recall] over the {seconds:.0f}s budget; delivering nothing",
              file=sys.stderr)
        return None, True
    if "error" in result:
        print(f"[recall] error: {result['error']!r}", file=sys.stderr)
    return result.get("value"), False


def _abandon() -> None:
    """Exit now: a thread still waits on the store, and closing the driver
    under it could wait too."""
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


def main() -> int:
    try:
        # A headless helper call spawned by hooks/llm.py is not a session.
        if in_llm_subprocess():
            return 0
        load_env()
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        event = str(payload.get("hook_event_name") or "")
        handler = HANDLERS.get(event)
        session_id = str(payload.get("session_id") or "")
        if handler is None or not session_id or session_id == "unknown":
            return 0
        driver = graph_driver()
        db = driver.session(database=neo4j_config()[3])
        block, hung = within(BUDGETS[event], handler, db, payload)
        if hung:
            _abandon()
        if block:
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": event,
                            "additionalContext": block,
                        }
                    }
                ),
                flush=True,
            )
        db.close()
        driver.close()
    except Exception as exc:  # hook must never crash the session
        print(f"[recall] error: {exc!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
