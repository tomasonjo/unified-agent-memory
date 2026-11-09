"""Shared helpers for Unified Agent Memory hooks.

Importable with the standard library alone; the Neo4j driver is imported
lazily inside the functions that talk to the graph, so the module loads
whether a hook runs under plain python3 or as a PEP 723 uv script.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path


def plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


LLM_SUBPROCESS_SENTINEL = "UAM_IN_LLM_SUBPROCESS"


def in_llm_subprocess() -> bool:
    """True inside a ``claude -p`` spawned by hooks/llm.py.

    Hooks must no-op when this is set: the nested session loads this
    plugin's own hooks (non-bare mode is what makes subscription auth
    work), so without the sentinel every helper call would be captured
    as a session of its own, and a hook that both listens and spawns
    would recurse.
    """
    return bool(os.environ.get(LLM_SUBPROCESS_SENTINEL))


def read_payload() -> dict:
    """The hook payload on stdin, parsed; ``{}`` when stdin is empty.

    Claude Code writes UTF-8, so the bytes are decoded as UTF-8 here.
    ``sys.stdin`` would decode with the locale's encoding, which on
    Windows before Python 3.15 is the ANSI code page: cp1252 raises on
    some UTF-8 bytes (a closing curly quote, ``č``) and garbles the rest.
    """
    if sys.stdin is None:
        return {}
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    return json.loads(raw) if raw.strip() else {}


def _claude_account_email() -> str:
    """Email of the account logged in to Claude Code, read at runtime.

    The hook payload does not carry identity, but the harness knows who
    is logged in: its local config JSON holds the OAuth account. This is
    an internal file rather than a documented interface, so it is one
    source in a chain, never the only one.
    """
    candidates = []
    config_dir = os.getenv("CLAUDE_CONFIG_DIR")
    if config_dir:
        candidates.append(Path(config_dir) / ".claude.json")
    candidates.append(Path.home() / ".claude" / ".claude.json")
    candidates.append(Path.home() / ".claude.json")
    for candidate in candidates:
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
        email = (data.get("oauthAccount") or {}).get("emailAddress") or ""
        if email.strip():
            return email.strip().lower()
    return ""


def _git_email() -> str:
    try:
        out = subprocess.run(
            ["git", "config", "--get", "user.email"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
        )
        return out.stdout.strip().lower()
    except Exception:
        return ""


def user_id() -> str:
    """Resolve the user identity every record is stamped with.

    An email address, because it is the one identity that stays the same
    across harnesses and machines. Resolution order: ``UAM_USER_ID`` when
    set, then the account logged in to the harness, then the email in the
    machine's git configuration. The override is how a shared deployment
    maps one person's several addresses to one id: set the same value on
    each of their machines. A locally configured email is a key, not proof
    of identity.
    """
    override = (os.getenv("UAM_USER_ID") or "").strip()
    return override or _claude_account_email() or _git_email() or "unknown"


def _git_main_root(directory: Path) -> Path | None:
    """Root of the main checkout that ``directory`` belongs to, if any.

    Asks git for the common directory rather than the top level, so a
    linked worktree (Claude Code creates them under .claude/worktrees)
    resolves to the checkout it was made from, not to a directory named
    after the worktree.
    """
    try:
        out = subprocess.run(
            [
                "git",
                "-C",
                str(directory),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
        )
    except Exception:
        return None
    common = out.stdout.strip()
    if out.returncode != 0 or not common:
        return None
    path = Path(common)
    return path.parent if path.name == ".git" else path


def project_id(cwd: str | None = None) -> str:
    """Resolve the project key episodic memory is organized by.

    ``UAM_PROJECT_ID`` wins when set. Exported per repository, it pins one
    id for every checkout of a project whatever its directory is called;
    set in the user-level env file it would pin every repository on the
    machine. Otherwise the key is the directory name of the main checkout,
    or of the working directory outside git. Directory names are only a
    convenient default: two unrelated repositories can share one, which is
    what the override is for.
    """
    override = (os.getenv("UAM_PROJECT_ID") or "").strip()
    if override:
        return override
    directory = Path(cwd or os.getenv("CLAUDE_PROJECT_DIR") or os.getcwd())
    root = _git_main_root(directory) or directory
    return root.name or "unknown"


def harness() -> str:
    """Name of the harness this capture is wired into.

    No lookup needed: hook registration is harness-specific by
    construction, so the script knows where it runs the way it knows its
    own filename. The env override is for ports — wire the same script
    into another harness and set UAM_HARNESS in that hook configuration.
    """
    return os.getenv("UAM_HARNESS", "claude-code")


def job_id() -> str | None:
    """The background-session job this hook runs in, or None.

    Claude Code sets ``CLAUDE_JOB_DIR`` to ``~/.claude/jobs/<id>`` in every
    background session (``claude --bg``, ``/background``, a dispatch from
    agent view), and the hook inherits it. A session started from a
    background session's own shell would inherit it too.
    """
    return Path((os.getenv("CLAUDE_JOB_DIR") or "").strip()).name or None


def harness_pid() -> str | None:
    """Process id of the harness that fired the hook (``CLAUDE_PID``).

    Every hook of one Claude Code process gets the same value, and a
    restarted process gets a new one: the supervisor waking an idle
    background session, ``claude respawn``, or ``--resume``.
    """
    return (os.getenv("CLAUDE_PID") or "").strip() or None


# Wrappers the harness puts around a prompt it submits on someone else's
# behalf. UserPromptSubmit fires for these too, with nothing in the payload
# to tell them from typed text, so the opening tag is the only mark.
HARNESS_PROMPT_TAGS = (
    "task-notification",  # a background task finished
    "agent-message",  # a background subagent's report
    "teammate-message",  # an agent-team teammate
    "cross-session-message",  # another session
    "channel",  # an MCP channel, such as a chat bridge
)
_PROMPT_TAG = re.compile(r"<([a-z][a-z-]*)[\s>]")


def prompt_origin(prompt) -> str:
    """Who a submitted prompt came from: the harness wrapper's tag that
    opens it, else ``user``.

    A scheduled prompt (``/loop``, a cron) arrives bare and still reads as
    ``user``; nothing in the prompt or payload marks it.
    """
    match = _PROMPT_TAG.match(prompt.lstrip()) if isinstance(prompt, str) else None
    return match.group(1) if match and match.group(1) in HARNESS_PROMPT_TAGS else "user"


# The sender of each report a submitted prompt carries: the agent named by an
# <agent-message from="...">, or the task id of a <task-notification>, which
# for a background subagent is its agent id.
_REPORT_SENDER = re.compile(
    r'<agent-message\b[^>]*?\bfrom="([^"]+)"'
    r"|<task-notification>(?:(?!</task-notification>).)*?<task-id>\s*([^<\s]+)\s*</task-id>",
    re.DOTALL,
)


def delivered_agent_ids(prompt) -> list[str]:
    """Ids of the agents whose reports a harness-submitted prompt delivers.

    One prompt can carry several reports. Empty for a prompt the user
    typed. The id of a background task that is not a subagent, such as a
    shell, matches no ``SubagentStop``, so it links nothing.
    """
    if not isinstance(prompt, str) or prompt_origin(prompt) == "user":
        return []
    ids: list[str] = []
    for agent, task in _REPORT_SENDER.findall(prompt):
        sender = agent or task
        if sender not in ids:
            ids.append(sender)
    return ids


def _transcript_model(transcript_path) -> str:
    """Model of the last assistant message in the session transcript.

    The transcript is the harness's own record of the conversation, one
    JSON object per line, and every assistant message names the model
    that produced it. Only the tail is read: the latest answer is the
    one that matters, and a hook that runs on every event cannot afford
    to reread a long session each time.
    """
    if not transcript_path:
        return ""
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as f:
            if size > 65536:
                f.seek(size - 65536)
            tail = f.read().decode("utf-8", errors="replace")
    except Exception:
        return ""
    for line in reversed(tail.splitlines()):
        try:
            message = json.loads(line).get("message")
        except Exception:
            continue
        if isinstance(message, dict) and message.get("model"):
            return str(message["model"])
    return ""


def data_dir() -> Path:
    """Root of the plugin's user-level state.

    One directory in the home folder, shared by every project on the
    machine, following claude-mem's layout (``~/.claude-mem``). Nothing
    the plugin reads or writes lives inside a project checkout.
    """
    override = os.getenv("UAM_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".unified-agent-memory"


# Named on purpose. An unnamed constraint gets a name Neo4j derives from
# its schema, and that same derived name is given to the backing index —
# so the two are only ever addressable by a hash the plugin never chose.
# Each entry is (name, label, property, statement). The User and Project
# anchors are MERGEd by capture, so their keys need uniqueness too: without
# it, two sessions starting at once could each create the same person.
SESSION_SCHEMA = (
    (
        "uam_session_id",
        "Session",
        "session_id",
        "CREATE CONSTRAINT uam_session_id IF NOT EXISTS "
        "FOR (s:Session) REQUIRE s.session_id IS UNIQUE",
    ),
    (
        "uam_session_event_id",
        "SessionEvent",
        "event_id",
        "CREATE CONSTRAINT uam_session_event_id IF NOT EXISTS "
        "FOR (e:SessionEvent) REQUIRE e.event_id IS UNIQUE",
    ),
    (
        "uam_user_id",
        "User",
        "user_id",
        "CREATE CONSTRAINT uam_user_id IF NOT EXISTS "
        "FOR (u:User) REQUIRE u.user_id IS UNIQUE",
    ),
    (
        "uam_project_id",
        "Project",
        "id",
        "CREATE CONSTRAINT uam_project_id IF NOT EXISTS "
        "FOR (p:Project) REQUIRE p.id IS UNIQUE",
    ),
)


# Range indexes for the lookups that cannot start from the Session. A
# subagent's branch has no relationship to the session, so the append finds
# the branch, and the Agent call that started it, by the events' own
# properties. Each entry is (name, label, properties, statement).
SESSION_INDEXES = (
    (
        "uam_event_session",
        "SessionEvent",
        ("session_id",),
        "CREATE INDEX uam_event_session IF NOT EXISTS "
        "FOR (e:SessionEvent) ON (e.session_id)",
    ),
    (
        "uam_event_agent",
        "SessionEvent",
        ("session_id", "agent_id", "event_name"),
        "CREATE INDEX uam_event_agent IF NOT EXISTS "
        "FOR (e:SessionEvent) ON (e.session_id, e.agent_id, e.event_name)",
    ),
    (
        "uam_event_spawned",
        "SessionEvent",
        ("session_id", "spawned_agent_id"),
        "CREATE INDEX uam_event_spawned IF NOT EXISTS "
        "FOR (e:SessionEvent) ON (e.session_id, e.spawned_agent_id)",
    ),
)


def _range_indexes(session) -> set[tuple[str, tuple[str, ...]]] | None:
    """(label, properties) of the range indexes that already exist, or
    ``None`` when the user may not list indexes."""
    try:
        records = session.run(
            "SHOW INDEXES YIELD type, labelsOrTypes, properties "
            "WHERE type = 'RANGE' RETURN labelsOrTypes, properties"
        )
        return {
            (labels[0], tuple(props))
            for labels, props in (tuple(r.values()) for r in records)
            if labels and props
        }
    except Exception:
        return None


def _unique_keys(session) -> set[tuple[str, str]] | None:
    """(label, property) pairs that already carry a uniqueness guarantee.

    Checked by schema rather than by name, so an equivalent constraint that
    another application created under its own name counts too. ``None``
    when the user may not list constraints.
    """
    try:
        records = session.run(
            "SHOW CONSTRAINTS YIELD type, labelsOrTypes, properties "
            "WHERE type IN ['UNIQUENESS', 'NODE_PROPERTY_UNIQUENESS', 'NODE_KEY'] "
            "RETURN labelsOrTypes, properties"
        )
        return {
            (labels[0], props[0])
            for labels, props in (tuple(r.values()) for r in records)
            if len(labels) == 1 and len(props) == 1
        }
    except Exception:
        return None


def _ensure_session_schema(session) -> None:
    """Create the uniqueness constraints and the range indexes, tolerating
    pre-existing schema.

    This runs on every captured event, so the common case, where all of
    them exist, costs one listing instead of one statement per constraint.
    Each missing one gets its own transaction. Schema commands do not
    compose into one atomic unit the way writes do, so folding them into
    a single ``execute_write`` means a failure on a later one rolls back
    the earlier ones and the graph ends up with none.

    Failures are reported and swallowed rather than raised. ``IF NOT
    EXISTS`` covers an existing constraint, but not a leftover backing
    index whose owning constraint was dropped: the index keeps the
    derived name, and the next create collides with it under a name
    nothing here asked for. The constraints are a guard on the dedupe in
    ``_append_event``, not the payload — that query already filters
    duplicates on its own, so a missing constraint costs atomicity under
    concurrent writes, never the event itself.
    """
    existing = _unique_keys(session)
    for name, label, prop, statement in SESSION_SCHEMA:
        if existing is not None and (label, prop) in existing:
            continue
        try:
            session.run(statement).consume()
        except Exception as exc:
            print(f"[uam] schema: {name} not created: {exc}", file=sys.stderr)
    indexed = _range_indexes(session)
    for name, label, props, statement in SESSION_INDEXES:
        if indexed is not None and (label, props) in indexed:
            continue
        try:
            session.run(statement).consume()
        except Exception as exc:
            print(f"[uam] schema: {name} not created: {exc}", file=sys.stderr)


# The owner and project are written once, when the session is created, and
# kept afterwards: a session has one owner and one project even if a later
# event resolves differently (a changed git email, a cd into another repo).
# The anchors are MERGEd only while missing, which also gives sessions
# captured before they existed their anchors on the next event.
#
# The first write takes the session's exclusive lock, so concurrent appends
# queue on it. Without it, a workflow's agents appending to one session
# took shared locks on its node (dense, after a few dozen events) before
# asking for the exclusive one, deadlocked, and backed off until the
# driver's retry window ran out: 43 of 1,126 events in a burst from 100
# agents were lost that way.
#
# A subagent's events (they carry ``agent_id``) go on a branch of their own
# instead of the session's chain. The branch starts at the subagent's
# SubagentStart, which keeps a BRANCH_LATEST pointer at the branch's tail,
# and has no relationship to the Session: FIRST_EVENT, LATEST_EVENT, and
# HAS_EVENT reach the main agent's events only. An event with an
# ``agent_id`` but no branch to join (the harness's own agents, such as
# prompt suggestions, fire SubagentStop without a start) stays on the main
# chain. The prefix takes the lock and reports whether the event is a
# duplicate and whether its branch exists; the append and the links run
# in the same transaction, so the lock covers them too.
APPEND_PREFIX = """
MERGE (s:Session {session_id: $session_id})
ON CREATE SET s.created_at = datetime($timestamp)
SET s._uam_lock = true
REMOVE s._uam_lock
SET s.user_id = coalesce(s.user_id, $user_id),
    s.project_id = coalesce(s.project_id, $project_id),
    s.harness = $harness,
    s.model = coalesce($model, s.model),
    s.job_id = coalesce($job_id, s.job_id)
