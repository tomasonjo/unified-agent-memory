"""Recall hooks against the scratch database: the chapter's handoff, end to end."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

import episodes
import extract_memory as em
import recall
from conftest import (
    ANALYST,
    MARIA,
    ROOT,
    TEST_DATABASE,
    observation,
    summary,
    turn,
)
from test_extraction import FIRST_PASS, maria_first_turn

pytestmark = pytest.mark.graph

HEADLINE = "Renewal drop explained; dashboard query corrected"


def start(session_id: str, source: str = "startup") -> dict:
    return {"session_id": session_id, "hook_event_name": "SessionStart",
            "source": source, "cwd": str(ROOT)}


@pytest.fixture
def maria_worked(graph, model, monkeypatch):
    """Maria's investigation, captured and consolidated; then the analyst's turn."""
    maria_first_turn()
    model(FIRST_PASS)
    em.consolidate(["s-maria"])
    monkeypatch.setenv("UAM_USER_ID", ANALYST)
    return graph


def delivered_at(graph, event_name: str, session_id: str) -> list[str]:
    return [
        row["memory"]
        for row in graph.rows(
            "MATCH (m)-[:INJECTED_AT]->(e:SessionEvent {event_name: $name, session_id: $sid}) "
            "RETURN coalesce(m.display_id, m.id) AS memory ORDER BY memory",
            name=event_name, sid=session_id,
        )
    ]


def test_a_fresh_analyst_starts_with_marias_handoff(maria_worked):
    graph = maria_worked
    block = recall.recap(graph.session, start("s-analyst"))
    lines = block.splitlines()
    assert lines[:3] == ["Previously, on renewal-analysis:", "", "Recent sessions:"]
    assert lines[3] == f"- #s1 · session · just now · {MARIA} · {HEADLINE}"
    assert lines[4:8] == [
        "",
        "Recent activity:",
        "- #o1 · discovery · just now · Renewal drop traced to March pipeline change",
        "- #o2 · bugfix · just now · Dashboard query corrected for reactivated contracts",
    ]
    assert block.endswith(episodes.FRAMING)
    assert "Where you left off" not in block  # Maria's stays in her summary

    event = graph.rows(
        "MATCH (e:SessionEvent {session_id: 's-analyst', event_name: 'SessionStart'}) "
        "RETURN e.recall_block AS block, e.recall_channel AS channel"
    )[0]
    assert event == {"block": block, "channel": "recap"}
    assert delivered_at(graph, "SessionStart", "s-analyst") == ["o1", "o2", "sum:s-maria"]

    # Which sessions received Maria's handoff: one hop from the event.
    assert graph.value(
        "MATCH (:SessionSummary {id: 'sum:s-maria'})-[:INJECTED_AT]->(:SessionEvent)"
        "<-[:HAS_EVENT]-(s:Session) RETURN collect(s.session_id)"
    ) == ["s-analyst"]

    # Acceptance check 2: the recap's session id opens the unfinished work.
    handoff = episodes.expand(episodes.reader(graph.session), "#s1")
    assert "Progress: Corrected the current dashboard query. Historical reports that cross March 3 are not yet checked." in handoff


def test_a_returning_user_sees_where_they_left_off(maria_worked, monkeypatch):
    monkeypatch.setenv("UAM_USER_ID", MARIA)
    block = recall.recap(maria_worked.session, start("s-maria-2"))
    assert (
        "  Where you left off: Corrected the current dashboard query. Historical reports that cross March 3 are not yet checked."
        in block.splitlines()
    )


def test_an_empty_project_gets_no_block(graph):
    assert recall.recap(graph.session, start("s-first")) is None


def test_each_session_start_records_its_own_recap(maria_worked):
    graph = maria_worked
    first = recall.recap(graph.session, start("s-analyst"))
    again = recall.recap(graph.session, start("s-analyst", "compact"))
    assert f"#s1 · session · just now · {MARIA}" in again
    assert graph.rows(
        "MATCH (e:SessionEvent {session_id: 's-analyst', event_name: 'SessionStart'}) "
        "OPTIONAL MATCH (m)-[:INJECTED_AT]->(e) "
        "RETURN e.source AS source, e.recall_block AS block, count(m) AS memories "
        "ORDER BY source"
    ) == [
        {"source": "compact", "block": again, "memories": 3},
        {"source": "startup", "block": first, "memories": 3},
    ]


def test_an_expanded_record_is_recorded_on_its_tool_event(maria_worked):
    graph = maria_worked
    opened = episodes.expand(episodes.reader(graph.session), "#o1")
    recall.tool_delivery(graph.session, {
        "session_id": "s-analyst", "hook_event_name": "PostToolUse", "cwd": str(ROOT),
        "tool_name": "mcp__plugin_unified-agent-memory_memory__expand_episodic",
        "tool_input": {"id": "#o1"}, "tool_use_id": "toolu_expand",
        "tool_response": [{"type": "text", "text": opened}],
    })
    assert delivered_at(graph, "PostToolUse", "s-analyst") == ["o1", "o2", "sum:s-maria"]
    assert graph.rows(
        "MATCH (e:SessionEvent {event_name: 'PostToolUse', session_id: 's-analyst'}) "
        "RETURN e.recall_block AS block, e.recall_channel AS channel"
    ) == [{"block": opened, "channel": "expand"}]


def test_the_delivery_keeps_the_handoff_the_session_received(maria_worked, model, monkeypatch):
    graph = maria_worked
    recall.recap(graph.session, start("s-analyst"))

    monkeypatch.setenv("UAM_USER_ID", MARIA)
    turn("s-maria", "Check the historical reports.", [],
         "All historical reports that cross March 3 were checked.")
    model({"observations": [observation("change", "Historical reports checked")],
           "summary": summary("Renewal drop resolved; history checked",
                              "Corrected the query and checked history.")})
    em.consolidate(["s-maria"])

    assert graph.value(
        "MATCH (:Session {session_id: 's-maria'})-[:HAS_SUMMARY]->(s) RETURN s.version"
    ) == 2
    # The link leads to the current summary; the event keeps what was shown.
    assert graph.value(
        "MATCH (s:SessionSummary)-[:INJECTED_AT]->(:SessionEvent {session_id: 's-analyst'}) "
        "RETURN s.headline"
    ) == "Renewal drop resolved; history checked"
    block = graph.value(
        "MATCH (e:SessionEvent {session_id: 's-analyst', event_name: 'SessionStart'}) "
        "RETURN e.recall_block"
    )
    assert HEADLINE in block and "history checked" not in block


def run_hook(payload: dict, **env) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "recall.py")],
        input=json.dumps(payload), text=True, capture_output=True, timeout=30,
        env={**os.environ, "NEO4J_DATABASE": TEST_DATABASE, **env},
    )


def test_the_hook_script_returns_the_recap_it_recorded(maria_worked):
    done = run_hook(start("s-analyst"), UAM_USER_ID=ANALYST)
    assert done.returncode == 0, done.stderr
    output = json.loads(done.stdout)["hookSpecificOutput"]
    assert output["hookEventName"] == "SessionStart"
    assert output["additionalContext"].startswith("Previously, on renewal-analysis:")
    assert maria_worked.value(
        "MATCH (e:SessionEvent {session_id: 's-analyst', event_name: 'SessionStart'}) "
        "RETURN e.recall_block"
    ) == output["additionalContext"]


def test_an_unreachable_store_costs_the_block_not_the_session(graph):
    started = time.monotonic()
    done = run_hook(start("s-analyst"), NEO4J_URI="bolt://127.0.0.1:1")
    assert done.returncode == 0 and done.stdout == ""
    assert time.monotonic() - started < 10
