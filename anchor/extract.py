"""Entity extraction. Offline, deterministic, stdlib only.

Finds the names an agent leaves behind in its own transcript: file paths,
snake_case / camelCase / UPPER_SNAKE identifiers, lowercase tokens containing
digits (p3c2, l9, zzq9), and "tool X" / "named X" style mentions.
"""
import re

PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/])?(?:[\w.-]+[\\/]){0,6}[\w-]{2,}\.[A-Za-z]{1,5}\b")
SNAKE_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
CAMEL_RE = re.compile(r"\b[a-z]+(?:[A-Z][a-z0-9]+)+\b")
UPPER_RE = re.compile(r"\b[A-Z][A-Z0-9]{2,}(?:_[A-Z0-9]+)+\b")
# lowercase token with at least one digit: p3c2, l9, zzq9
DIGIT_RE = re.compile(r"\b[a-z][a-z0-9]*\d[a-z0-9]*\b")
NAMED_RE = re.compile(
    r"\b(?:tool|script|module|function|command|named|called)\s+([A-Za-z][\w-]{2,30})\b")

# generic words that match the loose patterns but are never entities
STOP = {
    "the", "and", "for", "this", "that", "with", "from", "have", "not", "are",
    "was", "but", "all", "can", "her", "she", "his", "has", "were", "been",
    "will", "would", "could", "should", "than", "then", "them", "they", "its",
    "our", "out", "about", "into", "over", "after", "under", "more", "most",
    "some", "such", "only", "also", "just", "like", "when", "what", "which",
    "who", "how", "why", "where", "while", "these", "those", "there", "here",
    "one", "two", "three", "none", "true", "false", "self", "str", "int",
    "float", "bool", "list", "dict", "set", "file", "path", "name", "tool",
    "test", "data", "text", "line", "time", "make", "made", "used", "using",
    "use", "new", "get", "put", "run", "ran", "see", "saw", "tmp", "temp",
    "www", "com", "org", "http", "https", "utf", "bin", "usr", "var",
    "e.g", "i.e", "etc",
}


def _clean(cands):
    out = []
    for c in cands:
        c = c.strip(".\\/")
        if c.lower() in STOP or len(c) < 3:
            continue
        # JSON-escaped newline artifacts: "\nName" inside a string literal
        # extracts as "nName", "\nOutput" as "nOutput" — strip the artifact
        if len(c) > 2 and c[0] in "nr" and c[1].isupper():
            c = c[1:]
            if c.lower() in STOP or len(c) < 3 or not is_entity(c):
                continue
        out.append(c)
    return out


# "tool named p3c2" makes NAMED_RE capture the keyword itself from the pair
NAMED_KEYWORDS = {"named", "called"}


def extract_entities(text):
    """All candidate entities in `text`, most specific first (paths before names)."""
    paths = _clean(m.group(0).replace("\\", "/") for m in PATH_RE.finditer(text))
    # bare stems of paths are also entities (tools/p3c2.py -> p3c2)
    stems = _clean(p.rsplit("/", 1)[-1].rsplit(".", 1)[0] for p in paths)
    named = [m.group(1) for m in NAMED_RE.finditer(text)
             if m.group(1).lower() not in NAMED_KEYWORDS]
    # a captured "name" must be entity-shaped: digit, underscore or inner
    # capital ("p3c2", "BreakInModMenu") — rejects plain verbs like "finishes"
    named = [n for n in named if any(ch.isdigit() for ch in n)
             or "_" in n or any(ch.isupper() for ch in n[1:])]
    named = _clean(named)
    upper = _clean(m.group(0) for m in UPPER_RE.finditer(text))
    snake = _clean(m.group(0) for m in SNAKE_RE.finditer(text))
    camel = _clean(m.group(0) for m in CAMEL_RE.finditer(text))
    digits = _clean(m.group(0) for m in DIGIT_RE.finditer(text))
    seen, out = set(), []
    for group in (paths, stems, named, upper, snake, camel, digits):
        for e in group:
            if e not in seen:
                seen.add(e)
                out.append(e)
    return out


def is_entity(word):
    """Is this single token shaped like an entity we care about?"""
    return bool(extract_entities(word)) and word not in STOP
