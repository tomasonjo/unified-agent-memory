"""What capture keeps from each hook payload, without a graph."""

from __future__ import annotations

from common import prompt_origin
from log_event import MAX_LISTED, build_event_props

REPORT = (
    '<agent-message from="ab8bdabc82e08ba05">\n[Subagent hand-back] The text '
    "below is the final report of a subagent this session started.\n"
    "Renewal drop traced to the March 3 pipeline change."
)


def test_prompts_the_harness_submits_are_told_from_typed_ones():
    assert prompt_origin("Why did renewals drop?") == "user"
    assert prompt_origin(REPORT) == "agent-message"
    assert prompt_origin("<task-notification>\n<task-id>w87</task-id>") == "task-notification"
    assert prompt_origin('<cross-session-message from="local_1">hi') == "cross-session-message"
    assert prompt_origin("<teammate-message>done</teammate-message>") == "teammate-message"
    assert prompt_origin('<channel source="slack">ship it</channel>') == "channel"
    # Pasted markup is still the user's.
    assert prompt_origin('\n\n<pasted_content id="8cd2">\nEcho cycle') == "user"
    assert prompt_origin("<b>bold</b> request") == "user"

    props = build_event_props({"hook_event_name": "UserPromptSubmit", "prompt": REPORT})
    assert props["prompt_origin"] == "agent-message"
    typed = build_event_props({"hook_event_name": "UserPromptSubmit", "prompt": "Fix it"})
    assert typed["prompt_origin"] == "user"
    # UserPromptExpansion repeats the typed line; it is not a prompt the model reads.
    expansion = build_event_props({
        "hook_event_name": "UserPromptExpansion", "prompt": "/deploy staging",
        "expansion_type": "slash_command", "command_name": "deploy",
        "command_args": "staging", "command_source": "plugin",
    })
    assert "prompt_origin" not in expansion
    assert (expansion["command_name"], expansion["command_args"]) == ("deploy", "staging")


def test_stop_records_the_work_still_in_flight():
    payload = {
        "hook_event_name": "Stop",
        "last_assistant_message": "Started the audit.",
        "background_tasks": [
            {"id": "t1", "type": "subagent", "status": "running", "agent_type": "Explore",
             "description": "Map the  renewal\nqueries"},
            {"id": "t2", "type": "workflow", "status": "running", "name": "deep-research",
             "description": "x" * 1000},
            {"id": "t3", "type": "shell", "status": "running", "command": "tail -f app.log"},
        ],
        "session_crons": [
            {"id": "c1", "schedule": "*/5 * * * *", "recurring": True, "prompt": "check the deploy"},
        ],
    }
    props = build_event_props(payload)
    assert props["background_tasks"][0] == "subagent (running): Explore · Map the renewal queries"
    assert props["background_tasks"][1].startswith("workflow (running): deep-research · xxx")
    assert len(props["background_tasks"][1]) < 240
    assert props["background_tasks"][2] == "shell (running): tail -f app.log"
    assert props["session_crons"] == ["*/5 * * * * (recurring): check the deploy"]

    # Nothing in flight records nothing.
    idle = build_event_props({"hook_event_name": "Stop", "background_tasks": [],
                              "session_crons": []})
    assert "background_tasks" not in idle and "session_crons" not in idle


def test_a_long_task_list_is_capped():
    tasks = [{"type": "subagent", "status": "running", "agent_type": "workflow-subagent",
              "description": f"agent {i}"} for i in range(150)]
    props = build_event_props({"hook_event_name": "Stop", "background_tasks": tasks})
    assert len(props["background_tasks"]) == MAX_LISTED + 1
    assert props["background_tasks"][-1] == f"... and {150 - MAX_LISTED} more"


def test_subagent_stop_keeps_its_transcript_not_the_parents_task_list():
    props = build_event_props({
        "hook_event_name": "SubagentStop", "agent_id": "def456", "agent_type": "Explore",
        "agent_transcript_path": "~/.claude/projects/p/abc/subagents/agent-def456.jsonl",
        "last_assistant_message": "Found 3 files",
        "background_tasks": [{"type": "subagent", "status": "running"}],
        "session_crons": [{"schedule": "0 9 * * *", "prompt": "standup"}],
    })
    assert props["agent_transcript_path"].endswith("agent-def456.jsonl")
    assert "background_tasks" not in props and "session_crons" not in props


