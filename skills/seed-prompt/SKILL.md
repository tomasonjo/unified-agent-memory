---
name: seed-prompt
description: Publish or update the standing instructions as a versioned (:SystemPrompt) node in the knowledge graph. Use only when the user explicitly asks to seed, update, or publish them; never invoke on your own initiative.
---

# Seed prompt

This skill writes the standing instructions every future session starts
from. That makes it a write path worth treating with care: look before
writing, put the decision to the user, then report exactly what
happened. Follow the steps in order; never skip the check.

Step 1: identify the source. With no arguments the script works with
the plugin's bundled `prompts/default_system_prompt.md` under the name
`default`; a name and `--file` select other content. If the user points
at a file, read it first and confirm it is the content they mean to
publish.

Step 2: check what the graph already holds. Run the script with
`--status`, which writes nothing:

    uv run --script "${CLAUDE_PLUGIN_ROOT}/skills/seed-prompt/scripts/seed_system_prompt.py" --status

(From a repo checkout without the plugin installed, the script is at
`skills/seed-prompt/scripts/seed_system_prompt.py`. Neo4j connection
settings come from exported `NEO4J_*` variables or the plugin's env
file, the same sources the hooks read.)

Step 3: relay the status line and decide with the user.

- No node yet: confirm the user wants to create it, then seed.
- Node exists, content identical: report that re-seeding would change
  nothing and stop; there is nothing to do.
- Node exists, content differs: tell the user the current version and
  that seeding bumps it, and ask whether to overwrite. Never overwrite
  without an explicit yes.

Step 4: seed (the same command without `--status`) and repeat the
script's report line verbatim: `created` starts the node at v1,
`unchanged` means the content was identical and the version did not
move, `updated` means the counter bumped. On `updated`, tell the user
the new version applies to sessions that start or clear after this
moment; already-running and resumed sessions keep the instructions they
started with.
