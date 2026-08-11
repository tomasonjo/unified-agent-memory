#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["neo4j>=5.26.0"]
# ///
"""Skill script: read the capture log back, checked against the graph.

Bundled with the recap skill and executed by the agent, not by a hook.
With no arguments it prints one line per recent session: id, start time,
event count, tools used, and the first prompt. With a session id prefix
it prints that session's timeline, one event per line.

The graph adds the one thing the log alone cannot say: whether the
instructions a session started from are still current. Every session log
records the injected prompt's name, source, and version; the script asks
Neo4j for the current version of each ``(:SystemPrompt)`` node and flags
sessions that ran on instructions the graph has since replaced. No
reachable graph, no flags: the local index still prints.

The point of the script is cost: the JSONL logs are long, and an agent
that reads them raw pays for every line. The index is a few hundred
characters; the timeline is fetched only for the one session a question
is actually about.

Log location and Neo4j settings match the hooks (common.py): the env
file under the data dir, with exported variables winning. Run the script
through uv so the PEP 723 header pulls the Neo4j driver, or with plain
python3 for the local index alone.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parents[3] / "hooks"
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

from common import load_env, log_dir, neo4j_config  # noqa: E402

INDEX_SESSIONS = 10
PROMPT_PREVIEW_CHARS = 70


def read_records(path: Path) -> list[dict]:
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a torn write should not sink the whole session
    return records


def local_time(iso: str, fmt: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime(fmt)
    except ValueError:
        return iso[: len(fmt)]


def first_prompt(records: list[dict]) -> str:
    for rec in records:
        if rec.get("event") == "UserPromptSubmit":
            prompt = str(rec.get("payload", {}).get("prompt", ""))
            return " ".join(prompt.split())[:PROMPT_PREVIEW_CHARS]
    return ""


def tool_summary(records: list[dict]) -> str:
    counts: dict[str, int] = {}
    for rec in records:
        if rec.get("event") == "PostToolUse":
            name = str(rec.get("payload", {}).get("tool_name") or "?")
            counts[name] = counts.get(name, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    return ", ".join(f"{name} x{count}" for name, count in ranked)


def graph_prompt_versions() -> dict[str, int]:
    """Current version of every named prompt in the graph, or {}.

    Same discipline as the injection hook: a short connection timeout,
    and any failure (no driver, no graph, no credentials) degrades to an
    empty answer instead of an error.
    """
    try:
        from neo4j import GraphDatabase
    except ImportError:
        return {}
    # The driver retries failed queries and narrates each attempt; one
    # quick, quiet attempt is the right posture for an optional lookup.
    logging.getLogger("neo4j").setLevel(logging.ERROR)
    uri, user, password, database = neo4j_config()
    try:
        with GraphDatabase.driver(
            uri,
            auth=(user, password),
            connection_timeout=2.0,
            max_transaction_retry_time=0.0,
        ) as driver:
            records, _, _ = driver.execute_query(
                "MATCH (p:SystemPrompt) RETURN p.name AS name, p.version AS version",
                database_=database,
            )
        return {
            str(rec["name"]): int(rec["version"])
            for rec in records
            if rec.get("name") and rec.get("version") is not None
        }
    except Exception:
        return {}


def injected_prompt(records: list[dict]) -> tuple[str, str, int | None] | None:
    for rec in records:
        if rec.get("event") == "SystemPromptInjected":
            payload = rec.get("payload", {})
            version = payload.get("version")
            return (
                str(payload.get("name") or "default"),
                str(payload.get("source") or "?"),
                int(version) if version is not None else None,
            )
    return None


def prompt_note(
    injected: tuple[str, str, int | None] | None,
    current: dict[str, int],
) -> str:
    """A note only when the graph has moved past what the session got."""
    if not injected or not current:
        return ""
    name, source, version = injected
    now = current.get(name)
    if now is None:
        return ""
    if source == "neo4j" and version is not None and version >= now:
        return ""
    if source == "neo4j":
        return f"started on {name} v{version}, graph now at v{now}"
    return f"started on {name} from {source}, graph now at v{now}"


def print_index(limit: int) -> int:
    files = sorted(
        log_dir().glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not files:
        print(f"No session logs under {log_dir()}")
        return 0
    current = graph_prompt_versions()
    for path in files[:limit]:
        records = read_records(path)
        if not records:
            continue
        started = local_time(records[0].get("received_at", ""), "%Y-%m-%d %H:%M")
        print(
            f"{path.stem[:8]}  {started}  {len(records):4d} events  "
            f"{tool_summary(records) or 'no tools'}"
        )
        note = prompt_note(injected_prompt(records), current)
        if note:
            print(f"          {note}")
        prompt = first_prompt(records)
        if prompt:
            print(f"          {prompt}")
    return 0


def event_detail(rec: dict, current: dict[str, int]) -> str:
    payload = rec.get("payload", {})
    event = rec.get("event")
    if event == "UserPromptSubmit":
        prompt = str(payload.get("prompt", ""))
        return " ".join(prompt.split())[:PROMPT_PREVIEW_CHARS]
    if event == "SystemPromptInjected":
        version = payload.get("version")
        version_note = f" v{version}" if version is not None else ""
        detail = f"{payload.get('name')}{version_note} from {payload.get('source')}"
        now = current.get(str(payload.get("name")))
        if now is not None and (version is None or int(version) < now):
            detail += f" (graph now at v{now})"
        return detail
    return str(payload.get("tool_name") or "")


def print_timeline(prefix: str) -> int:
    matches = [p for p in log_dir().glob("*.jsonl") if p.stem.startswith(prefix)]
    if len(matches) != 1:
        print(f"{len(matches)} sessions match '{prefix}'; use a longer prefix")
        return 1
    current = graph_prompt_versions()
    for rec in read_records(matches[0]):
        when = local_time(rec.get("received_at", ""), "%H:%M:%S")
        print(f"{when}  {rec.get('event', '?'):22} {event_detail(rec, current)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "session",
        nargs="?",
        help="session id prefix; prints that session's timeline",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=INDEX_SESSIONS,
        help=f"sessions to index (default {INDEX_SESSIONS})",
    )
    args = parser.parse_args()
    load_env()
    if args.session:
        return print_timeline(args.session)
    return print_index(args.limit)


if __name__ == "__main__":
    sys.exit(main())