WITH s
FOREACH (_ IN CASE WHEN NOT EXISTS { (:User)-[:HAS_SESSION]->(s) }
                   THEN [1] ELSE [] END |
    MERGE (u:User {user_id: s.user_id})
    MERGE (u)-[:HAS_SESSION]->(s)
)
FOREACH (_ IN CASE WHEN NOT EXISTS { (:Project)-[:HAS_SESSION]->(s) }
                   THEN [1] ELSE [] END |
    MERGE (p:Project {id: s.project_id})
    ON CREATE SET p.name = s.project_id
    MERGE (p)-[:HAS_SESSION]->(s)
)
WITH s
OPTIONAL MATCH (dup:SessionEvent {event_id: $event_id})
OPTIONAL MATCH (root:SessionEvent {session_id: $session_id, agent_id: $agent_id,
                                   event_name: 'SubagentStart'})-[:BRANCH_LATEST]->()
RETURN dup IS NOT NULL AS duplicate, root IS NOT NULL AS branch
LIMIT 1
"""

APPEND_MAIN = """
MATCH (s:Session {session_id: $session_id})
CREATE (e:SessionEvent $event_props)
SET e.timestamp = datetime($timestamp)
CREATE (s)-[:HAS_EVENT]->(e)
WITH s, e
OPTIONAL MATCH (s)-[old_latest:LATEST_EVENT]->(prev:SessionEvent)
DELETE old_latest
WITH s, e, prev
FOREACH (_ IN CASE WHEN prev IS NOT NULL THEN [1] ELSE [] END |
    CREATE (prev)-[:NEXT]->(e)
)
FOREACH (_ IN CASE WHEN prev IS NULL THEN [1] ELSE [] END |
    CREATE (s)-[:FIRST_EVENT]->(e)
)
CREATE (s)-[:LATEST_EVENT]->(e)
"""

APPEND_BRANCH = """
CREATE (e:SessionEvent $event_props)
SET e.timestamp = datetime($timestamp)
WITH e
OPTIONAL MATCH (root:SessionEvent {session_id: $session_id, agent_id: $agent_id,
                                   event_name: 'SubagentStart'})
               -[old:BRANCH_LATEST]->(tail:SessionEvent)
