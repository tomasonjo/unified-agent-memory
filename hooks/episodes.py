"""Episodic memory reads, shared by the memory MCP server and the recall hooks.

Two kinds of record are read here, both written by extraction: an
``:Observation`` holds one development (a finding, a fix, a decision), and
a ``:SessionSummary`` holds the current handoff of one session. Both come
back as the same one-line row, so a recap, a search result, and the
neighbors of an expanded record all read alike::

    #o112 · discovery · yesterday · Renewal drop traced to March pipeline change
    #s41 · session · yesterday · maria@company.com · Renewal drop explained; ...

A summary is named by its session: ``#s41`` opens that session's current
handoff. Ages come from ``source_end``, the latest captured event a record
covers, never from when extraction wrote it.

Every query goes through a ``run(cypher, **params) -> list[dict]``
callable, so the caller owns sessions and access mode, and tests can run
the helpers inside a transaction they roll back. The module imports with
the standard library alone.

The recall hooks use the same rows. A delivery (a recap, prompt-time
episodes, or what ``search_episodic`` and ``expand_episodic`` returned)
is recorded on the event that carried it: the exact block, plus
``(memory)-[:INJECTED_AT]->(event)`` per delivered memory and
``(memory)-[:INJECTED_IN]->(session)``, whose properties say what the
session's current context has already seen, so the same thing is not
sent twice.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone

KINDS = ("observation", "session", "both")

MAX_SEARCH_LIMIT = 50
# Each leg hands rank fusion this many candidates per wanted row. The
# vector index filters by project as it searches, but the since and kind
# filters apply to its nearest nodes afterwards, so a vector leg
# over-fetches by this factor; the fulltext leg filters first and keeps
# its best this many.
CANDIDATES_PER_ROW = 5
RRF_K = 60

ROW_CHARS = 200
FIELD_CHARS = 1500
EXCERPT_CHARS = 300
SEARCH_OUTPUT_CHARS = 6000
EXPAND_OUTPUT_CHARS = 8000
OBSERVATION_PAGE = 20
EVENT_PAGE = 20

FULLTEXT_INDEX = "episode_text"
VECTOR_INDEX = "episode_embedding"
# One index per label, from before vector indexes could span labels.
LEGACY_VECTOR_INDEXES = ["observation_embedding", "summary_embedding"]


def reader(session):
    """A ``run`` callable over ``session``: one managed read transaction per query."""

    def run(cypher: str, **params) -> list[dict]:
        return session.execute_read(
            lambda tx: [record.data() for record in tx.run(cypher, **params)]
        )

    return run


# --- schema -------------------------------------------------------------------

FULLTEXT_INDEX_QUERY = """
CREATE FULLTEXT INDEX episode_text IF NOT EXISTS
FOR (n:Observation|SessionSummary)
ON EACH [n.title, n.narrative,
         n.headline, n.request, n.progress, n.outcome]
"""

# The fields FULLTEXT_INDEX_QUERY indexes. An index built from an older
# field list is dropped and rebuilt, since IF NOT EXISTS leaves it alone.
FULLTEXT_FIELDS = [
    "title", "narrative", "headline", "request", "progress", "outcome",
]

# project_id is a filter property, so a search can filter by project
# inside the index instead of after it (Neo4j 2026.01 or later).
VECTOR_INDEX_QUERY = """
CREATE VECTOR INDEX episode_embedding IF NOT EXISTS
FOR (n:Observation|SessionSummary) ON n.embedding
WITH [n.project_id]
OPTIONS {{indexConfig: {{
  `vector.dimensions`: {dimensions},
  `vector.similarity_function`: 'cosine'}}}}
