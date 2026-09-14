"""Aggregations against a seeded corpus, asserting exact numbers rather than shapes."""

from datetime import UTC, datetime

import pytest

from pipeline.api.app import app
from pipeline.db import get_session
from pipeline.upsert import upsert_papers
from tests.helpers import make_paper


def at(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


# Four papers chosen so every count below differs from every other plausible count:
# cs.AI 3 vs cs.LG 2 catches a swapped join; Ada's 3 vs everyone else's 1 catches a
# fan-out; and the paper published at 23:00 on New Year's Eve catches a time-zone slip.
SEED = [
    make_paper(
        "2601.00001",
        published=at(2026, 1, 15),
        categories=["cs.AI", "cs.LG"],
        authors=["Ada Lovelace", "Bob Metcalfe"],
    ),
    make_paper(
        "2601.00002", published=at(2026, 1, 20), categories=["cs.AI"], authors=["Ada Lovelace"]
    ),
    make_paper(
        "2602.00003",
        published=at(2026, 2, 5),
        categories=["cs.LG"],
        authors=["Ada Lovelace", "Carol Shaw"],
    ),
    make_paper(
        "2512.00004",
        published=at(2025, 12, 31, hour=23),
        categories=["cs.AI"],
        authors=["Dan Bricklin"],
    ),
]


@pytest.fixture
def corpus(session):
    """Seeded through the real write path, so the tests read what ingestion actually stores."""
    upsert_papers(session, SEED)
    session.commit()


def test_health_reports_ok(client):
    assert client.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize(
    "path", ["/stats/papers-over-time", "/stats/top-categories", "/stats/top-authors"]
)
def test_empty_corpus_returns_an_empty_series(client, session, path):
    """An empty chart is a valid chart — a reviewer who has not ingested yet sees [], not 404."""
    response = client.get(path)
    assert response.status_code == 200
    assert response.json() == []


def test_papers_over_time_buckets_by_month(client, corpus):
    assert client.get("/stats/papers-over-time").json() == [
        {"period": "2025-12", "category": "cs.AI", "count": 1},
        {"period": "2026-01", "category": "cs.AI", "count": 2},
        {"period": "2026-01", "category": "cs.LG", "count": 1},
        {"period": "2026-02", "category": "cs.LG", "count": 1},
    ]


def test_papers_over_time_buckets_by_year(client, corpus):
    assert client.get("/stats/papers-over-time?granularity=year").json() == [
        {"period": "2025", "category": "cs.AI", "count": 1},
        {"period": "2026", "category": "cs.AI", "count": 2},
        {"period": "2026", "category": "cs.LG", "count": 2},
    ]


def test_papers_over_time_filters_by_category(client, corpus):
    assert client.get("/stats/papers-over-time?category=cs.LG").json() == [
        {"period": "2026-01", "category": "cs.LG", "count": 1},
        {"period": "2026-02", "category": "cs.LG", "count": 1},
    ]


def test_date_range_includes_the_whole_final_day(client, corpus):
    """`to` names a day, not an instant — a naive `<= to` would drop the 20th entirely."""
    response = client.get("/stats/papers-over-time?from=2026-01-16&to=2026-01-20")
    assert response.json() == [{"period": "2026-01", "category": "cs.AI", "count": 1}]


def test_from_after_to_is_rejected(client):
    assert client.get("/stats/papers-over-time?from=2026-02-01&to=2026-01-01").status_code == 422


def test_unknown_granularity_is_rejected(client):
    """The enum stops it at the edge, so no unexpected text ever reaches date_trunc."""
    assert client.get("/stats/papers-over-time?granularity=week").status_code == 422


@pytest.mark.parametrize("client", ["Asia/Tokyo"], indirect=True)
def test_buckets_are_anchored_to_utc(client, corpus):
    """2025-12-31 23:00Z is 2026-01-01 08:00 in Tokyo: without the anchor it moves months."""
    periods = {row["period"] for row in client.get("/stats/papers-over-time").json()}
    assert "2025-12" in periods


def test_top_categories_counts_distinct_papers(client, corpus):
    """The first paper has two authors and two categories — neither may inflate the other."""
    assert client.get("/stats/top-categories").json() == [
        {"category": "cs.AI", "count": 3},
        {"category": "cs.LG", "count": 2},
    ]


def test_top_authors_breaks_ties_by_name(client, corpus):
    """Three authors tie on one paper each; without a tiebreak their order would drift."""
    assert client.get("/stats/top-authors").json() == [
        {"author": "Ada Lovelace", "count": 3},
        {"author": "Bob Metcalfe", "count": 1},
        {"author": "Carol Shaw", "count": 1},
        {"author": "Dan Bricklin", "count": 1},
    ]


def test_n_limits_the_rows(client, corpus):
    assert client.get("/stats/top-authors?n=1").json() == [{"author": "Ada Lovelace", "count": 3}]


@pytest.mark.parametrize("n", [0, 101])
def test_n_is_bounded(client, n):
    """Unbounded top-N is a free denial-of-service, so the bounds are part of the contract."""
    assert client.get(f"/stats/top-categories?n={n}").status_code == 422


def test_a_database_failure_is_a_clean_503(client):
    """A refused connection surfaces as a bare OSError, which 500s and leaks driver text."""

    async def broken_session():
        raise OSError("Connect call failed ('127.0.0.1', 5432)")
        yield

    app.dependency_overrides[get_session] = broken_session
    response = client.get("/stats/top-categories")

    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}
    assert "127.0.0.1" not in response.text
