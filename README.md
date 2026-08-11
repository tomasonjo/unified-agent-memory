# Unified Agent Memory

A starter Claude Code plugin that gives an agent the first two pieces of a
persistent memory system:

1. **Hook event capture.** Every lifecycle event (session start, prompts,
   tool calls, stop, compaction, session end) is appended to a local JSONL
   log, one file per session. This is the raw material every later memory
   stage is built from.
2. **System prompt injection.** At session start the plugin injects a
   system prompt as additional context. It ships with a bundled default
   that is harness-agnostic and use-case-agnostic, and it can instead fetch
   a versioned prompt from a `(:SystemPrompt)` node in Neo4j, so the prompt
   becomes data you manage rather than text baked into the harness.

This repo is the companion to the plugin chapter of the book. The full,
self-learning system it grows into (typed memory, extraction, consolidation,
recall) lives in the sister project,
[meta-knowledge-graph](https://github.com/neo4j-labs/meta-knowledge-graph).

## Layout

```
.claude-plugin/
  plugin.json          # plugin manifest
  marketplace.json     # lets this repo act as its own marketplace
hooks/
  hooks.json           # event wiring (which script runs on which event)
  common.py            # shared env loading + Neo4j config (stdlib only)
  log_event.py         # capture: every event -> JSONL (stdlib only)
  inject_system_prompt.py  # recall: system prompt -> session context
  seed_system_prompt.py    # write/version the prompt in Neo4j
prompts/
  default_system_prompt.md # the bundled default prompt
```

## Install

From a local checkout:

```
claude plugin marketplace add /path/to/unified-agent-memory
claude plugin install unified-agent-memory@uam
```

Or from GitHub once published:

```
claude plugin marketplace add tomasonjo/unified-agent-memory
claude plugin install unified-agent-memory@uam
```

Requirements: `python3` on PATH for event capture, and
[`uv`](https://docs.astral.sh/uv/) for the prompt hooks (they are PEP 723
scripts; uv builds a tiny cached environment with the Neo4j driver on first
run). Neo4j is optional: without it, the bundled default prompt is injected.

## What each hook does

**`log_event.py`** runs on every event, reads the hook payload from stdin,
and appends one JSON line to
`~/.unified-agent-memory/logs/<session_id>.jsonl` (override the directory
with `UAM_LOG_DIR`). Two storage decisions shape the record. Tool results
are not stored: they are the bulk of a session and regenerable, so the
record keeps only that the tool ran, what it was asked, and how many
characters came back (`tool_response_chars`). Inputs are stored: prompts
and tool inputs (bounded at 4,000 characters), and every injection the
plugin makes is appended to the same file as a `SystemPromptInjected`
record with the full injected content, so a session can be reproduced
from its log. Every record is also stamped with a `user_id`, an email
address resolved at runtime: the account logged in to the harness
(Claude Code keeps it in its local config JSON), falling back to
`git config user.email`. Sessions have owners, and later user-scoped
memory gets a stable key that crosses harnesses and machines. Inspect a
session:

```
jq -r '[.received_at, .event, .payload.tool_name // ""] | @tsv' \
  ~/.unified-agent-memory/logs/<session_id>.jsonl
```

**`inject_system_prompt.py`** runs on SessionStart (startup, resume, and
clear, not on compaction) and emits `additionalContext` JSON, which Claude
Code places at the start of the conversation. Resolution order:

1. Neo4j `(:SystemPrompt {name})`, name from `UAM_SYSTEM_PROMPT_NAME`
   (default `default`), when a graph is reachable,
2. the bundled `prompts/default_system_prompt.md`,
3. a minimal embedded constant.

Any failure falls through to the next source; the hook never blocks a
session, and the Neo4j lookup uses a short connection timeout so an
unreachable database cannot stall startup.

**`seed_system_prompt.py`** pushes a prompt into the graph:

```
uv run --script hooks/seed_system_prompt.py
uv run --script hooks/seed_system_prompt.py reviewer --file prompts/reviewer.md
```

The node keeps `content`, `version`, `created_at`, and `updated_at`.
Re-seeding identical content is a no-op; changed content bumps `version`.
The prompt is static at runtime (the SessionStart hook only reads it), but
it does not have to stay static between sessions: edit the node, and every
later session starts from the new prompt. That property is what later
chapters build on, when the prompt starts being revised from accumulated
memory instead of by hand.

## Configuration

Configuration follows the
[claude-mem](https://github.com/thedotmack/claude-mem) model: one
user-level env file under the plugin's data directory, never a `.env`
inside a project. The first hook run creates it as a commented template
(file mode 600, directory 700):

```
~/.unified-agent-memory/.env
```

Uncomment and edit what you need:

```
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=password
NEO4J_DATABASE=neo4j
UAM_SYSTEM_PROMPT_NAME=default
UAM_LOG_DIR=~/.unified-agent-memory/logs
```

Resolution rules:

- Exported environment variables win over file values.
- Only the keys listed above are ever copied out of the file; anything
  else in it stays in the file.
- Project and plugin-checkout `.env` files are never read, so a repo you
  happen to open cannot hand credentials to the hooks. This is the same
  leak class claude-mem closed by moving credentials to
  `~/.claude-mem/.env`.
- `UAM_DATA_DIR` moves the whole data directory (env file and logs), and
  `UAM_ENV_FILE` points at an alternative env file, mainly for tests.
  Both are environment-variable-only, since the file's location cannot
  come from the file itself.

## Design notes

- **One hook per concern.** Capture and injection are independent owners
  with separate scripts and separate wiring, because hooks for the same
  event run in parallel with no ordering guarantee. Each script emits a
  self-contained result and neither depends on the other having run.
- **Store what went in, not what came out.** The log keeps every input a
  session received, injected instructions included, in full, and drops
  tool results down to their size. Inputs are what reproduction and later
  memory extraction need; outputs are bulk that any tool can regenerate.
- **Never crash the session.** Hooks exit 0 on every error path and report
  problems to stderr. Memory is infrastructure; losing an event or falling
  back to the bundled prompt is always better than blocking the user.
- **Degrade by dependency.** Event capture is stdlib-only and works
  everywhere. The prompt hooks need uv, and only the graph-backed prompt
  needs Neo4j. Each capability that is missing removes one feature instead
  of breaking the plugin.
- **The prompt is data.** Moving the system prompt into the graph turns
  "edit a config file on every machine" into "update one node that every
  session, on any harness wired to the same store, reads at startup".