"""


# Written by extraction. Named like the capture constraints, and checked
# the same way: one statement per transaction, failures reported. The
# counter nodes hand out the short display ids (o112, s41).
EPISODE_SCHEMA = (
    (
        "uam_observation_id",
        "CREATE CONSTRAINT uam_observation_id IF NOT EXISTS "
        "FOR (o:Observation) REQUIRE o.id IS UNIQUE",
    ),
    (
        "uam_observation_display_id",
        "CREATE CONSTRAINT uam_observation_display_id IF NOT EXISTS "
        "FOR (o:Observation) REQUIRE o.display_id IS UNIQUE",
    ),
    (
        "uam_session_display_id",
        "CREATE CONSTRAINT uam_session_display_id IF NOT EXISTS "
        "FOR (s:Session) REQUIRE s.display_id IS UNIQUE",
    ),
    (
        "uam_summary_id",
        "CREATE CONSTRAINT uam_summary_id IF NOT EXISTS "
        "FOR (s:SessionSummary) REQUIRE s.id IS UNIQUE",
    ),
    (
        "uam_extraction_run_id",
        "CREATE CONSTRAINT uam_extraction_run_id IF NOT EXISTS "
        "FOR (r:ExtractionRun) REQUIRE r.id IS UNIQUE",
    ),
    (
        "uam_display_id_counter",
        "CREATE CONSTRAINT uam_display_id_counter IF NOT EXISTS "
        "FOR (c:DisplayIdCounter) REQUIRE c.prefix IS UNIQUE",
    ),
    (
        "uam_observation_recency",
        "CREATE INDEX uam_observation_recency IF NOT EXISTS "
        "FOR (o:Observation) ON (o.project_id, o.source_end)",
    ),
    (
        "uam_summary_recency",
        "CREATE INDEX uam_summary_recency IF NOT EXISTS "
        "FOR (s:SessionSummary) ON (s.project_id, s.source_end)",
    ),
)


def ensure_episode_schema(session) -> None:
    """Create the episode constraints and recency indexes that are missing."""
    for name, statement in EPISODE_SCHEMA:
        try:
            session.run(statement).consume()
        except Exception as exc:
            print(f"[uam] schema: {name} not created: {exc}", file=sys.stderr)


def ensure_retrieval_indexes(session, dimensions: int | None = None) -> None:
    """Create the retrieval indexes that are missing, then wait for them.

    Both indexes span both labels, so one call searches observations and
    summaries together. The vector index exists only when an embedding
    model is configured; ``dimensions`` must match that model. Each
    statement runs on its own, because schema commands do not compose
    into one transaction. Failures are reported rather than raised: search
    skips a leg whose index is missing. ``IF NOT EXISTS`` never updates an
    existing definition, so a fulltext index over other fields is dropped
    first and rebuilt, and the older per-label vector indexes are dropped.
    """
    _drop_stale_fulltext(session)
    statements = [(FULLTEXT_INDEX, FULLTEXT_INDEX_QUERY)]
    if dimensions:
        _drop_legacy_vector(session)
        statements.append(
            (VECTOR_INDEX, VECTOR_INDEX_QUERY.format(dimensions=int(dimensions)))
        )
    for name, statement in statements:
        try:
            session.run(statement).consume()
            session.run("CALL db.awaitIndex($name, 30)", name=name).consume()
        except Exception as exc:
            print(f"[uam] index {name} unavailable: {exc}", file=sys.stderr)


def _drop_stale_fulltext(session) -> None:
    try:
        record = session.run(
            "SHOW FULLTEXT INDEXES YIELD name, properties "
            "WHERE name = $name RETURN properties",
            name=FULLTEXT_INDEX,
        ).single()
        if record and sorted(record["properties"]) != sorted(FULLTEXT_FIELDS):
            session.run(f"DROP INDEX {FULLTEXT_INDEX} IF EXISTS").consume()
    except Exception as exc:
        print(f"[uam] index {FULLTEXT_INDEX} not checked: {exc}", file=sys.stderr)


def _drop_legacy_vector(session) -> None:
    try:
        names = session.run(
            "SHOW VECTOR INDEXES YIELD name WHERE name IN $names RETURN name",
            names=LEGACY_VECTOR_INDEXES,
        ).value()
        for name in names:
            session.run(f"DROP INDEX {name} IF EXISTS").consume()
    except Exception as exc:
        print(f"[uam] legacy vector indexes not checked: {exc}", file=sys.stderr)


# --- formatting ---------------------------------------------------------------


def clip(text, limit: int) -> str:
    """``text`` flattened to one line of at most ``limit`` characters."""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def bound(text: str, limit: int) -> str:
    """Cut a tool response at a line boundary once it passes ``limit``."""
    if len(text) <= limit:
        return text
    head = text[:limit].rsplit("\n", 1)[0]
    return f"{head}\n[… truncated at {limit} characters]"


def _native(value) -> datetime | None:
    """A timezone-aware datetime from a Neo4j temporal value or a datetime."""
    if value is None:
        return None
    when = value.to_native() if hasattr(value, "to_native") else value
    if not isinstance(when, datetime):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def age(value, now: datetime | None = None) -> str:
    """How long ago ``value`` was, in the recap's words."""
    when = _native(value)
    if when is None:
        return "undated"
    now = now or datetime.now(timezone.utc)
    seconds = max(0, int((now - when).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    days = seconds // 86400
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days} days ago"
    return when.date().isoformat()


_SPAN = re.compile(r"(\d+)\s*([hdw])", re.IGNORECASE)
_SPAN_UNITS = {"h": "hours", "d": "days", "w": "weeks"}


def parse_since(value: str | None, now: datetime | None = None) -> str | None:
    """``since`` as an ISO timestamp.

    Accepts an ISO date or datetime, or a span back from now such as 12h,
    7d, or 2w. Raises ValueError for anything else, so the caller can say
    so instead of silently searching everything.
    """
    text = (value or "").strip()
    if not text:
        return None
    span = _SPAN.fullmatch(text)
    if span:
        delta = timedelta(**{_SPAN_UNITS[span.group(2).lower()]: int(span.group(1))})
        return ((now or datetime.now(timezone.utc)) - delta).isoformat()
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            f"since must be an ISO date or a span such as 7d, not {value!r}"
        ) from None
    return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).isoformat()


_LUCENE_SPECIAL = re.compile(r'([+\-!(){}\[\]^"~*?:\\/&|])')


def lucene_query(text: str | None) -> str:
    """Free text as a fulltext query in which every term is optional.

    Lowercased so AND, OR, and NOT stay words rather than operators, and
    Lucene's special characters are escaped so a pasted path or a question
    mark cannot break the query. Text without a single word character is
    no query at all.
    """
    if not text or not re.search(r"\w", text):
        return ""
    return _LUCENE_SPECIAL.sub(r"\\\1", " ".join(text.lower().split()))


def _ref(display_id, stored) -> str:
    """How a record is named to the agent: #o112 or #s41, else its stored id."""
    return f"#{display_id}" if display_id else str(stored or "?")


def render_row(row: dict, now: datetime | None = None) -> str:
    """One row: ``id · type · age · [owner ·] one-liner``."""
    parts = [
        _ref(row.get("display_id"), row.get("ref")),
        row.get("type") or "?",
        age(row.get("source_end"), now),
    ]
    if row.get("kind") == "session" and row.get("user"):
        parts.append(row["user"])
    parts.append(row.get("text") or "(untitled)")
    return clip(" · ".join(parts), ROW_CHARS)


def render_rows(rows: list[dict], now: datetime | None = None) -> str:
    return "\n".join(render_row(row, now) for row in rows)


def _observation_row(o: dict) -> dict:
    return {
        "kind": "observation",
        "display_id": o.get("display_id"),
        "ref": o.get("id"),
        "type": o.get("type"),
        "source_end": o.get("source_end"),
        "text": o.get("title"),
    }


def _session_row(s: dict, summary: dict | None) -> dict:
    summary = summary or {}
    return {
        "kind": "session",
        "display_id": s.get("display_id"),
        "ref": s.get("session_id"),
        "type": "session",
        "user": s.get("user_id"),
        "source_end": summary.get("source_end") or s.get("created_at"),
        "text": summary.get("headline") or "(no summary yet)",
    }


