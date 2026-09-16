"""Measure retrieval and answering quality against `eval/golden.jsonl`.

    uv run python scripts/evaluate.py                      # offline, free, a few seconds
    uv run python scripts/evaluate.py --with-llm           # + end-to-end answer/refuse
    uv run python scripts/evaluate.py --save 001-baseline  # write eval/results/001-baseline.json

Three reports off one dataset:

  RETRIEVAL  positives only. Are the right papers coming back at all? recall@k and MRR.
             Independent of the threshold and of the LLM, so it isolates the one stage.

  SWEEP      positives + negatives. Where should the relevance gate sit, and what does each
             position cost? Threshold selection is binary classification, and this is the
             labelled data it needs -- which is why it replaced a script that looked for a
             gap between score ranges and gave up when the ranges overlapped.

  END-TO-END the whole of `/ask`. The number nothing else measures: how often a question the
             corpus cannot answer gets answered anyway.

Every query path here is the one that ships -- `retrieve`, `fit_context`, `answer_question`.
An eval that exercises its own private copy measures its own copy.
"""

import argparse
import asyncio
import json
import statistics
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import LLMUnavailableError
from app.core.logging import configure_logging
from app.db.session import AsyncSessionLocal
from app.models import Paper
from app.services.embeddings import FastEmbedEmbedder
from app.services.llm import OpenAICompatibleClient
from app.services.prompts import NO_ANSWER, fit_context
from app.services.qa import answer_question
from app.services.retrieval import RetrievedPaper, retrieve

MAX_K = 20  # deepest rank the reports quote; bounds one query per question
RECALL_AT = (1, 3, 5, 10, 20)

# The sweep prints a coarse grid for legibility and searches a fine one for the recommendation.
GRID_LO, GRID_HI = 0.60, 0.90
COARSE_STEP, FINE_STEP = 0.02, 0.01

# The rule the recommendation follows, stated once so it is derived rather than typed in.
#
# It is RELATIVE to what retrieval delivered, not an absolute target. With RETRIEVAL_TOP_K = 5
# the gold paper is within reach for 92 of 97 questions however the gate is set, so an absolute
# target above 0.948 is unsatisfiable by construction -- the first version of this rule asked
# for 0.95 and therefore recommended nothing at all. A threshold cannot repair retrieval; its
# only job is to avoid throwing away what retrieval found.
#
# The asymmetry is real: a wasted LLM call is recoverable and the grounded prompt is a second
# chance at catching it, while a wrongly refused question has no second gate behind it.
RETAINED_OF_CEILING = 0.95

BAR_WIDTH = 34

# One judge call per negative that clears the gate -- a handful, not a per-row cost. The
# system prompt tells the model to decline in prose, not with a sentinel, so refusal at the
# second gate cannot be detected by string comparison.
JUDGE_SYSTEM = """\
You classify whether an answer refused to answer.

Reply with exactly one word:
- DECLINED  if it says the provided material does not contain the information, or otherwise
  refuses to answer the question.
- ANSWERED  if it gives a substantive answer.

Output the single word and nothing else.\
"""


@dataclass
class Row:
    """One golden-set entry. `gold` is empty for a question that must be refused."""

    id: str
    question: str
    expects: str
    gold: str | None = None
    band: str | None = None
    group: str | None = None

    @property
    def is_positive(self) -> bool:
        return self.expects == "answer"


@dataclass
class Probe:
    """One question scored against the corpus once. Both offline reports derive from these."""

    row: Row
    hits: list[RetrievedPaper]
    rank: int | None = None  # 1-based position of the gold paper, None if outside MAX_K

    @property
    def top1(self) -> float:
        return self.hits[0].score if self.hits else 0.0

    @property
    def gold_score(self) -> float:
        """The gold paper's own similarity — what the threshold actually has to clear."""
        return self.hits[self.rank - 1].score if self.rank else 0.0


@dataclass
class Report:
    """Accumulated output: what is printed, and what `--save` serialises."""

    sections: list[str] = field(default_factory=list)
    data: dict[str, object] = field(default_factory=dict)

    def emit(self, text: str) -> None:
        self.sections.append(text)
        print(text)


