"""v0.8 — replay + incremental sync from a live OpenCode database.

Cross-platform: the OpenCode database lives at ~/.local/share/opencode/opencode.db
on every OS (OpenCode uses the XDG data home; on Windows that resolves under the
user profile). Sync is never destructive: a rebuild stages first, an empty
rebuild is refused outright, and the current archive is backed up before any swap.
"""
import json
import os
import sqlite3
import time

from .archive import Archive
from .index import BM25, build_entity_table
from .ladder import Resolver
from .trigger import find_dangling

MAX_TEXT = 8000       # cap per record — keeps the archive buildable
MAX_SUMMARY = 6000


def default_db_path():
    return os.path.join(os.path.expanduser("~"), ".local", "share",
                        "opencode", "opencode.db")


DEFAULT_DB = default_db_path()


def load_opencode_db(db_path, archive, max_sessions=None, only_new=None):
    """Append every session's raw events. Returns (sessions, compactions, probes).

    only_new: set of already-ingested source message ids — skip those (incremental sync).
    Records carry `session` (session id), `source_id` (message id), `source_ts` (ms).
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = None
    sessions = con.execute(
        "SELECT id, title, directory FROM session_v2 ORDER BY time_created").fetchall()
    if max_sessions:
        sessions = sessions[-max_sessions:]
    n_compactions, probes = 0, []

    for sid, title, directory in sessions:
        gen = 0
        rows = con.execute(
            "SELECT id, type, data FROM session_message WHERE session_id=? ORDER BY seq",
            (sid,)).fetchall()
        for mid, mtype, data in rows:
            if only_new is not None and mid in only_new:
                continue
            try:
                d = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                continue
            created_ms = (d.get("time", {}).get("created") or 0)
            ts = created_ms / 1000.0 or None
            src = {"session": sid, "source_id": mid, "source_ts": created_ms}

            if mtype == "user":
                text = (d.get("text") or "").strip()
                if text:
                    archive.append("message", f"user: {text}"[:MAX_TEXT],
                                   task=title or "", compaction=gen, ts=ts, **src)

            elif mtype == "assistant":
                for part in d.get("content", []):
                    ptype = part.get("type")
                    if ptype == "text":
                        txt = (part.get("text") or "").strip()
                        if txt:
                            archive.append("message", f"assistant: {txt}"[:MAX_TEXT],
                                           task=title or "", compaction=gen, ts=ts, **src)
                    elif ptype == "tool":
                        st = part.get("state", {})
                        inp = json.dumps(st.get("input", {}), ensure_ascii=False)
                        out = str(st.get("content", ""))[:500]
                        text = f"agent tool {part.get('name','?')}: {inp[:600]}"
                        if out:
                            text += f" -> {out}"
                        archive.append("tool_call", text[:MAX_TEXT],
                                       task=title or "", compaction=gen, ts=ts, **src)

            elif mtype == "compaction":
                gen += 1
                n_compactions += 1
                summary = (d.get("summary") or "")[:MAX_SUMMARY]
                archive.append("decision",
                               f"[COMPACTION {gen} of session '{title}'] summary: {summary}",
                               task=title or "", compaction=gen, ts=ts, **src)
                if summary:
                    ws = directory if directory and os.path.isdir(directory) else None
                    probes.append({"summary": summary, "workspace": ws,
                                   "session": title or sid})

    con.close()
    return len(sessions), n_compactions, probes


def archive_source_ids(archive):
    return {r.get("source_id") for r in archive.iter() if r.get("source_id")}


def sync(db_path, archive):
    """Incremental ONLY — never destructive (0.8.1: the rebuild path is gone).

    Appends DB messages whose source_id is not already in the archive. Manual
    memories (written via anchor_remember, no source_id) can never be
    displaced: the archive is append-only and sync only adds. A missing
    database is reported, not "rebuilt over".
    Tradeoff: archives created before 0.8 (records without source_id) cannot
    dedupe against the database — one ingest may add duplicates; manual
    memories are always preserved."""
    if not os.path.exists(db_path):
        return {"added": 0, "total": archive.count(), "staged": None,
                "rebuilt": False, "probes": [],
                "error": f"database not found: {db_path} — archive left untouched"}

    before = archive.count()
    _, n_comp, probes = load_opencode_db(db_path, archive,
                                         only_new=archive_source_ids(archive))
    return {"added": archive.count() - before, "total": archive.count(),
            "staged": None, "rebuilt": False, "probes": probes}


def probe_trigger(probes, resolver, per_summary=5):
    """Fire the trigger on real post-compaction summaries; resolve what fires."""
    results = []
    for p in probes:
        fired = find_dangling(p["summary"])
        resolved = []
        old_root = resolver.workspace_root
        resolver.workspace_root = p["workspace"]  # file lists cache per root
        for d in fired[:per_summary]:
            resolved.append(resolver.resolve(d["entity"], hint=d["surrounding"]))
        resolver.workspace_root = old_root
        results.append({"session": p["session"], "fired": fired, "resolved": resolved})
    return results


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "replay_archive.jsonl"))
    ap.add_argument("--probe", type=int, default=5, help="max resolutions per summary")
    ap.add_argument("--max-sessions", type=int, default=None)
    args = ap.parse_args()

    if os.path.exists(args.out):
        os.remove(args.out)
    arch = Archive(args.out)
    t0 = time.perf_counter()
    n_sess, n_comp, probes = load_opencode_db(args.db, arch, args.max_sessions)
    load_s = time.perf_counter() - t0

    records = list(arch.iter())
    table = build_entity_table(records)
    bm25 = BM25(records)
    resolver = Resolver(arch, table, bm25, llm=None)  # rung 2 off for the replay

    t0 = time.perf_counter()
    results = probe_trigger(probes, resolver, args.probe)
    probe_s = time.perf_counter() - t0

    # ---------------- report ----------------
    total_fired = sum(len(r["fired"]) for r in results)
    total_tried = sum(len(r["resolved"]) for r in results)
    by_rung = {}
    for r in results:
        for res in r["resolved"]:
            if res["found"]:
                by_rung[res["via"]] = by_rung.get(res["via"], 0) + 1
    lat = [res["elapsed_ms"] for r in results for res in r["resolved"]]

    print(f"corpus:      {n_sess} real sessions -> {arch.count()} archive records")
    print(f"compactions: {n_comp} real compaction events, {len(probes)} usable summaries")
    print(f"index:       {len(table)} entities extracted from real work")
    print(f"load time:   {load_s:.1f}s   probe time: {probe_s:.1f}s")
    print(f"\ntrigger on real post-compaction summaries:")
    print(f"  dangling refs fired: {total_fired}")
    print(f"  resolved (first {args.probe}/summary): {total_tried}")
    for rung, n in sorted(by_rung.items()):
        print(f"    {rung}: {n}")
    if lat:
        print(f"  resolve latency: avg {sum(lat)/len(lat):.1f} ms, max {max(lat):.1f} ms")

    print("\nsample resolutions:")
    for r in results:
        for res in r["resolved"][:2]:
            what = (res["what"] or res.get("file") or "")[:90]
            print(f"  '{res['entity']}' [{res['via'] or 'NOT FOUND'}] {what}")


if __name__ == "__main__":
    main()
