"""anchor MCP server — stdio transport, hand-rolled JSON-RPC 2.0, stdlib only.

Gives any MCP client (OpenCode, Claude Code, ...) live access to anchor over
the real session archive:

  anchor_resolve(entity, hint?, session?)  resolve a named thing -> citation card
  anchor_check(context)                    trigger: find dangling references
  anchor_search(query, k?, session?)       BM25 over the archive (session-scopable)
  anchor_sessions()                        list sessions (id, title, records)
  anchor_sync()                            incremental re-ingest from opencode.db
  anchor_stats()                           corpus stats + most-archived entities

stdout carries ONLY protocol JSON; everything else goes to stderr.
Run:  python -m anchor.mcp_server
"""
import json
import os
import sys

from .archive import Archive
from .index import BM25, build_entity_table
from .ladder import Resolver
from .trigger import find_dangling
from .replay import DEFAULT_DB, sync as archive_sync

HERE = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_PATH = os.path.join(HERE, "replay_archive.jsonl")
DB_PATH = os.path.join(os.environ.get("USERPROFILE", ""),
                       ".local", "share", "opencode", "opencode.db")
STATE = {}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def rebuild_state(archive=None, records=None):
    arch = archive or STATE["arch"]
    if records is None:
        records = list(arch.iter())
    table = build_entity_table(records)
    bm25 = BM25(records)
    STATE.update(table=table, bm25=bm25, records=records,
                 resolver=Resolver(arch, table, bm25),
                 sessions=_session_list(records))


def _session_list(records):
    """session id -> {title-ish task, records, last activity}"""
    sess = {}
    for r in records:
        sid = r.get("session") or "(global)"
        s = sess.setdefault(sid, {"records": 0, "task": r.get("task", ""),
                                  "last_ts": r.get("ts") or 0})
        s["records"] += 1
        s["task"] = s["task"] or r.get("task", "")
        s["last_ts"] = max(s["last_ts"], r.get("ts") or 0)
    return sess


def build_state():
    staged = ARCHIVE_PATH + ".staged"
    if os.path.exists(staged):  # promote a staged rebuild before opening
        try:
            os.replace(staged, ARCHIVE_PATH)
            log("anchor: promoted staged rebuild")
        except PermissionError:
            log("anchor: staged rebuild present but live file is locked")
    if os.path.exists(ARCHIVE_PATH):
        arch = Archive(ARCHIVE_PATH)
        src = f"prebuilt archive ({arch.count()} records)"
    elif os.path.exists(DB_PATH):
        from .replay import load_opencode_db
        arch = Archive(ARCHIVE_PATH)
        load_opencode_db(DB_PATH, arch)
        src = f"replayed from opencode.db ({arch.count()} records)"
    else:
        arch = Archive(ARCHIVE_PATH)
        src = "empty archive (no data found)"
    STATE.update(arch=arch)
    rebuild_state()
    log(f"anchor: {src}, {len(STATE['table'])} entities indexed, "
        f"{len(STATE['sessions'])} sessions")


def _resolve_session(session):
    """Accept full id, id prefix, or task/title substring -> canonical id."""
    if not session or session in STATE["sessions"]:
        return session
    low = session.lower()
    for sid in STATE["sessions"]:
        if sid.startswith(session) or low in (STATE["sessions"][sid]["task"] or "").lower():
            return sid
    return session  # unknown — pass through, will just match nothing


# ------------------------------------------------------------------ handlers
def h_resolve(entity, hint="", session=None):
    r = STATE["resolver"].resolve(entity, hint=hint, session=_resolve_session(session))
    if not r["found"]:
        tried = "; ".join(t.get("rung", "?") for t in r["tried"])
        return (f"NOT FOUND: '{entity}' — searched {tried}.\n"
                f"Negative result (relay payload):\n{json.dumps(r['tried'], indent=1)}")
    lines = [f"FOUND via {r['via']} (confidence {r['confidence']:.2f})",
             f"what: {r['what']}"]
    if r["file"]:
        lines.append(f"file: {r['file']}")
    ids = [e["archive"] for e in r["evidence"] if e.get("archive")]
    if ids:
        lines.append(f"evidence: archive records {ids}")
    return "\n".join(lines) + "\n\nCitation card:\n" + \
        f"- {entity}: {r['what']}" + (f" (file: {r['file']})" if r["file"] else "")