def load_rows(path: Path) -> list[Row]:
    rows: list[Row] = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        raw = json.loads(line)
        relevant = raw.get("relevant") or []
        rows.append(
            Row(
                id=raw["id"],
                question=raw["question"],
                expects=raw["expects"],
                gold=relevant[0] if relevant else None,
                band=raw.get("band"),
                group=raw.get("group"),
            )
        )
        if raw["expects"] == "answer" and not relevant:
            raise ValueError(f"{path}:{number}: a positive row must name a relevant paper")
    return rows


def bar(value: float, width: int = BAR_WIDTH) -> str:
    filled = round(value * width)
    return "█" * filled + "░" * (width - filled)


def heading(title: str) -> str:
    return f"\n {title}\n " + "─" * 70


async def probe_all(
    session: AsyncSession, rows: list[Row], embedder: FastEmbedEmbedder
) -> list[Probe]:
    """Retrieve once per question. Everything offline is computed from the result."""
    probes: list[Probe] = []
    for row in rows:
        vector = await asyncio.to_thread(embedder.embed_query, row.question)
        hits = await retrieve(session, vector, k=MAX_K)
        probe = Probe(row=row, hits=hits)
        if row.gold:
            for position, hit in enumerate(hits, start=1):
                if hit.arxiv_id == row.gold:
                    probe.rank = position
                    break
        probes.append(probe)
    return probes


# --- report 0 -------------------------------------------------------------------------


async def report_coverage(
    session: AsyncSession, rows: list[Row], meta: dict[str, object] | None, report: Report
) -> list[Row]:
    """Drop labelled papers this corpus does not hold, loudly.

    A golden set is bound to the corpus it was generated from — arXiv has no "as of" query,
    so a corpus ingested on another day is a different corpus. Scoring a missing paper as a
    retrieval failure would be a lie; skipping it silently would be a worse one.
    """
    positives = [r for r in rows if r.is_positive]
    negatives = [r for r in rows if not r.is_positive]

    wanted = {r.gold for r in positives if r.gold}
    present = set(
        (await session.execute(select(Paper.arxiv_id).where(Paper.arxiv_id.in_(wanted))))
        .scalars()
        .all()
    )
    live_papers = (await session.execute(select(Paper.arxiv_id))).scalars().all()

    lines = [
        heading("0. COVERAGE"),
        f" golden set        {len(positives)} positives · {len(negatives)} negatives",
    ]
    if meta:
        built = meta.get("corpus", {})
        matches = built.get("papers") == len(live_papers)
        lines += [
            f" built against     {built.get('papers')} papers, newest {built.get('newest_updated')}",
            f" this corpus       {len(live_papers)} papers"
            f"{'   ✓ same corpus' if matches else '   ✗ DIFFERENT CORPUS'}",
        ]
    found = len(present)
    lines.append(f" labelled papers   {found}/{len(wanted)} present in this corpus")

    if found < len(wanted):
        lines += [
            "",
            f" ⚠ {len(wanted) - found} positives reference papers this corpus does not hold and are",
            "   SKIPPED, not counted as misses. An eval set is only meaningful on the corpus",
            "   it was built from; regenerate with scripts/build_golden_set.py to re-bind it.",
        ]
    report.emit("\n".join(lines))
    report.data["coverage"] = {
        "positives": len(positives),
        "negatives": len(negatives),
        "labelled_present": found,
        "labelled_wanted": len(wanted),
        "corpus_papers": len(live_papers),
        "built_against": meta.get("corpus") if meta else None,
    }
    return [r for r in rows if not r.is_positive or r.gold in present]


# --- report 1 -------------------------------------------------------------------------


def recall_at(probes: list[Probe], k: int) -> float:
    if not probes:
        return 0.0
    return sum(1 for p in probes if p.rank and p.rank <= k) / len(probes)


def mrr(probes: list[Probe]) -> float:
    if not probes:
        return 0.0
    return statistics.fmean(1 / p.rank if p.rank else 0.0 for p in probes)


