# anchor — agents that remember their own work

**anchor = mm + ms.** Memory management plus memory search for AI agents.

When a long-running agent hits its context limit, compaction summarizes the
history — and the summary silently drops the details: the tool the agent built
(`p3c2`), the file it wrote, the decision it made, the approach it tried and
rejected. The agent then *rebuilds things it already built*. anchor fixes this
in two halves:

- **mm (memory management)** — every raw session event lands in an append-only
  archive *before* compaction can destroy it, and every entity (tool names,
  file paths, decisions) is extracted into an index: `entity → where it's
  explained`.
- **ms (memory search)** — a dangling-reference trigger notices *"the agent
  sees `p3c2` and doesn't know what it is"* **for the agent** (agents are bad
  at noticing this themselves), then a search ladder recovers the answer with
  citations: index → workspace files → BM25 over raw history → LLM rewording
  → piece-by-piece reconstruction. Misses return a **tried-log** (the negative
  result), which the relay passes to the next searcher.

Pure **stdlib Python** — no pip dependencies to run, works offline, optional
local-Ollama for one rung. Ships as an **MCP server** so Claude Code, OpenCode
and any MCP client can use it in one config block.

## install

```
pip install anchor-memory
```

**That's the whole install.** Verify with `python -m anchor.selftest`, then wire
it into your tool (next section). Source, if you'd rather: `pip install git+https://github.com/zacharyol/anchor-memory.git`

Website & full setup guide: https://zacharyol.github.io/anchor-memory/

## quickstart

```
anchor-mcp                      # starts the MCP server (stdio)
python -m anchor.selftest       # offline end-to-end test with numbers
python -m anchor.replay         # ingest your own OpenCode session history
```

Wire it into your agent (one block, then restart the session):

```json
{ "mcpServers": { "anchor": { "command": "anchor-mcp" } } }
```

Your agent gains: `anchor_resolve` (what was p3c2?), `anchor_check` (which
names in this summary are unexplained?), `anchor_search` (BM25 over raw
history), `anchor_sessions`, `anchor_sync` (incremental ingest from the live
session database), `anchor_stats`, and `anchor_relay` (shard-searchers with
negative-result merge — the last resort before "unfindable").

## measured results (real session history, llama3.1:8B judge-free agent)

| version | change | vanilla → anchored recall | token overhead |
|---|---|---|---|
| v0.3 | bulk cards | 33% → 44% | +48% |
| v0.4 | selective card | 33% → 44% | **+5%** |
| v0.5 | junk filter, repeats (n=36) | 19% → **47%** | +5% |
| v0.6 | best-explanation cards (n=72) | 19% → **42%** | +5% |
| v0.7 | full rerun on shipped code (n=72) | 26% → 24% (wash) | +5% |

v0.7 honesty note: the card-thinning regression (docstrings replacing rich
lines) ate the recall gap — root-caused and fixed in the current source
(docstring prepends, rich lines kept). Verification rerun of the fixed code
pending; coverage still favored anchored (+5 pts) and wobble stayed ±5–6%.

Trigger fire precision on labeled real data: 58% (firing is free — junk is
filtered at the card layer). Sampling wobble ±5–6%: model randomness is small;
question difficulty dominates. Full methodology:
[docs/BENCHMARK.md](docs/BENCHMARK.md).

## how it's made

- [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md) — the architecture walk-through:
  the two memory classes, the write path, the trigger rules, every ladder rung,
  the relay, the data formats.
- [docs/BENCHMARK.md](docs/BENCHMARK.md) — how the numbers above are produced
  and how to reproduce them on your own session history.
- [DESIGN.md](anchor/DESIGN.md) — the original design doc, kept as written.

## why open source

Memory loss after compaction is the defining production failure of long
running agents ("context rot"). anchor's answer is deliberately simple:
archive raw, index entities, detect the gap automatically, search in a cost
ladder, and never trust a summary when the primary source still exists.
MIT licensed — take it, wire it, fork it.

## works with

Claude Code · OpenCode · any MCP client · local Ollama (optional, rung 2)

MIT License · repo: https://github.com/zacharyol/anchor-memory
