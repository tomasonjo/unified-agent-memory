---
name: recap
description: Recap past sessions from the plugin's capture log. Use when asked what happened in a previous session, what was worked on earlier, or when something was done.
---

# Recap

The capture hook writes every session to a JSONL log. This skill reads
those logs back, checked against the graph. Work in two steps: index
first, detail only when the question needs it.

Step 1, always: run the bundled script for an index of recent sessions.

    uv run --script "${CLAUDE_PLUGIN_ROOT}/skills/recap/scripts/recap.py"

(From a repo checkout without the plugin installed, the script is at
`skills/recap/scripts/recap.py`. Without uv, plain python3 works too and
skips the graph check.)

Each line shows a session id, its start time, the event count, the tools
used, and the first prompt. That is usually enough to answer "what
happened" or "when did we do X". The script also asks Neo4j for the
current version of each `(:SystemPrompt)` node: a line like `started on
default v3, graph now at v5` means that session ran on instructions the
graph has since replaced, so weigh its conclusions accordingly.

Step 2, only when one session matters: rerun the script with that
session's id prefix to get its timeline, one event per line.

    uv run --script "${CLAUDE_PLUGIN_ROOT}/skills/recap/scripts/recap.py" 0e11a3f2

Do not read the raw JSONL files into context. The logs are long, and the
script exists precisely so you do not have to pay for every line.