# --- search -------------------------------------------------------------------

# The fields a row needs, projected from ``node`` (an Observation or a
# SessionSummary) and ``s`` (the summary's session; null for an
# observation).
ROW_FIELDS = """
       CASE WHEN node:Observation THEN 'observation' ELSE 'session' END AS kind,
       CASE WHEN node:Observation THEN node.display_id ELSE s.display_id END AS display_id,
       CASE WHEN node:Observation THEN node.id ELSE s.session_id END AS ref,
       CASE WHEN node:Observation THEN node.type ELSE 'session' END AS type,
       CASE WHEN node:Observation THEN node.title ELSE node.headline END AS text,
       s.user_id AS user,
       node.source_end AS source_end,
       node.id AS key"""

# A project filter organizes retrieval; it is not an authorization check.
FILTERS = """($project IS NULL OR node.project_id = $project)
  AND ($since IS NULL OR node.source_end >= datetime($since))
  AND ($kind = 'both'
       OR ($kind = 'observation' AND node:Observation)
       OR ($kind = 'session' AND node:SessionSummary))"""

_RECENT = """
MATCH (node:{label})
WHERE {filters}
WITH node ORDER BY node.source_end DESC LIMIT $limit
OPTIONAL MATCH (s:Session)-[:HAS_SUMMARY]->(node)
RETURN {fields}
"""
RECENT_OBSERVATIONS = _RECENT.format(
    label="Observation", filters=FILTERS, fields=ROW_FIELDS
)
RECENT_SUMMARIES = _RECENT.format(
    label="SessionSummary", filters=FILTERS, fields=ROW_FIELDS
)

# A SEARCH filter accepts only AND-joined predicates on the index's filter
# properties, so a search without a project leaves the filter out rather
# than testing $project for null. SEARCH is Cypher 25, so a query that
# uses it starts with the CYPHER 25 prefix.
_VECTOR_SEARCH = """MATCH (node:Observation|SessionSummary)
  SEARCH node IN (
    VECTOR INDEX episode_embedding
    FOR $vector
    {where}LIMIT $candidates
  ) SCORE AS score
"""
IN_PROJECT = "WHERE node.project_id = $project\n    "


def vector_search(in_project: bool) -> str:
    """The nearest episodes to ``$vector``, within ``$project`` if ``in_project``."""
    return _VECTOR_SEARCH.format(where=IN_PROJECT if in_project else "")


# Each leg of the fused search returns its nodes as one list, best first.
FULLTEXT_BRANCH = f"""
CALL db.index.fulltext.queryNodes($index, $text)
YIELD node, score
WHERE {FILTERS}
WITH node ORDER BY score DESC LIMIT $candidates
RETURN collect(node) AS ranked"""


def vector_branch(in_project: bool) -> str:
    return "\n" + vector_search(in_project) + f"""WHERE {FILTERS}
WITH node ORDER BY score DESC
RETURN collect(node) AS ranked"""


# Reciprocal rank fusion in the query: each position in a leg's list is
# worth 1 / (k + rank), and a node's shares from all legs are summed.
_FUSED_SEARCH = """CYPHER 25
CALL () {{{branches}
}}
UNWIND range(1, size(ranked)) AS rank
WITH ranked[rank - 1] AS node, 1.0 / ($rrf_k + rank) AS share
WITH node, sum(share) AS score
ORDER BY score DESC LIMIT $limit
OPTIONAL MATCH (s:Session)-[:HAS_SUMMARY]->(node)
RETURN {fields}, score
ORDER BY score DESC
"""


def fused_search(branches: list[str]) -> str:
    """One query that runs ``branches`` and fuses their lists by rank."""
    return _FUSED_SEARCH.format(branches="\nUNION ALL".join(branches), fields=ROW_FIELDS)


def _when(row: dict) -> datetime:
    return _native(row.get("source_end")) or datetime.min.replace(tzinfo=timezone.utc)


def recent(run, project, kind="both", since=None, limit=20) -> list[dict]:
    """The newest rows by ``source_end``: the timeline browse."""
    params = {"project": project, "kind": kind, "since": since, "limit": limit}
    rows: list[dict] = []
    if kind in ("observation", "both"):
        rows += run(RECENT_OBSERVATIONS, **params)
    if kind in ("session", "both"):
        rows += run(RECENT_SUMMARIES, **params)
    rows.sort(key=_when, reverse=True)
    return rows[:limit]


def fuse(legs: list[list[dict]], k: int = RRF_K) -> list[dict]:
    """Reciprocal rank fusion: a row gains ``1 / (k + rank)`` from each list.

    Ranks start at 1. Positions rather than raw scores, so a fulltext
    score and a cosine similarity never need to be comparable; ties keep
    the order in which rows were first seen.
    """
    scores: dict[str, float] = {}
    rows: dict[str, dict] = {}
    for leg in legs:
        for rank, row in enumerate(leg, start=1):
            key = row["key"]
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            rows.setdefault(key, row)
    return sorted(rows.values(), key=lambda row: -scores[row["key"]])


def _fused(run, branches: list[str], **params) -> list[dict]:
    """The fused search, or the first leg that runs alone if it fails.

    A missing index, or a server without SEARCH, then costs its own leg
    rather than the search.
    """
    attempts = [branches] + ([[branch] for branch in branches] if len(branches) > 1 else [])
    for attempt in attempts:
        try:
            return run(fused_search(attempt), **params)
        except Exception as exc:
            print(f"[uam] search legs skipped: {exc}", file=sys.stderr)
    return []


def _leg(run, cypher: str, **params) -> list[dict]:
    try:
        return run(cypher, **params)
    except Exception as exc:  # a missing index costs its own leg, not the search
        print(f"[uam] search leg {params.get('index')} skipped: {exc}", file=sys.stderr)
        return []


