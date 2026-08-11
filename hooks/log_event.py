#!/usr/bin/env python3
"""Hook: capture every Claude Code lifecycle event to a local JSONL log.

Wired in hooks/hooks.json for SessionStart, UserPromptSubmit, PreToolUse,
PostToolUse, PostToolUseFailure, Notification, Stop, SubagentStart,
SubagentStop, PreCompact, and SessionEnd. Reads the hook payload from stdin
and appends one JSON line per event to a per-session file.

Storage decisions:
- Tool results are NOT stored. They are the bulk of a session and they are
  regenerable packaging; the record keeps that the tool ran, what it was
  asked, and how many characters came back (``tool_response_chars``).
- Inputs are stored: prompts and tool inputs (bounded at 4,000 chars), and
  the injection hook appends what it injected to the same log, so a session
  can be reproduced from its record.
- Every record is stamped with a ``user_id`` (an email address, resolved
  from the harness's logged-in account, then the machine's git
  configuration), so sessions have owners and later user-scoped memory
  has a stable key.

Log location: $UAM_LOG_DIR when set, else ~/.unified-agent-memory/logs/.
One file per session: <session_id>.jsonl.

Stdlib only, so it runs with plain python3 and needs no package install.
This is the starter version of event capture; the graph-backed version
(events as a linked list of :SessionEvent nodes in Neo4j) is the episodic
memory upgrade covered later.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
if str(HOOK_DIR) not in sys.path:
    sys.path.insert(0, str(HOOK_DIR))

from common import append_session_record, load_env  # noqa: E402

MAX_FIELD_CHARS = 4000
TRUNCATED_FIELDS = ("tool_input", "prompt", "last_assistant_message")


def _truncate(value):
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    if len(text) <= MAX_FIELD_CHARS:
        return value
    return text[:MAX_FIELD_CHARS] + f"...[truncated {len(text) - MAX_FIELD_CHARS} chars]"


def _size(value) -> int:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return len(text)


def build_payload(data: dict) -> dict:
    payload = dict(data)
    response = payload.pop("tool_response", None)
    if response is not None:
        payload["tool_response_chars"] = _size(response)
    for field in TRUNCATED_FIELDS:
        if payload.get(field) is not None:
            payload[field] = _truncate(payload[field])
    return payload


def main() -> int:
    try:
        load_env()
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        append_session_record(
            str(data.get("session_id") or "unknown"),
            str(data.get("hook_event_name") or "unknown"),
            build_payload(data),
        )
    except Exception as exc:  # hook must never crash the session
        print(f"[log_event] error: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