def test_errors_land_where_they_belong():
    tool = build_event_props({"hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
                              "error": "exit code 1\n" + "e" * 2000})
    assert tool["tool_error"].startswith("exit code 1") and "error" not in tool
    assert len(tool["tool_error"]) < 1100
    api = build_event_props({"hook_event_name": "StopFailure", "error": "rate_limit",
                             "error_details": "429 Too Many Requests",
                             "last_assistant_message": "API Error: Rate limit reached"})
    assert (api["error"], api["error_details"]) == ("rate_limit", "429 Too Many Requests")
    assert "tool_error" not in api
    denied = build_event_props({"hook_event_name": "PermissionDenied", "tool_name": "Bash",
                                "tool_input": {"command": "rm -rf build"},
                                "reason": "[Irreversible Local Destruction]"})
    assert denied["reason"] == "[Irreversible Local Destruction]"
    ended = build_event_props({"hook_event_name": "SessionEnd", "reason": "prompt_input_exit"})
    assert ended["reason"] == "prompt_input_exit"


def test_new_lifecycle_payloads_are_kept():
    common = {"permission_mode": "auto", "effort": {"level": "high"}}
    compacted = build_event_props({"hook_event_name": "PostCompact", "trigger": "auto",
                                   "compact_summary": "s" * 9000, **common})
    assert compacted["trigger"] == "auto"
    assert compacted["compact_summary"].endswith("...[truncated 1000 chars]")
    assert (compacted["permission_mode"], compacted["effort_level"]) == ("auto", "high")

    switched = build_event_props({"hook_event_name": "PostModelSwitch", "source": "command",
                                  "from_model": "claude-sonnet-5-5",
                                  "to_model": "claude-opus-5-5", "requested_model": "opus"})
    assert (switched["from_model"], switched["to_model"]) == ("claude-sonnet-5-5",
                                                              "claude-opus-5-5")

    task = build_event_props({"hook_event_name": "TaskCreated", "task_id": "task-001",
                              "task_subject": "Check historical reports",
                              "task_description": "Rerun reports crossing March 3",
                              "teammate_name": "analyst"})
    assert (task["task_id"], task["task_subject"], task["teammate_name"]) == (
        "task-001", "Check historical reports", "analyst")

    loaded = build_event_props({"hook_event_name": "InstructionsLoaded",
                                "file_path": "/repo/CLAUDE.md", "memory_type": "Project",
                                "load_reason": "session_start"})
    assert (loaded["file_path"], loaded["memory_type"], loaded["load_reason"]) == (
        "/repo/CLAUDE.md", "Project", "session_start")

    notified = build_event_props({"hook_event_name": "Notification",
                                  "message": "Claude needs your permission",
                                  "notification_type": "permission_prompt"})
    assert notified["notification_type"] == "permission_prompt"


def test_events_of_one_process_collapse_and_restarts_do_not(monkeypatch):
    import common

    resume = build_event_props({"hook_event_name": "SessionStart", "source": "resume",
                                "session_id": "s-bg", "cwd": "/repo"})
    monkeypatch.setenv("CLAUDE_PID", "19038")
    first, props = common.event_record("s-bg", "SessionStart", resume)
    # Parallel hooks of the same process (capture, injection, recall).
    assert common.event_record("s-bg", "SessionStart", resume)[0] == first
    assert props["harness_pid"] == "19038"
    # The supervisor restarts the session: same payload, new process.
    monkeypatch.setenv("CLAUDE_PID", "19059")
    assert common.event_record("s-bg", "SessionStart", resume)[0] != first
    # Outside the harness nothing is added.
    monkeypatch.delenv("CLAUDE_PID")
    assert "harness_pid" not in common.event_record("s-bg", "SessionStart", resume)[1]


def test_a_background_session_is_known_by_its_job(monkeypatch):
    import common

    monkeypatch.setenv("CLAUDE_JOB_DIR", "/Users/maria/.claude/jobs/bb776ea2")
    assert common.job_id() == "bb776ea2"
    monkeypatch.setenv("CLAUDE_JOB_DIR", "")
    assert common.job_id() is None
    monkeypatch.delenv("CLAUDE_JOB_DIR")
    assert common.job_id() is None
