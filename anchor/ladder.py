"""Ladder — the ms search. Each rung runs only on the previous rung's miss.

  rung0  index exact-match + workspace filename/content grep   (free)
  rung1  BM25 over raw archive records                          (free)
  rung2  LLM rewording (HyDE-style) -> rerun 0+1, max 2 tries   (~1 LLM call)
  rung3  piece-by-piece reconstruction from creation-shaped
         records in the task/time neighborhood                  (free)

Every answer carries citations (archive ids / file paths). Every miss carries
the tried-log — the negative result the relay will pass down the chain later.
"""
import os
import re
import time

from . import extract
from .index import BM25, tokenize
from .ollama_client import ping as ollama_ping, chat


def stem_of(name):
    """First alphanumeric run of a name, lowercased: 'GameObject.Find' ->
    'gameobject', 'tools/p3c2.py' -> 'tools' — callers pair it with the
    next run when needed. Used to tolerate formatting variants."""
    parts = re.split(r"[^a-z0-9]+", name.lower())
    return max(parts, key=len) if parts else name.lower()

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist",
             "build", ".opencode", ".idea", ".vscode"}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".mp3", ".wav", ".mp4", ".exe",
            ".dll", ".pdb", ".so", ".zip", ".gz", ".tar", ".7z", ".bin",
            ".woff", ".woff2", ".ttf", ".pdf", ".lock"}
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_FILES = 5000
MAX_GREP_FILES = 200  # content-grep bound: misses cost at most this many reads

CREATION_WORDS = ("wrote", "created", "made", "added", "built", "implemented")


