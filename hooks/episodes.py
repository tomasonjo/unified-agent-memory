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
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone

KINDS = ("observation", "session", "both")

MAX_SEARCH_LIMIT = 50
# Each leg hands rank fusion this many candidates per wanted row. The
# vector procedure returns its nearest nodes before the project, since,
# and kind filters can apply, so a vector leg over-fetches by this
# factor; the fulltext leg filters first and keeps its best this many.
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
VECTOR_INDEXES = {
    "observation": ("observation_embedding", "Observation"),
    "session": ("summary_embedding", "SessionSummary"),
}


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
ON EACH [n.title, n.facts, n.narrative,
         n.headline, n.request, n.progress, n.learned, n.next_steps]
"""

VECTOR_INDEX_QUERY = """
CREATE VECTOR INDEX {name} IF NOT EXISTS
FOR (n:{label}) ON (n.embedding)
OPTIONS {{indexConfig: {{
  `vector.dimensions`: {dimensions},
  `vector.similarity_function`: 'cosine'}}}}
"""


def ensure_retrieval_indexes(session, dimensions: int | None = None) -> None:
    """Create the retrieval indexes that are missing, then wait for them.

    The fulltext index spans both labels, so one call searches
    observations and summaries together. A vector index binds to a single
    label, so each kind gets its own, and only when an embedding model is
    configured; ``dimensions`` must match that model. Each statement runs
    on its own, because schema commands do not compose into one
    transaction. Failures are reported rather than raised: search skips a
    leg whose index is missing. ``IF NOT EXISTS`` never updates an
    existing definition, so changing the indexed fields means dropping the
    index first.
    """
    statements = [(FULLTEXT_INDEX, FULLTEXT_INDEX_QUERY)]
    if dimensions:
        for name, label in VECTOR_INDEXES.values():
            statements.append(
                (
                    name,
                    VECTOR_INDEX_QUERY.format(
                        name=name, label=label, dimensions=int(dimensions)
                    ),
                )
            )
    for name, statement in statements:
        try:
            session.run(statement).consume()
            session.run("CALL db.awaitIndex($name, 30)", name=name).consume()
        except Exception as exc:
            print(f"[uam] index {name} unavailable: {exc}", file=sys.stderr)


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

FULLTEXT_LEG = f"""
CALL db.index.fulltext.queryNodes($index, $text)
YIELD node, score
WHERE {FILTERS}
WITH node, score ORDER BY score DESC LIMIT $candidates
OPTIONAL MATCH (s:Session)-[:HAS_SUMMARY]->(node)
RETURN {ROW_FIELDS}, score
ORDER BY score DESC
"""

VECTOR_LEG = f"""
CALL db.index.vector.queryNodes($index, $candidates, $vector)
YIELD node, score
WHERE {FILTERS}
OPTIONAL MATCH (s:Session)-[:HAS_SUMMARY]->(node)
RETURN {ROW_FIELDS}, score
ORDER BY score DESC
"""


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
    leg searches both kinds in a single call, each vector index adds a leg
    when a query vector is given, and reciprocal rank fusion merges them.
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}, not {kind!r}")
    limit = max(1, min(int(limit), MAX_SEARCH_LIMIT))
    text = lucene_query(query)
    if not text and vector is None:
        return recent(run, project, kind, since, limit)
    params = {
        "project": project,
        "kind": kind,
        "since": since,
        "candidates": limit * CANDIDATES_PER_ROW,
    }
    legs = []
    if text:
        legs.append(_leg(run, FULLTEXT_LEG, index=FULLTEXT_INDEX, text=text, **params))
    if vector is not None:
        for leg_kind, (index, _label) in VECTOR_INDEXES.items():
            if kind in (leg_kind, "both"):
                legs.append(_leg(run, VECTOR_LEG, index=index, vector=vector, **params))
    return fuse(legs)[:limit]


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
OPTIONAL MATCH (prev:Observation)-[:NEXT]->(o)
OPTIONAL MATCH (o)-[:NEXT]->(next:Observation)
OPTIONAL MATCH (o)-[:FROM_SESSION]->(s:Session)
OPTIONAL MATCH (s)-[:HAS_SUMMARY]->(sum:SessionSummary)
OPTIONAL MATCH (er:ExtractionRun {status: 'completed'})-[:PRODUCED]->(o)
RETURN o {.id, .display_id, .type, .title, .facts, .narrative, .source_end} AS o,
       prev {.id, .display_id, .type, .title, .source_end} AS prev,
       next {.id, .display_id, .type, .title, .source_end} AS next,
       s {.session_id, .display_id, .user_id, .created_at} AS s,
       sum {.headline, .source_end} AS sum,
       er {.id, .event_count} AS run
LIMIT 1
"""

EXPAND_SESSION = """
MATCH (s:Session {session_id: $key})
OPTIONAL MATCH (s)-[:HAS_SUMMARY]->(sum:SessionSummary)
RETURN s {.session_id, .display_id, .user_id, .created_at} AS s,
       sum {.headline, .request, .progress, .learned, .next_steps,
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
          .tool_input, .tool_error, .last_assistant_message, .agent_type,
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
    ("Learned", "learned"),
    ("Next steps", "next_steps"),
)


def render_observation(data: dict, now: datetime | None = None) -> str:
    o, s, summary = data["o"], data.get("s"), data.get("sum")
    name = _ref(o.get("display_id"), o.get("id"))
    lines = [render_row(_observation_row(o), now)]
    facts = [fact for fact in (o.get("facts") or []) if fact]
    if facts:
        lines += ["", "Facts:"] + [f"- {clip(fact, FIELD_CHARS)}" for fact in facts]
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
            f"Evidence: {produced_by['event_count']} captured events went into this "
            f'account; expand("{name}", events=true) opens them.'
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
        f'expand("{name}", events=true) opens them.',
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
            f'More: expand("{name}", events=true, '
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
        return f"No episode with id {ref!r}. search() lists ids."
    kind, key = target
    if kind == "observation":
        rows = run(EXPAND_OBSERVATION, key=key)
        if not rows:
            return f"No episode with id {ref!r}. search() lists ids."
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
        return f"No episode with id {ref!r}. search() lists ids."
    data = rows[0]
    name = _ref(data["s"].get("display_id"), key)
    if events:
        header = f"Captured events of {name}" + (" after the cursor:" if cursor else ":")
        return bound(events_page(run, key, name, header, cursor=cursor), EXPAND_OUTPUT_CHARS)
    observations = run(SESSION_OBSERVATIONS, key=key)
    return bound(render_session(data, observations, now), EXPAND_OUTPUT_CHARS)
