"""Shared helpers for Unified Agent Memory hooks.

Importable with the standard library alone; the Neo4j driver is imported
lazily inside the functions that talk to the graph, so the module loads
whether a hook runs under plain python3 or as a PEP 723 uv script.
"""

from __future__ import annotations

import json
import os
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
            data = json.loads(candidate.read_text())
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
            text=True,
            timeout=2,
        )
        return out.stdout.strip().lower()
    except Exception:
        return ""


def user_id() -> str:
    """Resolve the user identity every record is stamped with.

    An email address, because it is the one identity that stays the same
    across harnesses and machines. Resolution order: the account logged
    in to the harness, then the email in the machine's git configuration.
    """
    return _claude_account_email() or _git_email() or "unknown"


def harness() -> str:
    """Name of the harness this capture is wired into.

    No lookup needed: hook registration is harness-specific by
    construction, so the script knows where it runs the way it knows its
    own filename. The env override is for ports — wire the same script
    into another harness and set UAM_HARNESS in that hook configuration.
    """
    return os.getenv("UAM_HARNESS", "claude-code")


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
SESSION_SCHEMA = (
    (
        "uam_session_id",
        "CREATE CONSTRAINT uam_session_id IF NOT EXISTS "
        "FOR (s:Session) REQUIRE s.session_id IS UNIQUE",
    ),
    (
        "uam_session_event_id",
        "CREATE CONSTRAINT uam_session_event_id IF NOT EXISTS "
        "FOR (e:SessionEvent) REQUIRE e.event_id IS UNIQUE",
    ),
)


def _ensure_session_schema(session) -> None:
    """Create the uniqueness constraints, tolerating pre-existing schema.

    Each statement gets its own transaction. Schema commands do not
    compose into one atomic unit the way writes do, so folding both into
    a single ``execute_write`` means a failure on the second rolls back
    the first and the graph ends up with neither constraint.

    Failures are reported and swallowed rather than raised. ``IF NOT
    EXISTS`` covers an existing constraint, but not a leftover backing
    index whose owning constraint was dropped: the index keeps the
    derived name, and the next create collides with it under a name
    nothing here asked for. The constraints are a guard on the dedupe in
    ``_append_event``, not the payload — that query already filters
    duplicates on its own, so a missing constraint costs atomicity under
    concurrent writes, never the event itself.
    """
    for name, statement in SESSION_SCHEMA:
        try:
            session.run(statement).consume()
        except Exception as exc:
            print(f"[uam] schema: {name} not created: {exc}", file=sys.stderr)


def _append_event(tx, session_id: str, event_props: dict) -> None:
    tx.run(
        """
        MERGE (s:Session {session_id: $session_id})
        ON CREATE SET s.created_at = datetime($timestamp)
        SET s.user_id = $user_id, s.harness = $harness,
            s.model = coalesce($model, s.model)
        WITH s
        OPTIONAL MATCH (dup:SessionEvent {event_id: $event_id})
        WITH s, dup
        WHERE dup IS NULL
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
        """,
        session_id=session_id,
        user_id=event_props.get("user_id"),
        harness=harness(),
        model=event_props.get("model")
        or _transcript_model(event_props.get("transcript_path"))
        or None,
        timestamp=event_props.get("timestamp"),
        event_id=event_props.get("event_id"),
        event_props=event_props,
    )


def append_session_event(session_id: str, event_name: str, props: dict) -> str:
    """Append one :SessionEvent to the per-session chain in Neo4j.

    Only harness lifecycle events become chain entries. The injection
    hook appends the same SessionStart event the capture hook does (the
    shared content hash collapses the two writes into one node) and then
    records what it injected on that node via ``set_event_props``. The
    graph shape mirrors the meta-knowledge-graph sister project::

        (:Session)-[:FIRST_EVENT]->(:SessionEvent)-[:NEXT]->(:SessionEvent)...
        (:Session)-[:HAS_EVENT]->(every :SessionEvent)
        (:Session)-[:LATEST_EVENT]->(the newest :SessionEvent)

    The event id is a hash of the event's own content (timestamp excluded),
    so the same payload delivered to two parallel hook configs collapses to
    one node; the uniqueness constraint makes the dedupe atomic. Returns
    the event id.
    """
    from neo4j import GraphDatabase

    session_id = session_id or "unknown"
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

    # Short timeouts on purpose: this runs on every lifecycle event, so an
    # unreachable graph must cost a moment and one dropped event, never a
    # stalled session. The small retry window still absorbs transient
    # lock conflicts when parallel hooks write to the same session.
    uri, user, password, database = neo4j_config()
    with GraphDatabase.driver(
        uri,
        auth=(user, password),
        connection_timeout=2.0,
        max_transaction_retry_time=5.0,
    ) as driver:
        with driver.session(database=database) as session:
            _ensure_session_schema(session)
            session.execute_write(_append_event, session_id, event_props)
    return event_id


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


# The only keys ever copied out of the env file. Whatever else the file
# contains stays in the file; a stray or typo'd key can never reach the
# process environment.
ENV_KEYS = (
    "NEO4J_URI",
    "NEO4J_USERNAME",
    "NEO4J_PASSWORD",
    "NEO4J_DATABASE",
    "UAM_AGENT_NAME",
    "UAM_LLM_BACKEND",
    "UAM_LLM_MODEL",
    "UAM_CLAUDE_CLI_MODEL",
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
        lines = path.read_text().splitlines()
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
        with os.fdopen(fd, "w") as f:
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
