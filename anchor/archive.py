"""Archive — the mm side. Append-only JSONL of raw session events, written
BEFORE compaction can destroy anything. Every record is citable by id."""
import json
import os
import time

from . import extract


class Archive:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._n = 0
        if os.path.exists(path):  # resume ids after restart
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        self._n += 1
        self._fh = open(path, "a", encoding="utf-8")
        try:  # user-only perms on the archive (best effort; audit T4a)
            os.chmod(path, 0o600)
        except OSError:
            pass

    def append(self, kind, text, task="", compaction=0, entities=None, ts=None, **extra):
        """Record one raw event. Entities auto-extracted when not given.
        Extra keyword fields (session, source_id, source_ts) pass through.
        Ids are collision-proof across concurrent servers: time + pid + counter."""
        if entities is None:
            entities = extract.extract_entities(text)
        if ts is None:
            ts = time.time()
        rec = {
            "id": "a%013dx%02dx%03d" % (
                int(ts * 1000), os.getpid() % 100, self._n % 1000),
            "ts": ts,
            "kind": kind,          # message | tool_call | tool_result | decision
            "task": task,
            "compaction": compaction,
            "entities": entities,
            "text": text,
        }
        rec.update(extra)
        self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._fh.flush()
        self._n += 1
        return rec["id"]

    def get(self, rec_id):
        for rec in self.iter():
            if rec["id"] == rec_id:
                return rec
        return None

    def iter(self):
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)

    def count(self):
        return self._n

    def reopen(self, path=None):
        """Close the append handle and recount (used after a file swap)."""
        self._fh.close()
        if path:
            self.path = path
        self._n = 0
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    self._n += 1
        self._fh = open(self.path, "a", encoding="utf-8")

    def close(self):
        self._fh.close()
