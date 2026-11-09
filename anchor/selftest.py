"""End-to-end offline selftest with numbers.

Simulates the whole mm+ms cycle:
  1. a session builds a tool (p3c2) — everything archived raw
  2. compaction happens; a VANILLA summary loses what p3c2 is
  3. trigger must fire on the bare mention and the ladder must resolve it
  4. our OWN compaction (manifest in the summary) must fire nothing
  5. a truly unknown entity must come back found:false with a tried-log

Run:  python -m anchor.selftest    (from the repo root)
"""
import os
import sys
import tempfile
import time

from .archive import Archive
from .index import BM25, build_entity_table
from .ladder import Resolver
from .trigger import find_dangling

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


def main():
    tmp = tempfile.mkdtemp(prefix="anchor_selftest_")
    ws = os.path.join(tmp, "ws")
    os.makedirs(os.path.join(ws, "tools"), exist_ok=True)

    # --- the world: the tool really exists on disk (class A survival) -----
    with open(os.path.join(ws, "tools", "p3c2.py"), "w", encoding="utf-8") as f:
        f.write('"""p3c2 — parses .ini config files, exposes load()."""\n'
                "def load(path):\n"
                "    cfg = {}\n"
                "    # ... parse sections ...\n"
                "    return cfg\n")

    # --- the session, archived raw BEFORE compaction (mm) ------------------
    arch = Archive(os.path.join(tmp, "archive.jsonl"))
    T = 1762700000.0
    t = lambda i: T + i * 30.0
    arch.append("message", "user: build me a small tool called p3c2 that "
                "parses .ini config files and exposes load()", task="tools", ts=t(1))
    arch.append("tool_call", "agent: wrote tools/p3c2.py — p3c2 parses .ini "
                "config files, exposes load(path)", task="tools", ts=t(2))
    arch.append("tool_result", "p3c2 self-test passed: load() returned 3 sections "
                "from sample.ini", task="tools", ts=t(3))
    arch.append("decision", "decided p3c2 stays dependency-free so it can be "
                "reused by the installer script", task="tools", ts=t(4))
    for i in range(30):  # phase 2 filler: unrelated later work
        arch.append("tool_result", f"navgrid batch {i}: regenerated l9 grid "
                    f"tiles, map export took {i} seconds", task="maps", ts=t(10 + i))

    table = build_entity_table(arch.iter())
    bm25 = BM25(list(arch.iter()))
    res = Resolver(arch, table, bm25, workspace_root=ws)  # no LLM -> rung 2 off

    print(f"archive: {arch.count()} records, {len(table)} entities indexed\n")

    # --- case 1: VANILLA compaction — summary lost what p3c2 is (ms) -------
    vanilla = ("Summary of earlier work: the session produced several map "
               "exports and a config tool named p3c2. Continue with the maps task.")
    fired = find_dangling(vanilla)
    names = [d["entity"] for d in fired]
    check("trigger fires on bare p3c2", "p3c2" in names, f"fired: {names}")

    d = next(d for d in fired if d["entity"] == "p3c2")
    r1 = res.resolve(d["entity"], hint=d["surrounding"])
    check("resolved", r1["found"], f"via {r1['via']}")
    check("resolved at rung0 (cheap path)", r1["via"] == "rung0:index", r1["via"] or "")
    check("carries file citation", bool(r1["file"]) and "p3c2.py" in r1["file"],
          r1["file"] or "none")
    check("carries what-it-does line", "config" in (r1["what"] or "").lower(),
          (r1["what"] or "")[:80])

    # --- case 2: OUR compaction — manifest in the summary → silence --------
    anchored = vanilla + ("\nManifest:\n- p3c2: Python tool that parses .ini "
                          "config files (details: archive:a000002)")
    fired2 = find_dangling(anchored)
    check("no false fire when manifest present", "p3c2" not in
          [d["entity"] for d in fired2], f"fired: {[d['entity'] for d in fired2]}")

    # --- case 3: unknown entity — honest negative result + tried log -------
    r3 = res.resolve("zzq9", hint="the helper zzq9 we discussed")
    check("unknown stays honestly unknown", r3["found"] is False)
    check("tried-log records the scans", any("files_scanned" in t and
          t.get("files_scanned", 0) > 0 for t in r3["tried"]),
          f"{len(r3['tried'])} rungs logged")

    # --- report -------------------------------------------------------------
    print("\n--- numbers ---")
    print(f"  entities indexed:            {len(table)}")
    print(f"  trigger fires (vanilla):     {len(fired)}")
    print(f"  trigger fires (manifest):    {len(fired2)}")
    print(f"  resolution rung:             {r1['via']}")
    print(f"  resolve latency:             {r1['elapsed_ms']} ms")
    print(f"  files scanned for zzq9:      "
          f"{next((t2.get('files_scanned') for t2 in r3['tried'] if 'files_scanned' in t2), 0)}")

    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed: {FAILURES}")
        sys.exit(1)
    print("\nall checks passed.")


if __name__ == "__main__":
    main()
