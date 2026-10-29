"""The injection hook and capture's narration against the scratch database."""

from __future__ import annotations

import io
import json
import sys

import pytest

import inject_system_prompt as isp
import log_event
from conftest import ROOT

pytestmark = pytest.mark.graph

SESSION = "s-inject"


def start(session_id: str = SESSION) -> dict:
    return {"session_id": session_id, "hook_event_name": "SessionStart",
            "source": "startup", "cwd": str(ROOT)}


def run_hook(main, payload: dict, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert main() == 0
    return capsys.readouterr()


def session_start(graph) -> dict:
    return graph.rows(
        "MATCH (:Session {session_id: $id})-[:HAS_EVENT]->(e:SessionEvent "
        "{event_name: 'SessionStart'}) RETURN properties(e) AS e",
        id=SESSION,
    )[0]["e"]


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setenv("UAM_AGENT_NAME", "default")


def test_an_empty_graph_is_seeded_once_from_the_bundled_default(graph, agent):
    assert isp.resolve_prompt("default") == (isp.read_bundled_prompt(), "neo4j")
    node = graph.value("MATCH (p:SystemPrompt {name: 'default'}) RETURN properties(p)")
    assert sorted(node) == ["content", "created_at", "name"]

    graph.session.run(
        "MATCH (p:SystemPrompt {name: 'default'}) SET p.content = 'Team instructions.'"
    ).consume()
    assert isp.resolve_prompt("default") == ("Team instructions.", "neo4j")
    assert graph.value("MATCH (p:SystemPrompt) RETURN count(p)") == 1


def test_the_event_names_the_prompt_and_links_to_it(graph, agent, monkeypatch, capsys):
    out = run_hook(isp.main, start(), monkeypatch, capsys)
    injected = json.loads(out.out)["hookSpecificOutput"]
    assert injected["hookEventName"] == "SessionStart"
    assert injected["additionalContext"] == isp.read_bundled_prompt()

    event = session_start(graph)
    assert event["prompt_name"] == "default"
    assert event["prompt_source"] == "neo4j"
    assert "prompt_content" not in event and "prompt_version" not in event
    links = graph.rows(
        "MATCH (:SessionEvent {session_id: $id})-[r:INJECTED_PROMPT]->(p:SystemPrompt) "
        "RETURN p.name AS name, properties(r) AS props",
        id=SESSION,
    )
    assert links == [{"name": "default", "props": {}}]


def test_a_bundled_prompt_is_recorded_without_a_link(graph, agent, monkeypatch, capsys):
    monkeypatch.setattr(isp, "fetch_prompt_from_neo4j", lambda name: None)
    out = run_hook(isp.main, start(), monkeypatch, capsys)
    assert json.loads(out.out)["hookSpecificOutput"]["additionalContext"]
    assert "from bundled" in out.err

    assert session_start(graph)["prompt_source"] == "bundled"
    assert graph.value("MATCH ()-[r:INJECTED_PROMPT]->() RETURN count(r)") == 0


def test_capture_and_injection_share_one_session_start(graph, agent, monkeypatch, capsys):
    out = run_hook(log_event.main, start(), monkeypatch, capsys)
    assert out.out == ""
    assert f"[log_event] captured SessionStart for session {SESSION}" in out.err
    run_hook(isp.main, start(), monkeypatch, capsys)

    assert graph.value(
        "MATCH (:Session {session_id: $id})-[:HAS_EVENT]->(e:SessionEvent) "
        "RETURN count(e)",
        id=SESSION,
    ) == 1
    assert session_start(graph)["prompt_name"] == "default"
