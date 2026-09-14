"""The watermark is what makes a re-run cheap — and what can silently skip papers."""

from datetime import UTC, datetime, timedelta

from pipeline.watermark import (
    categories_key,
    page_is_older_than,
    read_watermark,
    save_watermark,
    stop_threshold,
)
from tests.helpers import make_paper

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def test_key_is_order_independent():
    """The same set typed in any order must not look like a different query."""
    assert categories_key(["cs.LG", "cs.AI"]) == categories_key(["cs.AI", "cs.LG"])
    assert categories_key(["cs.AI", "cs.LG"]) == "cs.AI|cs.LG"


def test_no_watermark_for_an_unseen_category_set(session):
    assert read_watermark(session, "cs.AI") is None


def test_watermark_round_trips(session):
    save_watermark(session, "cs.AI", NOW)
    session.commit()
    assert read_watermark(session, "cs.AI") == NOW


def test_widening_the_category_set_yields_no_watermark(session):
    """🔴 The bug this table exists to prevent (PRD §6.5).

    A mark earned by `cs.AI` alone must not apply once `cs.LG` joins the query, or the
    early stop fires and every older `cs.LG` paper is silently never fetched.
    """
    save_watermark(session, categories_key(["cs.AI"]), NOW)
    session.commit()

    assert read_watermark(session, categories_key(["cs.AI", "cs.LG"])) is None


def test_watermark_never_moves_backwards(session):
    """A partial run over stale data must not rewind progress."""
    save_watermark(session, "cs.AI", NOW)
    session.commit()
    save_watermark(session, "cs.AI", NOW - timedelta(days=5))
    session.commit()
    assert read_watermark(session, "cs.AI") == NOW


def test_threshold_backs_off_by_the_margin():
    """Re-seeing a paper is free; missing one is not."""
    assert stop_threshold(NOW) == NOW - timedelta(days=1)
    assert stop_threshold(None) is None


def test_first_run_never_stops_early():
    """With no mark there is no basis to stop — everything is new."""
    assert page_is_older_than([make_paper(updated=NOW)], None) is False


def test_page_with_anything_newer_does_not_stop():
    papers = [
        make_paper("2409.00001", updated=NOW - timedelta(days=10)),
        make_paper("2409.00002", updated=NOW),  # one fresh paper is enough
    ]
    assert page_is_older_than(papers, stop_threshold(NOW)) is False


def test_page_entirely_below_the_threshold_stops():
    papers = [
        make_paper("2409.00001", updated=NOW - timedelta(days=10)),
        make_paper("2409.00002", updated=NOW - timedelta(days=11)),
    ]
    assert page_is_older_than(papers, stop_threshold(NOW)) is True


def test_a_paper_inside_the_margin_does_not_stop():
    """Exactly what the margin is for: re-scan a little rather than miss something."""
    papers = [make_paper(updated=NOW - timedelta(hours=12))]
    assert page_is_older_than(papers, stop_threshold(NOW)) is False


def test_empty_page_does_not_stop():
    """An empty page means the feed ran out, which the caller handles — not a stop signal."""
    assert page_is_older_than([], stop_threshold(NOW)) is False
