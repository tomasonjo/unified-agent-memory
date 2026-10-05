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
    PROJECT,
    ROOT,
    TEST_DATABASE,
    capture,
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


def delivered_as(graph, event_name: str, session_id: str) -> list[dict]:
    return graph.rows(
        "MATCH (m)-[r:INJECTED_AT]->(e:SessionEvent {event_name: $name, session_id: $sid}) "
        "RETURN coalesce(m.display_id, m.id) AS memory, r.detail AS detail, "
        "r.version AS version, r.context_generation AS generation, "
        "r.channel AS channel, r.status AS status ORDER BY memory",
        name=event_name, sid=session_id,
    )


def test_a_fresh_analyst_starts_with_marias_handoff(maria_worked):
    graph = maria_worked
    delivery = recall.recap(graph.session, start("s-analyst"))
    lines = delivery.block.splitlines()
    assert lines[:3] == ["Previously, on renewal-analysis:", "", "Recent sessions:"]
    assert lines[3] == f"- #s1 · session · just now · {MARIA} · {HEADLINE}"
    assert lines[4:8] == [
        "",
        "Recent activity:",
        "- #o1 · discovery · just now · Renewal drop traced to March pipeline change",
        "- #o2 · bugfix · just now · Dashboard query corrected for reactivated contracts",
    ]
    assert delivery.block.endswith(episodes.FRAMING)
    assert "Where you left off" not in delivery.block  # Maria's stays in her summary

    event = graph.rows(
        "MATCH (e:SessionEvent {session_id: 's-analyst', event_name: 'SessionStart'}) "
        "RETURN e.recall_block AS block, e.recall_status AS status, e.recall_channel AS channel"
    )[0]
    assert event == {"block": delivery.block, "status": "prepared", "channel": "recap"}
    assert delivered_as(graph, "SessionStart", "s-analyst") == [
        {"memory": "o1", "detail": "title", "version": 1, "generation": 1,
         "channel": "recap", "status": "prepared"},
        {"memory": "o2", "detail": "title", "version": 1, "generation": 1,
         "channel": "recap", "status": "prepared"},
        {"memory": "sum:s-maria", "detail": "title", "version": 1, "generation": 1,
         "channel": "recap", "status": "prepared"},
    ]

    recall.finalize(graph.session, delivery)
    assert graph.value(
        "MATCH (e:SessionEvent {session_id: 's-analyst', event_name: 'SessionStart'}) "
        "RETURN e.recall_status"
    ) == "returned"
    assert graph.value(
        "MATCH (m)-[r:INJECTED_IN]->(:Session {session_id: 's-analyst'}) RETURN count(m)"
    ) == 3

    # Acceptance check 2: the recap's session id opens the unfinished work.
    handoff = episodes.expand(episodes.reader(graph.session), "#s1")
    assert "Progress: Corrected the current dashboard query. Historical reports that cross March 3 are not yet checked." in handoff


def test_a_returning_user_sees_where_they_left_off(maria_worked, monkeypatch):
    monkeypatch.setenv("UAM_USER_ID", MARIA)
    delivery = recall.recap(maria_worked.session, start("s-maria-2"))
    assert (
        "  Where you left off: Corrected the current dashboard query. Historical reports that cross March 3 are not yet checked."
        in delivery.block.splitlines()
    )


def test_an_empty_project_gets_no_block(graph):
    assert recall.recap(graph.session, start("s-first")) is None


def test_the_same_context_is_not_sent_twice_but_compaction_restores_it(maria_worked):
    graph = maria_worked
    recall.finalize(graph.session, recall.recap(graph.session, start("s-analyst")))
    assert recall.recap(graph.session, start("s-analyst", "resume")) is None
    capture("s-analyst", "PreCompact", trigger="auto")
    again = recall.recap(graph.session, start("s-analyst", "compact"))
    assert again is not None and again.generation == 2
    assert f"#s1 · session · just now · {MARIA}" in again.block


