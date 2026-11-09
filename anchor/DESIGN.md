# anchor — mm + ms for AI agents

**One line:** agents stop forgetting their own work — every reference the agent
still sees ("that tool p3c2") gets auto-detected and resolved from the workspace
and a raw session archive, with citations.

Working name: **anchor** (the trigger "anchors" dangling references).
mm = memory management (archive + index), ms = memory search (the resolver ladder).

---

## 1. Problem

When an agent's context is compacted, the summary drops entity details. The
agent still sees the name `p3c2` but no longer knows what it is, so it rebuilds
the tool or re-asks what it already answered. This is the dominant waste mode in
long-running agents ("context rot" / "the compaction cliff", 2026 literature).

Key asymmetry: **compaction only destroys memory, not the world.** The tool
file is still on disk; the reasoning that produced it is gone unless it was
archived raw. So lost knowledge splits into two classes:

| class | example | where it survives | recovery |
|---|---|---|---|
| A: hit disk | tools built, files written, paths | the workspace itself | grep / path scan — deterministic |
| B: never hit disk | why a decision was made, approaches tried and rejected, chat-only reasoning | nowhere unless archived | raw session archive written **before** compaction |

## 2. What is new here

Adjacent 2026 systems (Letta/MemGPT, mem0, Zep, Anthropic memory tool, Manus)
cover external memory and retrieval, but:

1. **Auto-trigger (dangling-reference detection).** Existing systems search when
   the agent decides to; agents are demonstrably bad at that (Sourcegraph, 2026:
   4,813 keyword calls vs 587 agentic-search calls). anchor detects the gap
   *for* the agent: an entity in context with no explanation in context.
2. **Negative-result relay.** When a search fails, the failure travels: what
   was queried, where, what was scanned — so a later retry (this agent or a
   spawned one) never repeats it.

## 3. Architecture

```
                 write path (before compaction destroys anything)
 session events ──► ARCHIVE (append-only JSONL, raw)
                        │  extract at write time
                        ├─► ENTITY INDEX:  entity → where it's explained
                        └─► compaction ALSO emits a manifest into the summary:
                            "created tool p3c2 — details: archive:a000012"

                 read path (any time, cheap)
 working context ──► TRIGGER: entities in context, unexplained in context
                        │  index lookup first (free); search agents only on miss
                        ▼
                 SEARCH LADDER ──► RESOLVER RESULT + citations
                     │
                     └─► negative result (tried-log) feeds the future relay
```

## 4. Data formats

**Archive record** (JSONL, append-only):

```json
{"id":"a000012","ts":1762700000.0,"kind":"tool_result","task":"build-config-tools",
 "compaction":0,"entities":["p3c2","tools/p3c2.py"],
 "text":"wrote tools/p3c2.py — parses .ini config files, exposes load()"}
```

**Entity index entry:**

```json
{"p3c2":{"type":"tool","line":"Python tool that parses config files",
         "explained_in":["a000012","file:tools/p3c2.py"],"first_seen":"a000010"}}
```

**Resolver result:**

```json
{"entity":"p3c2","found":true,"via":"rung0:index",
 "evidence":["a000012","file:tools/p3c2.py"],
 "what":"Python tool that parses .ini config files, exposes load()",
 "confidence":0.94}
```

**Negative result** (relay payload):

```json
{"entity":"zzq","found":false,
 "tried":[{"rung":"rung0:workspace","queries":["zzq"],"files_scanned":412},
          {"rung":"rung1:bm25","queries":["zzq"],"records_scanned":1207}]}
```

## 5. Trigger rules

An entity is **dangling** when: it appears in the working context, is not a
language keyword/common word, and has no explanation co-located in context —
no manifest entry, no defining sentence, no file path. Explained-in-context is
checked from the manifest our own compaction step emits; a *vanilla* summary
has no manifest, so its bare mentions all fire (that is the point).

Trigger resolution order: index lookup (free, resolves most cases) → workspace
→ search ladder. The trigger must be near-zero-cost or it won't stay on.

Archive citation ids (`a000012`) are anchor's own pointers — never dangling.

## 6. Search ladder (each rung only on the previous one's miss)

| rung | mechanism | cost | catches |
|---|---|---|---|
| 0 | index exact-match + workspace path/filename + content grep | free | class A: anything that hit disk or was archived |
| 1 | BM25 over archive records (top-k, entity-filtered) | free | reworded mentions in raw history |
| 2 | LLM rewording (HyDE-style: "how would I have said this?") → rerun 0+1; **max 2 attempts** | ~1 LLM call | vocabulary drift between summary and archive |
| 3 | piece-by-piece reconstruction: creation-shaped records in the task/time neighborhood of the hint terms | free | name totally gone; recover *what/why* from surroundings |

Returns citations (archive ids, file paths), never a summary-of-a-summary.
Rung 2 requires Ollama (stdlib client, offline-safe fallback). The relay
(spawned searchers over big archives) is phase 2 — it only pays off when the
archive outgrows one searcher.

## 7. Benchmark — real data and numbers

