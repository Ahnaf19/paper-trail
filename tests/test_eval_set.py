"""The golden set's shape, and the sweep's decision rule.

The rule is the part worth testing. `recall@k` is arithmetic; *"which threshold do we
recommend"* is a judgement encoded in code, and a judgement that nothing checks is a
judgement that can be quietly reversed.
"""

import json
from pathlib import Path

import pytest

from app.core.config import settings
from app.services.retrieval import RetrievedPaper
from scripts.evaluate import (
    GRID_HI,
    RETAINED_OF_CEILING,
    Probe,
    Row,
    ceiling,
    load_rows,
    recall_at,
    recommend,
    score_threshold,
)

GOLDEN = Path("eval/golden.jsonl")


def hit(arxiv_id: str, score: float) -> RetrievedPaper:
    return RetrievedPaper(arxiv_id=arxiv_id, title="t", abstract="a", score=score)


def positive(gold_score: float, rank: int = 1, band: str = "big") -> Probe:
    """A positive whose gold paper sits at `rank` with `gold_score`, under a stronger top-1."""
    hits = [hit(f"filler{i}", gold_score + 0.01 * (rank - i)) for i in range(1, rank)]
    hits.append(hit("gold", gold_score))
    return Probe(
        row=Row(id="p", question="q", expects="answer", gold="gold", band=band),
        hits=hits,
        rank=rank,
    )


def negative(top1: float, group: str = "adjacent_cs") -> Probe:
    return Probe(
        row=Row(id="n", question="q", expects="refuse", group=group), hits=[hit("x", top1)]
    )


# --- the committed dataset ------------------------------------------------------------


def test_the_golden_set_parses():
    rows = load_rows(GOLDEN)

    assert rows, "golden.jsonl is empty"
    assert {r.expects for r in rows} == {"answer", "refuse"}


def test_ids_are_unique():
    """Results are keyed by id across runs — a collision silently merges two questions."""
    ids = [r.id for r in load_rows(GOLDEN)]

    assert len(ids) == len(set(ids))


def test_every_positive_names_exactly_one_paper_and_a_band():
    for row in load_rows(GOLDEN):
        if row.is_positive:
            assert row.gold, f"{row.id} has no relevant paper"
            assert row.band in {"big", "mid", "thin"}, f"{row.id} has band {row.band!r}"


def test_every_negative_is_labelled_with_its_group():
    """The groups are not decoration: adjacent-CS is what actually sets the threshold, and
    reporting it merged with off-topic would hide that."""
    for row in load_rows(GOLDEN):
        if not row.is_positive:
            assert row.group in {"adjacent_cs", "off_topic"}, f"{row.id} has group {row.group!r}"
            assert row.gold is None


def test_a_positive_without_a_relevant_paper_is_rejected(tmp_path):
    """Scored silently, such a row would look like a permanent retrieval miss."""
    broken = tmp_path / "broken.jsonl"
    broken.write_text(json.dumps({"id": "p001", "question": "q?", "expects": "answer"}) + "\n")

    with pytest.raises(ValueError, match="must name a relevant paper"):
        load_rows(broken)


def test_the_metadata_records_the_corpus_it_was_built_against():
    """Without this the evaluator cannot tell "retrieval got worse" from "different corpus"."""
    meta = json.loads(GOLDEN.with_suffix(".meta.json").read_text())

    assert meta["corpus"]["papers"] > 0
    assert meta["corpus"]["newest_updated"]
    assert meta["seed"] and meta["generator_model"]


# --- the sweep's decision rule --------------------------------------------------------


def test_a_gold_paper_ranked_below_top_k_is_not_a_correct_answer():
    """`/ask` only ever sees `RETRIEVAL_TOP_K` hits, so a paper at rank k+1 cannot be cited
    however high it scores — the sweep must not credit it."""
    beyond = positive(gold_score=0.99, rank=settings.retrieval_top_k + 1)

    assert score_threshold([beyond], 0.5)["answered"] == 1.0
    assert score_threshold([beyond], 0.5)["correct"] == 0.0


def test_a_negative_scoring_above_the_line_counts_as_a_false_pass():
    probes = [negative(0.80), negative(0.60, group="off_topic")]

    assert score_threshold(probes, 0.70)["false_pass"] == 1
    assert score_threshold(probes, 0.70)["adjacent"] == 1
    assert score_threshold(probes, 0.70)["off_topic"] == 0


def test_the_ceiling_is_what_retrieval_delivers_with_the_gate_open():
    """Two of five golds sit outside top-k, so no threshold can ever score above 0.6."""
    probes = [positive(0.9) for _ in range(3)] + [
        positive(0.9, rank=settings.retrieval_top_k + 1) for _ in range(2)
    ]

    assert ceiling(probes) == pytest.approx(0.6)


def test_the_target_is_relative_so_a_low_ceiling_still_yields_a_recommendation():
    """The first version of this rule compared against an absolute 0.95 and recommended
    nothing whenever retrieval could not reach it — which is most real corpora."""
    probes = [positive(0.85) for _ in range(9)] + [
        positive(0.85, rank=settings.retrieval_top_k + 1)
    ]

    assert ceiling(probes) < 0.95, "the fixture must sit below the old absolute target"
    assert recommend(probes) is not None


def test_a_tie_on_false_passes_breaks_to_the_lower_threshold():
    """The regression that matters: taking the *largest* eligible threshold buys no extra
    refusals and costs real answers, so a tie must resolve downward."""
    probes = (
        [positive(0.90) for _ in range(20)]
        + [positive(0.72)]
        + [negative(0.65), negative(0.50, group="off_topic")]
    )

    chosen = recommend(probes)

    assert chosen is not None
    # Every threshold above 0.65 ties at zero false passes, so only the positives separate
    # them — and the chosen one must keep more of them than the top of that tied range.
    assert score_threshold(probes, chosen)["false_pass"] == 0
    assert score_threshold(probes, chosen)["correct"] > score_threshold(probes, GRID_HI)["correct"]


def test_the_recommendation_satisfies_the_retention_floor():
    """Stated as a property so the number stays derived — writing a literal in `recommend`
    would fail this on any fixture but the one it was written for."""
    probes = (
        [positive(0.90) for _ in range(18)] + [positive(0.62) for _ in range(2)] + [negative(0.70)]
    )

    chosen = recommend(probes)

    assert chosen is not None
    assert score_threshold(probes, chosen)["correct"] >= RETAINED_OF_CEILING * ceiling(probes)


def test_nothing_is_recommended_when_retrieval_found_nothing():
    """A ceiling of zero means the gate is not the problem, and naming a threshold would
    imply it was."""
    probes = [positive(0.9, rank=settings.retrieval_top_k + 1) for _ in range(3)]

    assert ceiling(probes) == 0.0
    assert recommend(probes) is None


def test_recall_counts_only_ranks_within_k():
    probes = [positive(0.8, rank=1), positive(0.8, rank=7), positive(0.8, rank=1)]

    assert recall_at(probes, 1) == pytest.approx(2 / 3)
    assert recall_at(probes, 10) == 1.0