def test_a_recap_title_does_not_block_opening_the_record(maria_worked):
    graph = maria_worked
    recall.finalize(graph.session, recall.recap(graph.session, start("s-analyst")))
    opened = episodes.expand(episodes.reader(graph.session), "#o1")
    recall.tool_delivery(graph.session, {
        "session_id": "s-analyst", "hook_event_name": "PostToolUse", "cwd": str(ROOT),
        "tool_name": "mcp__plugin_unified-agent-memory_memory__expand_episodic",
        "tool_input": {"id": "#o1"}, "tool_use_id": "toolu_expand",
        "tool_response": [{"type": "text", "text": opened}],
    })
    assert {
        (row["memory"], row["detail"])
        for row in delivered_as(graph, "PostToolUse", "s-analyst")
    } == {("o1", "full"), ("o2", "title"), ("sum:s-maria", "title")}
    assert graph.value(
        "MATCH (:Observation {display_id: 'o1'})-[r:INJECTED_IN]->"
        "(:Session {session_id: 's-analyst'}) RETURN r.detail"
    ) == "full"
    assert graph.value(
        "MATCH (e:SessionEvent {event_name: 'PostToolUse', session_id: 's-analyst'}) "
        "RETURN e.recall_block"
    ) == opened


def test_a_subagents_search_does_not_count_for_the_main_context(maria_worked):
    graph = maria_worked
    capture("s-analyst", "SessionStart", source="startup")
    page, _ = episodes.recent(episodes.reader(graph.session), PROJECT)
    rows = episodes.render_rows(page)
    recall.tool_delivery(graph.session, {
        "session_id": "s-analyst", "hook_event_name": "PostToolUse", "cwd": str(ROOT),
        "tool_name": "mcp__plugin_unified-agent-memory_memory__search_episodic",
        "tool_input": {}, "tool_use_id": "toolu_search", "agent_id": "a1",
        "agent_type": "Explore", "tool_response": rows,
    })
    assert graph.value(
        "MATCH ()-[r:INJECTED_IN]->(:Session {session_id: 's-analyst'}) "
        "RETURN count(r) AS n"
    ) == 3
    assert recall.recap(graph.session, start("s-analyst", "resume")) is not None


def test_the_delivery_keeps_the_handoff_the_session_received(maria_worked, model, monkeypatch):
    graph = maria_worked
    recall.finalize(graph.session, recall.recap(graph.session, start("s-analyst")))

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
    block = graph.value(
        "MATCH (e:SessionEvent {session_id: 's-analyst', event_name: 'SessionStart'}) "
        "RETURN e.recall_block"
    )
    assert HEADLINE in block and "history checked" not in block
    assert graph.value(
        "MATCH (:SessionSummary)-[r:INJECTED_AT]->(:SessionEvent {session_id: 's-analyst'}) "
        "RETURN r.version"
    ) == 1


def run_hook(payload: dict, **env) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "recall.py")],
        input=json.dumps(payload), text=True, capture_output=True, timeout=30,
        env={**os.environ, "NEO4J_DATABASE": TEST_DATABASE, **env},
    )


def test_the_hook_script_returns_the_recap_and_marks_it_returned(maria_worked):
    done = run_hook(start("s-analyst"), UAM_USER_ID=ANALYST)
    assert done.returncode == 0, done.stderr
    output = json.loads(done.stdout)["hookSpecificOutput"]
    assert output["hookEventName"] == "SessionStart"
    assert output["additionalContext"].startswith("Previously, on renewal-analysis:")
    assert maria_worked.value(
        "MATCH (e:SessionEvent {session_id: 's-analyst', event_name: 'SessionStart'}) "
        "RETURN e.recall_status"
    ) == "returned"


def test_an_unreachable_store_costs_the_block_not_the_session(graph):
    started = time.monotonic()
    done = run_hook(start("s-analyst"), NEO4J_URI="bolt://127.0.0.1:1")
    assert done.returncode == 0 and done.stdout == ""
    assert time.monotonic() - started < 10
