"""Index — the map of "what's explained where", plus BM25 over the archive.
Rebuilt from the archive JSONL on load; rebuild is cheap, no persistence needed."""
import math
import re
from collections import Counter

TOKEN_RE = re.compile(r"[a-z0-9]+")

BM25_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "at",
    "by", "is", "are", "was", "were", "be", "been", "it", "its", "this", "that",
    "as", "from", "was", "were", "but", "not", "so", "if", "then", "than",
    "into", "about", "over", "after", "under", "out", "up", "down", "can",
    "will", "would", "could", "should", "do", "does", "did", "done", "have",
    "has", "had", "i", "we", "you", "he", "she", "they", "them", "their",
}


def tokenize(text):
    return [t for t in TOKEN_RE.findall(text.lower())
            if len(t) > 1 and t not in BM25_STOP]


def norm_word(w):
    """Light plural-fold for fuzzy fact matching."""
    return w[:-1] if len(w) > 4 and w.endswith("s") else w


EXPLAIN_MARKERS = (" is ", " is a", " is the", "decided", "because", "purpose",
                   "wrote", "created", "made", "built", "added", "implemented",
                   "uses", "handles", "parses", "exposes", "fixed", "changed",
                   "summary")


def _explain_score(rec, entity):
    """How much this record EXPLAINS the entity (vs merely mentioning it).
    Conversation-only memory lives in decisions, compaction summaries and
    definition-shaped prose — those must outrank bare tool-JSON mentions.
    Markers count 0.5 each (uncapped) so purpose-rich records tie out thin ones."""
    s = 0.0
    low = rec["text"].lower()
    el = entity.lower()
    kind = rec.get("kind", "")
    if kind == "decision":
        s += 2
    elif kind == "tool_call":
        s += 1
    if "[compaction" in low or kind == "compaction":
        s += 2
    if el in low:
        s += 2
        if low.find(el) < 200:      # mentioned early = talked about, not passed by
            s += 1
    s += sum(0.5 for m in EXPLAIN_MARKERS if m in low)
    return s


def build_entity_table(records):
    """entity -> {type, line, explained_in, first_seen}
    `line` comes from the BEST-EXPLAINING record, not the first mention.
    Single pass: each record is scored once per entity it mentions."""
    records = list(records)
    table = {}
    for rec in records:
        text = rec["text"]
        for e in rec.get("entities", []):
            entry = table.setdefault(
                e, {"type": "file" if "/" in e or "." in e else "name",
                    "line": "", "explained_in": [], "first_seen": rec["id"],
                    "_best": -1.0, "_best_id": None, "_best_i": -1})
            entry["explained_in"].append(rec["id"])
            variants = (e, e.replace("/", "\\"), e.replace("\\", "/"))
            i = next((text.find(v) for v in variants if v in text), -1)
            if i == -1:
                continue
            score = _explain_score(rec, e)
            if score > entry["_best"]:
                entry["_best"] = score
                entry["_best_id"] = rec["id"]
                entry["_best_i"] = i
                snippet = text[max(0, i - 40):i + 160].replace("\n", " ").strip()
                entry["line"] = ("..." + snippet + "...") if i > 40 else snippet
    for entry in table.values():
        entry["best_evidence"] = entry.pop("_best_id")
        entry.pop("_best_i")
        entry.pop("_best")
    return table


class BM25:
    """Standard BM25 over archive record texts. k1=1.5, b=0.75."""

    def __init__(self, records, k1=1.5, b=0.75):
        self.k1, self.b = k1, b
        self.recs = list(records)
        self.docs = [tokenize(r["text"]) for r in self.recs]
        self.avgdl = (sum(len(d) for d in self.docs) / len(self.docs)) if self.docs else 1.0
        self.tfs = [Counter(d) for d in self.docs]
        self.df = Counter()
        for d in self.docs:
            for t in set(d):
                self.df[t] += 1
        self.N = len(self.recs)

    def _idf(self, t):
        df = self.df.get(t, 0)
        return math.log((self.N - df + 0.5) / (df + 0.5) + 1.0)

    def search(self, query, k=8):
        q = tokenize(query)
        if not q:
            return []
        scored = []
        for i, (rec, tf) in enumerate(zip(self.recs, self.tfs)):
            dl = len(self.docs[i]) or 1
            s = 0.0
            for t in q:
                f = tf.get(t, 0)
                if not f:
                    continue
                s += self._idf(t) * f * (self.k1 + 1) / (
                    f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
            if s > 0:
                scored.append((s, rec))
        scored.sort(key=lambda x: -x[0])
        return scored[:k]
