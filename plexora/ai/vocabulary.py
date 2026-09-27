"""The shipped marker vocabulary, and the one rule for recognising a marker name.

Panels name the same antibody a dozen ways -- CD8, CD8a, CD8A, cd8_AF488,
CD8-2 -- and nothing downstream should care. `canonical(name)` folds a name to
the vocabulary's entry (case, punctuation, a fluorophore or cycle suffix, a
trailing replicate number) or returns None when it is not a known marker; the
entry (`plexora/ai/knowledge/markers.yaml`) says what the marker is, where it
stains, whether it is binary and how it relates to other markers.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

PATH = Path(__file__).parent / "knowledge" / "markers.yaml"

#: What an entry may say (markers.yaml's header describes them in words;
#: `load` refuses an entry that says anything else). Relations and
#: confidences are in order of preference: a subset partner is the best
#: reference, a high-confidence relation the most trusted.
ROLES = ("context", "lineage_reliable", "lineage_other", "tumour_stromal", "state",
         "signalling")
COMPARTMENTS = ("nuclear", "cytoplasmic", "membrane", "nuclear_cytoplasmic", "extracellular")
RELATIONS = ("subset", "coexpressed", "exclusive")
CONFIDENCE = ("high", "moderate", "low")

#: The canonical name of the nuclear stain.
NUCLEAR = "DNA"

#: Suffixes that name a fluorophore, a cycle or a replicate, not the marker.
_SUFFIX = re.compile(
    r"([_\-\s.](af|alexa|alexafluor|cy|opal|atto|fitc|pe|apc|bv|dylight|cf|ef)\d*"
    r"|[_\-\s.](c|cycle|r|round|ch|channel)\d+"
    r"|[_\-\s.]\d+"
    r"|\(\d+\))+$", re.I)
_PREFIX = re.compile(r"^(anti[_\-\s]?|a[_\-])", re.I)


def fold(name) -> str:
    """Lowercase alphanumerics only -- the comparison key."""
    return re.sub(r"[^0-9a-z]+", "", str(name).casefold())


def _variants(name):
    text = str(name).strip()
    yield text
    stripped = _SUFFIX.sub("", text)
    if stripped and stripped != text:
        yield stripped
    unprefixed = _PREFIX.sub("", stripped or text)
    if unprefixed and unprefixed != text:
        yield unprefixed


@functools.lru_cache(maxsize=1)
def load() -> dict:
    import yaml

    with open(PATH, encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    entries = {}
    lookup = {}
    for entry in raw.get("markers") or []:
        entry = dict(entry)
        entry.setdefault("synonyms", [])
        entry.setdefault("partners", [])
        entry.setdefault("caveats", [])
        check(entry)
        entries[entry["canonical"]] = entry
        for alias in [entry["canonical"], *entry["synonyms"]]:
            lookup.setdefault(fold(alias), entry["canonical"])
    return {"version": str(raw.get("version") or "0"), "entries": entries, "lookup": lookup}


def check(entry):
    """ValueError naming the entry when it uses a value outside the schema."""
    name = entry.get("canonical")
    allowed = {"role": ROLES, "compartment": COMPARTMENTS}
    for field, values in allowed.items():
        if entry.get(field) is not None and entry[field] not in values:
            raise ValueError(f"markers.yaml entry {name!r}: {field} {entry[field]!r} is not "
                             f"one of {values}")
    for partner in entry.get("partners") or []:
        if partner.get("relation") not in RELATIONS:
            raise ValueError(f"markers.yaml entry {name!r}: partner {partner.get('marker')!r} "
                             f"has relation {partner.get('relation')!r}, not one of "
                             f"{RELATIONS}")
        if partner.get("confidence", CONFIDENCE[0]) not in CONFIDENCE:
            raise ValueError(f"markers.yaml entry {name!r}: partner {partner.get('marker')!r} "
                             f"has confidence {partner.get('confidence')!r}, not one of "
                             f"{CONFIDENCE}")


def version() -> str:
    return load()["version"]


def canonical(name) -> str | None:
    """The vocabulary's name for a marker, or None when it is not known."""
    lookup = load()["lookup"]
    for variant in _variants(name):
        found = lookup.get(fold(variant))
        if found:
            return found
    return None


def entry(name) -> dict | None:
    found = canonical(name)
    return dict(load()["entries"][found]) if found else None


def synonyms(name) -> list:
    found = entry(name)
    return [found["canonical"], *found["synonyms"]] if found else []


def resolve_panel(markers) -> dict:
    """{panel marker: canonical or None}, in panel order."""
    return {marker: canonical(marker) for marker in markers}