**Corpus.** 226 MB / 2,006 files of real OpenCode session logs on this machine
(`~/.local/share/opencode`), replayed; plus synthetic ground-truth sessions
(every entity/creation known exactly) for exact recall measurement.

**Method.** A/B on the same transcripts: arm A = vanilla compaction; arm B =
vanilla compaction + anchor attached. Same model, same splits, N repeats.
Compaction points come from the real logs; arm B writes the archive before
each compaction, arm A doesn't.

**Metrics.**

| metric | definition | target |
|---|---|---|
| recall-after-compaction | % of ground-truth entities resolvable after N compactions | ≥ 90% (baseline ≈ 0 for dropped ones) |
| rework rate | duplicate creations / re-reads / re-asks per session | −30% vs arm A |
| trigger precision | % of trigger fires that were genuine gaps | ≥ 90% |
| trigger recall | % of real gaps the trigger catches | ≥ 80% |
| rung distribution | % resolved at rung 0/1/2/3 (cost control: rung 2 should be rare) | rung 2 ≤ 20% of resolutions |
| net cost | (tokens added by anchor) − (tokens saved by avoided rework) | ≤ 0 |

**Gate to keep building:** rework rate down ≥ 30% at net cost ≤ 0 on both
corpora. If the number doesn't move, the idea is wrong and we say so.

### v0.6 changes and findings (in progress)

**Shipped:**
- **Best-explanation ranking** — an entity's card line now comes from the
  record that best *explains* it (decisions, compaction summaries, purpose-rich
  prose — scored by kind bonus + explanation markers), not the first mention.
  Directly targets conversation-only memory: the archive's summaries and
  decision records outrank accidental tool-JSON snippets.
- **Sampling wobble instrumentation** — the bench now reports mean per-question
  std over repeats (true model randomness, temperature-fixable) separately from
  pooled std (question difficulty, not fixable). Temperature 0.2 on all calls.
- **Stem matching in rung1** — `GameObject.Find` → `gameobject` accepted, so
  formatting variants stop causing misses.
- **Raw rows persisted** to `bench_results.jsonl` — analysis never re-pays the
  144 LLM calls.
- Single-pass index rebuild (was O(entities × records) ≈ 220M scans after the
  first v0.6 edit — caught by a selftest/debug timeout).

**Case study: `ai.py` — "found" is not "explained".** Both arms scored ~0-7%
in the first v0.6 run. Forensics: 31 archive records mention ai.py, but the
old first-mention evidence was a glob *path listing* — zero facts. The resolver
returned FOUND with a card that *looked* like memory and carried nothing —
a junk-truth card poisoning the prompt; the negative-result problem wearing a
success costume. Fix chain: best-explanation ranking promotes the compaction
summary (*"ai.py (Ollama + OpenAI-compat backends, default ...)"* — real
explanation), and file-type entities now get a workspace filename pass on
index hits (the file existed on disk the whole time; its docstring is the
ground-truth card when the index line is weak).

### v0.3/v0.4/v0.5 results (real corpus, llama3.1:8B agent)

v0.5 = junk filter at the card layer, 3× repeats, 12 questions (n=36/arm), 0 failed calls:

| metric | vanilla | selective (v0.5) |
|---|---|---|
| recall (fact coverage ≥ 50%) | 19% | **47%** (2.5×) |
| mean fact coverage | 28% ± 17% | **50% ± 23%** |
| tokens/question | 1522 | **1592 (+5%)** |
| per-entity regressions vs vanilla | — | **0 of 12** |

Per-entity wins: `ai.py` 13→80%, `In.exe` 23→64%, `logbridge.py` 10→46%,
`ai_local.py` 10→43%, `GameObject.Find` 50→83%. Ties on junk-ish entities
(`tgt.id`, `CLASSMAP.txt`) — the v0.5 junk filter removed the card-layer
regressions v0.4 showed (`doctor2.json`, `Plugin.cs`). Earlier arms:
bulk injection was +46% tokens for less recall than selective.

**Trigger fire precision (labeled, n=40):** ~58% ± 15. Junk = well-known
tools (dnSpy, de4dot, prompt_toolkit), transient diagnostics (doctor2.json,
dry.json), generic files (README.md). Firing is free (0.1 ms), so the fix is
**injection-side filtering**, not tighter firing — validated by v0.5: the
filter produced zero regressions.

Caveats: 8B agent, keyword-overlap scoring, single seed. gemma4:26b run is
optional (30+ min locally).

## 8. Build order

- [x] v0.1 (this repo): archive, extractor, index (BM25), trigger, ladder rungs 0/1/3, rung 2 via Ollama, offline selftest with numbers
- [x] v0.2: real-log replay — 69 real OpenCode sessions (12,842 records, 16,781 entities, 12 real compactions) loaded from `opencode.db` in ~5 s; trigger fired 449 times on the 12 real post-compaction summaries; 60/60 sampled resolutions succeeded at rung 0, <0.1 ms
- [ ] v0.3: A/B benchmark harness (bench/), trigger precision labeling on the 449 fires, report generator, first real numbers table
- [ ] v0.4: relay — spawned searchers over the archive with negative-result handoff
- [ ] v0.5: MCP server wrapper so Claude Code / OpenCode can attach it
