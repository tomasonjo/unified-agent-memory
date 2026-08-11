"""Shared helpers for Unified Agent Memory hooks.

Stdlib only, so every hook can import it whether it runs under plain
python3 or as a PEP 723 uv script.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


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


def log_dir() -> Path:
    override = os.getenv("UAM_LOG_DIR")
    if override:
        return Path(override).expanduser()
    return data_dir() / "logs"


def append_session_record(session_id: str, event: str, payload: dict) -> Path:
    """Append one record to the per-session JSONL log.

    Used by the capture hook for lifecycle events and by the injection
    hook to record what it injected, so the session log holds both what
    the session did and what it was given.
    """
    record = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "session_id": session_id or "unknown",
        "user_id": user_id(),
        "payload": payload,
    }
    directory = log_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{record['session_id']}.jsonl"
    with path.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")
    return path


# The only keys ever copied out of the env file. Whatever else the file
# contains stays in the file; a stray or typo'd key can never reach the
# process environment.
ENV_KEYS = (
    "NEO4J_URI",
    "NEO4J_USERNAME",
    "NEO4J_PASSWORD",
    "NEO4J_DATABASE",
    "UAM_SYSTEM_PROMPT_NAME",
    "UAM_LOG_DIR",
)

ENV_TEMPLATE = """\
# Unified Agent Memory configuration
# This is the only file the plugin reads configuration from. Exported
# environment variables win over values set here.

# NEO4J_URI=bolt://localhost:7687
# NEO4J_USERNAME=neo4j
# NEO4J_PASSWORD=password
# NEO4J_DATABASE=neo4j
# UAM_SYSTEM_PROMPT_NAME=default
# UAM_LOG_DIR=~/.unified-agent-memory/logs
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
