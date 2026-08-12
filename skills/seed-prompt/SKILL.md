---
name: seed-prompt
description: Push a system prompt into the knowledge graph as a versioned (:SystemPrompt) node. Use only when the user explicitly asks to seed, update, or publish the standing instructions; never invoke on your own initiative.
---

# Seed prompt

This skill writes the standing instructions every future session starts
from. That makes it a write path worth treating with care: confirm what
is about to be pushed, push it, and report exactly what changed.

Step 1: identify the source. With no arguments the script pushes the
plugin's bundled `prompts/default_system_prompt.md` under the name
`default`; a name and `--file` push other content. If the user points at
a file, read it first and confirm it is the content they mean to
publish.

Step 2: run the bundled script.

    uv run --script "${CLAUDE_PLUGIN_ROOT}/skills/seed-prompt/scripts/seed_system_prompt.py"

(From a repo checkout without the plugin installed, the script is at
`skills/seed-prompt/scripts/seed_system_prompt.py`. Neo4j connection
settings come from exported `NEO4J_*` variables or the plugin's env
file, the same sources the hooks read.)

Step 3: report the result line verbatim. The script prints the action
and the version: `created` starts a node at v1, `unchanged` means the
content was identical and the version did not move, `updated` means the
counter bumped. On `updated`, tell the user the new version applies to
sessions that start or clear after this moment; already-running and
resumed sessions keep the instructions they started with.
