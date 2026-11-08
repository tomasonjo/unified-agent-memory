"""The injection hook warns the user when it cannot use the graph.

None of these tests needs a database: the unreachable graph is a port
nothing listens on, and the failed write is a stub.
"""

from __future__ import annotations

import io
import json
import sys
import time

import pytest
from neo4j.exceptions import AuthError

import inject_system_prompt as isp
from conftest import ROOT


class Forbidden(Exception):
    code = "Neo.ClientError.Security.Forbidden"


@pytest.fixture
def unreachable(monkeypatch, tmp_path):
    monkeypatch.setenv("UAM_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("NEO4J_URI", "bolt://127.0.0.1:1")
    monkeypatch.setenv("NEO4J_DATABASE", "neo4j")
    monkeypatch.setenv("UAM_AGENT_NAME", "default")
    monkeypatch.delenv("UAM_IN_LLM_SUBPROCESS", raising=False)


def run_main(monkeypatch, capsys) -> tuple[dict, str]:
    payload = {"session_id": "s-warn", "hook_event_name": "SessionStart",
               "source": "startup", "cwd": str(ROOT)}
    raw = io.BytesIO(json.dumps(payload).encode("utf-8"))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(raw, encoding="utf-8"))
    assert isp.main() == 0
    out = capsys.readouterr()
    return json.loads(out.out), out.err


def test_an_unreachable_graph_warns_once_and_fast(unreachable, monkeypatch, capsys):
    def no_write(*args, **kwargs):
        raise AssertionError("the hook wrote to a graph it could not read")

    monkeypatch.setattr(isp, "append_session_event", no_write)
    started = time.monotonic()
    output, err = run_main(monkeypatch, capsys)
    assert time.monotonic() - started < 10

    warning = output["systemMessage"]
    assert "could not use Neo4j at bolt://127.0.0.1:1" in warning
    assert "the server could not be reached" in warning
    assert "bundled prompt" in warning
    assert "Neo4j lookup failed" in err
    assert output["hookSpecificOutput"]["additionalContext"] == (
        f"{isp.read_bundled_prompt().rstrip()}\n\n{isp.context_notice(warning)}"
    )


def test_a_failed_write_warns_the_user(unreachable, monkeypatch, capsys):
    monkeypatch.setattr(isp, "fetch_prompt_from_neo4j", lambda name: "Team instructions.")

    def forbidden(*args, **kwargs):
        raise Forbidden("Write operations are not allowed")

    monkeypatch.setattr(isp, "append_session_event", forbidden)
    output, err = run_main(monkeypatch, capsys)

    warning = output["systemMessage"]
    assert "could not write to Neo4j" in warning
    assert "not allowed to write" in warning
    assert "bundled prompt" not in warning
    assert "record failed" in err
    assert output["hookSpecificOutput"]["additionalContext"] == (
        f"Team instructions.\n\n{isp.context_notice(warning)}"
    )


def test_the_warning_reaches_the_model_without_a_prompt(unreachable, monkeypatch, capsys):
    monkeypatch.setattr(isp, "read_bundled_prompt", lambda: None)
    output, err = run_main(monkeypatch, capsys)

    warning = output["systemMessage"]
    assert "could not use Neo4j" in warning
    assert "no prompt" in err
    assert output["hookSpecificOutput"]["additionalContext"] == isp.context_notice(warning)


def test_a_working_graph_shows_no_warning(unreachable, monkeypatch, capsys):
    monkeypatch.setattr(isp, "fetch_prompt_from_neo4j", lambda name: "Team instructions.")
    monkeypatch.setattr(isp, "append_session_event", lambda *a, **k: "event-1")
    monkeypatch.setattr(isp, "set_event_props", lambda *a, **k: None)
    monkeypatch.setattr(isp, "link_event_to_prompt", lambda *a, **k: None)
    output, _ = run_main(monkeypatch, capsys)
    assert "systemMessage" not in output
    assert output["hookSpecificOutput"]["additionalContext"] == "Team instructions."


def test_the_reason_names_what_to_fix():
    assert isp.describe_graph_error(AuthError("unauthorized"), "neo4j") == (
        "the username or password was rejected"
    )
    assert isp.describe_graph_error(Forbidden("no"), "neo4j") == (
        "the user is not allowed to write to the graph"
    )

    class Missing(Exception):
        code = "Neo.ClientError.Database.DatabaseNotFound"

    assert isp.describe_graph_error(Missing("gone"), "uam") == (
        "there is no database named 'uam'"
    )
