"""Consolidation against the scratch database, with a scripted model."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import pytest

import common
import episodes
import extract_memory as em
from conftest import (
    MARIA,
    PROJECT,
    ROOT,
    TEST_DATABASE,
    capture,
    observation,
    summary,
    turn,
)

pytestmark = pytest.mark.graph

FINDING = "Renewal drop traced to March pipeline change"
FIX = "Dashboard query corrected for reactivated contracts"
CLOSING = (
    "The drop comes from the March 3 pipeline change that reclassified "
    "reactivated contracts; analytics confirmed it. I corrected the dashboard "
    "query. Historical reports that cross March 3 still need checking."
)
FIRST_PASS = {
    "observations": [observation("discovery", FINDING), observation("bugfix", FIX)],
    "summary": summary(
        "Renewal drop explained; dashboard query corrected",
        "Corrected the current dashboard query.",
        "Check historical reports that cross March 3.",
    ),
    "overflow": False,
}


def maria_first_turn(session_id: str = "s-maria") -> None:
    capture(session_id, "SessionStart", source="startup")
    turn(
        session_id,
        "Investigate the apparent drop in customer renewals.",
        [("Bash", {"command": "psql -f renewals.sql"}),
         ("Edit", {"file_path": "dashboards/renewals.sql"})],
        CLOSING,
    )


def test_a_turn_becomes_observations_and_a_summary(graph, model):
    maria_first_turn()
    scripted = model(FIRST_PASS)
    em.consolidate(["s-maria"])

    assert len(scripted.calls) == 1
    system, user = (m["content"] for m in scripted.calls[0])
    assert "Write up to 3 observations" in system
    assert "Project: renewal-analysis\nSession owner: maria@company.com" in user
    assert CLOSING in user and "psql -f renewals.sql" in user
    assert user.endswith("Previous session summary:\nnone yet")

    rows = graph.rows(
        "MATCH (:Project {id: $p})-[:HAS_OBSERVATION]->(o:Observation)"
        "-[:FROM_SESSION]->(:Session {session_id: 's-maria'}) "
        "RETURN o.type AS type, o.id AS id, o.display_id AS display_id, "
        "o.project_id AS project, o.source_end > o.source_start AS spans, "
        "o.created_at >= o.source_end AS written_after ORDER BY o.display_id",
        p=PROJECT,
    )
    assert [r["type"] for r in rows] == ["discovery", "bugfix"]
    assert [r["display_id"] for r in rows] == ["o1", "o2"]
    assert all(r["id"].startswith("obs:renewal-analysis:s-maria:") for r in rows)
    assert all(r["project"] == PROJECT and r["spans"] and r["written_after"] for r in rows)
    assert graph.value(
        "MATCH (:Observation {display_id: 'o1'})-[:NEXT]->(n) RETURN n.display_id"
    ) == "o2"
    assert graph.value(
        "MATCH (:Project {id: $p})-[:LATEST_OBSERVATION]->(o) RETURN o.display_id",
        p=PROJECT,
    ) == "o2"

    head = graph.rows(
        "MATCH (s:Session {session_id: 's-maria'})-[:HAS_SUMMARY]->(sum) "
        "RETURN s.display_id AS display_id, sum.version AS version, "
        "sum.id AS id, sum.next_steps AS next_steps, sum.project_id AS project"
    )[0]
    assert head == {
        "display_id": "s1", "version": 1, "id": "sum:s-maria",
        "next_steps": "Check historical reports that cross March 3.",
        "project": PROJECT,
    }

    run = graph.rows(
        "MATCH (r:ExtractionRun {status: 'completed'}) RETURN r.id AS id, "
        "r.llm_model AS model, r.event_count AS events, "
        "r.input_summary_json AS input, r.output_summary_version AS version, "
        "COUNT { (r)-[:PROCESSED_EVENT]->() } AS processed, "
        "COUNT { (r)-[:PRODUCED]->() } AS produced"
    )
    assert len(run) == 1
    # SessionStart, the prompt, two Pre/Post pairs, and the Stop. The side
    # agent's SubagentStop comes after the Stop, so it waits for the next window.
    assert run[0]["events"] == run[0]["processed"] == 7
    assert run[0]["produced"] == 2 and run[0]["version"] == 1
    assert run[0]["input"] is None  # no summary before: no snapshot
    assert run[0]["model"].startswith("claude-cli/")


def test_a_completed_window_is_never_processed_again(graph, model):
    maria_first_turn()
    model(FIRST_PASS)
    em.consolidate(["s-maria"])
    before = graph.value("MATCH (n) RETURN count(n)")
    untouched = model()  # any call would fail the test
    em.consolidate(["s-maria"])
    assert untouched.calls == []
    assert graph.value("MATCH (n) RETURN count(n)") == before


def test_the_next_turn_adds_an_observation_and_a_summary_version(graph, model):
    maria_first_turn()
    model(FIRST_PASS)
    em.consolidate(["s-maria"])

    turn("s-maria", "Check the historical reports.",
         [("Bash", {"command": "python check_reports.py --since 2026-03-03"})],
         "All historical reports that cross March 3 were checked; two needed a rerun.")
    scripted = model({
        "observations": [observation("change", "Historical renewal reports rerun")],
        "summary": summary("Renewal drop explained; history checked",
                           "Corrected the query and reran two reports.", ""),
        "overflow": False,
    })
    em.consolidate(["s-maria"])

    user = scripted.calls[0][1]["content"]
    assert "Earlier in this session" in user  # the opening prompt, as an excerpt
    assert '"next_steps": "Check historical reports that cross March 3."' in user
    assert "yes, commit it" not in user  # the side agent's guess is not work

    assert graph.value(
        "MATCH (:Session {session_id: 's-maria'})-[:HAS_SUMMARY]->(s) RETURN s.version"
    ) == 2
    assert graph.value(
        "MATCH (o:Observation)-[:FROM_SESSION]->(:Session {session_id: 's-maria'}) "
        "WITH o ORDER BY toInteger(substring(o.display_id, 1)) "
        "RETURN collect(o.title) AS titles"
    ) == [FINDING, FIX, "Historical renewal reports rerun"]
    assert graph.value(
        "MATCH (:Observation {display_id: 'o2'})-[:NEXT]->(n) RETURN n.display_id"
    ) == "o3"
    second = graph.rows(
        "MATCH (r:ExtractionRun {status: 'completed'}) WHERE r.input_summary_version = 1 "
        "RETURN r.output_summary_version AS out, r.input_summary_json AS input, "
        "r.output_summary_json AS output"
    )[0]
    assert second["out"] == 2
    assert json.loads(second["input"])["next_steps"].startswith("Check historical")
    assert json.loads(second["output"])["next_steps"] == ""


def test_a_failed_call_leaves_the_window_eligible(graph, model):
    maria_first_turn()
    model(RuntimeError("provider down"))
    em.consolidate(["s-maria"])
    failed = graph.rows(
        "MATCH (r:ExtractionRun) RETURN r.status AS status, r.error AS error, "
        "COUNT { (r)--() } AS edges"
    )
    assert failed == [{"status": "failed",
                       "error": "model call failed: provider down", "edges": 0}]
    assert graph.value("MATCH (o:Observation) RETURN count(o)") == 0

    model(FIRST_PASS)
    em.consolidate(["s-maria"])
    assert graph.value("MATCH (o:Observation) RETURN count(o)") == 2


def test_invalid_output_is_retried_with_the_reason(graph, model):
    maria_first_turn()
    too_long = json.loads(json.dumps(FIRST_PASS))
    too_long["observations"][0]["title"] = "Possible pipeline cause, " + "x" * 120
    scripted = model(too_long, FIRST_PASS)
    em.consolidate(["s-maria"])
    assert len(scripted.calls) == 2
    assert "Your previous response was rejected: observation 1 title is" in (
        scripted.calls[1][1]["content"]
    )
    assert graph.value("MATCH (o:Observation) RETURN count(o)") == 2
    assert graph.value(
        "MATCH (r:ExtractionRun {status: 'failed'}) RETURN count(r)"
    ) == 1


def test_three_invalid_responses_stop_the_worker(graph, model):
    maria_first_turn()
    scripted = model("no json", "no json", "no json")
    em.consolidate(["s-maria"])
    assert len(scripted.calls) == 3
    assert graph.value("MATCH (r:ExtractionRun {status: 'failed'}) RETURN count(r)") == 3
    assert graph.value("MATCH (r:ExtractionRun {status: 'completed'}) RETURN count(r)") == 0


def test_overflow_splits_at_the_turn_boundary_and_never_commits_the_parent(graph, model):
    capture("s-maria", "SessionStart", source="startup")
    turn("s-maria", "Profile the renewal job.", [("Bash", {"command": "make profile"})],
         closing=None)  # interrupted: no Stop fired
    turn("s-maria", "Now fix the slow join.", [("Edit", {"file_path": "job.sql"})],
         "Rewrote the join; the job runs in 4 minutes instead of 40.")
    scripted = model(
        {"observations": [], "summary": None, "overflow": True},
        {"observations": [observation("problem", "Renewal job profiling interrupted")],
         "summary": summary("Renewal job performance", "Profiling started.", "Fix the join."),
         "overflow": False},
        {"observations": [observation("bugfix", "Slow renewal join rewritten")],
         "summary": summary("Renewal job fixed", "Join rewritten.", ""),
         "overflow": False},
    )
    em.consolidate(["s-maria"])
    assert len(scripted.calls) == 3
    assert "Profile the renewal job." in scripted.calls[1][1]["content"]
    assert "Now fix the slow join." not in scripted.calls[1][1]["content"]

    runs = graph.rows(
        "MATCH (r:ExtractionRun) RETURN r.status AS status, r.window_key AS key, "
        "r.event_count AS events ORDER BY r.created_at"
    )
    assert [r["status"] for r in runs] == ["overflow", "completed", "completed"]
    parent = runs[0]["key"]
    assert parent not in {r["key"] for r in runs[1:]}
    assert runs[0]["events"] == runs[1]["events"] + runs[2]["events"]
    # No event was processed twice.
    assert graph.value(
        "MATCH (e:SessionEvent)<-[:PROCESSED_EVENT]-(r:ExtractionRun) "
        "WITH e, count(r) AS n WHERE n > 1 RETURN count(e)"
    ) == 0


def test_a_lifecycle_only_window_needs_no_model_call(graph, model):
    maria_first_turn()
    model(FIRST_PASS)
    em.consolidate(["s-maria"])
    capture("s-maria", "SessionEnd", reason="prompt_input_exit")
    untouched = model()
    em.consolidate(["s-maria"])
    assert untouched.calls == []
    last = graph.rows(
        "MATCH (r:ExtractionRun {status: 'completed'}) WITH r ORDER BY r.created_at DESC "
        "LIMIT 1 RETURN r.event_count AS events, r.llm_model AS model"
    )[0]
    assert last == {"events": 2, "model": None}  # the side agent's stop and SessionEnd


def test_the_lease_admits_one_worker_at_a_time(graph):
    maria_first_turn()
    first = em.Worker(graph.session, "s-maria")
    second = em.Worker(graph.session, "s-maria")
    assert first.acquire() and not second.acquire()
    first.release()
    assert second.acquire()


def test_two_workers_racing_for_the_lease_get_one_between_them(graph):
    maria_first_turn()
    for _ in range(10):
        graph.session.run(
            "MATCH (s:Session {session_id: 's-maria'}) "
            "REMOVE s.extraction_lease_owner, s.extraction_lease_until"
        ).consume()
        barrier = threading.Barrier(2)
        results: list[bool] = []

        def contend() -> None:
            # A driver each, so the two requests really meet at the database.
            with common.graph_driver(connection_timeout=5.0) as driver:
                with driver.session(database=TEST_DATABASE) as db:
                    worker = em.Worker(db, "s-maria")
                    barrier.wait()
                    results.append(worker.acquire())

        threads = [threading.Thread(target=contend) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(results) == [False, True]


def test_a_worker_whose_lease_expired_cannot_commit(graph, model):
    maria_first_turn()
    stale = em.Worker(graph.session, "s-maria")
    assert stale.acquire()
    window = stale.pending_window()
    key = em.window_key(window)
    context = stale.context(window)
    rendered = em.render_window(window, 20_000)
    extraction = em.validate(FIRST_PASS, None, 3, set())

    # The model call outlived the lease, and another worker took over.
    graph.session.run(
        "MATCH (s:Session {session_id: 's-maria'}) "
        "SET s.extraction_lease_until = datetime() - duration({minutes: 1})"
    ).consume()
    model(FIRST_PASS)
    em.Worker(graph.session, "s-maria").run()

    with pytest.raises(em.Stale):
        stale.commit(window, key, context, rendered, extraction, "claude-cli/haiku", 1)
    assert graph.value("MATCH (o:Observation) RETURN count(o)") == 2


def test_a_worker_that_read_an_older_summary_cannot_commit(graph, model):
    maria_first_turn()
    worker = em.Worker(graph.session, "s-maria")
    assert worker.acquire()
    window = worker.pending_window()
    context = worker.context(window)
    graph.session.run(
        "MATCH (s:Session {session_id: 's-maria'}) "
        "CREATE (s)-[:HAS_SUMMARY]->(:SessionSummary {id: 'sum:s-maria', version: 1})"
    ).consume()
    with pytest.raises(em.Stale, match="summary changed"):
        worker.commit(window, em.window_key(window), context,
                      em.render_window(window, 20_000),
                      em.validate(FIRST_PASS, None, 3, set()), "m", 1)


def test_the_worker_commits_the_closing_event_it_was_started_for(graph, model):
    # Capture of the Stop has not landed yet: the worker's own append makes
    # the window ready, and capture's later copy collapses into the same node.
    capture("s-maria", "SessionStart", source="startup")
    capture("s-maria", "UserPromptSubmit", prompt="Investigate the renewal drop.",
            prompt_id="p1")
    stop = {"session_id": "s-maria", "hook_event_name": "Stop", "cwd": str(ROOT),
            "prompt_id": "p1", "last_assistant_message": CLOSING,
            "stop_hook_active": False}
    model(FIRST_PASS)
    em.run_worker(stop)
    assert graph.value("MATCH (o:Observation) RETURN count(o)") == 2
    capture("s-maria", "Stop", prompt_id="p1", last_assistant_message=CLOSING,
            stop_hook_active=False)
    assert graph.value(
        "MATCH (e:SessionEvent {session_id: 's-maria', event_name: 'Stop'}) RETURN count(e)"
    ) == 1


def test_other_work_between_turns_joins_the_timeline_not_the_session(graph, model, monkeypatch):
    maria_first_turn()
    model(FIRST_PASS)
    em.consolidate(["s-maria"])

    monkeypatch.setenv("UAM_USER_ID", "alex@company.com")
    capture("s-alex", "SessionStart", source="startup")
    turn("s-alex", "Rotate the auth tokens.", [("Bash", {"command": "vault rotate"})],
         "Rotated the tokens.")
    model({"observations": [observation("change", "Auth tokens rotated")],
           "summary": summary("Auth tokens rotated", "Rotated.", ""), "overflow": False})
    em.consolidate(["s-alex"])

    monkeypatch.setenv("UAM_USER_ID", MARIA)
    turn("s-maria", "Check the historical reports.", [], "Checked; all fine.")
    scripted = model({"observations": [observation("change", "Historical reports checked")],
                      "summary": summary("History checked", "Checked.", ""),
                      "overflow": False})
    em.consolidate(["s-maria"])

    assert "Rotate" not in scripted.calls[0][1]["content"]
    timeline = graph.value(
        "MATCH (:Project {id: $p})-[:LATEST_OBSERVATION]->(last) "
        "MATCH path = (first:Observation)-[:NEXT*]->(last) "
        "WHERE NOT EXISTS { (:Observation)-[:NEXT]->(first) } "
        "RETURN [o IN nodes(path) | o.title]", p=PROJECT,
    )
    assert timeline == [FINDING, FIX, "Auth tokens rotated", "Historical reports checked"]
    assert graph.value(
        "MATCH (o:Observation)-[:FROM_SESSION]->(:Session {session_id: 's-maria'}) "
        "RETURN count(o)"
    ) == 3
    assert graph.value(
        "MATCH (:User {user_id: 'alex@company.com'})-[:HAS_SESSION]->(s) RETURN s.session_id"
    ) == "s-alex"


def test_an_observations_evidence_is_exactly_its_run_window(graph, model):
    maria_first_turn()
    model(FIRST_PASS)
    em.consolidate(["s-maria"])
    page = episodes.expand(episodes.reader(graph.session), "#o1", events=True)
    lines = page.splitlines()
    assert lines[0] == "Captured events behind #o1: the 7 its extraction run processed."
    assert "SessionStart" in lines[1] and "Stop" in lines[-1]
    assert "yes, commit it" not in page


def test_the_stop_hook_returns_at_once_and_its_worker_records_the_outcome(graph, tmp_path):
    maria_first_turn()
    env = {
        **os.environ,
        "NEO4J_DATABASE": TEST_DATABASE,
        "UAM_DATA_DIR": str(tmp_path),
        "UAM_LLM_BACKEND": "claude-cli",
        "UAM_LLM_NUM_RETRIES": "0",
        "PATH": "/nonexistent",  # no claude on PATH: the call fails at once
    }
    payload = {"session_id": "s-maria", "hook_event_name": "SessionEnd",
               "cwd": str(ROOT), "reason": "other"}
    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "extract_memory.py")],
        input=json.dumps(payload), text=True, capture_output=True, env=env, timeout=30,
    )
    assert done.returncode == 0 and done.stdout == ""
    assert time.monotonic() - started < 5
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if graph.value("MATCH (r:ExtractionRun) RETURN count(r)"):
            break
        time.sleep(0.5)
    run = graph.rows("MATCH (r:ExtractionRun) RETURN r.status AS status, r.error AS error")
    assert run and run[0]["status"] == "failed"
    assert run[0]["error"].startswith("model call failed")
    assert "SessionEnd" in graph.value(
        "MATCH (:Session {session_id: 's-maria'})-[:LATEST_EVENT]->(e) RETURN e.event_name"
    )
    assert "outcome=failed" in (tmp_path / "logs" / "extract.log").read_text()
