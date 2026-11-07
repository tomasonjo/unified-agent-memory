#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["neo4j>=5.26.0"]
# ///
"""Hook: capture every Claude Code lifecycle event to Neo4j.

Wired in hooks/hooks.json for SessionStart, InstructionsLoaded,
UserPromptSubmit, UserPromptExpansion, PreToolUse, PostToolUse,
PostToolUseFailure, PermissionDenied, Notification, MessageDisplay, Stop,
StopFailure, SubagentStart, SubagentStop, TaskCreated, TaskCompleted,
PreCompact, PostCompact, PostModelSwitch, and SessionEnd. Reads the hook
payload from stdin and appends one :SessionEvent to the per-session chain,
the same episodic shape the meta-knowledge-graph sister project uses::

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
- The agent's responses are stored: the final response each Stop
  carries, and the intermediate responses it writes between tool calls,
  which no other event carries. ``MessageDisplay`` fires with each batch
  of newly completed lines while a response streams (once per response,
  with the whole text, outside the interactive terminal). Each batch
  becomes one event with its ``message_id``, its ``index`` within the
  response, ``final`` on the last one, and the lines as ``delta``;
  consolidation reassembles the responses. The hook sits in the display
  path, since the terminal waits for it before showing those lines, and
  it prints nothing, so the original text is shown.
- Every :Session and :SessionEvent is stamped with a ``user_id`` (an email
  address, resolved from ``UAM_USER_ID``, then the harness's logged-in
  account, then the machine's git configuration), so sessions have owners
  and later user-scoped memory has a stable key. A session also gets a
  ``project_id`` (the main checkout's directory name, or
  ``UAM_PROJECT_ID``). Both are written once, when the session is created,
  and anchor it: ``(:User)-[:HAS_SESSION]->(:Session)`` and
  ``(:Project)-[:HAS_SESSION]->(:Session)``.
- A failed tool call keeps its reason (``tool_error``, at most 1,000
  characters): why it failed, not what it printed. A turn that an API
  error ended (StopFailure) keeps the error type and details.
- Subagents run in the background by default, so their reports reach the
  session as prompts the harness submits, as do teammate, cross-session,
  and channel messages. Each submitted prompt records its
  ``prompt_origin``: ``user``, or the wrapper tag that marks it as the
  harness's. Subagent and workflow-agent events are captured on the
  parent session's chain, told apart by ``agent_id`` and ``agent_type``.
- The main agent's Stop records the work still in flight when the turn
  ended, ``background_tasks`` (subagents, workflows, shells) and
  ``session_crons`` (scheduled prompts), as one-line strings, so a
  paused session is not mistaken for a finished one.
- Every :Session is also stamped with the ``harness`` it came from
  (``claude-code`` here; UAM_HARNESS overrides for ports), so a store
  collecting sessions from several harnesses keeps their origins apart,
  and with the ``model`` that did the work: the SessionStart payload
  when it announces one, else the last assistant message in the
  harness's own transcript. A background session also gets its
  ``job_id`` (see common.job_id), and every event the ``harness_pid``
  of the process that fired it, so each restart of a session is its
  own event.

Graph properties are flat, so ``tool_input`` is serialized to a JSON
string before storage. Connection details come from the NEO4J_* settings
in ``~/.unified-agent-memory/.env``; when the graph is unreachable the
hook reports to stderr, drops the event, and exits 0, because losing one
record is always better than blocking the session. The hook ignores
SIGTERM, which a headless run sends to the hooks of its last event as it
exits, so that event is not the one lost.
"""

from __future__ import annotations

import signal

if __name__ == "__main__":
    # A headless run (-p, the Agent SDK) exits as soon as an API error ends
    # its turn, and sends SIGTERM to the StopFailure hooks still running.
    # The write is bounded by the driver's short timeouts, so it is allowed
    # to finish rather than losing the one record of why the turn ended.
    # Set before the other imports, which take a while under load; only
    # when run as the hook, not when another hook imports this module.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

import json  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

HOOK_DIR = Path(__file__).resolve().parent
if str(HOOK_DIR) not in sys.path:
    sys.path.insert(0, str(HOOK_DIR))

from common import (  # noqa: E402
    append_session_event,
    in_llm_subprocess,
    load_env,
    prompt_origin,
    read_payload,
)

# Every text field, the serialized tool input included.
MAX_FIELD_CHARS = 8000
PRIMITIVES = (str, int, float, bool)
# A failed call's reason, which is not its result: enough to say why it
# failed, never the output of a command that printed before failing.
MAX_ERROR_CHARS = 1000
ERROR_FIELDS = ("tool_error", "error_details", "reason")
# Stop's view of the work still in flight: one line per entry, enough to
# say what is running, never a whole workflow's task list.
MAX_LISTED = 20
MAX_LINE_CHARS = 200


def _text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, default=str)


def _truncate(value, limit: int = MAX_FIELD_CHARS) -> str:
    text = _text(value)
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[truncated {len(text) - limit} chars]"


def _storable(value):
    """``value`` as Neo4j can hold it: a primitive or a list of one primitive
    type as is, anything else as JSON text. A payload field that arrives as
    an object would otherwise fail the whole write and lose the event."""
    if isinstance(value, PRIMITIVES):
        return value
    if (isinstance(value, list) and all(isinstance(v, PRIMITIVES) for v in value)
            and len({type(v) for v in value}) <= 1):
        return value
    return _text(value)


def _displayed_lines(data: dict):
    """The text a MessageDisplay flush added. Claude Code 2.1 sends it as
    ``delta``; the hooks reference documents it as ``content``."""
    if data.get("hook_event_name") != "MessageDisplay":
        return None
    return data["delta"] if "delta" in data else data.get("content")