DELETE old
WITH e, root, tail
FOREACH (_ IN CASE WHEN tail IS NOT NULL THEN [1] ELSE [] END |
    CREATE (tail)-[:NEXT]->(e)
    CREATE (root)-[:BRANCH_LATEST]->(e)
)
FOREACH (_ IN CASE WHEN tail IS NULL THEN [1] ELSE [] END |
    CREATE (e)-[:BRANCH_LATEST]->(e)
)
"""

# The Agent call's PostToolUse names the subagent it started
# (``spawned_agent_id``), and the subagent's SubagentStart begins its
# branch. Whichever of the two is appended second writes the link, since
# either can land first. A branch gets one SPAWNED, from the first call
# that names it.
LINK_SPAWN = """
MATCH (start:SessionEvent {session_id: $session_id, agent_id: $agent_id,
                           event_name: 'SubagentStart'})-[:BRANCH_LATEST]->()
WHERE NOT EXISTS { ()-[:SPAWNED]->(start) }
MATCH (call:SessionEvent {session_id: $session_id, spawned_agent_id: $agent_id})
WITH start, call ORDER BY call.timestamp LIMIT 1
MERGE (call)-[:SPAWNED]->(start)
"""

# Written when the event that delivers a report is appended: the Agent
# call's PostToolUse for a foreground subagent, the prompt the harness
# submits for a background one. The link starts at that agent's latest
# SubagentStop; a report that arrives before its stop gets none.
LINK_RETURN = """
MATCH (delivery:SessionEvent {event_id: $event_id})
UNWIND $agent_ids AS agent_id
MATCH (stop:SessionEvent {session_id: $session_id, agent_id: agent_id,
                          event_name: 'SubagentStop'})