def h_check(context):
    fired = find_dangling(context)
    if not fired:
        return "no dangling references — everything mentioned is explained."
    out = [f"{len(fired)} dangling reference(s):"]
    for d in fired[:25]:
        out.append(f"  {d['entity']:<28} ...{d['surrounding'][:90]}...")
    if len(fired) > 25:
        out.append(f"  ... and {len(fired) - 25} more")
    return "\n".join(out)


def h_search(query, k=8, session=None):
    if session:
        sid = _resolve_session(session)
        s = STATE["sessions"].get(sid)
        if not s:
            return f"no session matches '{session}'. Try anchor_sessions."
        recs = [r for r in STATE["records"] if r.get("session") == sid]
        if not recs:
            return f"session '{sid}' has no records — try anchor_sync."
        hits = BM25(recs).search(query, k=min(k, 20))
        scope = f"session {sid[:24]} ({s['task'] or 'untitled'}, {s['records']} records)"
    else:
        hits = STATE["bm25"].search(query, k=min(k, 20))
        scope = "ALL sessions (pass session= to scope)"
    if not hits:
        return f"no records matched in {scope}."
    out = [f"scope: {scope}"]
    for score, rec in hits:
        out.append(f"[{rec['id']} {rec['kind']} score={score:.2f}] {rec['text'][:160]}")
    return "\n".join(out)


def h_sessions():
    sess = sorted(STATE["sessions"].items(), key=lambda kv: -kv[1]["last_ts"])
    out = [f"{len(sess)} sessions, newest first:"]
    for sid, s in sess[:25]:
        out.append(f"  {sid[:28]:<28} {s['records']:>5} recs  "
                   f"{(s['task'] or 'untitled')[:50]}")
    return "\n".join(out)


def h_sync():
    before = STATE["arch"].count()
    result = archive_sync(DEFAULT_DB, STATE["arch"])
    if result["staged"]:
        # live file is locked by another server — serve the staged data NOW,
        # the file swap happens automatically on next server start
        with open(result["staged"], "r", encoding="utf-8") as f:
            records = [json.loads(line) for line in f if line.strip()]
        rebuild_state(records=records)
        total, note = len(records), " — full rebuild staged (live file locked); serving staged data now, swap on next start"
    else:
        rebuild_state()
        total, note = result["total"], ""
    return (f"sync: {before} -> {total} records "
            f"({', FULL REBUILD' if result['rebuilt'] else 'incremental'}), "
            f"{len(STATE['table'])} entities, {len(STATE['sessions'])} sessions"
            f"{note}")


def h_relay(entity, hint="", shards=4):
    r = STATE["resolver"].relay(entity, hint=hint, shards=min(max(shards, 2), 16))
    if not r["found"]:
        return (f"RELAY MISS: '{entity}' — {r['tried'][0]['shards']} shard-searchers "
                f"all came up empty; merged negative result:\n{json.dumps(r['tried'], indent=1)}")
    return (f"RELAY HIT via {r['via']} ({r['note']}, confidence {r['confidence']:.2f})\n"
            f"what: {r['what']}\n"
            f"evidence: archive records {r['evidence'][0]['archive']}\n\n"
            f"Citation card:\n- {entity}: {r['what']}")