def _effort_level(data: dict):
    effort = data.get("effort")
    return effort.get("level") if isinstance(effort, dict) else effort


def _listed(entries, line) -> list[str] | None:
    """Up to MAX_LISTED entries as one-line strings, or None when empty."""
    if not isinstance(entries, list):
        return None
    lines = [_truncate(" ".join(line(e).split()), MAX_LINE_CHARS)
             for e in entries if isinstance(e, dict)]
    if len(lines) > MAX_LISTED:
        lines = lines[:MAX_LISTED] + [f"... and {len(lines) - MAX_LISTED} more"]
    return lines or None


def _task_line(task: dict) -> str:
    head = f"{task.get('type') or 'task'} ({task.get('status') or 'unknown'})"
    who = (task.get("name") or task.get("agent_type")
           or "/".join(filter(None, (task.get("server"), task.get("tool")))))
    what = task.get("description") or task.get("command")
    body = " · ".join(filter(None, (who, what)))
    return f"{head}: {body}" if body else head


def _cron_line(cron: dict) -> str:
    when = "recurring" if cron.get("recurring") else "once"
    return f"{cron.get('schedule') or '?'} ({when}): {cron.get('prompt') or ''}"


def build_event_props(data: dict) -> dict:
    """Flatten the hook payload into :SessionEvent properties."""
    event_name = data.get("hook_event_name")
    response = data.get("tool_response")
    prompt = data.get("prompt")
    tool_event = "tool_name" in data
    # Background work is the parent session's, so only the main agent's
    # Stop records it: every SubagentStop of a workflow carries the same
    # list again.
    stop = event_name == "Stop"
    props = {
        "cwd": data.get("cwd"),
        # One id per user prompt, shared by every event of its turn. Part of
        # the content hash, so a turn that repeats an earlier one word for
        # word (a second "continue", a second "Done.") is still its own
        # event, while parallel hooks given the same payload still collapse.
        "prompt_id": data.get("prompt_id"),
        "permission_mode": data.get("permission_mode"),
        "effort_level": _effort_level(data),
        "source": data.get("source"),
        "model": data.get("model"),
        "prompt": prompt,
        # Background agents, teammates, and other sessions reach the
        # session as submitted prompts; see common.prompt_origin.
        "prompt_origin": (prompt_origin(prompt)
                          if event_name == "UserPromptSubmit" and prompt else None),
        # UserPromptExpansion: the command a typed prompt expanded.
        "expansion_type": data.get("expansion_type"),
        "command_name": data.get("command_name"),
        "command_args": data.get("command_args"),
        "command_source": data.get("command_source"),
        "tool_name": data.get("tool_name"),
        "tool_use_id": data.get("tool_use_id"),
        "tool_input": data.get("tool_input"),
        # PostToolUseFailure carries the reason as ``error``; StopFailure
        # carries the API error type there, with details beside it.
        "tool_error": data.get("tool_error") or (data.get("error") if tool_event else None),
        "error": None if tool_event else data.get("error"),
        "error_details": data.get("error_details"),
        # Why a session ended, or why auto mode denied a call.
        "reason": data.get("reason"),
        "is_interrupt": data.get("is_interrupt"),
        "last_assistant_message": data.get("last_assistant_message"),
        # MessageDisplay: one flush of a response streaming to the screen.
        "turn_id": data.get("turn_id"),
        "message_id": data.get("message_id"),
        "index": data.get("index"),
        "final": data.get("final"),
        "delta": _displayed_lines(data),
        "stop_hook_active": data.get("stop_hook_active"),
        "background_tasks": _listed(data.get("background_tasks"), _task_line) if stop else None,
        "session_crons": _listed(data.get("session_crons"), _cron_line) if stop else None,
        "agent_id": data.get("agent_id"),
        "agent_type": data.get("agent_type"),
        "agent_transcript_path": data.get("agent_transcript_path"),
        # TaskCreated and TaskCompleted.
        "task_id": data.get("task_id"),
        "task_subject": data.get("task_subject"),
        "task_description": data.get("task_description"),
        "teammate_name": data.get("teammate_name"),
        # PreCompact and PostCompact; the harness's own summary of the
        # conversation it compacted.
        "trigger": data.get("trigger"),
        "custom_instructions": data.get("custom_instructions"),
        "compact_summary": data.get("compact_summary"),
        # PostModelSwitch.
        "from_model": data.get("from_model"),
        "to_model": data.get("to_model"),
        "requested_model": data.get("requested_model"),
        # InstructionsLoaded: which CLAUDE.md or rules file entered context.
        "file_path": data.get("file_path"),
        "memory_type": data.get("memory_type"),
        "load_reason": data.get("load_reason"),
        "trigger_file_path": data.get("trigger_file_path"),
        "parent_file_path": data.get("parent_file_path"),
        "notification_type": data.get("notification_type"),
        "message": data.get("message"),
        "transcript_path": data.get("transcript_path"),
    }
    props = {k: _storable(v) for k, v in props.items() if v is not None}
    for field, value in props.items():
        if isinstance(value, str):
            limit = MAX_ERROR_CHARS if field in ERROR_FIELDS else MAX_FIELD_CHARS
            props[field] = _truncate(value, limit)
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
        data = read_payload()
        session_id = str(data.get("session_id") or "unknown")
        event_name = str(data.get("hook_event_name") or "unknown")
        append_session_event(session_id, event_name, build_event_props(data))
        print(
            f"[log_event] captured {event_name} for session {session_id}",
            file=sys.stderr,
        )
    except Exception as exc:  # hook must never crash the session
        print(f"[log_event] error: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
