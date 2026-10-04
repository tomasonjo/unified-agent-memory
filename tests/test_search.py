"""The vector leg against the scratch database: one index, filtered by project."""

from __future__ import annotations

import pytest

import episodes
from conftest import PROJECT

pytestmark = pytest.mark.graph

DIMENSIONS = 3
QUERY = [1.0, 0.0, 0.0]

# Six observations in another project sit closer to the query than this
# project's one observation and one summary.
SEED = """
UNWIND range(1, 6) AS i
CREATE (:Observation {id: 'other-' + i, display_id: 'o' + (100 + i),
                      project_id: 'other', session_id: 's-other',
                      type: 'discovery', title: 'Other project ' + i,
                      source_end: datetime(), embedding: [1.0, 0.01 * i, 0.0]})
WITH count(*) AS seeded
CREATE (:Observation {id: 'maria-1', display_id: 'o1', project_id: $project,
                      session_id: 's-maria', type: 'discovery',
                      title: 'Renewal drop traced to March pipeline change',
                      source_end: datetime(), embedding: [0.8, 0.6, 0.0]})
CREATE (:Session {session_id: 's-maria', display_id: 's1',
                  user_id: 'maria@company.com'})
         -[:HAS_SUMMARY]->
       (:SessionSummary {id: 'sum:s-maria', project_id: $project,
                         session_id: 's-maria', headline: 'Renewal drop explained',
                         source_end: datetime(), embedding: [0.7, 0.7, 0.0]})
"""


@pytest.fixture
def indexed(graph):
    graph.session.run(
        "CREATE VECTOR INDEX observation_embedding IF NOT EXISTS "
        "FOR (o:Observation) ON (o.embedding) OPTIONS {indexConfig: "
        "{`vector.dimensions`: 3, `vector.similarity_function`: 'cosine'}}"
    ).consume()
    graph.session.run(SEED, project=PROJECT).consume()
    episodes.ensure_retrieval_indexes(graph.session, DIMENSIONS)
    yield graph
    graph.session.run(f"DROP INDEX {episodes.VECTOR_INDEX} IF EXISTS").consume()


def vector_indexes(graph) -> dict:
    return {
        row["name"]: row["labelsOrTypes"]
        for row in graph.rows("SHOW VECTOR INDEXES YIELD name, labelsOrTypes")
    }


def test_one_vector_index_replaces_the_per_label_ones(indexed):
    assert vector_indexes(indexed) == {
        "episode_embedding": ["Observation", "SessionSummary"]
    }


def test_the_project_filter_runs_inside_the_index(indexed):
    run = episodes.reader(indexed.session)
    rows = episodes.hybrid_search(run, None, QUERY, PROJECT, limit=1)
    assert [row["display_id"] for row in rows] == ["o1"]
    rows = episodes.hybrid_search(run, None, QUERY, PROJECT, kind="session")
    assert [row["display_id"] for row in rows] == ["s1"]
    rows = episodes.hybrid_search(run, None, QUERY, None, limit=1)
    assert [row["display_id"] for row in rows] == ["o101"]


def test_related_recall_reads_the_same_index(indexed):
    run = episodes.reader(indexed.session)
    rows = episodes.related(run, None, QUERY, PROJECT, "s-analyst")
    assert {row["display_id"] for row in rows} == {"o1", "s1"}
    assert episodes.related(run, None, QUERY, PROJECT, "s-maria") == []


def test_both_legs_are_fused_in_one_query(indexed):
    run = episodes.reader(indexed.session)
    rows = episodes.hybrid_search(run, "renewal drop", QUERY, PROJECT)
    assert {row["display_id"] for row in rows} == {"o1", "s1"}
    # Found by both legs: two rank shares, more than any single leg's best.
    assert all(row["score"] > 1 / (episodes.RRF_K + 1) for row in rows)


def test_a_missing_vector_index_costs_only_its_leg(indexed):
    indexed.session.run(f"DROP INDEX {episodes.VECTOR_INDEX}").consume()
    run = episodes.reader(indexed.session)
    rows = episodes.hybrid_search(run, "renewal drop", QUERY, PROJECT)
    assert {row["display_id"] for row in rows} == {"o1", "s1"}
