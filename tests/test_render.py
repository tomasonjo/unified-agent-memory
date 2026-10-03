"""The extraction input and the writer's validation, without a graph."""

from __future__ import annotations

import json

import pytest

import extract_memory as em


def event(name: str, index: int, **fields) -> dict:
    return {"event_id": f"e{index}", "event_name": name, "timestamp": None, **fields}


def big_turn(calls: int) -> list[dict]:
    """A turn with a long prompt, many tool calls, and a long final response."""
    events = [event("UserPromptSubmit", 0, prompt="P" * 8000)]
    for i in range(calls):
        tool = ("Read", "Bash", "Edit")[i % 3]
        tool_input = json.dumps(
            {"file_path": f"src/module_{i}.py", "command": f"pytest -k case_{i}\n# more",
             "old_string": "x" * 600, "new_string": "y" * 600}
        )
        use = f"t{i}"
        events.append(event("PreToolUse", 2 * i + 1, tool_name=tool, tool_use_id=use,
                            tool_input=tool_input))
        events.append(event("PostToolUse", 2 * i + 2, tool_name=tool, tool_use_id=use,
                            tool_input=tool_input))
    events.append(event("Stop", 10**6, last_assistant_message="C" * 8000))
    return events


def test_only_the_prompt_and_final_response_reach_the_model():
    events = big_turn(400)
    rendered = em.render_window(events, 24_000)
    assert rendered is not None and rendered.messages == 2
    assert rendered.text == (
        "[--:--:--] User prompt:\n" + "P" * 8000
        + "\n[--:--:--] Agent final response:\n" + "C" * 8000
    )
    assert rendered.message_cap is None


def test_whole_input_stays_within_thirty_thousand_characters():
    events = big_turn(400)
    context = em.Context("s1", "renewal-analysis", "maria@company.com", None, None)
    instructions = em.INSTRUCTIONS
    frame = em.user_message(context, "", None)
    budget = em.INPUT_CHARS - len(instructions) - len(frame) - 400
    rendered = em.render_window(events, budget)
    total = len(instructions) + len(em.user_message(context, rendered.text, None))
    assert total <= em.INPUT_CHARS


def test_messages_are_cut_only_to_their_floor():
    events = [
        event("UserPromptSubmit", 0, prompt="A" * 8000),
        event("Stop", 1, last_assistant_message="B" * 8000),
    ]
    rendered = em.render_window(events, 6000)
    assert rendered is not None and rendered.message_cap >= em.MESSAGE_FLOOR
    assert "characters omitted" in rendered.text
    assert em.render_window(events, 2000) is None


def test_tools_subagents_and_bookkeeping_never_reach_the_model():
    events = [
        event("SessionStart", 0, source="startup"),
        event("UserPromptSubmit", 1, prompt="Fix the query"),
        event("PreToolUse", 2, tool_name="Bash", tool_use_id="t1",
              tool_input=json.dumps({"command": "psql -f fix.sql"})),
        event("PostToolUse", 3, tool_name="Bash", tool_use_id="t1",
              tool_input=json.dumps({"command": "psql -f fix.sql"})),
        event("PostToolUseFailure", 4, tool_name="Bash", tool_use_id="t2",
              tool_input=json.dumps({"command": "pytest"}), tool_error="exit code 1"),
        event("SubagentStart", 5, agent_type="Explore", agent_id="a-explore"),
        event("SubagentStop", 6, agent_type="Explore", agent_id="a-explore",
              last_assistant_message="Found 3 files"),
        event("PreCompact", 7),
        event("Stop", 8, last_assistant_message="Query corrected."),
        # The harness's own prompt-suggestion agent.
        event("SubagentStop", 9, agent_type="", agent_id="a-suggest",
              last_assistant_message="yes, ship it"),
        event("Notification", 10),
    ]
    rendered = em.render_window(events, 10**6)
    assert rendered.text == (
        "[--:--:--] User prompt:\nFix the query\n"
        "[--:--:--] Agent final response:\nQuery corrected."
    )


