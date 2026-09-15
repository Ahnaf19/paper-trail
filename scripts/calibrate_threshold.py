"""Measure where RELEVANCE_THRESHOLD should sit, against the corpus that is actually loaded.

    uv run python scripts/calibrate_threshold.py

The threshold is the one constant in this project that cannot be reasoned about — it is a
property of the embedding model *and* the corpus, so it has to be measured. This script is
how the committed value was derived, and re-running it is how you would change it.

Method: embed each question, take the **top-1** cosine similarity against the stored
abstracts, and group the results. A usable threshold is any value that separates the
highest-scoring group we must refuse from the lowest-scoring group we must answer.

The three groups exist because the obvious test is the useless one. Separating arXiv
questions from "how do I bake sourdough" is trivial and proves nothing. The group that
decides the number is ADJACENT: real computer science that this corpus simply does not
contain -- bge-family models score that deceptively high.
"""

import asyncio
import sys
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import configure_logging
from app.db.session import AsyncSessionLocal
from app.services.embeddings import FastEmbedEmbedder
from app.services.retrieval import retrieve

# MUST ANSWER -- answerable from cs.AI / cs.CL / cs.LG abstracts.
IN_CORPUS = [
    "How can LLM agents reuse experience from earlier tasks?",
    "What techniques improve reasoning in large language models?",
    "How is retrieval-augmented generation evaluated?",
    "What are approaches to reducing hallucination in LLMs?",
    "How do multi-agent LLM systems coordinate?",
    "What methods exist for parameter-efficient fine-tuning?",
    "How is long-context handling improved in transformers?",
    "What benchmarks measure code generation ability?",
    "How does reinforcement learning from human feedback work?",
    "What are recent advances in vision-language models?",
    "How can model outputs be made more interpretable?",
    "What approaches exist for continual learning without forgetting?",
]

# MUST REFUSE -- real computer science, absent from this corpus. The decisive group.
ADJACENT_CS = [
    "What is the CAP theorem?",
    "How does a B-tree index work?",
    "How does TCP congestion control avoid collapse?",
    "What guarantees does the Raft consensus algorithm provide?",
    "How does a generational garbage collector decide what to promote?",
]

# MUST REFUSE -- nothing to do with the domain. The easy case; included as a floor.
OFF_TOPIC = [
    "What is the capital of France?",
    "How do I bake sourdough bread?",
    "Who won the 2018 football World Cup?",
    "What is the best way to train for a marathon?",
    "How do I change a car tyre?",
    "What year was the Eiffel Tower built?",
]

GROUPS = {
    "in-corpus (must answer)": IN_CORPUS,
    "adjacent CS, absent (must refuse)": ADJACENT_CS,
    "off-topic (must refuse)": OFF_TOPIC,
}


@dataclass
class Scored:
    question: str
    score: float


async def top1(session: AsyncSession, embedder: FastEmbedEmbedder, question: str) -> float:
    hits = await retrieve(session, embedder.embed_query(question), k=1)
    return hits[0].score if hits else 0.0


async def main() -> int:
    configure_logging()
    embedder = FastEmbedEmbedder()

    results: dict[str, list[Scored]] = {}
    async with AsyncSessionLocal() as session:
        if await top1(session, embedder, "anything") == 0.0:
            print("No embeddings in the corpus. Run `paper ingest` first.")
            return 1
        for label, questions in GROUPS.items():
            results[label] = [Scored(q, await top1(session, embedder, q)) for q in questions]

    for label, scored in results.items():
        print(f"\n{label}")
        for item in sorted(scored, key=lambda s: -s.score):
            print(f"  {item.score:.3f}  {item.question}")

    answer_floor = min(s.score for s in results["in-corpus (must answer)"])
    refuse_ceiling = max(
        s.score
        for label in ("adjacent CS, absent (must refuse)", "off-topic (must refuse)")
        for s in results[label]
    )

    print("\n" + "=" * 72)
    for label, scored in results.items():
        lo = min(s.score for s in scored)
        hi = max(s.score for s in scored)
        print(f"{label:<36} {lo:.3f} .. {hi:.3f}   ({len(scored)} questions)")

    print("=" * 72)
    if refuse_ceiling >= answer_floor:
        print(
            f"NO SEPARABLE WINDOW: a refusal scored {refuse_ceiling:.3f}, at or above the "
            f"lowest answerable {answer_floor:.3f}.\nNo single threshold is correct — "
            "the retrieval or the corpus needs attention, not the constant."
        )
        return 1

    print(
        f"Separable window: ({refuse_ceiling:.3f}, {answer_floor:.3f}]\n"
        f"Midpoint: {(refuse_ceiling + answer_floor) / 2:.3f}   "
        "<- set RELEVANCE_THRESHOLD near here"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
