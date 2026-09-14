"""Parser tests run against a saved fixture — no network, by design."""

from datetime import UTC
from pathlib import Path

import pytest

from pipeline.arxiv_parser import parse_feed

FIXTURE = Path(__file__).parent / "fixtures" / "arxiv_page.xml"


@pytest.fixture(scope="module")
def page():
    return parse_feed(FIXTURE.read_bytes())


def test_all_entries_parse(page):
    assert len(page.papers) == 3


def test_feed_metadata_is_read(page):
    """`total_results` is what lets the client bound its pagination."""
    assert page.total_results == 184213
    assert page.start_index == 0


def test_version_suffix_is_stripped(page):
    """A revision must update the existing row, not fork a second one."""
    assert [p.arxiv_id for p in page.papers] == [
        "2409.01234",
        "2409.05678",
        "cond-mat/0211034",
    ]


def test_missing_doi_and_journal_ref_are_none(page):
    """FR-2.3 — absence is normal for a preprint, not an error."""
    minimal = page.papers[1]
    assert minimal.doi is None
    assert minimal.journal_ref is None


def test_present_doi_and_journal_ref_are_kept(page):
    full = page.papers[0]
    assert full.doi == "10.1000/jmlr.2026.0001"
    assert full.journal_ref == "JMLR 27 (2026) 1-24"


def test_dates_are_timezone_aware(page):
    """FR-2.2 — both the `Z` and `+00:00` spellings appear in real feeds."""
    for paper in page.papers:
        assert paper.published.tzinfo is not None
        assert paper.updated.tzinfo is not None
    assert page.papers[2].updated.astimezone(UTC).year == 2026


def test_authors_are_flattened_in_order(page):
    """FR-2.4 — five authors produce five names, and order is meaningful."""
    assert page.papers[2].authors == [
        "Émile Durand",
        "Wei Zhang",
        "Olusegun Adeyemi",
        "Maria José Ferreira",
        "Jan Novák",
    ]


def test_primary_category_is_separate_from_the_category_list(page):
    """FR-2.4 — primary is a single value with different meaning, but also a category."""
    paper = page.papers[2]
    assert paper.primary_category == "cond-mat.stat-mech"
    assert paper.categories == [
        "cond-mat.stat-mech",
        "cond-mat.dis-nn",
        "physics.comp-ph",
    ]


def test_abstract_whitespace_is_collapsed(page):
    """FR-2.5 — abstracts arrive hard-wrapped from the LaTeX source."""
    abstract = page.papers[2].abstract
    assert "\n" not in abstract
    assert "  " not in abstract
    assert abstract.startswith("We revisit transport in disordered chains")


def test_multiline_title_is_collapsed(page):
    assert page.papers[2].title == "Thermal Conductivity in Disordered Low-Dimensional Systems"


def test_a_malformed_entry_is_skipped_not_fatal():
    """One bad entry must not lose the other 199 on the page."""
    xml = FIXTURE.read_text().replace("<title>A Note on Vector Index Selection</title>", "")
    page = parse_feed(xml)
    assert len(page.papers) == 2
    assert "2409.05678" not in [p.arxiv_id for p in page.papers]


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
