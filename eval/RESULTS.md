# Evaluation results

Every row below is a saved run in [`results/`](results/), reproducible with the command shown.
The dataset is [`golden.jsonl`](golden.jsonl) — 97 generated positives and 24 hand-written
must-refuse questions — built against the 2,370-paper corpus of 16 Sep 2026
([`golden.meta.json`](golden.meta.json)).

Method, composition and limitations: [README → Evaluation](../README.md#evaluation).

## Run history

| Run                                       | Change                              | Retrieval ceiling | Correct at its own `t` | False passes | Recommended `t` |
| ----------------------------------------- | ----------------------------------- | ----------------- | ---------------------- | ------------ | --------------- |
| [001](results/001-baseline.json)          | baseline — `k=5`, `t=0.71`          | 92/97             | 92/97                  | 3/24         | 0.75            |
| [002](results/002-topk-10.json)           | `k=10` — measured, **not adopted**  | 94/97             | 94/97                  | 3/24         | 0.75            |
| [003](results/003-threshold-0.75-e2e.json) | **`t=0.75` adopted** + end-to-end   | 92/97             | 92/97                  | **2/24**     | 0.75            |

Run 003 is the shipped configuration. Runs 001 and 002 record `t=0.71`, which is what was
committed when they were taken — they are the *before*, not a mistake.

```bash
uv run python scripts/evaluate.py --save 001-baseline                      # run 001
RETRIEVAL_TOP_K=10 uv run python scripts/evaluate.py --save 002-topk-10    # run 002
uv run python scripts/evaluate.py --with-llm --save 003-threshold-0.75-e2e # run 003
```

## The threshold: what each position actually costs

The whole sweep, `k=5`. `correct` is capped at the **retrieval ceiling of 92/97** — the gold
paper is outside the top-5 for five questions no matter where the gate sits, and a gate cannot
repair retrieval.

```
   t      correct        false passes (24 must-refuse)
 0.60   92/97  ████████████████  15  ███████████████
 0.66   92/97  ████████████████  11  ███████████
 0.70   92/97  ████████████████   4  ████
 0.71   92/97  ████████████████   3  ███          <- was here
 0.74   92/97  ████████████████   3  ███
 0.75   92/97  ████████████████   2  ██           <- now here
 0.78   91/97  ███████████████▌   2  ██
 0.80   87/97  ██████████████▊    0
 0.84   70/97  ███████████▊       0
 0.90   22/97  ███▊               0
```

**0.71 → 0.75 is free.** Identical correctness, one fewer wasted LLM call. The question that
stops passing is *"How does a bloom filter trade memory for false positives?"* (0.7413), which
had been matching a paper on learned request routing.

**The last two false passes are not worth clearing.** Reaching 0/24 needs `t ≥ 0.80`, which
costs five real answers. They are:

| Question                                                               | top-1  | Nearest paper                                  |
| ---------------------------------------------------------------------- | ------ | ---------------------------------------------- |
| _"What guarantees does the Raft consensus algorithm provide?"_         | 0.7992 | Algorithmic stability via ensembling           |
| _"What problem does consistent hashing solve in a distributed cache?"_ | 0.7962 | Shared KV Caching for Replicated 27B Inference |

Both are real computer science absent from this corpus, and both match papers that genuinely
share vocabulary with them. `bge-small` encodes what a text is *about*, not whether it
*contains an answer*, so this overlap is structural. Gate 2 exists for exactly this residue.

## `RETRIEVAL_TOP_K`: measured, then rejected

| | `k=5` | `k=10` |
| --- | --- | --- |
| Retrieval ceiling | 92/97 (0.948) | 94/97 (0.969) |
| Mean context | 7,042 chars | 9,418 chars (**+34%**) |
| Trimmed by `MAX_CONTEXT_CHARS` | 0 of 30 | **20 of 30** |
| Recommended `t` | 0.75 | 0.75 |

`k=10` buys two more correct answers for a third more prompt tokens on every request — and
because `fit_context` then trims two thirds of questions, the tenth paper frequently never
reaches the model at all. Note the gap between the naive number and the honest one: scoring
the threshold alone suggested `k=10` reached 0.979, but scoring through `fit_context` (what
`/ask` actually does) gives **0.969**.

Kept at 5. The point is that it is now a priced decision rather than a convention, and the
recommended threshold is unchanged at both values, so the gate choice is robust to it.

## End-to-end: does `/ask` actually behave? (run 003)

Every negative runs — they are the point, and they are nearly free, because gate 1 refuses most
of them before the model is called at all. Positives are a fixed-seed sample of 40, since their
retrieval is already measured exactly, and for nothing, in report 1.

```
   positives                                    (40 sampled of 97)
     answered, gold paper cited       37/40  ███████████████████████████████▌
     answered, gold paper missing      3/40  ██▌
     wrongly refused                   0/40

   must-refuse                                  (all 24)
     refused at gate 1 (threshold)    22/24  ████████████████████████████████▌   0 LLM calls
     refused at gate 2 (prompt)        2/24  ███                                 1 LLM call
     ANSWERED ANYWAY                   0/24
```

**Three things this establishes that nothing else could.**

**1. The two-gate design holds end to end.** Not one of the 24 out-of-corpus questions got an
answer. The sweep predicted exactly which two would clear gate 1 — Raft and consistent hashing
— and the grounded prompt refused both. Gate 1 handled the other 22 deterministically, at zero
token cost.

**2. `t=0.75` does not over-refuse.** `wrongly refused: 0/40`. The fear when raising a threshold
is that real questions start bouncing; on this sample none did.

**3. The offline ceiling is honest.** Report 2 predicts 92/97 = 94.8% of positives can be
answered with the gold paper cited. End to end on 40 sampled positives: 37/40 = 92.5%. The gap
is three questions, within sampling noise of the prediction — which means the cheap offline
sweep can be trusted to stand in for the expensive run.

`degraded_abandoned: 0` — no row was dropped to a rate limit, so every count above is behaviour
rather than infrastructure. That distinction cost a bug fix: see below.

## Method changes — two rules that were wrong

Both were caught by running the eval, which is the argument for having one.

**1. The rule was absolute; it had to be relative.** The first version recommended *"the
largest `t` keeping correctly-answered positives ≥ 0.95"*. With `k=5` the ceiling is 0.9485,
so no threshold could ever satisfy it — the sweep printed `no t meets the rule` and
recommended nothing. A threshold cannot repair retrieval, only avoid discarding it, so the
target is now **95% of the measured ceiling**.

**2. Ties broke the wrong way.** Taking the *largest* eligible `t` then recommended 0.79 —
which has the same 2/24 false passes as 0.75 and loses two more correct answers. A tie means
the negatives cannot separate those thresholds, so only the positives are left to decide, and
the lower value keeps more of them. Ties now break **downward**.

**3. A rate limit was being scored as a refusal failure.** `answer_question` degrades
gracefully when the LLM is unreachable — correct for a caller, wrong for an eval. A 429 came
back as a non-refusal with sources attached, which the report read as *"the model answered a
question it should have refused"*: contamination of the single number the report exists for.
Now retried three times with backoff, and if still failing, reported on its own line and
excluded from every count.

The first two are pinned by tests in [`tests/test_eval_set.py`](../tests/test_eval_set.py);
reverting either turns exactly one test red.

> [!NOTE]
> **Budget, because it shapes what can be measured.** Groq's free tier meters 8,000 tokens a
> minute *and* 200,000 a day. Only the per-minute figure appears in a response header — the
> daily cap surfaces solely in the body of a 429, so a run paced correctly against the header
> still dies halfway. Building the golden set costs ~100,000; run 003 costs ~85,000 at a
> measured 2,095 tokens per `/ask` call. The two fit in one day exactly once.

## What the previous method could not do

Threshold selection started as *"find a gap between three groups of scores"*
(`scripts/calibrate_threshold.py`, since deleted).

| | Before — range separation | After — labelled classification |
| --- | --- | --- |
| Data | 23 hand-written questions | 121 rows, 97 generated paper-first |
| Question asked | is there a gap? | what does each threshold cost? |
| When groups overlap | `NO SEPARABLE WINDOW`, no recommendation | a tradeoff curve, and a derived answer |
| `RETRIEVAL_TOP_K` | out of scope | measured |
| Regression detection | none | every run saved and diffable |

The overlap never went away. It stopped being a dead end and became a visible price list.
