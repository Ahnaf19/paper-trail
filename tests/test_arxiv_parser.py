"""Parser tests run against a saved fixture — no network, by design."""

from datetime import UTC
from pathlib import Path

import pytest

from pipeline.arxiv_parser import parse_feed

FIXTURES = Path(__file__).parent / "fixtures"
LIVE = FIXTURES / "arxiv_page.xml"  # real capture, 50 entries
EDGE = FIXTURES / "arxiv_edge_cases.xml"  # hand-built, only for shapes real data lacks


@pytest.fixture(scope="module")
def page():
    return parse_feed(LIVE.read_bytes())


@pytest.fixture(scope="module")
def edge():
    return parse_feed(EDGE.read_bytes())


def test_every_real_entry_parses(page):
    assert len(page.papers) == 50


def test_feed_metadata_is_read(page):
    """`total_results` is what lets the client bound its pagination."""
    assert page.total_results == 199389
    assert page.start_index == 0


def test_version_suffix_is_stripped(page):
    """Real ids all carry a `vN`; a revision must update the row, not fork one."""
    assert all("v" not in p.arxiv_id.rsplit(".", 1)[-1] for p in page.papers)
    assert page.papers[0].arxiv_id == "2012.12104"


def test_missing_doi_and_journal_ref_are_none(page):
    """FR-2.3 — absence is the common case: 49/50 have no doi, 47/50 no journal_ref."""
    assert sum(1 for p in page.papers if p.doi is None) == 49
    assert sum(1 for p in page.papers if p.journal_ref is None) == 47


def test_present_doi_and_journal_ref_are_kept(page):
    assert any(p.doi for p in page.papers)
    assert any(p.journal_ref for p in page.papers)


def test_dates_are_timezone_aware(page):
    """FR-2.2 — stored as timestamps, never strings."""
    assert all(p.published.tzinfo and p.updated.tzinfo for p in page.papers)
    assert page.papers[0].published.astimezone(UTC).year == 2020


def test_authors_are_flattened_and_ordered(page):
    """FR-2.4 — real papers here carry 1 to 11 authors."""
    counts = [len(p.authors) for p in page.papers]
    assert min(counts) == 1
    assert max(counts) == 11
    assert all(p.authors == list(dict.fromkeys(p.authors)) for p in page.papers)


def test_primary_category_is_separate_but_also_listed(page):
    """FR-2.4 — primary is its own attribute and appears among the categories."""
    assert all(p.primary_category in p.categories for p in page.papers)
    assert max(len(p.categories) for p in page.papers) == 5


def test_abstract_whitespace_is_collapsed(page):
    """FR-2.5 — real abstracts arrive hard-wrapped from the LaTeX source."""
    assert not any("\n" in p.abstract for p in page.papers)
    assert not any("  " in p.abstract for p in page.papers)
    assert all(p.abstract == p.abstract.strip() for p in page.papers)


def test_old_style_ids_survive_version_stripping(edge):
    """`cond-mat/0211034v3` -> `cond-mat/0211034`; the slash must not be mangled."""
    assert "cond-mat/0211034" in [p.arxiv_id for p in edge.papers]


def test_multiline_title_is_collapsed(edge):
    assert edge.papers[2].title == "Thermal Conductivity in Disordered Low-Dimensional Systems"


def test_offset_datetime_spelling_is_accepted(edge):
    """Real feeds use `Z`; the documented examples use `+00:00`. Both must parse."""
    assert edge.papers[2].updated.astimezone(UTC).year == 2026


def test_a_malformed_entry_is_skipped_not_fatal():
    """One bad entry must not lose the other 49 on the page."""
    xml = LIVE.read_text().replace(
        "<title>A Deep Reinforcement Learning Approach for Ramp Metering "
        "Based on Traffic Video Data</title>",
        "",
    )
    page = parse_feed(xml)
    assert len(page.papers) == 49
    assert "2012.12104" not in [p.arxiv_id for p in page.papers]


def test_empty_feed_parses_to_no_papers():
    """`start` beyond the end of the result set is normal, not an error."""
    empty = """<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
      <opensearch:totalResults>0</opensearch:totalResults>
      <opensearch:startIndex>0</opensearch:startIndex>
    </feed>"""
    page = parse_feed(empty)
    assert page.papers == []
    assert page.total_results == 0
