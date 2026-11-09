# how anchor is made

This is the build walkthrough: what each file does, why the design is shaped
the way it is, and the reasoning behind every mechanism. The original design
doc lives at [../anchor/DESIGN.md](../anchor/DESIGN.md); this page is the
narrated version for contributors.

## the problem, precisely

When an agent's context window fills, the framework *compacts*: it summarizes
the conversation and continues from the summary. The summary is lossy — it
keeps the plot, drops the nouns. The nouns are the tools the agent built
(`p3c2`), the paths it wrote, the approach it rejected and *why*. The agent
then sees a bare name it no longer recognizes — and its default behavior is
the expensive one: rebuild the tool, re-read the files, re-derive the decision.

Key asymmetry: **compaction destroys memory, not the world.** The tool file is
still on disk. The reasoning is gone — unless it was archived. That splits all
lost knowledge into two classes:

| class | example | survives in | recovery |
|---|---|---|---|
| A: hit disk | tools built, files written | the workspace itself | filenames + docstrings — deterministic |
| B: conversation-only | decisions, rejected approaches, chat reasoning | nowhere — unless archived raw *before* compaction | the archive |

Any fix that only searches summaries is doomed: **search can't recover what
summarization deleted.** The archive must be written before compaction runs.

## the files

| file | role |
|---|---|
| `extract.py` | entity extraction — file paths, snake_case, camelCase, UPPER_SNAKE, digit-names (`p3c2`), "tool X" patterns. Filters generic words and JSON-escape artifacts (`\nName` → `nName`). |
| `archive.py` | append-only JSONL. Each record: id, ts, kind (message/tool_call/tool_result/decision), task, compaction generation, extracted entities, text. Never rewritten. |
| `index.py` | the entity table (`entity → best-explaining record`) + BM25 over all records. The line for an entity comes from the record that best EXPLAINS it — decisions and compaction summaries outrank bare tool-JSON mentions ("found" is not "explained"). |
| `trigger.py` | dangling-reference detection: an entity appears in the working context but is not explained *in* it (no manifest line, no defining sentence). This is the auto-trigger — the system notices the gap so the agent doesn't have to. |
| `ladder.py` | the search ladder + relay (below). Returns citations (archive ids, file paths), never a summary-of-a-summary. |
| `replay.py` | ingests a live OpenCode database (`opencode.db`, SQLite) into the archive — incremental sync by message id. |
| `bench.py` | the A/B benchmark harness (see BENCHMARK.md). |
| `mcp_server.py` | hand-rolled MCP server (JSON-RPC 2.0 over stdio, stdlib only) exposing the tools. |

## the trigger

Cheap by design — it must be, or agents won't keep it on:

1. Extract entities from the working context.
2. An entity is **dangling** if the context mentions it but doesn't explain it:
   no manifest entry (`- p3c2: Python tool that...`), no definition-shaped line
   (`p3c2 is ...`), no docstring block.
3. Dangling → resolve. Resolution order: index lookup (free, resolves most),
   then the ladder.

On a real 13k-record archive this costs ~0.1 ms per resolution.

## the search ladder

Each rung runs only on the previous rung's miss:

| rung | mechanism | cost | catches |
|---|---|---|---|
| 0 | index exact match; for file-type entities, workspace filename pass + the file's own docstring | free | class A + anything archived |
| 1 | BM25 over raw records — session-scoped first (in-session evidence outranks global history), stem matching (`GameObject.Find` → `gameobject`) | free | reworded mentions |
| 2 | LLM rewording, HyDE-style: "how would I have said it?" → re-run rungs 0–1. **Bounded: 2 attempts.** | ~1 call | vocabulary drift |
| 3 | piece-by-piece: creation-shaped records in the task/time neighborhood of the hint | free | the name is totally gone — recover what/why from surroundings |

Every answer carries citations. Every miss carries a **tried-log**: what was
queried, where, how much was scanned.

## the relay

`anchor_relay` — the spun-off searchers. The archive splits into shards; each
shard-searcher runs its own BM25 + stem search over only its slice; a hit in
any shard wins and carries its citation; **every miss passes its negative
result into one merged tried-log.** The negative-result handoff is the point:
a later searcher never repeats failed queries. (Deterministic thread-based
searchers today — LLM subagent searchers slot into the same contract.)

## why best-explanation ranking exists

The `ai.py` case study: 31 archive records mentioned it, but the first-mention
evidence was a glob *path listing* — the resolver returned FOUND and served a
card that looked like memory and carried zero facts. That is worse than an
honest miss: **a junk-truth card poisons the prompt.** So the index scores
records by how much they *explain* (kind bonuses for decisions and compaction
summaries, explanation-marker density, early mentions) and serves the
best-explaining record. For file-type entities, the file's own docstring is
ground truth — read from disk when the line is weak.

## the MCP server

Hand-rolled JSON-RPC 2.0 over stdio (newline-delimited), stdlib only — no MCP
SDK dependency. Tools: `anchor_resolve`, `anchor_check`, `anchor_search`,
`anchor_sessions`, `anchor_sync`, `anchor_relay`, `anchor_stats`. stdout
carries protocol JSON only; diagnostics go to stderr. Any MCP client (Claude
Code, OpenCode, ...) attaches it with a one-line config.

## what is not solved (yet)

- **The auto-hook layer**: compaction hooks (`PreCompact` → sync + manifest)
  so the write path needs no agent cooperation. Currently sync is a tool the
  agent or operator calls.
- **LLM-driven relay searchers** over huge archives (the deterministic relay
  is the core; subagents are the scale-out).
- **Fire-precision beyond 58%**: better junk-entity filtering (well-known
  tools, transient diagnostics) at the *fire* layer rather than the card layer.
