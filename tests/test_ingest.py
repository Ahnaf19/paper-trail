"""The ingest loop, driven by a fake client — these never reach arXiv."""

from collections.abc import Iterator
from datetime import timedelta

from sqlalchemy import func, select

from app.models import Paper
from app.services.arxiv_parser import ArxivPaper
from app.services.ingest import run_ingest
from app.services.watermark import categories_key, read_watermark, save_watermark
from tests.helpers import DEFAULT_TIME as NOW
from tests.helpers import make_paper

CATEGORIES = ["cs.AI"]


def feed(papers: list[ArxivPaper], total: int | None = None) -> str:
    """Render papers back into the Atom the parser expects."""
    entries = "".join(
        f"""<entry>
            <id>http://arxiv.org/abs/{p.arxiv_id}v1</id>
            <published>{p.published.isoformat()}</published>
            <updated>{p.updated.isoformat()}</updated>
            <title>{p.title}</title>
            <summary>{p.abstract}</summary>
            {"".join(f"<author><name>{a}</name></author>" for a in p.authors)}
            <arxiv:primary_category term="{p.primary_category}"
                xmlns:arxiv="http://arxiv.org/schemas/atom"/>
            {"".join(f'<category term="{c}"/>' for c in p.categories)}
        </entry>"""
        for p in papers
    )
    return f"""<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
      <opensearch:totalResults>{total if total is not None else len(papers)}</opensearch:totalResults>
      <opensearch:startIndex>0</opensearch:startIndex>
      {entries}
    </feed>"""


class FakeClient:
    """Yields prepared pages and records how many were actually requested."""

    def __init__(self, pages: list[list[ArxivPaper]], total: int | None = None) -> None:
        self._pages = pages
        self._total = total
        self.pages_requested = 0

    def iter_pages(self, query: str, *, start: int = 0) -> Iterator[tuple[int, str]]:
        for index, papers in enumerate(self._pages):
            self.pages_requested += 1
            yield index * 100, feed(papers, self._total)
        while True:  # the feed running dry looks like an empty page
            self.pages_requested += 1
            yield len(self._pages) * 100, feed([], self._total)

    def close(self) -> None:
        pass


def run(session_factory, pages, **kwargs):
    client = FakeClient(pages, total=kwargs.pop("total", None))
    report = run_ingest(
        CATEGORIES,
        max_papers=kwargs.pop("max_papers", 1000),
        client=client,
        session_factory=session_factory,
        **kwargs,
    )
    return report, client


def factory_for(session):
    """Reuse the test's session so assertions see the committed rows."""

    class _NoCloseSession:
        def __enter__(self):
            return session

        def __exit__(self, *exc):
            return False

    return _NoCloseSession


def test_ingests_papers_across_pages(session):
    pages = [[make_paper(f"2409.0000{i}") for i in range(3)], [make_paper("2409.10000")]]
    report, _ = run(factory_for(session), pages, total=4)

    assert report.pages_fetched == 2
    assert report.papers_seen == 4
    assert report.inserted == 4
    assert session.execute(select(func.count()).select_from(Paper)).scalar_one() == 4


def test_stops_at_max_papers(session):
    pages = [[make_paper(f"2409.0000{i}") for i in range(3)] for _ in range(5)]
    report, client = run(factory_for(session), pages, max_papers=3, total=999)

    assert report.papers_seen == 3
    assert client.pages_requested == 1, "kept fetching after reaching max"


def test_second_run_over_identical_data_inserts_nothing(session):
    """FR-1.4 end to end, through the loop rather than the upsert alone."""
    pages = [[make_paper("2409.00001"), make_paper("2409.00002")]]
    run(factory_for(session), pages, total=2)
    report, _ = run(factory_for(session), pages, total=2)

    assert (report.inserted, report.updated, report.unchanged) == (0, 0, 2)
    assert session.execute(select(func.count()).select_from(Paper)).scalar_one() == 2


