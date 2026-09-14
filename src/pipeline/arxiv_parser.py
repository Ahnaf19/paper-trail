"""Atom feed -> validated records. Knows nothing about HTTP or the database."""

import re
from datetime import datetime
from typing import Final
from xml.etree.ElementTree import Element

from defusedxml import ElementTree
from loguru import logger
from pydantic import BaseModel

_ATOM: Final = "{http://www.w3.org/2005/Atom}"
_ARXIV: Final = "{http://arxiv.org/schemas/atom}"
_OPENSEARCH: Final = "{http://a9.com/-/spec/opensearch/1.1/}"

_ABS_PREFIX: Final = "/abs/"
_VERSION_SUFFIX: Final = re.compile(r"v\d+$")


class ArxivPaper(BaseModel):
    """One paper, flattened and cleaned."""

    arxiv_id: str
    title: str
    abstract: str
    primary_category: str
    categories: list[str]
    authors: list[str]
    published: datetime
    updated: datetime
    doi: str | None = None
    journal_ref: str | None = None


class ArxivPage(BaseModel):
    """One response. `total_results` lets the caller bound its pagination."""

    total_results: int
    start_index: int
    papers: list[ArxivPaper]


def _collapse(text: str | None) -> str:
    """Abstracts and titles arrive hard-wrapped from the LaTeX source (FR-2.5)."""
    return " ".join((text or "").split())


def _strip_version(entry_id: str) -> str:
    """`http://arxiv.org/abs/2409.01234v1` -> `2409.01234`, so a revision updates a row."""
    raw = entry_id.rsplit(_ABS_PREFIX, 1)[-1]
    return _VERSION_SUFFIX.sub("", raw)


def _optional(entry: Element, tag: str) -> str | None:
    found = entry.find(tag)
    return (_collapse(found.text) or None) if found is not None else None


def _parse_entry(entry: Element) -> ArxivPaper:
    find, findall = entry.find, entry.findall

    authors = [_collapse(n.text) for n in findall(f"{_ATOM}author/{_ATOM}name")]
    categories = [c.get("term") for c in findall(f"{_ATOM}category") if c.get("term")]

    return ArxivPaper(
        arxiv_id=_strip_version(find(f"{_ATOM}id").text),
        title=_collapse(find(f"{_ATOM}title").text),
        abstract=_collapse(find(f"{_ATOM}summary").text),
        primary_category=find(f"{_ARXIV}primary_category").get("term"),
        # Deduped because paper_category/paper_author are keyed on the pair.
        categories=list(dict.fromkeys(categories)),
        authors=list(dict.fromkeys(a for a in authors if a)),
        published=datetime.fromisoformat(find(f"{_ATOM}published").text),
        updated=datetime.fromisoformat(find(f"{_ATOM}updated").text),
        doi=_optional(entry, f"{_ARXIV}doi"),
        journal_ref=_optional(entry, f"{_ARXIV}journal_ref"),
    )


def _int_text(root: Element, tag: str, default: int = 0) -> int:
    found = root.find(tag)
    return int(found.text) if found is not None and found.text else default


def parse_feed(xml: str | bytes) -> ArxivPage:
    """Parse one Atom response. A malformed entry is skipped, not fatal."""
    root = ElementTree.fromstring(xml)

    papers: list[ArxivPaper] = []
    skipped = 0
    for entry in root.findall(f"{_ATOM}entry"):
        try:
            papers.append(_parse_entry(entry))
        except (AttributeError, TypeError, ValueError) as exc:
            skipped += 1
            node = entry.find(f"{_ATOM}id")
            logger.warning(
                "skipping unparseable entry: {} ({})",
                node.text if node is not None else "<no id>",
                exc,
            )

    if skipped:
        logger.warning("skipped {} of {} entries", skipped, skipped + len(papers))

    return ArxivPage(
        total_results=_int_text(root, f"{_OPENSEARCH}totalResults"),
        start_index=_int_text(root, f"{_OPENSEARCH}startIndex"),
        papers=papers,
    )
