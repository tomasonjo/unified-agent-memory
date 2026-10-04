"""Browsing the project timeline: time bounds and paging back."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import episodes
from conftest import PROJECT

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)

# Five observations over four days. o3 and o4 come from one extraction
# window, so they share their source_end.
SEED = """
UNWIND $rows AS row
CREATE (:Observation {id: 'obs-' + row.n, display_id: 'o' + row.n,
                      project_id: $project, session_id: 's-maria',
                      type: 'discovery', title: 'Finding ' + row.n,
                      source_end: datetime(row.at)})
"""
DAYS = {1: "2026-09-01", 2: "2026-09-02", 3: "2026-09-03", 4: "2026-09-03", 5: "2026-09-04"}


@pytest.fixture
def timeline(graph):
    rows = [{"n": n, "at": f"{day}T10:00:00Z"} for n, day in DAYS.items()]
    graph.session.run(SEED, rows=rows, project=PROJECT).consume()
    return episodes.reader(graph.session)


def ids(rows: list[dict]) -> list[str]:
    return [row["display_id"] for row in rows]


def test_spans_count_back_from_now_for_either_bound():
    assert episodes.parse_time("7d", "until", NOW) == (NOW - timedelta(days=7)).isoformat()
    assert episodes.parse_time("2026-09-03", "until") == "2026-09-03T00:00:00+00:00"
    with pytest.raises(ValueError, match="^until must be"):
        episodes.parse_time("last week", "until")


def test_the_older_call_repeats_only_the_filters_that_were_set():
    call = episodes.older_call(
        "2026-09-03T10:00:00+00:00", project=None, kind="observation", since=None, limit=2
    )
    assert call == (
        'Older: search_episodic(kind="observation", limit=2, '
        'until="2026-09-03T10:00:00+00:00")'
    )


@pytest.mark.graph
def test_since_and_until_bound_a_half_open_window(timeline):
    rows, older = episodes.recent(
        timeline, PROJECT, since="2026-09-02T10:00:00+00:00",
        until="2026-09-04T10:00:00+00:00",
    )
    assert sorted(ids(rows)) == ["o2", "o3", "o4"]
    assert older is None


@pytest.mark.graph
def test_paging_back_moves_a_shared_window_whole(timeline):
    pages, until = [], None
    while True:
        rows, until = episodes.recent(timeline, PROJECT, until=until, limit=2)
        pages.append(sorted(ids(rows)))
        if until is None:
            break
    # A page of two would split o3 and o4, so the first page stops at o5.
    assert pages == [["o5"], ["o3", "o4"], ["o1", "o2"]]
