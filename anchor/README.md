# anchor — mm + ms for AI agents

Agents stop forgetting their own work. When compaction drops what "p3c2" is,
anchor notices the dangling reference automatically and resolves it — from the
workspace first, then the raw session archive — with citations, not
summaries-of-summaries. See [DESIGN.md](DESIGN.md) for the full spec and
benchmark plan.

**Live site + setup guide:** https://anchor-memory.zachary-remote.workers.dev

Pure stdlib Python 3. No pip. Ladder rung 2 (LLM rewording) uses a local
Ollama if one is running; everything else works fully offline.

## Layout

| file | role |
|---|---|
| `extract.py` | entity extraction (paths, snake/camel, digit-names like `p3c2`) |
| `archive.py` | append-only raw session archive (JSONL) — mm write path |
| `index.py` | entity table (`entity → where explained`) + BM25 over records |
| `trigger.py` | dangling-reference detection — the ms trigger |
| `ladder.py` | resolver: rung0 index/workspace → rung1 BM25 → rung2 LLM rewording → rung3 reconstruction |
| `ollama_client.py` | stdlib Ollama client (rung 2) |
| `selftest.py` | end-to-end offline check with numbers |

## Run

```
python -m anchor.selftest    # from the repo root
```

## Status

- [x] v0.1 — core cycle works offline: trigger fires on bare mentions,
      resolves at rung 0 in <1 ms, honest negative results with tried-logs
- [x] v0.2 — real-data replay: your 69 real OpenCode sessions (12.8k records,
      16.8k entities, 12 real compactions) loaded in ~5 s; trigger fired 449
      times on real post-compaction summaries, 60/60 sampled resolutions
      correct at rung 0 in <0.1 ms (`python -m anchor.replay`)
- [x] v0.3 — first A/B numbers on real data: recall 33%→56%, fact coverage
      32%→56%, trigger fire precision ~58% (labeled); token cost +48% with
      bulk injection
- [x] v0.4 — selective injection: same recall as bulk, mean fact coverage
      57% (beats bulk's 44%), token overhead +5% — target was ≤6%
- [x] v0.5 — hardened numbers (junk filter, 3× repeats, 12 questions, n=36):
      recall 19%→47% (2.5×), coverage 28%→50%, +5% tokens, 0 per-entity
      regressions; MCP server shipped with session scoping + anchor_sync
- [ ] v0.6 — relay (spawned searchers with negative-result handoff)
- [ ] v0.7 — auto-hook layer (PreCompact sync + manifest injection)