WITH delivery, agent_id, stop ORDER BY stop.timestamp DESC
WITH delivery, agent_id, head(collect(stop)) AS stop
MERGE (stop)-[:RETURNED_TO]->(delivery)
"""


def _append_event(tx, event_props: dict, project: str, model: str | None) -> None:
    session_id = event_props["session_id"]
    agent_id = event_props.get("agent_id")
    state = tx.run(
        APPEND_PREFIX,
        session_id=session_id,
        user_id=event_props.get("user_id"),
        project_id=project,
        harness=harness(),
        model=model,
        job_id=job_id(),
        timestamp=event_props.get("timestamp"),
        event_id=event_props.get("event_id"),
        agent_id=agent_id,
    ).single()
    if state is None or state["duplicate"]:
        return
    starts = event_props.get("event_name") == "SubagentStart"
    in_branch = bool(agent_id) and (state["branch"] or starts)
    tx.run(
        APPEND_BRANCH if in_branch else APPEND_MAIN,
        session_id=session_id,
        agent_id=agent_id,
        timestamp=event_props.get("timestamp"),
        event_props=event_props,
    ).consume()
    spawned = event_props.get("spawned_agent_id")
    if spawned or (starts and in_branch and not state["branch"]):
        tx.run(LINK_SPAWN, session_id=session_id, agent_id=spawned or agent_id).consume()
    if spawned and event_props.get("spawn_status") == "completed":
        returned = [spawned]
    else:
        returned = event_props.get("delivered_agent_ids") or []
    if returned:
        tx.run(
            LINK_RETURN,
            session_id=session_id,
            event_id=event_props.get("event_id"),
            agent_ids=list(returned),
        ).consume()


def event_record(session_id: str, event_name: str, props: dict) -> tuple[str, dict]:
    """The event id and the properties ``append_event`` stores for ``props``.

    The id is a hash of the event's own content (timestamp excluded), so
    every hook that appends the same payload computes the same id. The
    content includes the harness process that fired it (``harness_pid``),
    shared by every hook of that process: a restarted session repeats its
    resume ``SessionStart``, and the ``SessionEnd`` before it, word for
    word, and each restart is still its own event.
    """
    session_id = session_id or "unknown"
    pid = props.get("harness_pid") or harness_pid()
    if pid:
        props = {**props, "harness_pid": pid}
    payload_sig = sha1(
        json.dumps(props, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    event_id = f"{session_id}:{event_name}:{payload_sig}"

    event_props = {k: v for k, v in props.items() if v is not None}
    event_props.update(
        {
            "event_id": event_id,
            "event_name": event_name,
            "session_id": session_id,
            "user_id": user_id(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    return event_id, event_props


def append_event(session, session_id: str, event_name: str, props: dict) -> str:
    """``append_session_event`` on a Neo4j session the caller already holds."""
    event_id, event_props = event_record(session_id, event_name, props)
    # Resolved outside the transaction function, which the driver may
    # retry: the project asks git, and the model reads the transcript.
    project = project_id(event_props.get("cwd"))
    model = (
        event_props.get("model")
        or _transcript_model(event_props.get("transcript_path"))
        or None
    )
    _ensure_session_schema(session)
    session.execute_write(_append_event, event_props, project, model)
    return event_id


def graph_driver(connection_timeout: float = 2.0, max_retry_time: float = 5.0):
    """A driver for the configured graph, with the hooks' short timeouts.

    Short on purpose: hooks run inside a session, so an unreachable graph
    must cost a moment and one dropped record, never a stalled session.
    The small retry window still absorbs transient lock conflicts when
    parallel hooks write to the same session.
    """
    import logging

    from neo4j import GraphDatabase

    # The memory queries name labels and relationship types that a young
    # graph does not have yet (no summary has been written, nothing has
    # been recalled). The server's notices about that are expected, and
    # would otherwise land on the hook's stderr and in the worker's log.
    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
    uri, user, password, _ = neo4j_config()
    driver = GraphDatabase.driver(
        uri,
        auth=(user, password),
        connection_timeout=connection_timeout,
        max_transaction_retry_time=max_retry_time,
    )
    # An unreachable graph fails here, in milliseconds when nothing listens.
    # Left to the first write, it was retried for the whole retry window,
    # seven to eight seconds on every hooked event.
    try:
        driver.verify_connectivity()
    except Exception:
        driver.close()
        raise
    return driver


def append_session_event(session_id: str, event_name: str, props: dict) -> str:
    """Append one :SessionEvent to the per-session chain in Neo4j.

    Only harness lifecycle events become chain entries. The injection
    hook appends the same SessionStart event the capture hook does (the
    shared content hash collapses the two writes into one node) and then
    records what it injected on that node via ``set_event_props``. The
    graph shape mirrors the meta-knowledge-graph sister project::

        (:Session)-[:FIRST_EVENT]->(:SessionEvent)-[:NEXT]->(:SessionEvent)...
        (:Session)-[:HAS_EVENT]->(every main-agent :SessionEvent)
        (:Session)-[:LATEST_EVENT]->(the newest main-agent :SessionEvent)
        (:User)-[:HAS_SESSION]->(:Session)<-[:HAS_SESSION]-(:Project)

    A subagent's events form a branch of their own, which has no
    relationship to the Session::

        (Agent call's PostToolUse)-[:SPAWNED]->(:SessionEvent {SubagentStart})
        (SubagentStart)-[:NEXT]->(subagent event)-[:NEXT]->...
        (SubagentStart)-[:BRANCH_LATEST]->(the branch's newest event)
        (SubagentStop)-[:RETURNED_TO]->(the event that delivered its report)

    The event id is a hash of the event's own content (timestamp excluded),
    so the same payload delivered to two parallel hook configs collapses to
    one node; the uniqueness constraint makes the dedupe atomic. Returns
    the event id.
    """
    database = neo4j_config()[3]
    with graph_driver() as driver:
        with driver.session(database=database) as session:
            return append_event(session, session_id, event_name, props)


def set_event_props(event_id: str, props: dict) -> None:
    """Set additional properties on an already-appended :SessionEvent.

    The injection hook uses this to record what it injected on the
    SessionStart event itself: the injection is not a lifecycle event,
    the harness never announces one for it, so it must not appear in the
    chain under a name of its own. It is something that happened at
    session start, and the record keeps it there.
    """
    from neo4j import GraphDatabase

    props = {k: v for k, v in props.items() if v is not None}
    if not props:
        return
    uri, user, password, database = neo4j_config()
    with GraphDatabase.driver(
        uri,
        auth=(user, password),
        connection_timeout=2.0,
        max_transaction_retry_time=5.0,
    ) as driver:
        with driver.session(database=database) as session:

            def _set_props(tx):
                tx.run(
                    "MATCH (e:SessionEvent {event_id: $event_id}) "
                    "SET e += $props",
                    event_id=event_id,
                    props=props,
                ).consume()

            session.execute_write(_set_props)


def link_event_to_prompt(event_id: str, prompt_name: str) -> None:
    """Link a SessionStart event to the SystemPrompt node it injected.

    A prompt node is never changed in place, so the link alone says what
    this session was told, and the event carries no copy of the text.
    """
    from neo4j import GraphDatabase

    uri, user, password, database = neo4j_config()
    with GraphDatabase.driver(
        uri,
        auth=(user, password),
        connection_timeout=2.0,
        max_transaction_retry_time=5.0,
    ) as driver:
        with driver.session(database=database) as session:

            def _link(tx):
                tx.run(
                    "MATCH (e:SessionEvent {event_id: $event_id}) "
                    "MATCH (p:SystemPrompt {name: $name}) "
                    "MERGE (e)-[:INJECTED_PROMPT]->(p)",
                    event_id=event_id,
                    name=prompt_name,
                ).consume()

            session.execute_write(_link)


# The only keys ever copied out of the env file. Whatever else the file
# contains stays in the file; a stray or typo'd key can never reach the
# process environment.
ENV_KEYS = (
    "NEO4J_URI",
    "NEO4J_USERNAME",
    "NEO4J_PASSWORD",
    "NEO4J_DATABASE",
    "UAM_AGENT_NAME",
    "UAM_USER_ID",
    "UAM_PROJECT_ID",
    "UAM_LLM_BACKEND",
    "UAM_LLM_MODEL",
    "UAM_CLAUDE_CLI_MODEL",
    "UAM_EMBEDDING_MODEL",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
)

ENV_TEMPLATE = """\
# Unified Agent Memory configuration
# This is the only file the plugin reads configuration from. Exported
# environment variables win over values set here.

# NEO4J_URI=bolt://localhost:7687
# NEO4J_USERNAME=neo4j
# NEO4J_PASSWORD=password
# NEO4J_DATABASE=neo4j
# UAM_AGENT_NAME=default

# LLM for the plugin's background agents (memory extraction,
# consolidation, and similar hook-driven jobs); the model answering
# the interactive session is unaffected. The default backend,
# claude-cli, runs headless Claude Code (Haiku by default) on the
# Claude subscription this machine is already logged in with, so
# there is nothing to set up.
# UAM_LLM_BACKEND=claude-cli
# UAM_CLAUDE_CLI_MODEL=haiku

# Set the backend to litellm to use any other provider instead: the
# model is a LiteLLM model string, authenticated by the matching key.
# UAM_LLM_BACKEND=litellm
# UAM_LLM_MODEL=gpt-5.4-mini
# OPENAI_API_KEY=
# ANTHROPIC_API_KEY=
# GEMINI_API_KEY=

# Episodic memory. The user key defaults to the email of the account
# logged in to the harness, then git's user.email; UAM_USER_ID pins it,
# so one person's machines and addresses share one id. The project key
# defaults to the repository's directory name; UAM_PROJECT_ID pins it,
# but set here it pins every repository on this machine, so prefer
# exporting it per repository. An embedding model (a LiteLLM model
# string, paid for by its provider key) adds similarity search to the
# memory tools and recall; without one they search stored text only.
# UAM_USER_ID=
# UAM_PROJECT_ID=
# UAM_EMBEDDING_MODEL=openai/text-embedding-3-small
"""


def env_file_path() -> Path:
    """The canonical env file: ``<data_dir>/.env``.

    UAM_ENV_FILE overrides it so tests can point at a temp file without
    touching the real one.
    """
    override = os.getenv("UAM_ENV_FILE")
    if override:
        return Path(override).expanduser()
    return data_dir() / ".env"


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        # UTF-8 on every platform, so a value means the same everywhere;
        # "-sig" drops the byte-order mark some Windows editors write, which
        # would otherwise become part of the first key.
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _seed_env_file(path: Path) -> None:
    """Create the env file as a commented template on first run.

    The file is meant to hold credentials, so it gets 0600 and its data
    directory 0700. Seeding is best-effort: on any failure the hooks
    simply run on defaults.
    """
    try:
        directory = path.parent
        directory.mkdir(parents=True, exist_ok=True)
        if directory == data_dir():
            os.chmod(directory, 0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(ENV_TEMPLATE)
    except OSError:
        pass


def load_env() -> None:
    """Load configuration from the user-level env file.

    Follows claude-mem's model: one canonical file under the data dir
    (``~/.unified-agent-memory/.env``), auto-created as a template on
    first run. Project and plugin-checkout ``.env`` files are deliberately
    never read, so a repo you happen to open cannot hand credentials to
    the hooks. Only the ENV_KEYS whitelist is copied out, and explicitly
    exported environment variables win over file values.
    """
    path = env_file_path()
    if not path.exists():
        _seed_env_file(path)
        return
    values = _parse_env_file(path)
    for key in ENV_KEYS:
        if key in values:
            os.environ.setdefault(key, values[key])


def neo4j_config() -> tuple[str, str, str, str]:
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USERNAME", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "password")
    database = os.getenv("NEO4J_DATABASE", "neo4j")
    return uri, user, password, database
