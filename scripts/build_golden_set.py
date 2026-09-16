"""Build the evaluation golden set from the corpus that is actually loaded.

    uv run python scripts/build_golden_set.py [--per-band 40 35 25] [--out eval/golden.jsonl]
    uv run python scripts/build_golden_set.py --dry-run      # stratification only, no LLM

Two kinds of row, because the set answers two different questions:

  POSITIVES  question -> the one paper that answers it. Generated paper-first: a paper is
             sampled, then an LLM writes a question for it. Never the other way round --
             labelling a question with whatever retrieval returned would make recall@k
             1.0 by construction.

  NEGATIVES  questions the corpus must refuse. Hand-written and kept in this file, because
             they carry no vocabulary leakage and because "generate questions about things
             absent from this corpus" reliably generates things that are present.

Sampling is stratified by how common a paper's rarest category is. Five categories hold most
of this corpus, so a uniform sample would only test retrieval where it is densest.
"""

import argparse
import asyncio
import json
import random
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import LLMUnavailableError
from app.core.logging import configure_logging
from app.db.session import AsyncSessionLocal
from app.services.llm import OpenAICompatibleClient

SEED = 20260916  # fixed, so the same corpus yields the same set

# Groq's free tier meters TOKENS, not requests: `x-ratelimit-limit-tokens` is 8000 per minute,
# while `x-ratelimit-limit-requests` is 1000 per day and never binds here. One generation against
# the longest abstract in the corpus measured 999 total tokens -- 674 prompt + 325 completion, of
# which 300 are gpt-oss-20b's reasoning tokens. 8000 / 1000 = 8 calls a minute. Raise it with
# --pause 0 on a paid tier.
PAUSE_SECONDS = 7.5

# Band thresholds: how many papers carry a category before it stops being rare.
BIG_MIN = 100
MID_MIN = 10

SYSTEM = """\
You write evaluation questions for a paper search system.

Given one arXiv abstract, write ONE question that a researcher would plausibly ask and that
THIS abstract answers.

Rules:
- Paraphrase. Do not reuse the title's wording or the abstract's distinctive phrases.
- Ask about the idea, not the paper: never write "in this paper" or name the method's acronym.
- One sentence, under 25 words, ending in a question mark.
- Output the question and nothing else.\
"""

# Must refuse. Real computer science this corpus does not contain -- the group that decides
# where the threshold sits, because bge scores it deceptively high.
ADJACENT_CS = [
    "What is the CAP theorem?",
    "How does a B-tree index work?",
    "How does TCP congestion control avoid collapse?",
    "What guarantees does the Raft consensus algorithm provide?",
    "How does a generational garbage collector decide what to promote?",
    "What is the difference between optimistic and pessimistic locking?",
    "How does a bloom filter trade memory for false positives?",
    "What does the two-phase commit protocol do when a participant times out?",
    "How does copy-on-write work in a filesystem snapshot?",
    "What problem does consistent hashing solve in a distributed cache?",
    "How does TLS certificate pinning defend against a rogue CA?",
    "What is the difference between a mutex and a semaphore?",
    "How does a write-ahead log make a database crash-safe?",
    "What makes a hash function suitable for password storage?",
]

# Must refuse. Nothing to do with the domain -- the easy case, included as a floor.
OFF_TOPIC = [
    "What is the capital of France?",
    "How do I bake sourdough bread?",
    "Who won the 2018 football World Cup?",
    "What is the best way to train for a marathon?",
    "How do I change a car tyre?",
    "What year was the Eiffel Tower built?",
    "What should I feed a six-month-old kitten?",
    "How do I remove a red wine stain from a carpet?",
    "When is the best time to prune an apple tree?",
    "What is the offside rule in football?",
]


async def corpus_fingerprint(session: AsyncSession) -> dict[str, object]:
    """What the set was built against. A golden set is only meaningful on its own corpus —
    recording this lets the evaluator say so instead of quietly scoring a mismatch."""
    row = (
        await session.execute(
            text("""
            select (select count(*) from paper)     papers,
                   (select count(*) from embedding) embeddings,
                   (select max(updated) from paper) newest
        """)
        )
    ).one()
    return {
        "papers": row.papers,
        "embeddings": row.embeddings,
        "newest_updated": row.newest.isoformat() if row.newest else None,
    }


@dataclass
class Candidate:
    arxiv_id: str
    title: str
    abstract: str
    band: str
    rarest_category: str


async def sample_papers(session: AsyncSession, per_band: dict[str, int]) -> list[Candidate]:
    """Stratify by the paper's RAREST category, so a paper living in a thin corner counts as thin."""
    rows = (
        await session.execute(
            text("""
            with cat_size as (
                select cat.id, cat.code, count(distinct pc.paper_id) n
                from category cat join paper_category pc on pc.category_id = cat.id
                group by cat.id, cat.code),
            rarest as (
                select distinct on (pc.paper_id)
                       pc.paper_id, cs.code, cs.n
                from paper_category pc join cat_size cs on cs.id = pc.category_id
                order by pc.paper_id, cs.n asc, cs.code)
            select p.arxiv_id, p.title, p.abstract, r.code, r.n
            from paper p join rarest r on r.paper_id = p.id
            join embedding e on e.paper_id = p.id
        """)
        )
    ).all()

    pools: dict[str, list[Candidate]] = {"big": [], "mid": [], "thin": []}
    for arxiv_id, title, abstract, code, n in rows:
        band = "big" if n >= BIG_MIN else "mid" if n >= MID_MIN else "thin"
        pools[band].append(Candidate(arxiv_id, title, abstract, band, code))

    rng = random.Random(SEED)  # noqa: S311 -- sampling an eval set, not generating secrets
    picked: list[Candidate] = []
    for band, want in per_band.items():
        pool = sorted(pools[band], key=lambda c: c.arxiv_id)  # deterministic before shuffling
        if len(pool) < want:
            logger.warning("band {} has only {} papers; wanted {}", band, len(pool), want)
        picked.extend(rng.sample(pool, min(want, len(pool))))
    return picked