class Resolver:
    def __init__(self, archive, entity_table, bm25, workspace_root=None, llm=None):
        self.archive = archive
        self.table = entity_table
        self.bm25 = bm25
        self.workspace_root = workspace_root
        self.llm = llm  # {"model": "..."} — enables rung 2
        self._files = None
        self._files_by_root = {}  # workspace root -> cached file list
        self._rec_by_id = {r["id"]: r for r in archive.iter()}
        self._sess_bm25 = {}  # session id -> BM25 over that session's records

    # ------------------------------------------------------------ workspace
    def _workspace_files(self):
        root = self.workspace_root
        if not root:
            return []
        if root in self._files_by_root:
            return self._files_by_root[root]
        files = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                if len(files) >= MAX_FILES:
                    break
                p = os.path.join(dirpath, fn)
                ext = os.path.splitext(fn)[1].lower()
                if ext in SKIP_EXT:
                    continue
                try:
                    if os.path.getsize(p) > MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                files.append(p)
        self._files_by_root[root] = files
        return files

    def _read(self, path):
        try:
            with open(path, "rb") as f:
                head = f.read(1024)
                if b"\0" in head:  # binary
                    return None
                with open(path, "r", encoding="utf-8", errors="replace") as f2:
                    return f2.read(MAX_FILE_BYTES)
        except OSError:
            return None

    # --------------------------------------------------------------- rungs
    def _line_is_junk(self, line):
        """A path dump or fragment — long, but explains nothing."""
        if not line:
            return True
        low = line.lower()
        from .index import EXPLAIN_MARKERS
        if any(m in low for m in EXPLAIN_MARKERS):
            return False
        seps = sum(line.count(c) for c in "\\/")
        return seps >= 3 or len(line) - len(line.replace(".", "")) >= 6

    def _rung0(self, entity, hint, tried):
        queries = [entity]
        if entity in self.table:
            entry = self.table[entity]
            file_hit = self._file_from_evidence(entity, entry)
            if not file_hit and entry["type"] == "file":
                # the thing may still exist on disk — index hit must not hide it
                file_hit = self._workspace_hit(entity, name_only=True)
            what = entry["line"]
            junk = self._line_is_junk(what)
            if file_hit and (entry["type"] == "file" or junk or
                             len(entry["line"] or "") < 60):
                # weak or junky index line — let the file describe itself
                text = self._read(file_hit) or ""
                line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
                if len(line) > 10 and not self._line_is_junk(line):
                    if len(entry["line"] or "") < 60:
                        what = line[:200]                    # replace: nothing to keep
                    elif line[:40] not in (entry["line"] or ""):
                        what = (line[:110] + " | " + (entry["line"] or ""))[:200]
                    junk = False
            res = {"found": True, "via": "rung0:index",
                   "evidence": [{"archive": entry["explained_in"][:3]}],
                   "what": what, "confidence": 0.4 if junk else 0.95,
                   "file": file_hit}
            if junk:
                res["note"] = "mention recovered but no explanation found"
            return res
        # true miss: now (and only now) touch the workspace
        file_hit = self._workspace_hit(entity)
        if file_hit:
            text = self._read(file_hit) or ""
            line = next((ln.strip() for ln in text.splitlines() if entity in ln),
                        text.splitlines()[0] if text else "")
            return {"found": True, "via": "rung0:workspace", "evidence": [],
                    "what": line[:200], "confidence": 0.9, "file": file_hit}
        tried.append({"rung": "rung0", "queries": queries, "files_scanned":
                      len(self._workspace_files()), "records_scanned": 0})
        return None

    def _file_from_evidence(self, entity, entry):
        """File citation from the archive records themselves — no disk walk."""
        for rec_id in entry["explained_in"][:5]:
            rec = self._rec_by_id.get(rec_id)
            if not rec:
                continue
            for e2 in rec.get("entities", []):
                if e2 != entity and entity.lower() in e2.lower() and \
                        ("/" in e2 or "\\" in e2 or "." in e2):
                    return e2
        return None

    def _workspace_hit(self, entity, name_only=False):
        files = self._workspace_files()
        base = entity.rsplit(".", 1)[0].rsplit("/", 1)[-1]
        # pass 1: filename match
        for p in files:
            fn = os.path.basename(p)
            if entity in fn or (base and len(base) > 2 and base in fn):
                return p
        if name_only:
            return None
        # pass 2: content grep, bounded — first file containing the literal
        for p in files[:MAX_GREP_FILES]:
            text = self._read(p)
            if text and entity in text:
                return p
        return None

    def _session_bm25(self, session):
        """BM25 restricted to one session's records (cached). None if empty."""
        if session not in self._sess_bm25:
            recs = [r for r in self._rec_by_id.values()
                    if r.get("session") == session]
            self._sess_bm25[session] = BM25(recs) if recs else None
        return self._sess_bm25[session]

    def _rung1(self, entity, hint, tried, session=None):
        queries = [entity + " " + hint]
        scanned = len(self.bm25.recs)

        def hit(rec_text_low):
            if entity.lower() in rec_text_low:
                return True
            stem = stem_of(entity)          # formatting-variant tolerance
            return len(stem) >= 4 and stem in rec_text_low

        found = None
        if session:  # current-session evidence outranks global history
            sess = self._session_bm25(session)
            if sess:
                for score, rec in sess.search(queries[0], k=8):
                    if hit(rec["text"].lower()):
                        found = {"found": True, "via": "rung1:bm25@session",
                                 "evidence": [{"archive": [rec["id"]]}],
                                 "what": rec["text"][:200],
                                 "confidence": min(0.9, 0.5 + score / 10),
                                 "file": None}
                        break
                scanned = len(sess.recs)
        if not found:
            for score, rec in self.bm25.search(queries[0], k=8):
                if hit(rec["text"].lower()):
                    found = {"found": True, "via": "rung1:bm25",
                             "evidence": [{"archive": [rec["id"]]}],
                             "what": rec["text"][:200],
                             "confidence": min(0.9, 0.5 + score / 10),
                             "file": None}
                    break
        if found:
            return found
        tried.append({"rung": "rung1", "queries": queries, "files_scanned": 0,
                      "records_scanned": scanned})
        return None

    def _rung2(self, entity, hint, tried):
        """LLM rewording: how would the agent itself have written about this?"""
        if not self.llm:
            tried.append({"rung": "rung2", "skipped": "no LLM available"})
            return None
        queries = []
        for _ in range(2):  # bounded: 2 rewording attempts, then give up
            try:
                hypo = chat(self.llm["model"], [
                    {"role": "system", "content":
                        "You are reconstructing your own session notes. "
                        "Answer with ONE sentence, nothing else."},
                    {"role": "user", "content":
                        f"You previously worked on something called '{entity}'. "
                        f"Context: {hint or '(none)'}\n"
                        "How would you have described it in your notes?"},
                ], timeout=60)
            except Exception:
                tried.append({"rung": "rung2", "skipped": "LLM unreachable"})
                return None
            hypo = hypo.replace(f"'{entity}'", "").replace(f'"{entity}"', "")
            queries.append(hypo)
            for score, rec in self.bm25.search(entity + " " + hypo, k=8):
                words = [w for w in tokenize(hypo) if len(w) > 3]
                overlap = sum(1 for w in words if w in rec["text"].lower())
                if entity.lower() in rec["text"].lower() or overlap >= 2:
                    return {"found": True, "via": "rung2:rewording",
                            "evidence": [{"archive": [rec["id"]]}],
                            "what": rec["text"][:200], "hypothesis": hypo,
                            "confidence": 0.7, "file": None}
        tried.append({"rung": "rung2", "queries": queries, "attempts": len(queries)})
        return None

    def _rung3(self, entity, hint, tried):
        """Name totally gone — reconstruct from creation-shaped neighbors."""
        queries = [hint]
        if not hint:
            tried.append({"rung": "rung3", "skipped": "no context hint"})
            return None
        best, best_score = None, 0.0
        for score, rec in self.bm25.search(hint, k=15):
            low = rec["text"].lower()
            is_creation = rec["kind"] in ("tool_call", "tool_result") and \
                any(w in low for w in CREATION_WORDS)
            if is_creation and score > best_score:
                best, best_score = rec, score
        if best:
            return {"found": True, "via": "rung3:reconstruction",
                    "evidence": [{"archive": [best["id"]]}],
                    "what": best["text"][:200], "confidence": 0.5,
                    "file": None, "note": "name not found; nearest creation event"}
        tried.append({"rung": "rung3", "queries": queries})
        return None

    # -------------------------------------------------------------- relay
    def relay(self, entity, hint="", shards=4):
        """v0.7 relay — the spun-off searchers, with negative-result handoff.

        Splits the archive into shards; each shard-searcher runs its own
        BM25 + stem match over only its slice; a hit in any shard wins and
        carries its citation; every miss passes its negative result forward
        into one merged tried-log. This is the deterministic core of the
        relay pattern — LLM-driven subagent searchers slot in later.
        """
        import threading
        recs = list(self._rec_by_id.values())
        if not recs:
            return {"entity": entity, "found": False,
                    "tried": [{"rung": "relay", "skipped": "empty archive"}]}
        size = max(1, len(recs) // shards)
        chunks = [recs[i:i + size] for i in range(0, len(recs), size)]
        results = [None] * len(chunks)
        lock = threading.Lock()

        def searcher(idx, chunk):
            bm = BM25(chunk)
            stem = stem_of(entity)
            # entity alone first (hint terms dilute BM25 ranking)
            for q in (entity, entity + " " + (hint or "")):
                for score, rec in bm.search(q, k=8):
                    low = rec["text"].lower()
                    if entity.lower() in low or (len(stem) >= 4 and stem in low):
                        with lock:
                            results[idx] = (score, rec)
                        return

        threads = [threading.Thread(target=searcher, args=(i, c))
                   for i, c in enumerate(chunks)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        hits = [h for h in results if h]
        if hits:
            score, rec = max(hits, key=lambda x: x[0])
            return {"entity": entity, "found": True, "via": "relay",
                    "evidence": [{"archive": [rec["id"]]}],
                    "what": rec["text"][:200],
                    "confidence": min(0.85, 0.4 + score / 10),
                    "shards": len(chunks),
                    "hit_shards": len(hits),
                    "note": f"{len(hits)}/{len(chunks)} shard-searchers found it"}
        return {"entity": entity, "found": False,
                "tried": [{"rung": "relay", "shards": len(chunks),
                           "negative_results_merged": len(chunks),
                           "queries": [entity + " " + (hint or "")]}]}

    # -------------------------------------------------------------- public
    def resolve(self, entity, hint="", session=None):
        t0 = time.perf_counter()
        tried, result = [], None
        rungs = (self._rung0,
                 lambda e, h, t: self._rung1(e, h, t, session=session),
                 self._rung2, self._rung3)
        for rung in rungs:
            result = rung(entity, hint, tried)
            if result:
                break
        elapsed_ms = (time.perf_counter() - t0) * 1000
        out = {"entity": entity, "found": bool(result),
               "via": result["via"] if result else None,
               "evidence": result["evidence"] if result else [],
               "what": result.get("what", "") if result else "",
               "file": result.get("file") if result else None,
               "confidence": result.get("confidence", 0.0) if result else 0.0,
               "tried": tried, "elapsed_ms": round(elapsed_ms, 1)}
        if result and "hypothesis" in result:
            out["hypothesis"] = result["hypothesis"]
        return out
