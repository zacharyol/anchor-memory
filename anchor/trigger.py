"""Trigger — dangling-reference detection.

An entity is dangling when it appears in the working context but is not
explained IN the context: no manifest entry, no defining sentence. This is the
ms trigger — the system noticing "the agent sees p3c2 and doesn't know what it
is" — without relying on the agent to notice by itself.

`explained-in-context` is detected from definition-shaped lines
("p3c2: a tool that...", "p3c2 is ...", "p3c2 — ...") plus an optional explicit
set (entries of the manifest emitted by our own compaction step).
"""
import re

from . import extract

DEF_SHAPED = re.compile(
    r"(?:^|\n)[ \t]*(?:[-*][ \t]*)?(?P<name>[A-Za-z][\w./\\-]{2,40})"
    r"(?::[^\S\n]| — | - | is (?:a|an|the) | is )", re.UNICODE)


def explained_in_context(context):
    """Entities the context itself defines (manifest entries, definitions)."""
    explained = set()
    for m in DEF_SHAPED.finditer(context):
        name = m.group("name").strip()
        if extract.is_entity(name):
            explained.add(name)
    return explained


def find_dangling(context, explicit_explained=None):
    """Entities referenced in `context` that the context does not explain.

    Returns list of {"entity", "surrounding"} — surrounding text is the hint
    passed to the resolver's ladder.
    """
    explained = explained_in_context(context)
    if explicit_explained:
        explained |= {e for e in explicit_explained if extract.is_entity(e)}
    dangling = []
    seen = set()
    for e in extract.extract_entities(context):
        if e in explained or e in seen:
            continue
        # archive citation ids are pointers, not entities
        if re.match(r"^a\d{6}(\.x\d+)?$", e) or re.match(r"^a\d{13}x\d+x\d+$", e):
            continue
        seen.add(e)
        i = context.find(e)
        dangling.append({
            "entity": e,
            "surrounding": context[max(0, i - 120):i + 160].replace("\n", " ").strip(),
        })
    return dangling
