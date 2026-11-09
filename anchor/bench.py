"""v0.3 — precision labeling + A/B benchmark on real compaction data.

Two modes:

  python -m anchor.bench --sample N     dump N random fires + their ground truth
                                        for precision labeling
  python -m anchor.bench [--sessions N] [--entities N] [--model NAME]
                                        A/B: vanilla summary vs anchored summary
                                        (auto-resolved citation cards injected),
                                        same questions, real Ollama, token counts.
"""
import json
import os
import random
import statistics
import time

from .archive import Archive
from .index import BM25, build_entity_table
from .ladder import Resolver
from .replay import DEFAULT_DB, load_opencode_db
from .trigger import find_dangling
from .ollama_client import _post

QUESTION = ("Before this summary, you were working in the same session and made "
            "or used things that are only named above. What is '{e}' and what "
            "did you do with it? One short paragraph, from memory.")


# generic names that are named but never "forgotten knowledge" (label aid)
GENERIC = {"readme.md", "license", "package.json", "opencode.json", "agents.md",
           "cmd.exe", "powershell.exe", "python.exe", "server.py", "start.bat",
           "install.bat", "index.html", "todo.md", "notes.txt", "game.log",
           "settings.json", "config.json", ".gitignore", "requirements.txt"}


def build(db, max_sessions=None):
    out = db + ".anchor.jsonl"
    if os.path.exists(out):
        os.remove(out)
    arch = Archive(out)
    n_sess, n_comp, probes = load_opencode_db(db, arch, max_sessions)
    records = list(arch.iter())
    table = build_entity_table(records)
    return arch, table, BM25(records), probes


def fire_set(summary):
    return [d["entity"] for d in find_dangling(summary)]


def make_qa(summary, table, k):
    """(entity, ground-truth line) for entities the summary names but doesn't explain."""
    qa = []
    for e in fire_set(summary):
        if e.lower() in GENERIC or e not in table:
            continue
        gt = table[e]["line"] or ""
        if len(gt) > 30:
            qa.append((e, gt))
        if len(qa) >= k:
            break
    return qa


def chat_full(model, messages, timeout=180, num_ctx=8192, retries=3, temp=0.2):
    last = None
    for attempt in range(retries):
        try:
            resp = _post("/api/chat", {
                "model": model, "messages": messages, "stream": False,
                "options": {"num_ctx": num_ctx, "temperature": temp}}, timeout)
            usage = {"prompt": resp.get("prompt_eval_count", 0),
                     "output": resp.get("eval_count", 0)}
            return resp.get("message", {}).get("content", "").strip(), usage
        except Exception as e:  # Ollama 500s / timeouts: back off and retry
            last = e
            time.sleep(5 * (attempt + 1))
    raise last


def fact_overlap(answer, ground_truth):
    """Fraction of ground-truth content words present in the answer
    (plural-folded — 'villagers' matches 'villager' — to cut metric noise)."""
    from .index import tokenize, norm_word
    gt = {norm_word(w) for w in tokenize(ground_truth) if len(w) > 3}
    if not gt:
        return 0.0
    a = {norm_word(w) for w in tokenize(answer)}
    return sum(1 for w in gt if w in a) / len(gt)


def card(entity, r):
    c = f"- {entity}: {(r['what'] or '')[:160]}"
    if r.get("file"):
        c += f" (file: {r['file']})"
    return c


MEMORY_INTRO = "\n\nYour memory of the above (from your session archive):\n"