def report_retrieval(probes: list[Probe], report: Report) -> None:
    positives = [p for p in probes if p.row.is_positive]
    lines = [
        heading(f"1. RETRIEVAL  ({len(positives)} positives, single relevant paper each)"),
    ]
    for k in RECALL_AT:
        value = recall_at(positives, k)
        lines.append(f"   recall@{k:<3}  {value:.3f}  {bar(value)}")
    lines.append(f"   MRR@{MAX_K:<5}  {mrr(positives):.3f}")

    lines += ["", f"   {'band':<8}{'n':>5}{'r@1':>8}{'r@5':>8}{'r@10':>8}{'MRR':>8}"]
    per_band: dict[str, dict[str, float]] = {}
    for band in ("big", "mid", "thin"):
        group = [p for p in positives if p.row.band == band]
        if not group:
            continue
        stats = {
            "n": len(group),
            "recall@1": recall_at(group, 1),
            "recall@5": recall_at(group, 5),
            "recall@10": recall_at(group, 10),
            "mrr": mrr(group),
        }
        per_band[band] = stats
        lines.append(
            f"   {band:<8}{len(group):>5}{stats['recall@1']:>8.3f}"
            f"{stats['recall@5']:>8.3f}{stats['recall@10']:>8.3f}{stats['mrr']:>8.3f}"
        )

    lines += [
        "",
        "   Bands are the paper's rarest category: big ≥100 papers, mid 10-99, thin <10.",
        "   Reported per band as well as overall because thin categories are deliberately",
        "   over-sampled — the overall figure is not what real traffic would see.",
        "",
        "   Not precision@k: with one relevant paper per question, precision@5 cannot exceed",
        "   0.2 however perfect the ranking, so it measures the value of k, not the system.",
        "   Recall@k here is hit-rate@k, and is an upper bound — the questions were generated",
        "   from the abstracts they point at, so they share vocabulary with them.",
    ]
    report.emit("\n".join(lines))
    report.data["retrieval"] = {
        "n": len(positives),
        "recall": {f"@{k}": recall_at(positives, k) for k in RECALL_AT},
        "mrr": mrr(positives),
        "per_band": per_band,
    }


# --- report 2 -------------------------------------------------------------------------


def cited_sources(probe: Probe, t: float) -> list[RetrievedPaper]:
    """Exactly what `/ask` would return in `sources` at this threshold.

    Modelling the gate alone would overstate a large `k`: `fit_context` drops the tail once
    the context budget runs out, and a gold paper dropped there is never seen by the model
    and never cited. At k=5 nothing trims; at k=10 two thirds of questions do.
    """
    relevant = [hit for hit in probe.hits[: settings.retrieval_top_k] if hit.score >= t]
    return fit_context(relevant, max_chars=settings.max_context_chars)


def score_threshold(probes: list[Probe], t: float) -> dict[str, float]:
    """What `/ask` would do at `t`, computed through the code that ships.

    It refuses when nothing in the top-k clears the threshold, so with descending scores a
    question is answered exactly when its top-1 clears — and answered *usefully* only when
    the gold paper survives into the sources.
    """
    positives = [p for p in probes if p.row.is_positive]
    negatives = [p for p in probes if not p.row.is_positive]
    answered = [p for p in positives if p.top1 >= t]
    correct = [p for p in positives if any(s.arxiv_id == p.row.gold for s in cited_sources(p, t))]
    return {
        "answered": len(answered) / len(positives) if positives else 0.0,
        "correct": len(correct) / len(positives) if positives else 0.0,
        "false_pass": sum(1 for p in negatives if p.top1 >= t),
        "adjacent": sum(1 for p in negatives if p.row.group == "adjacent_cs" and p.top1 >= t),
        "off_topic": sum(1 for p in negatives if p.row.group == "off_topic" and p.top1 >= t),
    }


def ceiling(probes: list[Probe]) -> float:
    """What retrieval delivers with the gate wide open — the most any threshold can preserve."""
    return score_threshold(probes, 0.0)["correct"]


def recommend(probes: list[Probe]) -> float | None:
    """Fewest false passes among the thresholds that retain enough of the retrieval ceiling.

    Ties break DOWNWARD. A tie means the negatives cannot tell those thresholds apart, so the
    only thing left to decide on is the positives — and the lower threshold keeps more of them.
    Taking the largest instead (the obvious first rule, and the one written here first) picks a
    threshold that costs real answers and buys no refusals.
    """
    target = RETAINED_OF_CEILING * ceiling(probes)
    if target <= 0.0:
        return None
    steps = round((GRID_HI - GRID_LO) / FINE_STEP)
    eligible = []
    for i in range(steps + 1):
        t = round(GRID_LO + i * FINE_STEP, 2)
        scored = score_threshold(probes, t)
        if scored["correct"] >= target:
            eligible.append((scored["false_pass"], t))
    return min(eligible)[1] if eligible else None


