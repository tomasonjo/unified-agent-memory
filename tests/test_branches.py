"""A subagent's events on a branch of their own, linked by SPAWNED and RETURNED_TO."""

from __future__ import annotations

import json

import pytest

import episodes
from common import delivered_agent_ids
from conftest import capture
from log_event import build_event_props

S = "s-maria"


def report(agent_id: str, text: str) -> str:
    return f'<agent-message from="{agent_id}">\n[Subagent hand-back] {text}\n'


def launched(agent_id: str) -> dict:
    return {"status": "async_launched", "agentId": agent_id, "description": "Trace it"}


# -- capture, without a graph ------------------------------------------------


def test_a_submitted_prompt_names_the_agents_whose_reports_it_delivers():
    assert delivered_agent_ids(report("a-1", "Found it.")) == ["a-1"]
    both = report("a-1", "One.") + report("a-2", "Two.")
    assert delivered_agent_ids(both) == ["a-1", "a-2"]
    note = ("<task-notification>\n<task-id>a-3</task-id>\n<status>completed</status>\n"
            "</task-notification>\n<task-notification><task-id>a-4</task-id>"
            "</task-notification>")
    assert delivered_agent_ids(note) == ["a-3", "a-4"]
    # A typed prompt delivers nothing, even when it quotes the markup.
    assert delivered_agent_ids('see <agent-message from="a-1"> above') == []

    props = build_event_props({"hook_event_name": "UserPromptSubmit", "prompt": both})
    assert props["delivered_agent_ids"] == ["a-1", "a-2"]
    typed = build_event_props({"hook_event_name": "UserPromptSubmit", "prompt": "Fix it"})
    assert "delivered_agent_ids" not in typed


def test_the_agent_calls_response_names_the_subagent_it_started():
    background = build_event_props({
        "hook_event_name": "PostToolUse", "tool_name": "Agent",
        "tool_input": {"subagent_type": "Explore"}, "tool_response": launched("a-1"),
    })
    assert background["spawned_agent_id"] == "a-1"
    assert background["spawn_status"] == "async_launched"
    foreground = build_event_props({
        "hook_event_name": "PostToolUse", "tool_name": "Agent",
        "tool_response": json.dumps({"status": "completed", "agentId": "a-2",
                                     "content": [{"type": "text", "text": "Done"}]}),
    })
    assert (foreground["spawned_agent_id"], foreground["spawn_status"]) == ("a-2", "completed")
    other = build_event_props({
        "hook_event_name": "PostToolUse", "tool_name": "Bash",
        "tool_response": {"agentId": "a-3"},
    })
    assert "spawned_agent_id" not in other


# -- the graph -----------------------------------------------------------------


def main_chain(graph) -> list[str]:
    rows = graph.rows(
        "MATCH (:Session {session_id: $s})-[:FIRST_EVENT]->(first) "
        "MATCH path = (first)-[:NEXT*0..]->(e) "
        "RETURN e.event_name AS name, e.agent_id AS agent ORDER BY length(path)",
        s=S,
    )
    return [(r["name"], r["agent"]) for r in rows]


def branch(graph, agent_id: str) -> list[str]:
    rows = graph.rows(
        "MATCH (start:SessionEvent {session_id: $s, agent_id: $a, "
        "event_name: 'SubagentStart'})-[:BRANCH_LATEST]->() "
        "MATCH path = (start)-[:NEXT*0..]->(e) "
        "RETURN e.event_name AS name ORDER BY length(path)",
        s=S, a=agent_id,
    )
    return [r["name"] for r in rows]


def links(graph, rel: str) -> set[tuple[str, str]]:
    rows = graph.rows(
        f"MATCH (a:SessionEvent)-[:{rel}]->(b:SessionEvent) "
        "RETURN coalesce(a.agent_id, a.spawned_agent_id) AS agent, b.event_name AS target"
    )
    return {(r["agent"], r["target"]) for r in rows}