def run_bench(sessions_n, entities_k, model, resolve_cap=12, repeats=1,
              arms=("vanilla", "selective"), title_filter=None):
    arch, table, bm25, probes = build(DEFAULT_DB, max_sessions=None)
    resolver = Resolver(arch, table, bm25, llm=None)
    random.seed(7)
    probes = random.sample(probes, min(sessions_n, len(probes)))

    rows = []
    for p in probes:
        if title_filter and title_filter.lower() not in (p["session"] or "").lower():
            continue
        summary = p["summary"]
        qa = make_qa(summary, table, entities_k)
        if not qa:
            continue
        resolver.workspace_root = p["workspace"] or resolver.workspace_root
        if "bulk" in arms:  # v0.3 behavior, kept for comparison runs
            fired = fire_set(summary)[:resolve_cap]
            bulk_cards = []
            for e in fired:
                r = resolver.resolve(e, hint="")
                if r["found"]:
                    bulk_cards.append(card(e, r))
            bulk_ctx = summary + MEMORY_INTRO + "\n".join(bulk_cards)

        for entity, gt in qa:
            r = resolver.resolve(entity, hint="")
            # ground truth = union of true memory facets (index line + the
            # resolver's evidence line/docstring) — an answer echoing any
            # genuine recovered fact scores, whichever facet it echoes
            gt_full = gt + " " + (r["what"] or "")
            # v0.5 junk filter at the card layer: skip generic names and
            # resolutions that add no real "what" line
            use_card = (r["found"] and entity.lower() not in GENERIC
                        and len(r["what"] or "") > 30)
            sel_ctx = summary + MEMORY_INTRO + (card(entity, r) if use_card else "")
            for arm in arms:
                ctx = summary if arm == "vanilla" else sel_ctx
                for rep in range(repeats):
                    t0 = time.perf_counter()
                    try:
                        ans, usage = chat_full(model, [
                            {"role": "system", "content":
                                "You are a coding agent continuing after a context "
                                "compaction. Answer only from your memory of the session."},
                            {"role": "user", "content": ctx + "\n\n" + QUESTION.format(e=entity)}])
                    except Exception as e:
                        rows.append({"session": p["session"][:40], "entity": entity,
                                     "arm": arm, "repeat": rep, "overlap": None,
                                     "tokens": 0, "latency": round(
                                         time.perf_counter() - t0, 1),
                                     "answer": f"<call failed: {e}>"})
                        continue
                    lat = time.perf_counter() - t0
                    rows.append({"session": p["session"][:40], "entity": entity,
                                 "arm": arm, "repeat": rep,
                                 "overlap": fact_overlap(ans, gt_full),
                                 "tokens": usage["prompt"] + usage["output"],
                                 "latency": round(lat, 1), "answer": ans[:200]})

    # ---------------- report ----------------
    if not rows:
        print("no QA pairs produced — nothing to score")
        return
    print(f"model: {model}   repeats: {repeats}   arms: {', '.join(arms)}")
    print(f"\nper-question (mean coverage over repeats):")
    seen = {}
    for r in rows:
        seen.setdefault((r["entity"], r["arm"]), []).append(r["overlap"])
    for (entity, arm), ovs in seen.items():
        mean = sum(ovs) / len(ovs)
        print(f"  {entity:<40} {arm:<10} {mean:>4.0%}  (n={len(ovs)})")

    base = None
    q = {}
    for r in rows:
        if r["overlap"] is not None:
            q.setdefault((r["arm"], r["session"], r["entity"]), []).append(r["overlap"])

    # persist raw rows so analysis never needs another 144 LLM calls
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "bench_results.jsonl")
    with open(out_path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(f"rows appended to {out_path}")

    base = None
    for arm in arms:
        arm_rows = [r for r in rows if r["arm"] == arm]
        if not arm_rows:
            continue
        failed = sum(1 for r in arm_rows if r["overlap"] is None)
        valid = [r for r in arm_rows if r["overlap"] is not None]
        if not valid:
            continue
        acc = sum(1 for r in valid if r["overlap"] >= 0.5) / len(valid)
        ovs = [r["overlap"] for r in valid]
        mean_overlap = sum(ovs) / len(ovs)
        std_overlap = statistics.pstdev(ovs) if len(ovs) > 1 else 0.0
        # true sampling wobble: mean of per-question std over repeats
        wobbles = [statistics.pstdev(v) for (a, _, _), v in q.items()
                   if a == arm and len(v) > 1]
        wobble = sum(wobbles) / len(wobbles) if wobbles else 0.0
        toks = sum(r["tokens"] for r in valid) / len(valid)
        if arm == "vanilla":
            base = toks
        delta = f"({(toks - base) / base:+.0%} vs vanilla)" if base else ""
        print(f"\n{arm.upper()}: recall {acc:.0%}, fact coverage "
              f"{mean_overlap:.0%} ± {std_overlap:.0%} pooled, "
              f"sampling wobble ±{wobble:.0%}, "
              f"{toks:.0f} tokens/question {delta}, "
              f"n={len(valid)}" + (f" ({failed} failed calls)" if failed else ""))


def run_sample(n):
    arch, table, bm25, probes = build(DEFAULT_DB)
    random.seed(7)
    fires = []
    for p in probes:
        for e in fire_set(p["summary"]):
            fires.append((e, table.get(e, {}).get("line", "")))
    random.shuffle(fires)
    print(f"{len(fires)} total fires; random sample of {n} with ground truth:\n")
    for e, line in fires[:n]:
        print(f"  {e:<28} {line[:110]}")


if __name__ == "__main__":
    import argparse
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # cp1252-safe
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--sessions", type=int, default=6)
    ap.add_argument("--entities", type=int, default=4)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--arms", default="vanilla,selective")
    ap.add_argument("--model", default="gemma4:26b")
    ap.add_argument("--title", default=None, help="only probes whose session title contains this")
    args = ap.parse_args()
    if args.sample:
        run_sample(args.sample)
    else:
        run_bench(args.sessions, args.entities, args.model,
                  repeats=args.repeats, arms=tuple(args.arms.split(",")),
                  title_filter=args.title)
