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

#: The canonical name of an autofluorescence / blank channel.
AUTOFLUORESCENCE = "Autofluorescence"

#: A nuclear stain named anywhere in a channel name, as a whole token: a
#: prefix or suffix may ride along (`c2_DAPI`, `DAPI-cycle-2`, `DNA3`,
#: `Hoechst_04`, `Nucleus2`), an embedded letter may not (`pDNA`, `DNase`,
#: `Nucleolin`). Shared with `plexora.agent.presets` -- one rule for "this
#: channel is the nuclear stain" everywhere.
NUCLEAR_PATTERN = re.compile(r"(?<![a-z0-9])(?:dna\d*|dapi\d*|hoechst\d*|h3{2,3}(?:342|258)?"
                             r"|nucle(?:ar|i|us)\d*|ir19[13]|iridium|syto\s?\d+"
                             r"|draq\d|topro\d?|to-pro-?\d?|sytox)(?![a-z])", re.I)
#: DNA-PK (a kinase) and DNA-damage readouts are protein markers, not stains.
NOT_NUCLEAR_PATTERN = re.compile(r"dna[\s_.-]?(pk|damage)|nucle(ar|us)[\s_.-]?(factor|lamin)",
                                 re.I)
#: An autofluorescence or blank channel, as the whole name (after a
#: fluorophore or cycle suffix is stripped): `AF`, `AF1`, `AF_2`,
#: `Autofluorescence`, `Blank3`, `Background`, `Empty`, `Unstained`. Anchored
#: at both ends, so `CD3_AF488` (a fluorophore suffix) is never one.
AUTOFLUORESCENCE_PATTERN = re.compile(
    r"^(?:af|auto[\s_.-]?fluo\w*|autofluor\w*|blank|background|bkg|bg|empty|unstained"
    r"|no[\s_.-]?antibody)(?:[\s_.-]?\d+)?$", re.I)

#: Suffixes that name a fluorophore, a cycle or a replicate, not the marker.
_SUFFIX = re.compile(
    r"([_\-\s.](af|alexa|alexafluor|cy|opal|atto|fitc|pe|apc|bv|dylight|cf|ef)\d*"
    r"|[_\-\s.](c|cycle|r|round|ch|channel)\d+"
    r"|[_\-\s.]\d+"
    r"|\(\d+\))+$", re.I)
_PREFIX = re.compile(r"^(anti[_\-\s]?|a[_\-])", re.I)

#: A trailing capital `L` some panels append to an antibody's name (`CD20L`,
#: `PCNAL`, `HLADRL`: a lab's clone or long-exposure tag, live run lsp11385),
#: tried only after every other spelling missed.
_L_TAG = re.compile(r"(?<=[A-Za-z0-9])L$")
#: Real markers whose name ends in L because they are a ligand or a selectin:
#: never folded onto their receptor.
LIGANDS = frozenset(fold for fold in ("cd40l", "cd30l", "cd62l", "cd70l", "ox40l", "41bbl",
                                       "fasl", "rankl", "gitrl", "icosl", "lightl", "trail"))


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
    """The vocabulary's name for a marker, or None when it is not known.

    A channel that is plainly a structural stain or a control -- the nuclear
    counterstain (`DAPI2`, `Hoechst_04`, `Nucleus2`, `DNA_1`) or an
    autofluorescence / blank channel (`AF1`, `Blank_3`) -- resolves to
    `DNA` / `Autofluorescence` even when its exact spelling is not a synonym
    (`structural`)."""
    lookup = load()["lookup"]
    for variant in _variants(name):
        found = lookup.get(fold(variant))
        if found:
            return found
    found = structural(name)
    if found:
        return found
    for variant in _variants(name):
        if _L_TAG.search(variant) and fold(variant) not in LIGANDS:
            found = lookup.get(fold(_L_TAG.sub("", variant)))
            if found:
                return found
    return None


def structural(name) -> str | None:
    """`DNA` for a nuclear counterstain, `Autofluorescence` for a control
    channel, else None -- by name pattern, not by a list of spellings, so a
    numbered or prefixed variant (`Nucleus2`, `c3_DAPI`, `AF_2`) is caught.
    Neither is ever gated (role `context`)."""
    text = str(name).strip()
    if NUCLEAR_PATTERN.search(text) and not NOT_NUCLEAR_PATTERN.search(text):
        return NUCLEAR
    for variant in _variants(text):
        if AUTOFLUORESCENCE_PATTERN.match(variant.strip()):
            return AUTOFLUORESCENCE
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
