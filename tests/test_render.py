"""The extraction input and the writer's validation, without a graph."""

from __future__ import annotations

import json

import pytest

import extract_memory as em


def event(name: str, index: int, **fields) -> dict:
    return {"event_id": f"e{index}", "event_name": name, "timestamp": None, **fields}


def big_turn(calls: int) -> list[dict]:
    """A turn with a long prompt, many tool calls, and a long closing message."""
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


def test_prompt_and_closing_message_stay_whole_under_budget():
    events = big_turn(400)
    rendered = em.render_window(events, 24_000)
    assert rendered is not None
    assert len(rendered.text) <= 24_000
    assert "P" * 8000 in rendered.text and "C" * 8000 in rendered.text
    assert rendered.trim["message_cap"] is None
    assert rendered.tool_calls == 400


def test_whole_input_stays_within_thirty_thousand_characters():
    events = big_turn(400)
    context = em.Context("s1", "renewal-analysis", "maria@company.com", None, None)
    instructions = em.INSTRUCTIONS.format(max_observations=em.MAX_OBSERVATIONS)
    frame = em.user_message(context, "", None)
    budget = em.INPUT_CHARS - len(instructions) - len(frame) - 400
    rendered = em.render_window(events, budget)
    total = len(instructions) + len(em.user_message(context, rendered.text, None))
    assert total <= em.INPUT_CHARS


def test_tools_step_down_read_only_first():
    events = big_turn(30)
    full = em.render_window(events, 10**6)
    assert full.trim["read_only_tools"] == "input_1000"
    squeezed = em.render_window(events, len(full.text) - 2000)
    assert squeezed.trim["read_only_tools"] != "input_1000"
    assert squeezed.trim["action_tools"] == "input_1000"


def test_collapsed_calls_name_their_first_targets():
    events = [event("UserPromptSubmit", 0, prompt="go")]
    for i in range(12):
        events.append(event("PostToolUse", i + 1, tool_name="Read", tool_use_id=f"t{i}",
                            tool_input=json.dumps({"file_path": f"f{i}.py"})))
    text = em.render_items(em.window_items(events), (em.COLLAPSED, em.COLLAPSED), None)
    assert "Tool Read ×12: f0.py, f1.py, … +10" in text


def test_messages_are_cut_only_to_their_floor():
    events = [
        event("UserPromptSubmit", 0, prompt="A" * 8000),
        event("Stop", 1, last_assistant_message="B" * 8000),
    ]
    rendered = em.render_window(events, 6000)
    assert rendered is not None and rendered.trim["message_cap"] >= em.MESSAGE_FLOOR
    assert "characters omitted" in rendered.text
    assert em.render_window(events, 2000) is None


def test_side_agent_and_bookkeeping_never_reach_the_model():
    events = [
        event("SessionStart", 0, source="startup"),
        event("UserPromptSubmit", 1, prompt="Fix the query"),
        event("PreToolUse", 2, tool_name="Bash", tool_use_id="t1",
              tool_input=json.dumps({"command": "psql -f fix.sql"})),
        event("PostToolUse", 3, tool_name="Bash", tool_use_id="t1",
              tool_input=json.dumps({"command": "psql -f fix.sql"})),
        event("PreToolUse", 4, tool_name="Bash", tool_use_id="t2",
              tool_input=json.dumps({"command": "rm -rf build"})),
        event("PostToolUseFailure", 5, tool_name="Bash", tool_use_id="t3",
              tool_input=json.dumps({"command": "pytest"}), tool_error="exit code 1"),
        event("SubagentStart", 6, agent_type="Explore", agent_id="a-explore"),
        event("SubagentStop", 7, agent_type="Explore", agent_id="a-explore",
              last_assistant_message="Found 3 files"),
        event("Stop", 8, last_assistant_message="Query corrected."),
        # The harness's own agents: no SubagentStart, whatever their type.
        event("SubagentStop", 9, agent_type="", agent_id="a-suggest",
              last_assistant_message="yes, ship it"),
        event("SubagentStop", 10, agent_type="reviewer", agent_id="a-suggest-2",
              last_assistant_message="now open a PR"),
        event("Notification", 11),
    ]
    text = em.render_window(events, 10**6).text
    assert "yes, ship it" not in text
    assert "now open a PR" not in text  # a session run with --agent reviewer
    assert "Subagent Explore started" in text
    assert "Subagent Explore final message:\nFound 3 files" in text
    assert text.count("psql -f fix.sql") == 1  # PreToolUse skipped once a result followed
    assert "(no result recorded)" in text and "rm -rf build" in text
    assert "(failed)" in text and "error: exit code 1" in text
    assert "startup" not in text


def test_a_subagent_started_in_an_earlier_window_still_reports():
    events = [event("SubagentStop", 0, agent_type="general-purpose", agent_id="a-bg",
                    last_assistant_message="Background review done"),
              event("Stop", 1, last_assistant_message="ok")]
    assert "Background review done" not in em.render_window(events, 10**6).text
    text = em.render_window(events, 10**6, spawned={"a-bg"}).text
    assert "Subagent general-purpose final message:\nBackground review done" in text


def test_lifecycle_only_window_renders_nothing():
    events = [
        event("SubagentStop", 0, agent_type="", last_assistant_message="continue"),
        event("SessionEnd", 1),
    ]
    rendered = em.render_window(events, 10**6)
    assert rendered.messages == 0 and rendered.tool_calls == 0


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
         "facts": ["March 3 changed the classification."],
         "narrative": "Maria investigated the drop.", "cites": ["#o7", "o99"]},
    ],
    "summary": {"headline": "Renewal drop explained", "request": "Investigate",
                "progress": "Diagnosed", "learned": "", "next_steps": "Check reports"},
    "overflow": False,
}


def test_parse_accepts_a_fenced_object_with_trailing_prose():
    text = "```json\n" + json.dumps(GOOD) + "\n```"
    assert em.parse_response(text)["overflow"] is False
    assert em.parse_response("Here: " + json.dumps(GOOD) + " done")["summary"]


def test_parse_tells_truncation_from_garbage():
    with pytest.raises(em.Truncated):
        em.parse_response(json.dumps(GOOD)[:-40])
    with pytest.raises(em.Invalid):
        em.parse_response("I could not do that.")


def test_validate_keeps_only_delivered_cites():
    result = em.validate(GOOD, None, 3, {"o7"})
    assert result.observations[0]["cites"] == ["o7"]
    assert result.summary["headline"] == "Renewal drop explained"


def test_validate_rejects_rather_than_shortens():
    bad = json.loads(json.dumps(GOOD))
    bad["observations"][0]["title"] = "Possible pipeline cause, " + "x" * 120
    with pytest.raises(em.Invalid, match="title is .* characters"):
        em.validate(bad, None, 3, set())
    bad = json.loads(json.dumps(GOOD))
    bad["observations"][0]["type"] = "insight"
    with pytest.raises(em.Invalid, match="type must be one of"):
        em.validate(bad, None, 3, set())
    bad = json.loads(json.dumps(GOOD))
    del bad["summary"]
    with pytest.raises(em.Invalid, match="summary is missing"):
        em.validate(bad, None, 3, set())


def test_more_observations_than_allowed_is_overflow():
    many = {**GOOD, "observations": GOOD["observations"] * 4}
    result = em.validate(many, None, 3, set())
    assert result.overflow and len(result.observations) == 3


def test_an_unchanged_summary_is_not_rewritten():
    previous = dict(GOOD["summary"])
    assert em.validate(GOOD, previous, 3, set()).summary is None
    assert em.validate({"observations": [], "summary": None}, previous, 3, set()).summary is None
