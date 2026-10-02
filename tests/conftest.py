"""Shared fixtures: a scratch Neo4j database, captured events, a scripted model.

The graph tests need a database they may wipe. Name it in
``UAM_TEST_DATABASE`` (the name must contain "test"); connection settings
come from the plugin's env file or exported NEO4J_* variables, as for the
hooks. Without it, only the tests that need no graph run::

    UAM_TEST_DATABASE=uamtest uv run --with pytest --with neo4j pytest tests
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

import common  # noqa: E402

TEST_DATABASE = os.getenv("UAM_TEST_DATABASE", "")
PROJECT = "renewal-analysis"
MARIA = "maria@company.com"
ANALYST = "analyst@company.com"


def pytest_configure(config):
    config.addinivalue_line("markers", "graph: needs the scratch database")


def pytest_collection_modifyitems(config, items):
    if TEST_DATABASE and "test" in TEST_DATABASE:
        return
    skip = pytest.mark.skip(reason="set UAM_TEST_DATABASE to a scratch database")
    for item in items:
        if "graph" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def graph(monkeypatch, tmp_path):
    """An empty scratch database, with the project pinned and no embeddings."""
    common.load_env()
    monkeypatch.setenv("NEO4J_DATABASE", TEST_DATABASE)
    monkeypatch.setenv("UAM_PROJECT_ID", PROJECT)
    monkeypatch.setenv("UAM_USER_ID", MARIA)
    monkeypatch.setenv("UAM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("UAM_EMBEDDING_MODEL", raising=False)
    with common.graph_driver(connection_timeout=5.0) as driver:
        with driver.session(database=TEST_DATABASE) as session:
            session.run("MATCH (n) DETACH DELETE n").consume()
            yield Graph(driver, session)


class Graph:
    def __init__(self, driver, session):
        self.driver = driver
        self.session = session

    def rows(self, cypher: str, **params) -> list[dict]:
        return [record.data() for record in self.session.run(cypher, **params)]

    def value(self, cypher: str, **params):
        rows = self.rows(cypher, **params)
        return next(iter(rows[0].values())) if rows else None


def capture(session_id: str, event_name: str, **fields) -> str:
    """Append one event the way the capture hook does."""
    from log_event import build_event_props

    payload = {
        "session_id": session_id,
        "hook_event_name": event_name,
        "cwd": str(ROOT),
        **fields,
    }
    return common.append_session_event(session_id, event_name, build_event_props(payload))


def flushes(prompt_id: str, text: str) -> list[dict]:
    """A displayed message as MessageDisplay delivers it: one line per flush.

    Each item is the payload of one flush, for ``capture(..., "MessageDisplay", **item)``.
    """
    message_id = str(uuid.uuid4())
    lines = text.splitlines(keepends=True)
    return [
        {"prompt_id": prompt_id, "message_id": message_id, "index": index,
         "final": index == len(lines) - 1, "delta": line}
        for index, line in enumerate(lines)
    ]


def turn(
    session_id: str,
    prompt: str,
    tools: list[tuple[str, dict]],
    closing: str | None,
) -> str:
    """One turn: a prompt, tool calls (Pre and Post), and the closing message.

    ``closing=None`` is a turn the user interrupted: no Stop fires. Returns
    the turn's prompt id.
    """
    prompt_id = uuid.uuid4().hex
    capture(session_id, "UserPromptSubmit", prompt=prompt, prompt_id=prompt_id)
    for tool, tool_input in tools:
        use = f"toolu_{uuid.uuid4().hex[:12]}"
        capture(session_id, "PreToolUse", tool_name=tool, tool_input=tool_input,
                tool_use_id=use, prompt_id=prompt_id)
        capture(session_id, "PostToolUse", tool_name=tool, tool_input=tool_input,
                tool_use_id=use, tool_response="ok", prompt_id=prompt_id)
    if closing is not None:
        capture(session_id, "Stop", last_assistant_message=closing,
                stop_hook_active=False, prompt_id=prompt_id)
        # The prompt-suggestion side agent that runs after every turn.
        capture(session_id, "SubagentStop", agent_type="", agent_id=f"a{prompt_id[:8]}",
                last_assistant_message="yes, commit it", stop_hook_active=False,
                prompt_id=prompt_id)
    return prompt_id


class ScriptedModel:
    """Stands in for the background model: returns queued responses in order."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []

    def __call__(self, messages, max_tokens=None):
        self.calls.append(messages)
        if not self.responses:
            raise AssertionError("the model was called more often than scripted")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response if isinstance(response, str) else json.dumps(response)


@pytest.fixture
def model(monkeypatch):
    import extract_memory

    def install(*responses) -> ScriptedModel:
        scripted = ScriptedModel(*responses)
        monkeypatch.setattr(extract_memory, "_complete", scripted)
        return scripted

    return install


def observation(kind: str, title: str, narrative=None, cites=None) -> dict:
    return {
        "type": kind,
        "title": title,
        "narrative": narrative or f"The session reported: {title.lower()}.",
        "cites": cites or [],
    }


def summary(headline: str, progress: str, outcome: str = "") -> dict:
    return {
        "headline": headline,
        "request": "Investigate the apparent drop in customer renewals.",
        "progress": progress,
        "outcome": outcome,
    }
