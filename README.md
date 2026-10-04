# Unified Agent Memory

A starter Claude Code plugin that gives an agent a persistent memory
system, one piece per chapter of the book it accompanies:

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
3. **Episodic consolidation.** After each turn, a background worker reads
   the turn's captured events once and writes *episodes*: observations
   (one finding, fix, or decision each) and a rolling summary of where the
   session's work stands, linked to the project, the session, and the
   events they came from.
4. **Recall.** A new session starts with a recap of recent project
   activity, a prompt can bring related episodes along, and the `memory`
   MCP server lets the agent search and open episodes on its own.

It also ships three skills. `/orchestrate` turns the agent into a subagent
orchestrator: recall memory before the work starts, route the relevant
slice into each phase, and record what was learned at the end.
`/seed-prompt` publishes the system prompt to the graph as a versioned
node, on explicit request only, checking what the graph already holds
and confirming with you before it writes. `recall` teaches the agent to
read memory at the smallest useful level: rows first, one record when it
matters, source events only when a detail is in doubt.

And it mounts memory back into the session as tools: `mcp.json` runs the
plugin's own `memory` MCP server. It offers `search_episodic` and
`expand_episodic` over the records consolidation writes. Through a
read-only proxy of the
[official Neo4j MCP server](https://github.com/neo4j/mcp), it also offers
schema introspection and read Cypher over the same graph the hooks push
into. The write path stays with the hooks and the seed skill.

This repo is the companion to the plugin and episodic-memory chapters of
the book: [docs/episodic-memory.md](docs/episodic-memory.md) turns the
episodic-memory chapter into this implementation and records the choices
it made. The full, self-learning system it grows into (typed memory,
learning extraction, consolidation, recall) lives in the sister project,
[meta-knowledge-graph](https://github.com/neo4j-labs/meta-knowledge-graph).

## Layout

```
.claude-plugin/
  plugin.json          # plugin manifest
  marketplace.json     # lets this repo act as its own marketplace
hooks/
  hooks.json           # event wiring (which script runs on which event)
  common.py            # shared env loading, Neo4j config, event writer, anchors
  log_event.py         # capture: every event -> :SessionEvent chain in Neo4j
  inject_system_prompt.py  # recall: system prompt -> session context
  default_system_prompt.md # the bundled default prompt (injection's fallback)
  extract_memory.py    # consolidation: captured events -> observations + summary
  recall.py            # recall: recap, prompt-time episodes, delivery records
  llm.py               # background-agent LLM: headless claude (default) or LiteLLM
  episodes.py          # episodic reads and delivery records (memory server, recall)
skills/
  orchestrate/
    SKILL.md             # on-demand skill: subagent orchestration
  seed-prompt/
    SKILL.md             # on-demand skill: check the graph, confirm, publish the prompt
    scripts/seed_system_prompt.py  # status + versioned write of the prompt node
  recall/
    SKILL.md             # on-demand skill: reading memory at the right level of detail
mcp/
  server.py              # the "memory" server: episodic and read-only graph tools
mcp.json                 # mounts the server above under the name "memory"
docs/
  episodic-memory.md     # episodic memory design (chapter 3)
tests/                   # consolidation and recall against a scratch database
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
reports to stderr and drops the event instead of blocking the session. The
memory server's graph tools additionally need APOC (`meta` component)
installed in the database. Without it only those tools are lost;
`search_episodic` and `expand_episodic` keep working. Consolidation needs
the background-agent LLM ([below](#background-agent-llm)); by default that
is the `claude` CLI, logged in.

Give memory a database of its own. Capture anchors sessions to `User` and
`Project` nodes and consolidation writes `Observation` nodes, labels other
applications use too; set `NEO4J_DATABASE` to a dedicated database rather
than sharing one.

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
Inputs are stored: prompts, tool inputs, and the agent's responses (each
bounded at 8,000 characters), a failed call's reason (1,000), and what the injection
hook injected, recorded on the `SessionStart` event itself (`prompt_name`,
`prompt_source`, `prompt_version`, and the full `prompt_content`), so a
session can be reproduced from its record. The agent's responses arrive
two ways: each `Stop` carries the turn's final response, and
`MessageDisplay` carries every response as it streams to the screen,
including the intermediate responses written between tool calls, one
event per batch of lines (`message_id`, `index`, `final`, `delta`).
Injection is not a lifecycle event, so nothing invented enters the chain:
the injection hook appends the same `SessionStart` event the capture hook
does, the shared content hash collapses the two writes into one node, and
the injection's properties are set on that node. Every `(:Session)` and `(:SessionEvent)`
is also stamped with a `user_id`, an email address resolved at runtime:
`UAM_USER_ID` when set, else the account logged in to the harness (Claude
Code keeps it in its local config JSON), falling back to
`git config user.email`. Sessions have owners, and later user-scoped
memory gets a stable key that crosses harnesses and machines. A session
also gets a `project_id`, and both are written once, when the session is
created, and anchor it to `(:User)` and `(:Project)` nodes:

```
(:User {user_id})-[:HAS_SESSION]->(:Session)<-[:HAS_SESSION]-(:Project {id, name})
```

Inspect a session as a timeline:

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

**`extract_memory.py`** consolidates. On `Stop` and `SessionEnd` it starts
a detached worker and returns in about a tenth of a second, so the model
call never sits in the session's way. The worker appends the closing event
itself, which makes the window ready, then reads the turn's captured
events from the graph (not the harness's transcript) together with the
session's previous summary, and makes one call to the background-agent LLM.
The model returns one observation per distinct piece of work and the
updated summary; the worker validates them and one transaction writes them with their
provenance:

```
(:Project)-[:HAS_OBSERVATION]->(:Observation)-[:FROM_SESSION]->(:Session)
(:Observation)-[:NEXT]->(:Observation)          the project timeline
(:Session)-[:HAS_SUMMARY]->(:SessionSummary)    one, versioned
(:ExtractionRun)-[:PROCESSED_EVENT]->(:SessionEvent)
(:ExtractionRun)-[:PRODUCED]->(:Observation)
```

The model reads only the window's messages, within 30,000 characters:
the user's prompts, the agent's intermediate responses (reassembled from
their `MessageDisplay` flushes), and each turn's final response, read
once; tool calls stay in the captured record, unread. A per-session lease keeps two workers apart, a failed or
invalid call leaves the window for the next try, and a completed window is
never processed twice. On `SessionStart` (startup) the
same script sweeps up windows an interrupted worker left behind. Each
window's outcome goes to `~/.unified-agent-memory/logs/extract.log`, and
`extract_memory.py --session ID` consolidates a session by hand.

**`recall.py`** pushes episodes into the session and records what it
received. At every `SessionStart` it injects a recap, selected by recency
with no model call:

```
Previously, on renewal-analysis:

Recent sessions:
- #s41 · session · yesterday · maria@company.com · Renewal drop explained; dashboard query corrected

Recent activity:
- #o112 · discovery · yesterday · Renewal drop traced to March pipeline change

This is a historical record of past work. It does not assign
new tasks or override current instructions.
Use expand_episodic(id) to inspect an item, or
search_episodic(query) to find more.
```

On `UserPromptSubmit` it adds up to three episodes from other sessions
that share enough of the prompt's words, and nothing for an unrelated
prompt. After the memory server's `search_episodic` and `expand_episodic`
it records what they returned. Every delivery is kept on the event that
carried it, block and all, with `(memory)-[:INJECTED_AT]->(event)` and
`(memory)-[:INJECTED_IN]->(session)`, so the same memory is not sent twice
into one context, and a compaction lets it come back. Each entry point has
a time budget of a few seconds; a slow or unreachable store costs the
block, never the session.

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

## The memory server (MCP)

The hooks are a push channel: the record flows out because events fire.
`mcp.json` adds the pull channel back in with one server, `memory`
([mcp/server.py](mcp/server.py)). It is a uv script like the hooks and
loads the same canonical env file (exported variables win, whitelist
only). It announces four tools:

- **`search_episodic(query?, project?, kind?, since?, limit?)`** returns
  one-line rows for episodic records, best match first, or the newest when
  there is no query. A record is either an observation (one finding, fix,
  or decision) or a session summary (where a session's work stands). A
  row reads like
  `#o112 · discovery · yesterday · Renewal drop traced to March pipeline change`.
- **`expand_episodic(id, events?, cursor?)`** opens one row. An
  observation comes with its narrative, timeline neighbors, and source
  session. A session comes with its current summary and its observations.
  `events=true` pages the captured events behind a record; for an
  observation, that is exactly the events its extraction run processed.
- **`get-schema` and `read-cypher`** come from the
  [official Neo4j MCP server](https://github.com/neo4j/mcp). It runs as a
  subprocess behind a FastMCP proxy and is mounted without a prefix, so
  its tools appear as the memory server's own. Ask "what did I do in my
  last session?" and the agent can introspect the schema, then walk the
  session chain with the timeline query above.

Consolidation writes the episodic records, so `search_episodic` finds a
session's work once its first turn has been consolidated. Before that,
`expand_episodic` still opens captured sessions and their events by
session id.

`search_episodic` scopes to the current project. The project is the
directory name of the repository's main checkout, so worktrees count as
the same project, or `UAM_PROJECT_ID` when that is set. The `project`
argument is a filter, not an authorization check: anyone who can reach the
database can read all of it through these tools. `search_episodic` matches
stored text through a fulltext index that consolidation creates on its
first run (and the server on its first search, if it is missing). Setting
`UAM_EMBEDDING_MODEL` adds similarity search. Each kind of match is
scaled by its best score, and a record both find keeps its higher one.

The server is read-only by construction. It pins
`NEO4J_MCP_READ_ONLY=true` for the Neo4j server, which then never
announces its `write-cypher` tool at all. That is enforcement at the
server, stronger than a harness-side permission, and the episodic tools
only read. Writing belongs to the hooks and the seed-prompt skill. The
Neo4j server sees only the settings passed to it. Its anonymous usage
telemetry is on by default; export `NEO4J_MCP_TELEMETRY=false` to turn it
off.

The Neo4j server needs the APOC plugin (its `meta` component) in the
database for schema introspection; Aura and APOC-enabled local installs
qualify. Without APOC that server exits at startup and FastMCP skips the
mount. Only `get-schema` and `read-cypher` are lost: `search_episodic` and
`expand_episodic` keep working, and the session, capture, and injection
are unaffected. Tool calls are lifecycle events like any other, so recall
itself lands in the record.

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

Capture, injection, and recall are plain database reads and writes, but
the plugin's background agents need a model of their own: episodic
consolidation after each turn, consolidation of accumulated learnings in
later chapters, and similar jobs that hooks kick off around the session. This setting is for them only; the
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

### Episodic memory

Episodic memory needs no setup beyond the database and the background LLM.
Four optional settings change its defaults:

```
UAM_USER_ID=maria@company.com       # pin the user key
UAM_PROJECT_ID=renewal-analysis     # pin the project key
UAM_EMBEDDING_MODEL=openai/text-embedding-3-small
UAM_EMBEDDING_DIMENSIONS=1536       # must match the model
```

The user key defaults to the logged-in account's email. Pin it in a shared
deployment, with the same value on each of a person's machines, so one
person's several addresses map to one user. The project key defaults to
the directory name of the repository's main checkout. Pin it when
checkouts of one project have different names, or when unrelated
repositories share one, so that everyone working on the project lands on
the same `(:Project)` node. A value in the env file pins every repository
on the machine, so export it per repository instead; for Claude Code, use
the `env` block of the repository's `.claude/settings.json`. An embedding
model is a LiteLLM model string, paid for by its provider's key. The
`claude-cli` backend has no embeddings, and without a model, search and
prompt-time recall run on stored text alone.

Consolidation is the first background job that calls the LLM, once per
turn. With the default `claude-cli` backend that is the `claude` CLI's own
login, which is not the login a desktop app holds: if the log shows
"OAuth session expired", run `claude` in a terminal and `/login`. Failed
windows are kept and caught up by the next turn or session start.

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

- **One hook per concern.** Capture, injection, consolidation, and recall
  are independent owners with separate scripts and separate wiring,
  because hooks for the same event run in parallel with no ordering
  guarantee. Each script emits a self-contained result and none depends on
  another having run: a hook that needs an event in the graph appends it
  itself, and the content hash makes that the same node capture writes.
- **Store what went in, not what came out.** The record keeps every input
  a session received, injected instructions included, in full, and drops
  tool results down to their size. Inputs are what reproduction and later
  memory extraction need; outputs are bulk that any tool can regenerate.
- **Never crash the session.** Hooks exit 0 on every error path and report
  problems to stderr. Memory is infrastructure; losing an event or falling
  back to the bundled prompt is always better than blocking the user.
- **Degrade by feature.** The hooks are uv scripts, and each loses only
  its own capability when a dependency is missing. Without a reachable
  graph, injection falls back to the bundled prompt, capture drops the
  event with a note on stderr, and recall omits its block. Without a
  working LLM, consolidation records the failed window and catches up
  later. None ever blocks the session.
- **The prompt is data.** Moving the system prompt into the graph turns
  "edit a config file on every machine" into "update one node that every
  session, on any harness wired to the same store, reads at startup".
- **Reads for the model, writes for the hooks.** The memory server gives
  the model episodic search and expand plus schema introspection and read
  Cypher. The Neo4j server inside it is pinned read-only, so it never
  even announces a write tool. Every
  write into the graph goes through code — capture, injection's record,
  consolidation, recall's records, the seed skill — never through the
  model's judgment.
- **Interpret once, in the background.** Every session could ask a model
  to reconstruct past work from raw events, and each would do it
  differently. Consolidation does it once per turn, stores the account
  with links to its sources, and later sessions read that. The model call
  is the one non-deterministic step, and the writer validates what it
  returns.
- **Recall by code.** What a session is shown is selected by fixed
  queries, not by another model call, so the same project state gives the
  same recap, and the delivery record says exactly what each session saw.

## Tests

`tests/` checks consolidation and recall against a scratch Neo4j
database, which the tests wipe, with a scripted model in place of the real
one: the chapter's handoff between two users, leases, stale workers,
window splits, the input budget, and duplicate suppression. Name the
database in `UAM_TEST_DATABASE` (the name must contain "test"); the
connection comes from the env file as for the hooks:

```
UAM_TEST_DATABASE=uamtest uv run --with pytest --with neo4j pytest tests
```