def report_sweep(probes: list[Probe], report: Report) -> None:
    negatives = [p for p in probes if not p.row.is_positive]
    n_adjacent = sum(1 for p in negatives if p.row.group == "adjacent_cs")
    n_off_topic = sum(1 for p in negatives if p.row.group == "off_topic")
    current = settings.relevance_threshold
    suggested = recommend(probes)

    steps = round((GRID_HI - GRID_LO) / COARSE_STEP)
    grid = {round(GRID_LO + i * COARSE_STEP, 2) for i in range(steps + 1)}
    grid.update({current} | ({suggested} if suggested else set()))

    lines = [
        heading(
            f"2. THRESHOLD SWEEP  ({len(probes) - len(negatives)} positives"
            f" + {len(negatives)} must-refuse)"
        ),
        f"   {'t':>6}{'answered':>11}{'correct':>9}"
        f"{'false-pass':>13}{'adjacent':>12}{'off-topic':>12}",
    ]
    table: dict[str, dict[str, float]] = {}
    for t in sorted(grid):
        s = score_threshold(probes, t)
        table[f"{t:.2f}"] = s
        marks = " ←current" if abs(t - current) < 1e-9 else ""
        if suggested and abs(t - suggested) < 1e-9:
            marks += " ★recommended"
        ratios = (
            f"{s['false_pass']:.0f}/{len(negatives)}",
            f"{s['adjacent']:.0f}/{n_adjacent}",
            f"{s['off_topic']:.0f}/{n_off_topic}",
        )
        lines.append(
            f"   {t:>6.2f}{s['answered']:>11.2f}{s['correct']:>9.2f}"
            f"{ratios[0]:>13}{ratios[1]:>12}{ratios[2]:>12}{marks}"
        )

    reach = ceiling(probes)
    lines += [
        "",
        f"   retrieval ceiling  {reach:.3f}  — the gold paper is inside top-{settings.retrieval_top_k}",
        "                              for this fraction of positives however the gate is set,",
        "                              so no threshold can score above it.",
        "",
        f"   rule: fewest false passes among t retaining {RETAINED_OF_CEILING:.0%} of that",
        f"         ceiling (≥ {RETAINED_OF_CEILING * reach:.3f} correct); ties break to the lower t.",
        "   Relative, not absolute — a threshold cannot repair retrieval, only preserve it.",
        "   Ties break downward because the asymmetry is real: a false pass costs one LLM call",
        "   and still faces the grounded prompt, while a wrongly refused question has no",
        "   second gate behind it.",
        "",
        f"   recommended  {suggested if suggested else 'none — no t meets the rule'}"
        f"        current  {current}",
    ]
    report.emit("\n".join(lines))
    report.data["sweep"] = {
        "rule": (
            f"fewest false passes among t with correct >= "
            f"{RETAINED_OF_CEILING} * retrieval ceiling; ties break low"
        ),
        "retrieval_ceiling": reach,
        "recommended": suggested,
        "current": current,
        "table": table,
    }


# --- report 3 -------------------------------------------------------------------------


async def judge_refusal(llm: OpenAICompatibleClient, question: str, answer: str) -> bool:
    try:
        verdict = await llm.complete(JUDGE_SYSTEM, f"Question: {question}\n\nAnswer: {answer}")
    except LLMUnavailableError as exc:
        logger.warning("judge unavailable: {}", exc)
        return False
    return verdict.strip().upper().startswith("DECLINED")