def _leaks_title(question: str, title: str, run: int = 5) -> bool:
    """Reject a question that lifts `run` consecutive words from the title."""
    words = re.findall(r"\w+", title.lower())
    q = " ".join(re.findall(r"\w+", question.lower()))
    return any(" ".join(words[i : i + run]) in q for i in range(len(words) - run + 1))


async def write_question(llm: OpenAICompatibleClient, paper: Candidate) -> str | None:
    """One question per paper. Three attempts: the model parrots a title often enough, and a
    free-tier 429 partway through 100 sequential calls is transient, not a reason to drop
    the paper from the set."""
    prompt = f"Title: {paper.title}\n\nAbstract: {paper.abstract}"
    for attempt in (1, 2, 3):
        try:
            question = (await llm.complete(SYSTEM, prompt)).strip().strip('"')
        except LLMUnavailableError as exc:
            logger.warning("{} attempt {}: {}", paper.arxiv_id, attempt, exc)
            await asyncio.sleep(20 * attempt)  # longer than the token bucket's reset window
            continue
        if not question.endswith("?") or len(question) > 250:
            logger.warning("{} attempt {}: malformed question", paper.arxiv_id, attempt)
            continue
        if _leaks_title(question, paper.title):
            logger.warning("{} attempt {}: copies the title", paper.arxiv_id, attempt)
            continue
        return question
    return None


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--per-band", nargs=3, type=int, default=[40, 35, 25], metavar=("BIG", "MID", "THIN")
    )
    parser.add_argument("--out", type=Path, default=Path("eval/golden.jsonl"))
    parser.add_argument(
        "--dry-run", action="store_true", help="show the stratification; make no LLM calls"
    )
    parser.add_argument(
        "--pause", type=float, default=PAUSE_SECONDS, help="seconds between LLM calls"
    )
    args = parser.parse_args()
    configure_logging()

    per_band = dict(zip(("big", "mid", "thin"), args.per_band, strict=True))
    async with AsyncSessionLocal() as session:
        fingerprint = await corpus_fingerprint(session)
        papers = await sample_papers(session, per_band)

    if not papers:
        print("No embedded papers in the corpus. Run `paper ingest` first.")
        return 1

    logger.info(
        "corpus: {} papers, newest {}", fingerprint["papers"], fingerprint["newest_updated"]
    )
    if args.dry_run:
        print(f"\nwould generate {len(papers)} positives from this corpus:")
        for band in ("big", "mid", "thin"):
            chosen = [p for p in papers if p.band == band]
            spread = len({p.rarest_category for p in chosen})
            print(f"  {band:<5} {len(chosen):>3} papers across {spread:>3} distinct categories")
        print(f"  + {len(ADJACENT_CS)} adjacent-CS and {len(OFF_TOPIC)} off-topic negatives")
        return 0

    llm = OpenAICompatibleClient()
    if not llm.configured:
        print("LLM_API_KEY is required to generate questions (or pass --dry-run).")
        return 1

    logger.info(
        "generating {} questions at {}s apart — about {:.0f} minutes",
        len(papers),
        args.pause,
        len(papers) * args.pause / 60,
    )
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    dropped = 0
    for index, paper in enumerate(papers, start=1):
        if index > 1:
            await asyncio.sleep(args.pause)
        question = await write_question(llm, paper)
        if question is None or question.lower() in seen:
            dropped += 1
            continue
        seen.add(question.lower())
        rows.append(
            {
                "id": f"p{len(rows) + 1:03d}",
                "question": question,
                "expects": "answer",
                "relevant": [paper.arxiv_id],
                "band": paper.band,
                "source_category": paper.rarest_category,
            }
        )
        if index % 10 == 0:
            logger.info("generated {}/{}", index, len(papers))
    await llm.aclose()

    positives = len(rows)
    for group, questions in (("adjacent_cs", ADJACENT_CS), ("off_topic", OFF_TOPIC)):
        for question in questions:
            rows.append(
                {
                    "id": f"n{len(rows) - positives + 1:03d}",
                    "question": question,
                    "expects": "refuse",
                    "relevant": [],
                    "group": group,
                }
            )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    meta = args.out.with_suffix(".meta.json")
    meta.write_text(
        json.dumps(
            {
                "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "seed": SEED,
                "generator_model": settings.llm_model,
                "corpus": fingerprint,
                "positives": positives,
                "negatives": len(rows) - positives,
                "dropped": dropped,
            },
            indent=2,
        )
        + "\n"
    )

    print(f"\nwrote {len(rows)} rows to {args.out}  (+ {meta.name})")
    for band in ("big", "mid", "thin"):
        print(f"  positives, {band:<12} {sum(1 for r in rows if r.get('band') == band)}")
    for group in ("adjacent_cs", "off_topic"):
        print(f"  negatives, {group:<12} {sum(1 for r in rows if r.get('group') == group)}")
    if dropped:
        print(f"  dropped                {dropped}  (unusable or duplicate generations)")
    print(f"\ncorpus: {fingerprint['papers']} papers, newest {fingerprint['newest_updated']}")
    print(f"model:  {settings.llm_model} · seed: {SEED}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
