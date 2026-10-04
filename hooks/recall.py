#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["neo4j>=5.26.0", "litellm>=1.0"]
# ///
"""Recall: push episodes into a session, and record what it received.

One script, three entry points, chosen by the hook event:

- ``SessionStart`` (every source): a recap of recent project activity, so
  the agent starts aware of the team's work. Selection is code, by recency;
  no model call. The block names the project, labels itself as history, and
  says how to open more, because it lands beside the standing instructions
  from another hook and must stand alone.
- ``UserPromptSubmit``: up to three episodes from other sessions that the
  prompt is likely about. A candidate must clear a relevance floor, so an
  unrelated prompt receives nothing.
- ``PostToolUse`` on the memory server's ``search_episodic`` and
  ``expand_episodic``: records what those tools returned, so the delivery record covers what the agent
  opened itself, not only what hooks pushed.

Every delivery is recorded on the event that carried it (the exact block,
and ``INJECTED_AT`` per memory), and ``INJECTED_IN`` keeps what the
session's current context has seen, so the same thing is not sent twice.
Delivery establishes exposure, not influence.

Each entry point works within a time budget and returns nothing when the
store is slow or unavailable: a recap that cannot be built is omitted.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from dataclasses import dataclass
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
# nothing. The prompt budget includes the query embedding, when one is
# configured. The hook timeouts in hooks.json are only a backstop.
BUDGETS = {"SessionStart": 5.0, "UserPromptSubmit": 3.0, "PostToolUse": 5.0}
FINALIZE_SECONDS = 2.0
# mcp__plugin_unified-agent-memory_memory__search_episodic as a plugin
# server; mcp__memory__search_episodic when the same server is configured
# directly. The delivery channel is the verb: search or expand.
MEMORY_TOOL = re.compile(r"_memory__(search|expand)_episodic$")
_LEADING_ID = re.compile(r"^#([os]\d+)\b")


@dataclass
class Delivery:
    """A block prepared for the harness, and what it carries."""

    session_id: str
    event_id: str
    block: str
    hook_event: str
    memories: list[dict]
    generation: int


def _memories(rows: list[dict], detail: str = "title") -> list[dict]:
    return [
        {"key": row["key"], "version": row.get("version") or 1, "detail": detail}
        for row in rows
    ]


def recap(db, payload: dict) -> Delivery | None:
    """Recent sessions and activity in the project, as a session-start block."""
    session_id = str(payload.get("session_id") or "")
    event_id = append_event(db, session_id, "SessionStart", build_event_props(payload))
    run = episodes.reader(db)
    state = episodes.session_state(run, session_id)
    if not state or not state["project"]:
        return None
    rows = episodes.recap_rows(run, state["project"], session_id, state["user"])
    seen = episodes.delivered(run, session_id, state["generation"])
    sessions = episodes.unseen(rows["sessions"], seen)
    observations = episodes.unseen(rows["observations"], seen)
    own = episodes.unseen([rows["own"]], seen) if rows["own"] else []
    block = episodes.render_recap(
        state["project"], sessions, observations, own[0] if own else None
    )
    if not block:
        return None
    shown = {row["key"]: row for row in sessions + own + observations}
    memories = _memories(list(shown.values()))
    db.execute_write(
        episodes.prepare_delivery, event_id, block, "recap", memories,
        state["generation"],
    )
    return Delivery(session_id, event_id, block, "SessionStart", memories,
                    state["generation"])


def _query_vector(prompt: str):
    from llm import embed_texts, embeddings_ready

    if not embeddings_ready():
        return None
    try:
        return embed_texts([prompt])[0]
    except Exception as exc:
        print(f"[recall] prompt embedding failed, fulltext only: {exc}", file=sys.stderr)
        return None


def prompt_episodes(db, payload: dict) -> Delivery | None:
    """Episodes from other sessions that the prompt is likely about."""
    from llm import embeddings_ready

    session_id = str(payload.get("session_id") or "")
    prompt = str(payload.get("prompt") or "")
    if not episodes.prompt_terms(prompt) and not embeddings_ready():
        return None
    run = episodes.reader(db)
    state = episodes.session_state(run, session_id)
    if not state or not state["project"]:
        return None
    rows = episodes.related(
        run, prompt, _query_vector(prompt), state["project"], session_id
    )
    rows = episodes.unseen(rows, episodes.delivered(run, session_id, state["generation"]))
    block = episodes.render_related(state["project"], rows)
    if not block:
        return None
    event_id = append_event(
        db, session_id, "UserPromptSubmit", build_event_props(payload)
    )
    memories = _memories(rows)
    db.execute_write(
        episodes.prepare_delivery, event_id, block, "prompt", memories,
        state["generation"],
    )
    return Delivery(session_id, event_id, block, "UserPromptSubmit", memories,
                    state["generation"])


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

    ``search_episodic`` rows are titles. ``expand_episodic`` opened one
    record in full, the one its response leads with, unless it paged source events; its
    neighbor rows are titles. A subagent's context is its own, so its
    deliveries are recorded but never suppress the main context's.
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
    run = episodes.reader(db)
    state = episodes.session_state(run, session_id)
    if not state:
        return None
    ids = episodes.display_ids(text)
    refs = episodes.resolve_display_ids(run, ids)
    tool_input = payload.get("tool_input") or {}
    opened = None
    if channel == "expand" and not (
        isinstance(tool_input, dict) and tool_input.get("events")
    ):
        leading = _LEADING_ID.match(text.lstrip())
        opened = leading.group(1) if leading else None
    memories = [
        {
            "key": refs[ref]["key"],
            "version": refs[ref]["version"] or 1,
            "detail": "full" if ref == opened else "title",
        }
        for ref in ids
        if ref in refs
    ]
    agent_id = payload.get("agent_id")

    def record(tx):
        episodes.prepare_delivery(
            tx, event_id, text, channel, memories, state["generation"],
            status="returned", agent_id=agent_id,
        )
        episodes.mark_received(
            tx, session_id, memories, state["generation"], main_context=not agent_id
        )

    db.execute_write(record)
    return None


HANDLERS = {
    "SessionStart": recap,
    "UserPromptSubmit": prompt_episodes,
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


def finalize(db, delivery: Delivery) -> None:
    """After the block is handed back: mark it returned, and count it as seen."""

    def work(tx):
        episodes.mark_returned(tx, delivery.event_id)
        episodes.mark_received(
            tx, delivery.session_id, delivery.memories, delivery.generation
        )

    db.execute_write(work)


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
        delivery, hung = within(BUDGETS[event], handler, db, payload)
        if hung:
            _abandon()
        if delivery:
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": delivery.hook_event,
                            "additionalContext": delivery.block,
                        }
                    }
                ),
                flush=True,
            )
            _, hung = within(FINALIZE_SECONDS, finalize, db, delivery)
            if hung:
                _abandon()
        db.close()
        driver.close()
    except Exception as exc:  # hook must never crash the session
        print(f"[recall] error: {exc!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