def test_watermark_is_recorded_after_a_run(session):
    newest = NOW + timedelta(days=3)
    run(factory_for(session), [[make_paper("2409.00001", updated=newest)]], total=1)

    assert read_watermark(session, categories_key(CATEGORIES)) == newest


def test_early_stop_when_a_whole_page_predates_the_watermark(session):
    """The saving the watermark exists for: stop instead of walking the whole feed."""
    save_watermark(session, categories_key(CATEGORIES), NOW + timedelta(days=30))
    session.commit()

    old = [make_paper(f"2409.0000{i}", updated=NOW) for i in range(3)]
    report, client = run(factory_for(session), [old, old, old], total=999)

    assert report.stopped_early is True
    assert client.pages_requested == 1


def test_full_flag_disarms_the_early_stop(session):
    """`--full` must scan regardless of how advanced the watermark is."""
    save_watermark(session, categories_key(CATEGORIES), NOW + timedelta(days=30))
    session.commit()

    old = [make_paper(f"2409.0000{i}", updated=NOW) for i in range(3)]
    report, _ = run(factory_for(session), [old], full=True, total=3)

    assert report.stopped_early is False
    assert report.papers_seen == 3


def test_run_ends_when_the_feed_is_exhausted(session):
    """`total_results` bounds the walk, so an exhausted feed is not an error."""
    report, client = run(factory_for(session), [[make_paper("2409.00001")]], total=1)

    assert report.papers_seen == 1
    assert client.pages_requested == 1


def test_an_entirely_empty_feed_fetches_no_pages(session):
    """The signal `paper ingest` turns into a non-zero exit — see tests/test_cli.py.

    A mistyped category and a genuinely empty one are indistinguishable at the API, so
    the loop only reports what it saw: no usable page at all.
    """
    report, client = run(factory_for(session), [[]])

    assert report.pages_fetched == 0
    assert report.papers_seen == 0
    assert client.pages_requested == 1


def test_a_capped_run_that_never_reaches_the_watermark_leaves_it_alone(session):
    """The mark means "everything at or above this is ingested". A run stopped by --max
    above the old mark never fetched the span between them — advancing would bury that
    span below the next run's stop threshold, permanently."""
    old_mark = NOW
    save_watermark(session, categories_key(CATEGORIES), old_mark)
    session.commit()

    # Every paper is newer than the mark, so the walk never reaches down to it.
    recent = [make_paper(f"2409.0000{i}", updated=NOW + timedelta(days=10)) for i in range(3)]
    report, _ = run(factory_for(session), [recent] * 5, max_papers=3, total=999)

    assert report.gap_left_open is True
    assert read_watermark(session, categories_key(CATEGORIES)) == old_mark


def test_a_capped_run_that_does_reach_the_watermark_still_advances_it(session):
    """The guard tests contiguity, not the stop reason — hitting --max is fine as long as
    the walk came down far enough to join what we already held."""
    save_watermark(session, categories_key(CATEGORIES), NOW)
    session.commit()

    reaching = [
        make_paper("2409.00001", updated=NOW + timedelta(days=10)),
        make_paper("2409.00002", updated=NOW - timedelta(days=1)),  # below the mark
    ]
    report, _ = run(factory_for(session), [reaching] * 5, max_papers=2, total=999)

    assert report.gap_left_open is False
    assert read_watermark(session, categories_key(CATEGORIES)) == NOW + timedelta(days=10)


def test_a_capped_first_run_records_a_watermark(session):
    """With nothing held there is no span to skip, so the newest is honest — this is what
    keeps the cold `--max` run's second pass able to stop early."""
    newest = NOW + timedelta(days=3)
    pages = [[make_paper(f"2409.0000{i}", updated=newest - timedelta(hours=i)) for i in range(3)]]
    report, _ = run(factory_for(session), pages * 5, max_papers=3, total=999)

    assert report.gap_left_open is False
    assert read_watermark(session, categories_key(CATEGORIES)) == newest