@pytest.mark.graph
def test_parallel_background_subagents_each_get_a_branch(graph):
    capture(S, "SessionStart", source="startup")
    capture(S, "UserPromptSubmit", prompt="Why did renewals drop?", prompt_id="p1")
    capture(S, "PreToolUse", tool_name="Agent", tool_use_id="t-1",
            tool_input={"subagent_type": "Explore"}, prompt_id="p1")
    capture(S, "PreToolUse", tool_name="Agent", tool_use_id="t-2",
            tool_input={"subagent_type": "Explore"}, prompt_id="p1")
    # The first call's response lands after its subagent starts, the second's before.
    capture(S, "SubagentStart", agent_id="a-1", agent_type="Explore", prompt_id="p1")
    capture(S, "PostToolUse", tool_name="Agent", tool_use_id="t-1",
            tool_response=launched("a-1"), prompt_id="p1")
    capture(S, "PostToolUse", tool_name="Agent", tool_use_id="t-2",
            tool_response=launched("a-2"), prompt_id="p1")
    capture(S, "SubagentStart", agent_id="a-2", agent_type="Explore", prompt_id="p1")
    capture(S, "PreToolUse", tool_name="Bash", tool_use_id="t-3", agent_id="a-1",
            agent_type="Explore", tool_input={"command": "psql -f q1.sql"}, prompt_id="p1")
    capture(S, "PreToolUse", tool_name="Bash", tool_use_id="t-4", agent_id="a-2",
            agent_type="Explore", tool_input={"command": "psql -f q2.sql"}, prompt_id="p1")
    capture(S, "PreToolUse", tool_name="Read", tool_use_id="t-5",
            tool_input={"file_path": "report.md"}, prompt_id="p1")
    capture(S, "PostToolUse", tool_name="Bash", tool_use_id="t-3", agent_id="a-1",
            agent_type="Explore", tool_input={"command": "psql -f q1.sql"},
            tool_response="ok", prompt_id="p1")
    capture(S, "SubagentStop", agent_id="a-1", agent_type="Explore",
            last_assistant_message="One.", prompt_id="p1")
    capture(S, "SubagentStop", agent_id="a-2", agent_type="Explore",
            last_assistant_message="Two.", prompt_id="p1")
    capture(S, "UserPromptSubmit", prompt=report("a-1", "One.") + report("a-2", "Two."),
            prompt_id="p2")
    capture(S, "Stop", last_assistant_message="Both traced.", prompt_id="p2")

    assert main_chain(graph) == [
        ("SessionStart", None), ("UserPromptSubmit", None), ("PreToolUse", None),
        ("PreToolUse", None), ("PostToolUse", None), ("PostToolUse", None),
        ("PreToolUse", None), ("UserPromptSubmit", None), ("Stop", None),
    ]
    assert graph.value(
        "MATCH (:Session {session_id: $s})-[:LATEST_EVENT]->(e) RETURN e.event_name", s=S
    ) == "Stop"
    # The session reaches the main agent's events only.
    assert graph.value(
        "MATCH (:Session {session_id: $s})-[:HAS_EVENT]->(e) RETURN count(e)", s=S
    ) == 9
    assert graph.value(
        "MATCH (:Session)-[:HAS_EVENT]->(e) WHERE e.agent_id IS NOT NULL RETURN count(e)"
    ) == 0

    assert branch(graph, "a-1") == ["SubagentStart", "PreToolUse", "PostToolUse", "SubagentStop"]
    assert branch(graph, "a-2") == ["SubagentStart", "PreToolUse", "SubagentStop"]
    assert graph.value(
        "MATCH (:SessionEvent {agent_id: 'a-1', event_name: 'SubagentStart'})"
        "-[:BRANCH_LATEST]->(t) RETURN t.event_name"
    ) == "SubagentStop"

    assert links(graph, "SPAWNED") == {("a-1", "SubagentStart"), ("a-2", "SubagentStart")}
    assert graph.value(
        "MATCH (c)-[:SPAWNED]->(s) WHERE c.spawned_agent_id <> s.agent_id RETURN count(*)"
    ) == 0
    assert links(graph, "RETURNED_TO") == {("a-1", "UserPromptSubmit"),
                                           ("a-2", "UserPromptSubmit")}


@pytest.mark.graph
def test_a_foreground_subagent_returns_to_the_call_that_started_it(graph):
    capture(S, "UserPromptSubmit", prompt="Check the dashboard query.", prompt_id="p1")
    capture(S, "PreToolUse", tool_name="Agent", tool_use_id="t-1",
            tool_input={"subagent_type": "Explore"}, prompt_id="p1")
    capture(S, "SubagentStart", agent_id="a-f", agent_type="Explore", prompt_id="p1")
    capture(S, "PreToolUse", tool_name="Grep", tool_use_id="t-2", agent_id="a-f",
            agent_type="Explore", tool_input={"pattern": "renewal"}, prompt_id="p1")
    capture(S, "SubagentStop", agent_id="a-f", agent_type="Explore",
            last_assistant_message="The filter drops March.", prompt_id="p1")
    capture(S, "PostToolUse", tool_name="Agent", tool_use_id="t-1", prompt_id="p1",
            tool_response={"status": "completed", "agentId": "a-f",
                           "content": [{"type": "text", "text": "The filter drops March."}]})

    rows = graph.rows(
        "MATCH (call:SessionEvent {spawned_agent_id: 'a-f'})-[:SPAWNED]->(start) "
        "MATCH (stop:SessionEvent {agent_id: 'a-f', event_name: 'SubagentStop'})"
        "-[:RETURNED_TO]->(back) RETURN call = back AS same, start.event_name AS start"
    )
    assert rows == [{"same": True, "start": "SubagentStart"}]
    assert branch(graph, "a-f") == ["SubagentStart", "PreToolUse", "SubagentStop"]