def test_a_window_without_messages_renders_nothing():
    events = [
        event("PostToolUse", 0, tool_name="Read", tool_use_id="t1",
              tool_input=json.dumps({"file_path": "a.py"})),
        event("SubagentStop", 1, agent_type="", last_assistant_message="continue"),
        event("SessionEnd", 2),
    ]
    rendered = em.render_window(events, 10**6)
    assert rendered.messages == 0 and rendered.text == ""


def test_split_prefers_the_turn_boundary():
    events = [event("SessionStart", 0), event("UserPromptSubmit", 1),
              event("PostToolUse", 2), event("UserPromptSubmit", 3),
              event("PostToolUse", 4), event("Stop", 5)]
    assert em.split_point(events) == 3  # the second prompt, not the first
    assert em.split_point([event("PostToolUse", i) for i in range(6)]) == 3


def test_window_key_ignores_order():
    a = [event("UserPromptSubmit", 1), event("Stop", 2)]
    assert em.window_key(a) == em.window_key(list(reversed(a)))


# --- parsing and validation ---------------------------------------------------

GOOD = {
    "observations": [
        {"type": "discovery", "title": "Renewal drop traced to March pipeline change",
         "narrative": "Maria investigated the drop.", "cites": ["#o7", "o99"]},
    ],
    "summary": {"headline": "Renewal drop explained", "request": "Investigate",
                "progress": "Diagnosed; reports unchecked", "outcome": ""},
}


def test_parse_accepts_a_fenced_object_with_trailing_prose():
    text = "```json\n" + json.dumps(GOOD) + "\n```"
    assert em.parse_response(text) == GOOD
    assert em.parse_response("Here: " + json.dumps(GOOD) + " done")["summary"]


def test_parse_tells_truncation_from_garbage():
    with pytest.raises(em.Truncated):
        em.parse_response(json.dumps(GOOD)[:-40])
    with pytest.raises(em.Invalid):
        em.parse_response("I could not do that.")


def test_validate_keeps_only_delivered_cites():
    result = em.validate(GOOD, None, {"o7"})
    assert result.observations[0]["cites"] == ["o7"]
    assert result.summary["headline"] == "Renewal drop explained"


def test_validate_rejects_rather_than_shortens():
    bad = json.loads(json.dumps(GOOD))
    bad["observations"][0]["title"] = "Possible pipeline cause, " + "x" * 120
    with pytest.raises(em.Invalid, match="title is .* characters"):
        em.validate(bad, None, set())
    bad = json.loads(json.dumps(GOOD))
    bad["observations"][0]["type"] = "insight"
    with pytest.raises(em.Invalid, match="type must be one of"):
        em.validate(bad, None, set())
    bad = json.loads(json.dumps(GOOD))
    del bad["summary"]
    with pytest.raises(em.Invalid, match="summary is missing"):
        em.validate(bad, None, set())


def test_every_observation_is_kept():
    many = {**GOOD, "observations": GOOD["observations"] * 4}
    assert len(em.validate(many, None, set()).observations) == 4


def test_an_unchanged_summary_is_not_rewritten():
    previous = dict(GOOD["summary"])
    assert em.validate(GOOD, previous, set()).summary is None
    assert em.validate({"observations": [], "summary": None}, previous, set()).summary is None


# --- intermediate responses ---------------------------------------------------


def shown(position: int, message_id: str, part: int, delta: str, prompt_id="p1", **fields) -> dict:
    """One MessageDisplay flush: ``part`` is its index within the message."""
    return {**event("MessageDisplay", position, prompt_id=prompt_id,
                    message_id=message_id, delta=delta, **fields), "index": part}


