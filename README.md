# Unified Agent Memory

A starter Claude Code plugin that gives an agent the first two pieces of a
persistent memory system:

1. **Hook event capture.** Every lifecycle event (session start, prompts,
   tool calls, stop, compaction, session end) is appended to Neo4j as a
   per-session chain of `(:SessionEvent)` nodes, the same episodic shape
   the meta-knowledge-graph sister project uses. This is the raw material
   every later memory stage is built from.
2. **System prompt injection.** At session start the plugin injects a
   system prompt as additional context. It ships with a bundled default
   that is harness-agnostic and use-case-agnostic, and it can instead fetch
   a versioned prompt from a `(:SystemPrompt)` node in Neo4j, so the prompt
   becomes data you manage rather than text baked into the harness.

It also ships two skills. `/orchestrate` turns the agent into a subagent
orchestrator: recall memory before the work starts, route the relevant
slice into each phase, and record what was learned at the end.
`/seed-prompt` publishes the system prompt to the graph as a versioned
node, on explicit request only, checking what the graph already holds
and confirming with you before it writes.

And it mounts the graph back into the session as tools: `mcp.json`
runs the [official Neo4j MCP server](https://github.com/neo4j/mcp) in
read-only mode, so the model can pull from the same graph the hooks
push into — introspect the schema and run read Cypher — while the
write path stays with the hooks and the seed skill.

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
  common.py            # shared env loading, Neo4j config, event writer
  log_event.py         # capture: every event -> :SessionEvent chain in Neo4j
  inject_system_prompt.py  # recall: system prompt -> session context
  default_system_prompt.md # the bundled default prompt (injection's fallback)
  llm.py               # background-agent LLM: headless claude (default) or LiteLLM
skills/
  orchestrate/
    SKILL.md             # on-demand skill: subagent orchestration
  seed-prompt/
    SKILL.md             # on-demand skill: check the graph, confirm, publish the prompt
    scripts/seed_system_prompt.py  # status + versioned write of the prompt node
mcp/
  run_neo4j_mcp.py       # launcher: canonical env -> official Neo4j MCP server, read-only
mcp.json                 # mounts the server above under the name "neo4j"
```

`mcp.json` carries the file name the
[Agent Plugins Specification](https://agent-plugins.org/specification)
standardizes rather than Claude Code's default `.mcp.json`; one line in
`plugin.json` (`"mcpServers": "./mcp.json"`) wires it in.

## Install

From GitHub:

```
claude plugin marketplace add tomasonjo/unified-agent-memory
claude plugin install unified-agent-memory@uam
```

For development, load a checkout for a single session instead of
installing:

```
claude --plugin-dir /path/to/unified-agent-memory
```

Requirements: [`uv`](https://docs.astral.sh/uv/) for the hooks (they are
PEP 723 scripts; uv builds a tiny cached environment with the Neo4j driver
on first run) and a reachable Neo4j for event capture. Without Neo4j the
plugin still runs: the bundled default prompt is injected, and capture
reports to stderr and drops the event instead of blocking the session.
The read-only MCP mount additionally needs APOC (`meta` component)
installed in the database; without it the mount alone is lost.

## What each hook does

**`log_event.py`** runs on every event, reads the hook payload from stdin,
and appends one `(:SessionEvent)` node to the session's chain in Neo4j,
the same episodic shape the
[meta-knowledge-graph](https://github.com/neo4j-labs/meta-knowledge-graph)
sister project uses:

```
(:Session)-[:FIRST_EVENT]->(:SessionEvent)-[:NEXT]->(:SessionEvent)-...
(:Session)-[:HAS_EVENT]->(every :SessionEvent)
(:Session)-[:LATEST_EVENT]->(the newest :SessionEvent)
```

Each event's id is a hash of its content, so the same payload delivered to
two parallel hook configs collapses to one node. Two storage decisions
shape the record. Tool results are not stored: they are the bulk of a
session and regenerable, so the record keeps only that the tool ran, what
it was asked, and how many characters came back (`tool_response_chars`).
Inputs are stored: prompts and tool inputs (bounded at 4,000 characters),
and what the injection hook injected is recorded on the `SessionStart`
event itself (`prompt_name`, `prompt_source`, `prompt_version`, and the
full `prompt_content`), so a session can be reproduced from its record.
Injection is not a lifecycle event, so nothing invented enters the chain:
the injection hook appends the same `SessionStart` event the capture hook
does, the shared content hash collapses the two writes into one node, and
the injection's properties are set on that node. Every `(:Session)` and `(:SessionEvent)`
is also stamped with a `user_id`, an email address resolved at runtime:
the account logged in to the harness (Claude Code keeps it in its local
config JSON), falling back to `git config user.email`. Sessions have
owners, and later user-scoped memory gets a stable key that crosses
harnesses and machines. Inspect a session as a timeline:

```
MATCH (s:Session {session_id: $session_id})-[:FIRST_EVENT]->(first)
MATCH path = (first)-[:NEXT*0..]->(e)
RETURN e.timestamp, e.event_name, e.tool_name
ORDER BY length(path)
```

**`inject_system_prompt.py`** runs on SessionStart (startup and clear,
the sources whose conversation begins empty; a resumed session replays
its transcript, injection included, and compaction carries a summary
forward) and emits `additionalContext` JSON, which Claude
Code places at the start of the conversation. Resolution order:

1. Neo4j `(:SystemPrompt {name})`, name from `UAM_AGENT_NAME`
   (default `default`), when a graph is reachable,
2. the bundled `hooks/default_system_prompt.md`,
3. a minimal embedded constant.

Any failure falls through to the next source; the hook never blocks a
session, and the Neo4j lookup uses a short connection timeout so an
unreachable database cannot stall startup. What it injected it records
on the session's `SessionStart` event, full text included.

## The skills

A skill is a named folder holding a `SKILL.md`: only its name and one-line
description sit in context, and the full text loads when you run the skill
by name or the model matches the task to the description.

**`orchestrate`** is instructions, not code. It makes the agent delegate a
multi-phase plan to subagents. Subagents start with an empty context, so
the orchestrator's job is routing memory: recall what is relevant before
the work, hand each phase only the slice it needs, verify evidence between
phases, and record lessons when the last phase lands. Capture needs no
help from the skill: `SubagentStart` and `SubagentStop` are among the
events `log_event.py` already writes down.

**`seed-prompt`** shows the other thing a skill can carry: an
executable. Its `SKILL.md` is a few lines of discipline; the work lives
in a bundled script run through uv like the injection hook, because it
talks to the same graph. It is the write side of the prompt story, and
it looks before it writes. Invoked explicitly (`/seed-prompt`), the
skill first runs the script with `--status`, a read-only flag that
reports what the graph already holds (no node yet, identical content,
or content that differs and would bump the version), relays that to the
user, and asks before creating or overwriting anything. Only on an
explicit yes does it seed:

```
uv run --script skills/seed-prompt/scripts/seed_system_prompt.py --status
uv run --script skills/seed-prompt/scripts/seed_system_prompt.py
uv run --script skills/seed-prompt/scripts/seed_system_prompt.py reviewer --file path/to/reviewer.md
```

The node keeps `content`, `version`, `created_at`, and `updated_at`.
Re-seeding identical content is a no-op; changed content bumps `version`.
The prompt is static at runtime (the SessionStart hook only reads it), but
it does not have to stay static between sessions: seed a new version, and
every later session starts from it. Because this is the write path for
the instructions every future session starts from, the skill runs only on
explicit request and reports the version transition it caused. That
property is what later chapters build on, when the prompt starts being
revised from accumulated memory instead of by hand.

The skill format and the pairing of memory hooks with skills follow
[claude-mem](https://github.com/thedotmack/claude-mem), whose `do` and
`mem-search` skills are worth reading.

## The graph as tools (read-only MCP mount)

The hooks are a push channel: the record flows out because events fire.
`mcp.json` adds the pull channel back in: it mounts the
[official Neo4j MCP server](https://github.com/neo4j/mcp) under the
name `neo4j`, so the model can query the same graph the hooks write to
— ask "what did I do in my last session?" and the agent introspects the
schema, then walks the session chain with the timeline query above.
Those tool calls are lifecycle events like any other, so recall itself
lands in the record.

The mount is read-only by construction. `mcp.json` does not launch the
server directly; it runs `mcp/run_neo4j_mcp.py`, a uv script that loads
the same canonical env file the hooks read (exported variables win,
whitelist only), resolves the same connection defaults, pins
`NEO4J_READ_ONLY=true`, and execs the server. With that flag the server
never announces its `write-cypher` tool at all — enforcement at the
server, stronger than a harness-side permission — leaving `get-schema`
and `read-cypher` (plus a GDS procedure listing when the database has
GDS installed). The model reads; writing belongs to the hooks and the
seed-prompt skill.

The server needs the APOC plugin (its `meta` component) available in
the database for schema introspection; Aura and APOC-enabled local
installs qualify. Without it the server exits at startup and the mount
costs only its own feature: the session runs, capture and injection are
unaffected, and the tools are simply absent.

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
UAM_AGENT_NAME=default
```

### Background-agent LLM

Capture and injection are plain database reads and writes, but the
plugin's background agents need a model of their own: memory extraction
at stop, consolidation of accumulated learnings, and similar jobs that
hooks kick off around the session. This setting is for them only; the
model answering your interactive session is unaffected. Every
background call goes through one entry point, `llm_complete()` in
`hooks/llm.py`, behind one switch:

**Default: `claude-cli`, nothing to set up.** Hooks run headless Claude
Code (`claude -p`), which authenticates with the same Claude login as
the session that triggered the hook, so your existing subscription pays
for the call and no API key is involved. Haiku is the default model to
keep background calls fast and cheap. The only requirements are that
the `claude` CLI is on PATH and logged in (run `/login` once if
headless calls report an expired token).

```
UAM_LLM_BACKEND=claude-cli   # the default; no need to set it
UAM_CLAUDE_CLI_MODEL=haiku   # raise to sonnet/opus when a task needs more
```

**Fallback: `litellm`, for any other provider.** Switch the backend to
`litellm` when completions should come from OpenAI, Google, a local
server, or anything else LiteLLM reaches. `UAM_LLM_MODEL` takes a
LiteLLM model string, paid for by the matching provider key:

```
UAM_LLM_BACKEND=litellm
UAM_LLM_MODEL=gpt-5.4-mini   # or gemini/gemini-2.5-flash, ollama/..., etc.
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GEMINI_API_KEY=
```

Anthropic model strings on the litellm backend still prefer the
subscription: a fresh Claude Code OAuth token is read from the platform
credential store when no key is set, and the call degrades to headless
`claude -p` when neither is usable. Either way, a `claude -p` spawned
by a hook carries the `UAM_IN_LLM_SUBPROCESS` sentinel, so the plugin's
own hooks no-op inside it instead of capturing it or recursing.

Resolution rules:

- Exported environment variables win over file values.
- Only the keys listed above are ever copied out of the file; anything
  else in it stays in the file.
- Project and plugin-checkout `.env` files are never read, so a repo you
  happen to open cannot hand credentials to the hooks. This is the same
  leak class claude-mem closed by moving credentials to
  `~/.claude-mem/.env`.
- `UAM_DATA_DIR` moves the whole data directory, and `UAM_ENV_FILE`
  points at an alternative env file, mainly for tests. Both are
  environment-variable-only, since the file's location cannot come from
  the file itself.

## Design notes

- **One hook per concern.** Capture and injection are independent owners
  with separate scripts and separate wiring, because hooks for the same
  event run in parallel with no ordering guarantee. Each script emits a
  self-contained result and neither depends on the other having run.
- **Store what went in, not what came out.** The record keeps every input
  a session received, injected instructions included, in full, and drops
  tool results down to their size. Inputs are what reproduction and later
  memory extraction need; outputs are bulk that any tool can regenerate.
- **Never crash the session.** Hooks exit 0 on every error path and report
  problems to stderr. Memory is infrastructure; losing an event or falling
  back to the bundled prompt is always better than blocking the user.
- **Degrade by feature.** Both hooks are uv scripts, but each loses only
  its own capability when a dependency is missing. Without a reachable
  graph, injection falls back to the bundled prompt and capture drops the
  event with a note on stderr; neither ever blocks the session.
- **The prompt is data.** Moving the system prompt into the graph turns
  "edit a config file on every machine" into "update one node that every
  session, on any harness wired to the same store, reads at startup".
- **Reads for the model, writes for the hooks.** The MCP mount gives the
  model schema introspection and read Cypher only, pinned read-only in
  the launcher so the server never even announces a write tool. Every
  write into the graph goes through code — capture, injection's record,
  the seed skill — never through the model's judgment.
