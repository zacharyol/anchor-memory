# benchmark methodology (and how to reproduce it)

All numbers on the website and in the README come from this harness:
[`anchor/bench.py`](../anchor/bench.py). No cloud services, no paid APIs —
the judge-free agent is a local Ollama model.

## the question being measured

> After compaction destroys the details, can the agent still answer
> "what was X and what did I do with it?" — and at what token cost?

## corpus

Real session history, not synthetic chat: the operator's actual OpenCode
database (`opencode.db`, SQLite — sessions, messages, tool calls, and the 12+
real compaction events with their summaries). The replay loader
(`anchor/replay.py`) converts it into the archive format, keeping session
boundaries and compaction generations. As of v0.6: **12,991 records, 16,907
entities, 70 sessions.**

## the A/B

For each real compaction summary:

- **vanilla arm** — the agent gets ONLY the summary (what production does).
- **anchored arm** — the summary + one citation card for the entity being
  asked about (selective injection; resolved from the archive by the ladder,
  junk-filtered: generic names and explanation-free cards are dropped).

Question: *"What is '{entity}' and what did you do with it? Answer from your
memory."* Model: llama3.1:8B on local Ollama, temperature 0.2, 2–3 repeats
per question.

## scoring

- **fact coverage** — fraction of ground-truth content words (plural-folded)
  present in the answer. Ground truth = the union of the entity's true memory
  facets: the index's best-explaining record + the resolver's evidence
  (summary snippet or file docstring). An answer echoing any genuine
  recovered fact scores.
- **recall** — fraction of questions with coverage ≥ 50%.
- **token overhead** — (anchored prompt+output tokens) vs (vanilla), per
  question.
- **sampling wobble** — mean per-question std across repeats. This separates
  *model randomness* (temperature-fixable) from *question difficulty* (not):
  pooled std mixes both and reads ±17–23%, while true wobble measures ±5–6%.

## results (as shipped on the website)

| version | change | vanilla → anchored recall | token overhead |
|---|---|---|---|
| v0.3 | bulk cards (all fired entities pasted) | 33% → 44% | +48% |
| v0.4 | selective card | 33% → 44% | **+5%** |
| v0.5 | junk filter + repeats (n=36) | 19% → **47%** | +5% |
| v0.6 | best-explanation ranking (n=72) | 19% → **42%** | +5% |

Trigger fire precision (hand-labeled, n=40 random fires from real summaries):
**58% genuine gaps** — junk = well-known tools (`dnSpy`, `prompt_toolkit`),
transient diagnostics, generic files. Firing costs ~0.1 ms, so junk is
filtered at the card layer, not the trigger.

## reproduce on your own history

```
pip install anchor-memory          # or copy the anchor/ folder
python -m anchor.replay            # ingest YOUR opencode.db into the archive
python -m anchor.bench --sessions 4 --entities 3 --repeats 2 \
    --model llama3.1:latest        # A/B on your own compactions
```

Every raw answer lands in `bench_results.jsonl` — re-analyze without paying
the LLM calls again.

## known limits (stated plainly)

- 8B judge-free agent; answers are keyword-overlap scored, not human-judged.
- One seed per reported run; the wobble metric exists because of that.
- `gemma4:26b` runs are 3–4× slower locally and were sampled, not completed.
- Cross-version comparisons re-use the same seed but different question
  subsets when the session count changes — per-entity numbers wobble,
  aggregates are the signal.