async def report_end_to_end(
    session: AsyncSession,
    rows: list[Row],
    embedder: FastEmbedEmbedder,
    llm: OpenAICompatibleClient,
    pause: float,
    report: Report,
) -> None:
    """The whole of `/ask`, including both refusal gates."""
    outcomes = {"cited": 0, "answered_uncited": 0, "wrongly_refused": 0}
    refusals = {"gate1": 0, "gate2": 0, "wrongly_answered": 0}
    wrong: list[str] = []

    for index, row in enumerate(rows):
        if index:
            await asyncio.sleep(pause)
        result = await answer_question(session, row.question, embedder, llm)
        refused_at_gate1 = result.answer == NO_ANSWER

        if row.is_positive:
            if refused_at_gate1:
                outcomes["wrongly_refused"] += 1
            elif any(source.arxiv_id == row.gold for source in result.sources):
                outcomes["cited"] += 1
            else:
                outcomes["answered_uncited"] += 1
            continue

        if refused_at_gate1:
            refusals["gate1"] += 1
        else:
            await asyncio.sleep(pause)
            if await judge_refusal(llm, row.question, result.answer):
                refusals["gate2"] += 1
            else:
                refusals["wrongly_answered"] += 1
                wrong.append(f"{row.id} [{row.group}] {row.question}")

    positives = sum(1 for r in rows if r.is_positive)
    negatives = len(rows) - positives
    lines = [
        heading(f"3. END-TO-END  ({positives} positives + {negatives} must-refuse, through /ask)"),
        "   positives",
        f"     answered, gold paper cited     {outcomes['cited']:>4}/{positives}",
        f"     answered, gold paper missing   {outcomes['answered_uncited']:>4}/{positives}",
        f"     wrongly refused                {outcomes['wrongly_refused']:>4}/{positives}",
        "",
        "   must-refuse",
        f"     refused at gate 1 (threshold)  {refusals['gate1']:>4}/{negatives}   deterministic, 0 LLM calls",
        f"     refused at gate 2 (prompt)     {refusals['gate2']:>4}/{negatives}   probabilistic, 1 LLM call",
        f"     ANSWERED ANYWAY                {refusals['wrongly_answered']:>4}/{negatives}   ← the number that matters",
    ]
    if wrong:
        lines += ["", "   answered anyway:"] + [f"     {w}" for w in wrong]
    lines += [
        "",
        "   Gate 2 is judged by one LLM call per negative that clears gate 1 — the system",
        "   prompt tells the model to decline in prose, so there is no sentinel to match on.",
    ]
    report.emit("\n".join(lines))
    report.data["end_to_end"] = {
        "positives": outcomes,
        "negatives": refusals,
        "answered_anyway": wrong,
    }


# --- entry point ----------------------------------------------------------------------


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--golden", type=Path, default=Path("eval/golden.jsonl"))
    parser.add_argument("--with-llm", action="store_true", help="also run report 3 through /ask")
    parser.add_argument("--save", metavar="LABEL", help="write eval/results/<LABEL>.json")
    parser.add_argument("--limit", type=int, help="score only the first N rows (smoke test)")
    parser.add_argument("--pause", type=float, default=7.5, help="seconds between LLM calls")
    args = parser.parse_args()
    configure_logging()

    if not args.golden.exists():
        print(f"{args.golden} not found — run scripts/build_golden_set.py first.")
        return 1
    meta_path = args.golden.with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else None

    rows = load_rows(args.golden)
    if args.limit:
        rows = rows[: args.limit]

    report = Report()
    report.emit(
        "\n"
        + "═" * 72
        + f"\n paper-trail evaluation · {datetime.now(UTC):%Y-%m-%d %H:%M}Z\n"
        + "═" * 72
    )

    embedder = FastEmbedEmbedder()
    async with AsyncSessionLocal() as session:
        rows = await report_coverage(session, rows, meta, report)
        probes = await probe_all(session, rows, embedder)
        report_retrieval(probes, report)
        report_sweep(probes, report)

        if args.with_llm:
            llm = OpenAICompatibleClient()
            if not llm.configured:
                print("\nLLM_API_KEY is not set — skipping report 3.")
            else:
                await report_end_to_end(session, rows, embedder, llm, args.pause, report)
                await llm.aclose()

    report.data["config"] = {
        "relevance_threshold": settings.relevance_threshold,
        "retrieval_top_k": settings.retrieval_top_k,
        "embedding_model": settings.embedding_model,
        "llm_model": settings.llm_model,
        "max_context_chars": settings.max_context_chars,
    }
    report.data["golden_set"] = {"path": str(args.golden), "meta": meta}
    report.data["ran_at"] = datetime.now(UTC).isoformat(timespec="seconds")

    if args.save:
        out = Path("eval/results") / f"{args.save}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report.data, indent=2, default=str) + "\n")
        print(f"\nsaved {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