def hybrid_search(
    run, query, vector, project, kind="both", since=None, limit=20
) -> list[dict]:
    """Rows matching ``query`` (and ``vector``), best first.

    Without a query this is the recency listing. With one, the fulltext
    leg searches both kinds in a single call, the vector index adds a leg
    when a query vector is given, and reciprocal rank fusion merges them
    in the same query.
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}, not {kind!r}")
    limit = max(1, min(int(limit), MAX_SEARCH_LIMIT))
    text = lucene_query(query)
    if not text and vector is None:
        return recent(run, project, kind, since, limit)
    branches = []
    if text:
        branches.append(FULLTEXT_BRANCH)
    if vector is not None:
        branches.append(vector_branch(project is not None))
    return _fused(
        run, branches, index=FULLTEXT_INDEX, text=text, vector=vector,
        project=project, kind=kind, since=since, limit=limit,
        candidates=limit * CANDIDATES_PER_ROW, rrf_k=RRF_K,
    )


# --- expand -------------------------------------------------------------------


def resolve(run, ref: str) -> tuple[str, str] | None:
    """``(kind, key)`` for a display id (#o112, #s41) or a stored id.

    ``kind`` is 'observation' with the Observation id as key, or 'session'
    with the session id: a session and its summary both open the session's
    current handoff.
    """
    token = (ref or "").strip().lstrip("#")
    if re.fullmatch(r"o\d+", token):
        rows = run(
            "MATCH (o:Observation {display_id: $key}) RETURN o.id AS key LIMIT 1",
            key=token,
        )
        return ("observation", rows[0]["key"]) if rows else None
    if re.fullmatch(r"s\d+", token):
        rows = run(
            "MATCH (s:Session {display_id: $key}) RETURN s.session_id AS key LIMIT 1",
            key=token,
        )
        return ("session", rows[0]["key"]) if rows else None
    if not token:
        return None
    rows = run("MATCH (o:Observation {id: $key}) RETURN o.id AS key LIMIT 1", key=token)
    if rows:
        return ("observation", rows[0]["key"])
    rows = run(
        "MATCH (s:Session {session_id: $key}) RETURN s.session_id AS key LIMIT 1",
        key=token.removeprefix("sum:"),
    )
    return ("session", rows[0]["key"]) if rows else None


# Writers keep at most one predecessor, successor, source session, and
# producing run per observation, so these optional matches cannot
# multiply rows; LIMIT 1 guards the output anyway.
EXPAND_OBSERVATION = """
MATCH (o:Observation {id: $key})
OPTIONAL MATCH (o)-[:FROM_SESSION]->(s:Session)
OPTIONAL MATCH (s)-[:HAS_SUMMARY]->(sum:SessionSummary)
OPTIONAL MATCH (prev:Observation)-[:NEXT]->(o)
OPTIONAL MATCH (o)-[:NEXT]->(next:Observation)
OPTIONAL MATCH (er:ExtractionRun {status: 'completed'})-[:PRODUCED]->(o)
RETURN o {.id, .display_id, .type, .title, .narrative, .source_end} AS o,
       s {.session_id, .display_id, .user_id, .created_at} AS s,
       sum {.headline, .source_end} AS sum,
       prev {.id, .display_id, .type, .title, .source_end} AS prev,
       next {.id, .display_id, .type, .title, .source_end} AS next,
       er {.id, .event_count} AS run
LIMIT 1
"""

EXPAND_SESSION = """
MATCH (s:Session {session_id: $key})
OPTIONAL MATCH (s)-[:HAS_SUMMARY]->(sum:SessionSummary)
RETURN s {.session_id, .display_id, .user_id, .created_at} AS s,
       sum {.headline, .request, .progress, .outcome,
            .version, .source_end} AS sum,
       COUNT { (s)-[:HAS_EVENT]->() } AS event_count
LIMIT 1
"""

SESSION_OBSERVATIONS = f"""
MATCH (node:Observation)-[:FROM_SESSION]->(:Session {{session_id: $key}})
WITH node, null AS s
RETURN {ROW_FIELDS}
ORDER BY source_end, key
"""

EVIDENCE_START = """
MATCH (er:ExtractionRun {status: 'completed'})-[:PRODUCED]->(:Observation {id: $key})
MATCH (er)-[:PROCESSED_EVENT]->(e:SessionEvent)
RETURN e.event_id AS event_id
ORDER BY e.timestamp
LIMIT 1
"""

FIRST_EVENT = """
MATCH (:Session {session_id: $key})-[:FIRST_EVENT]->(e:SessionEvent)
RETURN e.event_id AS event_id
"""

EVENT_FIELDS = """e {.event_id, .event_name, .timestamp, .tool_name, .prompt,
          .tool_input, .tool_error, .last_assistant_message, .delta, .agent_type,
          .source, .prompt_name, .recall_channel} AS e,
       e.recall_block IS NOT NULL AS delivered"""

_EVENTS = """
MATCH (:Session {{session_id: $key}})-[:HAS_EVENT]->(start:SessionEvent {{event_id: $event_id}})
MATCH path = (start)-[:NEXT*{low}..{high}]->(e:SessionEvent)
RETURN {fields}
ORDER BY length(path)
"""
# One extra event past the page says whether another page exists.
EVENTS_FROM = _EVENTS.format(low=0, high=EVENT_PAGE, fields=EVENT_FIELDS)
EVENTS_AFTER = _EVENTS.format(low=1, high=EVENT_PAGE + 1, fields=EVENT_FIELDS)

# The same walk, kept inside one extraction run's window: an observation's
# evidence is what its run processed, not whatever the session did next.
_WINDOW_EVENTS = """
MATCH (er:ExtractionRun {{id: $run_id}})-[:PROCESSED_EVENT]->(start:SessionEvent {{event_id: $event_id}})
MATCH path = (start)-[:NEXT*{low}..{high}]->(e:SessionEvent)
WHERE all(n IN nodes(path) WHERE EXISTS {{ (er)-[:PROCESSED_EVENT]->(n) }})
RETURN {fields}
ORDER BY length(path)
"""
WINDOW_EVENTS_FROM = _WINDOW_EVENTS.format(low=0, high=EVENT_PAGE, fields=EVENT_FIELDS)
WINDOW_EVENTS_AFTER = _WINDOW_EVENTS.format(
    low=1, high=EVENT_PAGE + 1, fields=EVENT_FIELDS
)

SUMMARY_FIELDS = (
    ("Request", "request"),
    ("Progress", "progress"),
    ("Outcome", "outcome"),
)


def render_observation(data: dict, now: datetime | None = None) -> str:
    o, s, summary = data["o"], data.get("s"), data.get("sum")
    name = _ref(o.get("display_id"), o.get("id"))
    lines = [render_row(_observation_row(o), now)]
    if o.get("narrative"):
        lines += ["", "Narrative:", clip(o["narrative"], FIELD_CHARS)]
    context = []
    if s:
        context.append("Source: " + render_row(_session_row(s, summary), now))
    for label, neighbor in (("Previous", data.get("prev")), ("Next", data.get("next"))):
        if neighbor:
            context.append(f"{label}: " + render_row(_observation_row(neighbor), now))
    produced_by = data.get("run") or {}
    if produced_by.get("event_count"):
        context.append(
            f"Evidence: written from the prompts and agent responses among "
            f"{produced_by['event_count']} captured events; "
            f'expand_episodic("{name}", events=true) opens them.'
        )
    if context:
        lines += [""] + context
    return "\n".join(lines)


def render_session(
    data: dict, observations: list[dict], now: datetime | None = None
) -> str:
    s, summary = data["s"], data.get("sum")
    name = _ref(s.get("display_id"), s.get("session_id"))
    lines = [render_row(_session_row(s, summary), now)]
    if summary:
        lines.append(f"Summary version {summary.get('version') or 1}.")
        for label, field in SUMMARY_FIELDS:
            if summary.get(field):
                lines += ["", f"{label}: {clip(summary[field], FIELD_CHARS)}"]
    else:
        lines.append("No summary yet: extraction has not processed this session.")
    if observations:
        shown = observations[-OBSERVATION_PAGE:]
        count = (
            f"{len(observations)}"
            if len(shown) == len(observations)
            else f"latest {len(shown)} of {len(observations)}"
        )
        lines += ["", f"Observations ({count}):"] + [render_row(row, now) for row in shown]
    lines += [
        "",
        f"Captured events: {data.get('event_count') or 0}. "
        f'expand_episodic("{name}", events=true) opens them.',
    ]
    return "\n".join(lines)


def render_event(e: dict, delivered: bool) -> str:
    """One captured event: time · name · [tool ·] excerpt · [delivery]."""
    when = _native(e.get("timestamp"))
    name = e.get("event_name") or "?"
    parts = [when.strftime("%Y-%m-%d %H:%M:%S") if when else "undated", name]
    if e.get("tool_name"):
        parts.append(e["tool_name"])
    detail = {
        "UserPromptSubmit": e.get("prompt"),
        "PostToolUse": e.get("tool_input"),
        "PostToolUseFailure": e.get("tool_error") or e.get("tool_input"),
        "MessageDisplay": e.get("delta"),
        "Stop": e.get("last_assistant_message"),
        "SubagentStop": e.get("last_assistant_message"),
        "SubagentStart": e.get("agent_type"),
        "SessionStart": e.get("source"),
    }.get(name)
    if detail:
        parts.append(clip(detail, EXCERPT_CHARS))
    if name == "SessionStart" and e.get("prompt_name"):
        parts.append(f"[system prompt {e['prompt_name']} injected]")
    if delivered:
        parts.append(f"[memory delivered: {e.get('recall_channel') or 'recall'}]")
    return " · ".join(parts)


def events_page(
    run,
    session_id: str,
    name: str,
    header: str,
    start: str | None = None,
    cursor: str | None = None,
    run_id: str | None = None,
) -> str:
    """A page of captured events, from ``start`` or after ``cursor``.

    With ``run_id`` the page stays inside that extraction run's window.
    """
    if run_id:
        queries = (WINDOW_EVENTS_FROM, WINDOW_EVENTS_AFTER)
        params = {"run_id": run_id}
    else:
        queries = (EVENTS_FROM, EVENTS_AFTER)
        params = {"key": session_id}
    if cursor:
        rows = run(queries[1], event_id=cursor, **params)
        if not rows:
            return f"No captured events after that cursor in {name}."
    else:
        if start is None:
            first = run(FIRST_EVENT, key=session_id)
            if not first:
                return f"No captured events in {name}."
            start = first[0]["event_id"]
        rows = run(queries[0], event_id=start, **params)
    page, more = rows[:EVENT_PAGE], len(rows) > EVENT_PAGE
    lines = [header] + [render_event(row["e"], row["delivered"]) for row in page]
    if more:
        lines.append(
            f'More: expand_episodic("{name}", events=true, '
            f'cursor="{page[-1]["e"]["event_id"]}")'
        )
    return "\n".join(lines)


def expand(
    run,
    ref: str,
    events: bool = False,
    cursor: str | None = None,
    now: datetime | None = None,
) -> str:
    """Open one record, or with ``events`` a page of its captured source.

    An observation comes with rows for its timeline neighbors and source
    session; its events start at the first one its extraction run
    processed. A session comes with its current summary and observation
    rows; its events start at the beginning of the session.
    """
    target = resolve(run, ref)
    if target is None:
        return f"No episode with id {ref!r}. search_episodic() lists ids."
    kind, key = target
    if kind == "observation":
        rows = run(EXPAND_OBSERVATION, key=key)
        if not rows:
            return f"No episode with id {ref!r}. search_episodic() lists ids."
        data = rows[0]
        if not events:
            return bound(render_observation(data, now), EXPAND_OUTPUT_CHARS)
        name = _ref(data["o"].get("display_id"), key)
        session = data.get("s")
        if not session:
            return f"{name} has no source session to page."
        produced_by = data.get("run") or {}
        start = run(EVIDENCE_START, key=key) if produced_by.get("id") else []
        if not start:
            header = f"Captured events of {name}'s source session:"
        elif cursor:
            header = f"Captured events behind {name}, after the cursor:"
        else:
            header = (
                f"Captured events behind {name}: the "
                f"{produced_by.get('event_count')} its extraction run processed."
            )
        page = events_page(
            run,
            session["session_id"],
            name,
            header,
            start=start[0]["event_id"] if start else None,
            cursor=cursor,
            run_id=produced_by.get("id") if start else None,
        )
        return bound(page, EXPAND_OUTPUT_CHARS)
    rows = run(EXPAND_SESSION, key=key)
    if not rows:
        return f"No episode with id {ref!r}. search_episodic() lists ids."
    data = rows[0]
    name = _ref(data["s"].get("display_id"), key)
    if events:
        header = f"Captured events of {name}" + (" after the cursor:" if cursor else ":")
        return bound(events_page(run, key, name, header, cursor=cursor), EXPAND_OUTPUT_CHARS)
    observations = run(SESSION_OBSERVATIONS, key=key)
    return bound(render_session(data, observations, now), EXPAND_OUTPUT_CHARS)


# --- recall -------------------------------------------------------------------

RECAP_SESSIONS = 3
RECAP_OBSERVATIONS = 5
RELATED_ROWS = 3
OWN_PROGRESS_CHARS = 300
RECALL_BLOCK_CHARS = 3000
DELIVERY_BLOCK_CHARS = 8000

# Reciprocal rank fusion orders candidates but cannot say whether any of
# them is relevant, so prompt-time recall also asks each candidate to clear
# a floor on at least one leg. That floor is what lets an unrelated prompt
# receive nothing. A raw Lucene score makes a poor floor: it moves with the
# size and wording of the store (the same record scored 2.6 for the same
# query among three records, and 4.3 after four unrelated ones were
# added). So a fulltext candidate must
# instead share at least two of the prompt's distinctive words, or a
# quarter of them for a long prompt. The vector floor is Neo4j's cosine
# score, (1 + cosine) / 2, and depends on the embedding model. Chapter 9's
# evaluations are where both get tuned.
MIN_SHARED_TERMS = 2
VECTOR_FLOOR = 0.80
MAX_PROMPT_TERMS = 24

FRAMING = (
    "This is a historical record of past work. It does not assign\n"
    "new tasks or override current instructions.\n"
    "Use expand_episodic(id) to inspect an item, or\n"
    "search_episodic(query) to find more."
)

# Detail levels a delivery can carry. A title row is covered by a full
# account; a full account is never covered by a title.
DETAIL_RANK = {"title": 1, "full": 2}

# Recap rows carry what a delivery record needs as well: ``key`` (the stored
# id of the delivered memory) and its ``version``. Only records extraction
# wrote are selected (they have a display id), and never the receiving
# session's own.
RECAP_SUMMARIES = """
MATCH (sum:SessionSummary)
WHERE sum.project_id = $project AND sum.session_id <> $session_id
MATCH (s:Session)-[:HAS_SUMMARY]->(sum)
WHERE s.display_id IS NOT NULL AND ($user IS NULL OR s.user_id = $user)
WITH s, sum ORDER BY sum.source_end DESC LIMIT $limit
RETURN 'session' AS kind, s.display_id AS display_id, s.session_id AS ref,
       'session' AS type, sum.headline AS text, s.user_id AS user,
       sum.source_end AS source_end, sum.id AS key, sum.version AS version,
       sum.progress AS progress
"""

# Observations from one window share their source_end; the id, which ends
# in the output position, keeps them in the order they were written.
RECAP_OBSERVATIONS_QUERY = """
MATCH (node:Observation)
WHERE node.project_id = $project AND node.session_id <> $session_id
  AND node.display_id IS NOT NULL
WITH node ORDER BY node.source_end DESC, node.id LIMIT $limit
RETURN 'observation' AS kind, node.display_id AS display_id, node.id AS ref,
       node.type AS type, node.title AS text, null AS user,
       node.source_end AS source_end, node.id AS key, 1 AS version
"""

_RELATED_TAIL = f"""
WITH node, score ORDER BY score DESC LIMIT $candidates
OPTIONAL MATCH (s:Session)-[:HAS_SUMMARY]->(node)
RETURN {ROW_FIELDS}, coalesce(node.version, 1) AS version, score,
       [text IN [node.title, node.narrative, node.headline, node.request,
                 node.progress, node.outcome]
        WHERE text IS NOT NULL] AS searchable
ORDER BY score DESC
"""
_RELATED_FILTERS = """node.project_id = $project AND node.session_id <> $session_id
  AND score >= $floor"""
RELATED_FULLTEXT = (
    "CALL db.index.fulltext.queryNodes($index, $text)\nYIELD node, score\n"
    f"WHERE {_RELATED_FILTERS}" + _RELATED_TAIL
)
RELATED_VECTOR = (
    "CYPHER 25\n"
    + vector_search(in_project=True)
    + "WHERE node.session_id <> $session_id AND score >= $floor"
    + _RELATED_TAIL
)

DELIVERED = """
MATCH (m)-[r:INJECTED_IN]->(:Session {session_id: $session_id})
WHERE r.context_generation = $generation
RETURN m.id AS key, r.version AS version, r.detail AS detail
"""

RESOLVE_DISPLAY_IDS = """
UNWIND $ids AS ref
OPTIONAL MATCH (o:Observation {display_id: ref})
OPTIONAL MATCH (:Session {display_id: ref})-[:HAS_SUMMARY]->(sum:SessionSummary)
WITH ref, o, sum
WHERE o IS NOT NULL OR sum IS NOT NULL
RETURN ref, coalesce(o.id, sum.id) AS key,
       CASE WHEN o IS NULL THEN sum.version ELSE 1 END AS version
"""

# One statement per step of the delivery record. The event carries the
# exact block; INJECTED_AT is the per-memory audit of that delivery.
PREPARE_DELIVERY = """
MATCH (e:SessionEvent {event_id: $event_id})
SET e.recall_block = $block, e.recall_channel = $channel,
    e.recall_status = $status
WITH e
UNWIND $memories AS mem
OPTIONAL MATCH (o:Observation {id: mem.key})
OPTIONAL MATCH (sum:SessionSummary {id: mem.key})
WITH e, mem, coalesce(o, sum) AS m
WHERE m IS NOT NULL
CREATE (m)-[:INJECTED_AT {version: mem.version, detail: mem.detail,
                          context_generation: $generation,
                          channel: $channel, status: $status,
                          agent_id: $agent_id}]->(e)
"""

RETURNED = """
MATCH (e:SessionEvent {event_id: $event_id})
SET e.recall_status = 'returned'
WITH e
MATCH (m)-[r:INJECTED_AT]->(e)
SET r.status = 'returned'
"""

# INJECTED_IN answers "which sessions received this account?" and holds
# what the session's main context has seen in its current generation: the
# newest version delivered, at the most detail delivered for it. Only a
# delivery that was returned counts, and a subagent's context is its own,
# so its deliveries are linked but never suppress the main context's.
RECEIVED = """
MATCH (s:Session {session_id: $session_id})
UNWIND $memories AS mem
OPTIONAL MATCH (o:Observation {id: mem.key})
OPTIONAL MATCH (sum:SessionSummary {id: mem.key})
WITH s, mem, coalesce(o, sum) AS m
WHERE m IS NOT NULL
MERGE (m)-[r:INJECTED_IN]->(s)
ON CREATE SET r.first_delivered_at = datetime()
SET r.last_delivered_at = datetime()
FOREACH (_ IN CASE WHEN $main_context THEN [1] ELSE [] END |
  SET r.detail = CASE
        WHEN r.context_generation IS NULL OR r.context_generation <> $generation
          THEN mem.detail
        WHEN mem.version > r.version THEN mem.detail
        WHEN mem.version = r.version AND (mem.detail = 'full' OR r.detail = 'full')
          THEN 'full'
        WHEN mem.version = r.version THEN mem.detail
        ELSE r.detail END,
      r.version = CASE
        WHEN r.context_generation IS NULL OR r.context_generation <> $generation
          THEN mem.version
        WHEN mem.version > r.version THEN mem.version
        ELSE r.version END,
      r.context_generation = $generation
)
"""

SESSION_STATE = """
MATCH (s:Session {session_id: $session_id})
RETURN s.project_id AS project, s.user_id AS user,
       coalesce(s.context_generation, 1) AS generation
"""

_STOPWORDS = frozenset(
    """
    a about above after again against all also am an and any are as at be
    because been before being below between both but by can could did do
    does doing done down during each else few for from further get got had
    has have having he her here hers him his how i if in into is it its
    itself just let like me more most my no nor not now of off on once only
    or other our ours out over own please same she should so some such than
    that the their them then there these they this those through to too
    under until up us very want was we were what when where which while who
    whom why will with would yes you your yours okay ok thanks thank sure
    make need use using used tell show give look find see try let's i'm
    it's that's there's what's don't can't won't
    """.split()
)
_WORD = re.compile(r"[a-z0-9][a-z0-9_\-./]*[a-z0-9]|[a-z0-9]", re.IGNORECASE)


def prompt_terms(text: str | None, limit: int = MAX_PROMPT_TERMS) -> list[str]:
    """The prompt's distinctive words, in order: no stopwords, no short words.

    A whole prompt as a fulltext query would match any record sharing one
    common word with it; these are the words worth matching on. Long pasted
    text contributes its first ``limit`` distinct terms.
    """
    terms: list[str] = []
    for match in _WORD.finditer((text or "")[:4000].lower()):
        word = match.group(0).strip("-./_")
        if len(word) < 3 or word in _STOPWORDS or word.isdigit() or word in terms:
            continue
        terms.append(word)
        if len(terms) == limit:
            break
    return terms


def _singular(term: str) -> str:
    if len(term) > 4 and term.endswith("ies"):
        return term[:-3] + "y"
    if len(term) > 3 and term.endswith("s") and not term.endswith(("ss", "us", "is")):
        return term[:-1]
    return term


def with_singulars(terms: list[str]) -> list[str]:
    """``terms`` plus a plain singular of each plural-looking one.

    The fulltext index uses Lucene's standard analyzer, which does not
    stem, so a prompt about "renewals" would miss a record about a
    "renewal". A naive singular is enough for the common case, and a
    variant that matches nothing costs nothing.
    """
    out: list[str] = []
    for term in terms:
        out.append(term)
        if _singular(term) != term:
            out.append(_singular(term))
    return out


def shared_terms(terms: list[str], texts: list[str]) -> int:
    """How many of the prompt's terms a record's text contains, a word and
    its singular counting once."""
    words = {
        _singular(match.group(0).strip("-./_"))
        for text in texts
        for match in _WORD.finditer(str(text).lower())
    }
    return len({_singular(term) for term in terms} & words)


def enough_shared(terms: list[str]) -> int:
    """Shared terms a fulltext candidate needs: two, or a quarter of a long prompt's."""
    return max(MIN_SHARED_TERMS, -(-len(terms) // 4))


def session_state(run, session_id: str) -> dict | None:
    """The receiving session's project, owner, and context generation."""
    rows = run(SESSION_STATE, session_id=session_id)
    return rows[0] if rows else None


def delivered(run, session_id: str, generation: int) -> dict[str, dict]:
    """What the session's current context already holds, by stored memory id."""
    return {
        row["key"]: row
        for row in run(DELIVERED, session_id=session_id, generation=generation)
    }


def unseen(rows: list[dict], seen: dict[str, dict], detail: str = "title") -> list[dict]:
    """Rows whose delivery would add a new version or more detail.

    A memory is skipped only when the current context already holds the
    same or a later version at the same or greater detail: a title never
    blocks the full account, a new summary version is sent again, and after
    a compaction (a new generation) useful memory comes back.
    """
    wanted = DETAIL_RANK[detail]
    fresh = []
    for row in rows:
        had = seen.get(row["key"])
        if (
            had
            and (had.get("version") or 1) >= (row.get("version") or 1)
            and DETAIL_RANK.get(had.get("detail"), 0) >= wanted
        ):
            continue
        fresh.append(row)
    return fresh


def recap_rows(run, project: str, session_id: str, user: str | None) -> dict:
    """What the session-start recap shows, selected by recency alone.

    Up to three other sessions of the project with a summary and up to five
    observations from other sessions, newest source first. The current
    user's own most recent other session is included even when it is not
    among the newest, so where they left off can be shown; other people's
    progress stays inside their summaries.
    """
    params = {"project": project, "session_id": session_id}
    sessions = run(RECAP_SUMMARIES, user=None, limit=RECAP_SESSIONS, **params)
    observations = run(RECAP_OBSERVATIONS_QUERY, limit=RECAP_OBSERVATIONS, **params)
    own = run(RECAP_SUMMARIES, user=user, limit=1, **params) if user else []
    return {"sessions": sessions, "observations": observations, "own": own[0] if own else None}


def render_recap(
    project: str,
    sessions: list[dict],
    observations: list[dict],
    own: dict | None = None,
    now: datetime | None = None,
) -> str:
    """The session-start block: recent sessions, recent activity, framing.

    Empty when there is nothing to show, so an empty project gets no block.
    """
    if own and all(row["key"] != own["key"] for row in sessions):
        sessions = sessions + [own]
    if not sessions and not observations:
        return ""
    lines = [f"Previously, on {project}:", ""]
    if sessions:
        lines.append("Recent sessions:")
        for row in sessions:
            lines.append("- " + render_row(row, now))
            if own and row["key"] == own["key"] and own.get("progress"):
                lines.append(
                    "  Where you left off: "
                    + clip(own["progress"], OWN_PROGRESS_CHARS)
                )
    if observations:
        if sessions:
            lines.append("")
        lines.append("Recent activity:")
        lines += ["- " + render_row(row, now) for row in observations]
    lines += ["", FRAMING]
    return bound("\n".join(lines), RECALL_BLOCK_CHARS)


def related(
    run,
    prompt: str | None,
    vector,
    project: str,
    session_id: str,
    limit: int = RELATED_ROWS,
    vector_floor: float = VECTOR_FLOOR,
) -> list[dict]:
    """Episodes from other sessions that a prompt is likely about.

    The same legs as ``hybrid_search``, restricted to the project and to
    other sessions' records, with each leg's candidates cut at its floor
    before rank fusion: shared words for the fulltext leg, similarity for
    the vector leg. No candidate above a floor means no rows.
    """
    terms = prompt_terms(prompt)
    params = {
        "project": project,
        "session_id": session_id,
        "candidates": limit * CANDIDATES_PER_ROW,
    }
    legs = []
    if terms:
        needed = enough_shared(terms)
        rows = _leg(
            run,
            RELATED_FULLTEXT,
            index=FULLTEXT_INDEX,
            text=lucene_query(" ".join(with_singulars(terms))),
            floor=0.0,
            **params,
        )
        legs.append([row for row in rows if shared_terms(terms, row["searchable"]) >= needed])
    if vector is not None:
        legs.append(
            _leg(run, RELATED_VECTOR, index=VECTOR_INDEX, vector=vector,
                 floor=vector_floor, **params)
        )
    return fuse([leg for leg in legs if leg])[:limit]


def render_related(project: str, rows: list[dict], now: datetime | None = None) -> str:
    """The prompt-time block: a few related rows and the same framing."""
    if not rows:
        return ""
    lines = [f"Related memory from {project}:"]
    lines += ["- " + render_row(row, now) for row in rows]
    lines += ["", FRAMING]
    return bound("\n".join(lines), RECALL_BLOCK_CHARS)


_DISPLAY_ID = re.compile(r"#([os]\d+)\b")


def display_ids(text: str) -> list[str]:
    """Display ids (o112, s41) in the order a response first names them."""
    seen: list[str] = []
    for token in _DISPLAY_ID.findall(text or ""):
        if token not in seen:
            seen.append(token)
    return seen


def resolve_display_ids(run, ids: list[str]) -> dict[str, dict]:
    """Stored id and current version for each display id that resolves."""
    if not ids:
        return {}
    return {row["ref"]: row for row in run(RESOLVE_DISPLAY_IDS, ids=list(ids))}


def prepare_delivery(
    tx,
    event_id: str,
    block: str,
    channel: str,
    memories: list[dict],
    generation: int,
    status: str = "prepared",
    agent_id: str | None = None,
) -> None:
    """Record a delivery on the event that carries it, before it is returned.

    ``memories`` holds one ``{key, version, detail}`` per delivered memory.
    The block is stored exactly as rendered, since the memory it came from
    may read differently tomorrow.
    """
    tx.run(
        PREPARE_DELIVERY,
        event_id=event_id,
        block=bound(block, DELIVERY_BLOCK_CHARS),
        channel=channel,
        status=status,
        memories=memories,
        generation=generation,
        agent_id=agent_id,
    ).consume()


def mark_returned(tx, event_id: str) -> None:
    """The hook handed the block back to the harness.

    Claude Code gives a hook no acceptance signal beyond its own exit, so
    this is the strongest status the adapter can record. A ``prepared``
    block without ``returned`` means the hook died before delivering it.
    """
    tx.run(RETURNED, event_id=event_id).consume()


def mark_received(
    tx,
    session_id: str,
    memories: list[dict],
    generation: int,
    main_context: bool = True,
) -> None:
    """Link delivered memories to the session and update what it has seen."""
    if memories:
        tx.run(
            RECEIVED,
            session_id=session_id,
            memories=memories,
            generation=generation,
            main_context=main_context,
        ).consume()