@pytest.mark.graph
def test_what_has_no_branch_or_no_report_is_left_unlinked(graph):
    capture(S, "UserPromptSubmit", prompt="Start a check.", prompt_id="p1")
    capture(S, "SubagentStart", agent_id="a-x", agent_type="Explore", prompt_id="p1")
    capture(S, "SubagentStop", agent_id="a-x", agent_type="Explore",
            last_assistant_message="Stopped.", prompt_id="p1")
    capture(S, "Stop", last_assistant_message="Started it.", prompt_id="p1")
    # The prompt-suggestion agent stops without ever starting.
    capture(S, "SubagentStop", agent_id="a-suggest", agent_type="",
            last_assistant_message="yes, ship it", prompt_id="p1")

    assert links(graph, "SPAWNED") == set()
    assert links(graph, "RETURNED_TO") == set()
    assert branch(graph, "a-x") == ["SubagentStart", "SubagentStop"]
    assert main_chain(graph) == [
        ("UserPromptSubmit", None), ("Stop", None), ("SubagentStop", "a-suggest"),
    ]


@pytest.mark.graph
def test_a_resumed_subagent_continues_its_branch_and_repeats_collapse(graph):
    capture(S, "PreToolUse", tool_name="Agent", tool_use_id="t-1", prompt_id="p1")
    for _ in range(2):  # the same payload twice, as two hook configs deliver it
        capture(S, "SubagentStart", agent_id="a-r", agent_type="Plan", prompt_id="p1")
        capture(S, "PostToolUse", tool_name="Agent", tool_use_id="t-1",
                tool_response=launched("a-r"), prompt_id="p1")
    capture(S, "SubagentStop", agent_id="a-r", agent_type="Plan",
            last_assistant_message="Plan drafted.", prompt_id="p1")
    capture(S, "SubagentStart", agent_id="a-r", agent_type="Plan", prompt_id="p2")
    capture(S, "SubagentStop", agent_id="a-r", agent_type="Plan",
            last_assistant_message="Plan revised.", prompt_id="p2")

    assert branch(graph, "a-r") == [
        "SubagentStart", "SubagentStop", "SubagentStart", "SubagentStop",
    ]
    assert graph.value("MATCH ()-[r:SPAWNED]->() RETURN count(r)") == 1
    assert graph.value("MATCH ()-[r:BRANCH_LATEST]->() RETURN count(r)") == 1


@pytest.mark.graph
def test_expand_pages_the_main_chain_and_then_a_branch(graph):
    capture(S, "UserPromptSubmit", prompt="Trace the drop.", prompt_id="p1")
    start = capture(S, "SubagentStart", agent_id="a-1", agent_type="Explore", prompt_id="p1")
    capture(S, "PostToolUse", tool_name="Agent", tool_use_id="t-1",
            tool_input={"subagent_type": "Explore", "description": "Trace it"},
            tool_response=launched("a-1"), prompt_id="p1")
    capture(S, "PostToolUse", tool_name="Bash", tool_use_id="t-2", agent_id="a-1",
            agent_type="Explore", tool_input={"command": "psql -f q1.sql"},
            tool_response="ok", prompt_id="p1")
    capture(S, "SubagentStop", agent_id="a-1", agent_type="Explore",
            last_assistant_message="Traced to March 3.", prompt_id="p1")
    capture(S, "UserPromptSubmit", prompt=report("a-1", "Traced to March 3."), prompt_id="p2")

    run = episodes.reader(graph.session)
    main = episodes.events_page(run, S, S, "Events:")
    assert f'[started subagent a-1: expand_episodic("{S}", events=true, cursor="{start}")]' in main
    assert "[report of subagent a-1]" in main
    assert "psql -f q1.sql" not in main

    inside = episodes.events_page(run, S, S, "Branch:", cursor=start)
    assert "psql -f q1.sql" in inside and "Traced to March 3." in inside
    assert "Trace the drop." not in inside

    count = run(episodes.EXPAND_SESSION, key=S)[0]["event_count"]
    assert count == 6