def test_intermediate_responses_are_reassembled_from_their_flushes():
    events = [
        event("UserPromptSubmit", 0, prompt="Why did renewals drop?", prompt_id="p1"),
        # Parallel hooks can land a message's flushes out of order.
        shown(1, "m1", 1, "Then the pipeline log.\n"),
        shown(2, "m1", 0, "Checking the renewal query first.\n"),
        event("PreToolUse", 3, tool_name="Bash", tool_input="psql -f renewals.sql"),
        event("PostToolUse", 4, tool_name="Bash", tool_input="psql -f renewals.sql"),
        shown(5, "m2", 0, "The March 3 change reclassified reactivated contracts.\n"),
        event("Stop", 6, prompt_id="p1", last_assistant_message="Query corrected."),
    ]
    rendered = em.render_window(events, 10**6)
    assert rendered.text == (
        "[--:--:--] User prompt:\nWhy did renewals drop?\n"
        "[--:--:--] Agent intermediate response:\n"
        "Checking the renewal query first.\nThen the pipeline log.\n"
        "[--:--:--] Agent intermediate response:\n"
        "The March 3 change reclassified reactivated contracts.\n"
        "[--:--:--] Agent final response:\nQuery corrected."
    )


def test_the_final_response_is_read_once():
    final = "Query corrected.\nAnalytics confirmed the fix."
    events = [
        event("UserPromptSubmit", 0, prompt="Fix the query", prompt_id="p1"),
        shown(1, "m1", 0, "Query corrected.\n"),
        shown(2, "m1", 1, "Analytics confirmed the fix.", final=True),
        event("Stop", 3, prompt_id="p1", last_assistant_message=final),
    ]
    rendered = em.render_window(events, 10**6)
    assert rendered.messages == 2
    assert rendered.text.count("Analytics confirmed the fix.") == 1
    assert "Agent intermediate response" not in rendered.text


def test_final_response_lines_that_land_after_their_stop_are_dropped():
    # The previous turn's Stop is outside this window; the worker passes
    # its final response along as final_responses.
    earlier = ["Query corrected.\nAnalytics confirmed the fix."]
    events = [
        shown(0, "m1", 1, "Analytics confirmed the fix.", prompt_id="p0",
              final=True, final_responses=earlier),
        event("SessionEnd", 1),
    ]
    rendered = em.render_window(events, 10**6)
    assert rendered.messages == 0 and rendered.text == ""


def test_an_interrupted_turn_keeps_what_the_assistant_said():
    events = [
        event("UserPromptSubmit", 0, prompt="Why did renewals drop?", prompt_id="p1"),
        shown(1, "m1", 0, "The March 3 change reclassified reactivated contracts.\n"),
        event("PreToolUse", 2, tool_name="Edit", tool_input="dashboards/renewals.sql"),
        # Interrupted: no Stop. The next turn closes the window.
        event("UserPromptSubmit", 3, prompt="Stop, summarize instead.", prompt_id="p2"),
        event("Stop", 4, prompt_id="p2", last_assistant_message="Summary sent."),
    ]
    rendered = em.render_window(events, 10**6)
    assert "Agent intermediate response:\nThe March 3 change" in rendered.text
    assert rendered.messages == 4


def test_capture_keeps_the_displayed_lines():
    from log_event import build_event_props

    flush = {"hook_event_name": "MessageDisplay", "turn_id": "t1", "message_id": "m1",
             "index": 0, "final": False, "delta": "Checking the query.\n"}
    props = build_event_props(flush)
    assert props["delta"] == "Checking the query.\n"
    assert (props["message_id"], props["index"], props["final"]) == ("m1", 0, False)
    # The hooks reference names the field content.
    documented = {"hook_event_name": "MessageDisplay", "content": "Checking the query."}
    assert build_event_props(documented)["delta"] == "Checking the query."
    # Other events' content is not displayed text.
    elicitation = {"hook_event_name": "ElicitationResult", "content": {"answer": "yes"}}
    assert "delta" not in build_event_props(elicitation)