def h_stats():
    table = STATE["table"]
    top = sorted(table.items(), key=lambda kv: -len(kv[1]["explained_in"]))[:15]
    return (f"archive records: {STATE['arch'].count()}\n"
            f"entities indexed: {len(table)}\n"
            f"sessions: {len(STATE['sessions'])}\n"
            f"most-archived entities:\n" +
            "\n".join(f"  {e:<28} {len(v['explained_in'])} mentions"
                      for e, v in top))


TOOLS = {
    "anchor_resolve": {
        "description": ("Resolve a named thing (tool, file, command) the agent "
                        "mentioned but no longer remembers. Searches the raw "
                        "session archive and workspace. Returns a citation card. "
                        "Pass session to prefer the current session's evidence."),
        "inputSchema": {"type": "object", "properties": {
            "entity": {"type": "string", "description": "the name to resolve, e.g. p3c2"},
            "hint": {"type": "string", "description": "surrounding context text"},
            "session": {"type": "string", "description": "session id/prefix/title to scope evidence"}},
            "required": ["entity"]},
        "handler": h_resolve,
    },
    "anchor_check": {
        "description": ("Run the dangling-reference trigger on a block of text "
                        "(e.g. a post-compaction summary). Lists every name that "
                        "is mentioned but not explained."),
        "inputSchema": {"type": "object", "properties": {
            "context": {"type": "string", "description": "the text to check"}},
            "required": ["context"]},
        "handler": h_check,
    },
    "anchor_search": {
        "description": ("BM25 search over the raw session archive. Use session= "
                        "(id, prefix, or title substring) to search one session — "
                        "run anchor_sync first to ingest the latest messages."),
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string"},
            "k": {"type": "integer", "description": "max hits (default 8)"},
            "session": {"type": "string", "description": "session id/prefix/title to scope"}},
            "required": ["query"]},
        "handler": h_search,
    },
    "anchor_sessions": {
        "description": ("List archived sessions (id, record count, title). Use a "
                        "session id to scope anchor_search / anchor_resolve."),
        "inputSchema": {"type": "object", "properties": {}},
        "handler": h_sessions,
    },
    "anchor_sync": {
        "description": ("Incrementally re-ingest new messages from the live "
                        "opencode database (including the current session). Run "
                        "this before searching for very recent things."),
        "inputSchema": {"type": "object", "properties": {}},
        "handler": h_sync,
    },
    "anchor_relay": {
        "description": ("The relay: spins up shard-searcher threads over the "
                        "whole archive, each searching its slice; a hit anywhere "
                        "wins with its citation, and every miss passes its "
                        "negative result into one merged tried-log. Use when a "
                        "straight resolve misses — the last resort before "
                        "declaring something unfindable."),
        "inputSchema": {"type": "object", "properties": {
            "entity": {"type": "string"},
            "hint": {"type": "string"},
            "shards": {"type": "integer", "description": "searcher count (default 4)"}},
            "required": ["entity"]},
        "handler": h_relay,
    },
    "anchor_stats": {
        "description": "Archive size, entity count, most-archived entities.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": h_stats,
    },
}


def handle(msg):
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": (msg.get("params") or {}).get(
                "protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "anchor", "version": "0.7.0"}}}
    if method is None or mid is None:      # notification — no response
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [
            {"name": n, "description": t["description"],
             "inputSchema": t["inputSchema"]} for n, t in TOOLS.items()]}}
    if method == "tools/call":
        params = msg.get("params") or {}
        name, args = params.get("name"), params.get("arguments") or {}
        try:
            text = str(TOOLS[name]["handler"](**args))
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": text}]}}
        except KeyError:
            if name in TOOLS:
                return {"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": f"missing argument for {name}"}],
                    "isError": True}}
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": f"unknown tool: {name}"}],
                "isError": True}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": f"error: {e}"}],
                "isError": True}}
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601,
            "message": f"method not found: {method}"}}


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    log("anchor: loading archive...")
    build_state()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log("anchor: skipping malformed line")
            continue
        out = handle(msg)
        if out is not None:
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
