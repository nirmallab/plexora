"""The marker vocabulary, and the one rule for recognising a marker name.

Panels name the same antibody a dozen ways -- CD8, CD8a, CD8A, cd8_AF488,
CD8-2 -- and nothing downstream should care. `canonical(name)` folds a name to
the vocabulary's entry (case, punctuation, a fluorophore or cycle suffix, a
trailing replicate number) or returns None when it is not a known marker; the
entry says what the marker is, where it stains, whether it is binary and how
it relates to other markers.

The entries come from the BioCognia marker graph (`biocognia_markers`), which
SCIMAP Pro reads too: markers, their relations, and what users and agents add
to it. Plexora knows a graph marker it can gate -- one with a gating role and
a compartment, or one Plexora said how it gates (`products.plexora`: its own
name, PanCK or Ki67, and its role where the class does not give it). Its
partners are the graph's `subset`, `subset_any`, `coexpressed` and
`exclusive` edges (a family endpoint stands for the family's anchors; an
exclusion on both markers, a co-expression on both unless one is read `under`
the other), with the graph's grades as high, moderate and low. A relation scoped to a tissue or disease reaches a session through its
biology (`autogate/biology.py`), never here.

Two layers, as before. The shipped graph always wins. The overlays
(`overlay_paths()`: the user's own, which SCIMAP Pro shares, or a lab's
shared folder) hold what a person or a model said about a marker the shipped
graph does not let Plexora gate, written the first time it was said (`learn`,
from a gating session's panel context). A marker learned once is known from
then on, in every panel and in SCIMAP Pro, and is not asked about again. A
relation an agent proposed there counts only once its quotes are checked or a
person states it. The learned file from before the graph moves into the
overlay the first time it is read.
"""

from __future__ import annotations

import functools
import os
import re
import threading
import time
from pathlib import Path

from biocognia_markers import graph as _graph

#: What Plexora is called in the graph (`products`, an overlay item's origin).
PRODUCT = "plexora"
#: What `learn` writes in an overlay item's origin (`layer`): the learned
#: layer's own account, whose relations Plexora reads as before.
LAYER = "learned"
#: The learned layer's file from before the graph, under the data root when
#: PLEXORA_VOCABULARY_PATH is not set; moved into the overlay once.
LEARNED_FILE = Path("vocabulary") / "learned_markers.yaml"
#: A lab's shared folder from before the graph: the overlay is kept there
#: (BIOCOGNIA_MARKER_GRAPH, which SCIMAP Pro reads too, wins over it).
ENV_LEARNED = "PLEXORA_VOCABULARY_PATH"
#: Who may teach the learned layer, in order of trust: the user's word
#: replaces a model's, a model's never replaces anything.
LEARNED_SOURCES = ("user", "ai")
#: Who said so, in the graph's words (`biocognia_markers.overlay.ORIGINS`).
_ORIGIN = {"user": "user", "ai": "agent"}
#: How often (s) the overlays are looked at for a change made elsewhere
#: (another process, another install sharing them, SCIMAP Pro).
_RECHECK_S = 1.0
_LEARN_LOCK = threading.RLock()
_MIGRATE_LOCK = threading.RLock()
_MIGRATING = threading.local()
_signature = {"at": 0.0, "value": None}

#: What an entry may say (`load` refuses an entry that says anything else).
#: Relations and confidences are in order of preference: a subset partner is
#: the best reference, a high-confidence relation the most trusted.
#:
#: role       context | lineage_reliable | lineage_other | tumour_stromal | state | signalling
#:            (gating order: context is never gated; the rest in that order)
#: relation   subset       this marker's positives should be positive for the partner
#:            coexpressed  the two are usually positive together
#:            exclusive    the two should almost never be positive together
#:            subset_any   every positive is positive for at least one of the marker's
#:                         subset_any partners, one per lineage that carries it, and only
#:                         some cells of each are (LAG-3 on T or NK cells). Two or more.
#: binary     false for markers expressed on a continuum (HLA-ABC, B2M, PD-L1): there
#:            is no valley, but there is background, so the gate marks where real
#:            expression rises out of it (autogate/scoring.py, the `onset` candidate)
#: morphology cell (the default) | fibrous (autogate/cellcalls.reading_for)
#: expression which cells carry the marker, in plain words, and how that varies with
#:            the tissue. A marker whose carriers depend on the tissue (PD-L1, HLA-DR)
#:            names no partners: a gating session's planner fills its empty partner
#:            slots from this note and the panel's lineages (autogate/context.py)
ROLES = ("context", "lineage_reliable", "lineage_other", "tumour_stromal", "state",
         "signalling")
COMPARTMENTS = ("nuclear", "cytoplasmic", "membrane", "nuclear_cytoplasmic", "extracellular")
#: Relations between two markers: what a reference, a score or a quadrant
#: count can read.
PAIR_RELATIONS = ("subset", "coexpressed", "exclusive")
#: `subset_any` is a group, not a pair: every positive is positive for at
#: least one of the marker's `subset_any` partners (PD-L1 on myeloid or on
#: tumour cells), and only some cells of each are. It draws a look's
#: expected partner tile; nothing that weighs a gate reads it.
RELATIONS = (*PAIR_RELATIONS, "subset_any")
#: What the stain is shaped like (`morphology`; absent is `cell`). `fibrous`: fibres, processes or vessel walls run between cells
#: and across their masks, so stain beside a nucleus is weak evidence of its
#: own (`autogate/cellcalls.reading_for`).
MORPHOLOGIES = ("cell", "fibrous")
CONFIDENCE = ("high", "moderate", "low")
#: The graph's grades in Plexora's words, and back.
GRADE = {"strong": "high", "moderate": "moderate", "weak": "low"}
GRADE_BACK = {word: grade for grade, word in GRADE.items()}
#: What a relation stated without a confidence is read as: every reader of a
#: partner uses this one value, and `learn` writes it explicitly.
DEFAULT_CONFIDENCE = "moderate"
#: A learned role as the graph's class and tier.
ROLE_CLASS = {"lineage_reliable": ("lineage", "reliable"), "lineage_other": ("lineage", "other"),
              "tumour_stromal": ("lineage", "tumour_stromal"), "state": ("state", None),
              "signalling": ("signalling", None), "context": ("structural", None)}

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

#: A fluorophore named after a dye family: `_AlexaFluor488`, `_ArgoFluor555L`,
#: `-iFluor647` (letters, `fluor`, the wavelength, an optional letter).
_FLUOR = r"[_\-\s.][a-z]*fluor\d+[a-z]?"
#: Suffixes that name a fluorophore, a cycle or a replicate, not the marker.
_SUFFIX = re.compile(
    r"([_\-\s.](af|alexa|alexafluor|cy|opal|atto|fitc|pe|apc|bv|dylight|cf|ef)\d*"
    rf"|{_FLUOR}"
    r"|[_\-\s.](c|cycle|r|round|ch|channel)\d+"
    r"|[_\-\s.]\d+"
    r"|\(\d+\))+$", re.I)
#: The fluorophore alone, so a marker whose name ends in digits (`Ki-67`)
#: survives where `_SUFFIX` would take its number for a replicate.
_FLUOR_SUFFIX = re.compile(rf"({_FLUOR})+$", re.I)
_PREFIX = re.compile(r"^(anti[_\-\s]?|a[_\-])", re.I)
#: A channel's position in the acquisition, ahead of its name: `03_CD45`,
#: `12-PD-L1` (an OME channel list exported in order).
_POSITION = re.compile(r"^\d{1,3}[_\-\s.]+(?=[A-Za-z])")

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
    # Last, so a name the spellings above already resolve never changes: a
    # leading acquisition position (`03_CD45_ArgoFluor520`), and the
    # fluorophore alone before every other suffix (`04_Ki-67_ArgoFluor555L`).
    for base in (_POSITION.sub("", text), text):
        for variant in (_FLUOR_SUFFIX.sub("", base), base, _SUFFIX.sub("", base)):
            if variant and variant != text:
                yield variant


def overlay_paths() -> list:
    """The overlays the graph is read with: `BIOCOGNIA_MARKER_GRAPH`'s, else the
    folder PLEXORA_VOCABULARY_PATH names (a lab's shared folder from before the
    graph, which keeps its `overlay.json` beside the old file), else the user's
    own, which SCIMAP Pro reads too."""
    from biocognia_markers import overlay

    if os.environ.get(overlay.ENV, "").strip():
        return overlay.paths()
    configured = os.environ.get(ENV_LEARNED, "").strip()
    if configured:
        path = Path(configured).expanduser()
        return [(path if path.is_dir() or not path.suffix else path.parent) / overlay.FILE]
    return overlay.paths()


def learned_path() -> Path:
    """Where what Plexora learns is written (it may not exist yet): the first
    overlay."""
    return overlay_paths()[0]


def _legacy_path() -> Path:
    """The learned layer's file from before the graph."""
    configured = os.environ.get(ENV_LEARNED, "").strip()
    if configured:
        path = Path(configured).expanduser()
        return path / LEARNED_FILE.name if path.is_dir() or not path.suffix else path
    from plexora import paths

    return paths.data_root() / LEARNED_FILE


def _learned_signature():
    """What the overlays are now ((path, mtime, size) each), looked at no more
    than once a second: `load` is called for every name it resolves. A learned
    file from before the graph is moved into the overlay on the way."""
    now = time.monotonic()
    held = _signature["value"]
    if held is not None and now - _signature["at"] < _RECHECK_S \
            and [p for p, _m, _s in held] == [str(p) for p in overlay_paths()]:
        return held
    _migrate_legacy()
    value = []
    for path in overlay_paths():
        try:
            stat = path.stat()
            value.append((str(path), stat.st_mtime_ns, stat.st_size))
        except OSError:
            value.append((str(path), None, None))
    value = tuple(value)
    _signature.update(at=now, value=value)
    return value


def load() -> dict:
    """{version, entries, lookup, shipped_lookup}: the graph alone and with its
    overlays, the shipped graph winning every name they share."""
    return _load(_learned_signature())


def _grade(word):
    return GRADE.get(word, DEFAULT_CONFIDENCE)


def _known(item) -> bool:
    """Whether Plexora can gate a graph marker: it has a role and a
    compartment, or Plexora said how it gates it (`products.plexora`). A
    marker the graph holds without either (CDX2, MPO: classed, never placed in
    the cell) is asked about as before, and what a person says fills it in."""
    if (item.get("products") or {}).get(PRODUCT):
        return True
    return _graph.role(item, PRODUCT) is not None and bool(item.get("compartment"))


def counts(edge) -> bool:
    """Whether Plexora reads an edge as a partner: the shipped graph's (verified
    or not: Plexora's own relations were entered unverified), a person's, one
    whose quotes checked, or one of its learned layer's (`learn`, which a
    model taught before only a person's word was learned); never an agent's
    proposal or a dataset's that no one has checked."""
    origin = edge.get("origin")
    if origin is None:
        return True
    source = str(edge.get("source") or "")
    return source == "user" or source.startswith("citation:") or origin.get("layer") == LAYER


def _build(graph) -> dict:
    """{entries, lookup, ids}: Plexora's entries for the graph's markers it can
    gate, keyed by Plexora's name for each."""
    markers = {mid: item for mid, item in graph.markers.items() if _known(item)}
    names = {mid: ((item.get("products") or {}).get(PRODUCT) or {}).get("name") or item["canonical"]
             for mid, item in markers.items()}

    def expand(ref):
        if _graph.is_family(ref):
            return [a for a in graph.anchors(_graph.family_slug(ref)) if a in markers]
        return [ref] if ref in markers else []

    partners = {mid: {} for mid in markers}

    def add(mid, other, relation, confidence):
        if mid not in markers or other == mid:
            return
        key = (names[other], relation)
        held = partners[mid].get(key)
        if held is None or CONFIDENCE.index(confidence) < CONFIDENCE.index(held):
            partners[mid][key] = confidence

    for edge in graph.edges:
        if not graph.applies(edge) or not counts(edge):
            continue
        kind, confidence = edge["type"], _grade(edge.get("confidence"))
        if kind in ("subset", "subset_any"):
            group = sorted({m for ref in _graph.targets(edge) for m in expand(ref)} - {edge["from"]})
            relation = "subset_any" if kind == "subset_any" and len(group) > 1 else "subset"
            for other in group:
                add(edge["from"], other, relation, confidence)
        elif kind in ("exclusive", "coexpressed"):
            left, right = expand(edge["from"]), expand(edge["to"])
            for a in left:
                for b in right:
                    # A coexpressed partner read beneath the other (`under`:
                    # CD163 under CD68) is listed on that side only, which
                    # places it in the gating tree (`hierarchy.build`).
                    if edge.get("under") in (None, a):
                        add(a, b, kind, confidence)
                    if edge.get("under") in (None, b):
                        add(b, a, kind, confidence)
    # How deep each marker sits: in the graph's family tree (immune, T, CD4 T),
    # else in the subset tree of its partners (CD45 0, CD3 1). A marker's
    # partners come narrowest first, as its carriers are read.
    wider = {mid: {names_to_ids[o] for (o, relation) in held if relation == "subset"}
             for mid, held in partners.items()
             for names_to_ids in [{names[m]: m for m in markers}]}
    depth = {}

    def deep(mid, seen=()):
        if mid not in depth:
            family = markers[mid].get("family")
            depth[mid] = len(graph.ancestors(family)) if family else 1 + max(
                (deep(w, (*seen, mid)) for w in wider.get(mid, ()) if w not in seen), default=-1)
        return depth[mid]

    by_name = {names[m]: m for m in markers}
    entries, lookup, ids, tiers = {}, {}, {}, {}
    for mid, item in markers.items():
        name = names[mid]
        spellings = []
        tiers[name] = ([item["canonical"], *(item.get("synonyms") or ())], item.get("protein") or (),
                       item.get("gene") or ())
        for spelling in [s for tier in tiers[name] for s in tier]:
            if spelling != name and spelling not in spellings:
                spellings.append(spelling)
        entry = {"canonical": name, "synonyms": spellings, "role": _graph.role(item, PRODUCT),
                 "partners": [{"marker": other, "relation": relation, "confidence": confidence}
                              for (other, relation), confidence in sorted(
                                  partners[mid].items(),
                                  key=lambda kv: (RELATIONS.index(kv[0][1]),
                                                  -deep(by_name[kv[0][0]]),
                                                  CONFIDENCE.index(kv[1]), fold(kv[0][0])))],
                 "caveats": list(item.get("caveats") or ()), "id": mid}
        for field, key in (("compartment", "compartment"), ("lineage", "lineage_text"),
                           ("expression", "expression"), ("binary", "binary"),
                           ("morphology", "morphology"), ("expected_fraction", "expected_fraction")):
            if item.get(key) is not None:
                entry[field] = item[key]
        if not entry.get("expression") and entry["role"] != "context":
            entry["expression"] = _expression(graph, mid, item, names)
        entries[name] = entry
        ids[mid] = name
    # Names first, then synonyms, protein names and genes, each tier after the
    # one before; a spelling two markers share within a tier is neither's
    # (never a guess: PTPRC is the gene of CD45, CD45RA and CD45RO alike, and
    # CD45's because it is a synonym of CD45's).
    for name in entries:
        lookup.setdefault(fold(name), name)
    for tier in range(3):
        claims = {}
        for name in entries:
            for spelling in tiers[name][tier]:
                claims.setdefault(fold(spelling), set()).add(name)
        for key, owners in claims.items():
            if key and key not in lookup and len(owners) == 1:
                lookup[key] = next(iter(owners))
    return {"entries": entries, "lookup": lookup, "ids": ids}


def _expression(graph, mid, item, names) -> str:
    """Which cells carry a marker, in plain words, from what the graph states,
    for a marker it holds without an `expression` note (one from SCIMAP Pro's
    library): its family and the families it is also on, or the lineages that
    carry it. A planner fills partner slots from this note."""

    def family(slug):
        return (graph.families.get(slug) or {}).get("name") or slug

    def label(ref):
        return family(_graph.family_slug(ref)) if _graph.is_family(ref) else names.get(ref) or ref

    edges = [e for e in graph.edges_of(mid) if e["from"] == mid and counts(e)]
    carriers = [label(ref) for e in edges if e["type"] in ("subset", "subset_any")
                for ref in _graph.targets(e)]
    also = [f"{family(_graph.family_slug(e['to']))}"
            + (" (rare)" if e.get("frequency") == "rare" else "")
            + (f" in {', '.join(e['tissues'])}" if e.get("tissues") else "")
            for e in edges if e["type"] == "also_on"]
    if item.get("family"):
        words = f"{family(item['family'])} lineage"
    elif carriers:
        words = "carried by " + ", ".join(dict.fromkeys(carriers))
    else:
        words = ("carried by cells of several lineages, depending on the tissue; no lineage holds "
                 "most of its positives")
    return words + (f"; also on {', '.join(also)}" if also else "")


@functools.lru_cache(maxsize=1)
def _shipped() -> dict:
    graph = _graph.load(overlays=False)
    built = _build(graph)
    for entry in built["entries"].values():
        check(entry)
        entry["origin"] = "shipped"
    # The graph's digest beside its version: a stored panel context rebuilds
    # when the version changes (`context.for_project`), and an edited caveat
    # that left the version alone kept reaching readers in its old words
    # (lsp11385 PCNA, 2026-10-20).
    return {"version": f"{graph.shipped_version}.{_graph.shipped_digest()}", **built}


@functools.lru_cache(maxsize=4)
def _load(signature) -> dict:
    shipped = _shipped()
    paths = [Path(p) for p, mtime, _size in signature if mtime is not None]
    if not paths:
        return {"version": shipped["version"], "entries": dict(shipped["entries"]),
                "lookup": dict(shipped["lookup"]), "shipped_lookup": shipped["lookup"],
                "ids": dict(shipped["ids"])}
    graph = _graph.load(paths=paths)
    built = _build(graph)
    entries, lookup = dict(shipped["entries"]), dict(shipped["lookup"])
    learned = []
    for name, entry in built["entries"].items():
        if fold(name) in shipped["lookup"] or entry["id"] in shipped["ids"]:
            # Known before the overlays: the shipped entry stands, but a
            # relation a person or Plexora added reaches it.
            held = shipped["entries"].get(shipped["ids"].get(entry["id"], name))
            if held is not None and entry["partners"] != held["partners"]:
                entries[held["canonical"]] = dict(held, partners=list(entry["partners"]))
            continue
        try:
            check(entry)
        except ValueError:
            continue          # a hand-edited line gone wrong: skipped, not fatal
        entries[name] = dict(entry, origin="learned")
        learned.append(entries[name])
        lookup.setdefault(fold(name), name)
        for spelling in entry["synonyms"]:
            if built["lookup"].get(fold(spelling)) == name:
                lookup.setdefault(fold(spelling), name)
    version = shipped["version"]
    if graph.overlays:
        version = f"{version}+learned.{graph.version.rpartition('.')[2]}"
    return {"version": version, "entries": entries, "lookup": lookup,
            "shipped_lookup": shipped["lookup"], "ids": {**shipped["ids"], **built["ids"]}}


def clear_cache():
    """Forget what was read: the next `load` reads the graph and its overlays
    again."""
    _signature.update(at=0.0, value=None)
    _shipped.cache_clear()
    _load.cache_clear()


def check(entry):
    """ValueError naming the entry when it uses a value outside the schema."""
    name = entry.get("canonical")
    allowed = {"role": ROLES, "compartment": COMPARTMENTS, "morphology": MORPHOLOGIES}
    for field, values in allowed.items():
        if entry.get(field) is not None and entry[field] not in values:
            raise ValueError(f"vocabulary entry {name!r}: {field} {entry[field]!r} is not "
                             f"one of {values}")
    for partner in entry.get("partners") or []:
        if partner.get("relation") not in RELATIONS:
            raise ValueError(f"vocabulary entry {name!r}: partner {partner.get('marker')!r} "
                             f"has relation {partner.get('relation')!r}, not one of "
                             f"{RELATIONS}")
        if partner.get("confidence", DEFAULT_CONFIDENCE) not in CONFIDENCE:
            raise ValueError(f"vocabulary entry {name!r}: partner {partner.get('marker')!r} "
                             f"has confidence {partner.get('confidence')!r}, not one of "
                             f"{CONFIDENCE}")
    group = [p for p in entry.get("partners") or [] if p.get("relation") == "subset_any"]
    if len(group) == 1:
        raise ValueError(f"vocabulary entry {name!r}: one subset_any partner is a subset "
                         f"({group[0].get('marker')!r})")


def version() -> str:
    return load()["version"]


def canonical(name, *, learned=True) -> str | None:
    """The vocabulary's name for a marker, or None when it is not known.

    A channel that is plainly a structural stain or a control -- the nuclear
    counterstain (`DAPI2`, `Hoechst_04`, `Nucleus2`, `DNA_1`) or an
    autofluorescence / blank channel (`AF1`, `Blank_3`) -- resolves to
    `DNA` / `Autofluorescence` even when its exact spelling is not a synonym
    (`structural`). `learned=False`: the shipped layer only."""
    found = synonym(name, learned=learned)
    if found:
        return found
    lookup = _lookup(learned)
    found = structural(name)
    if found:
        return found
    for variant in _variants(name):
        if _L_TAG.search(variant) and fold(variant) not in LIGANDS:
            found = lookup.get(fold(_L_TAG.sub("", variant)))
            if found:
                return found
    return None


def _lookup(learned=True) -> dict:
    loaded = load()
    return loaded["lookup"] if learned else loaded["shipped_lookup"]


def synonym(name, *, learned=True) -> str | None:
    """The vocabulary's name for a marker when `name` is one of its listed
    spellings (give or take a fluorophore or cycle suffix), else None -- the
    first, strictest step of `canonical`, without the name patterns."""
    lookup = _lookup(learned)
    for variant in _variants(name):
        found = lookup.get(fold(variant))
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


def match_name(name, candidates) -> str | None:
    """The one of `candidates` that names the same marker as `name` -- a table
    column for an image channel, or the reverse: the same name, else the same
    name folded, else the same vocabulary entry (`03_CD45_ArgoFluor520` and
    `CD45`) when exactly one candidate has it. None when none does, or when
    several do (two `Nucleus` cycles): never a guess."""
    candidates = [c for c in candidates if c is not None]
    if name in candidates:
        return name
    key = fold(name)
    same = [c for c in candidates if fold(c) == key]
    if same:
        return same[0]
    target = canonical(name)
    if target is None:
        return None
    hits = [c for c in candidates if canonical(c) == target]
    return hits[0] if len(hits) == 1 else None


def resolve_panel(markers) -> dict:
    """{panel marker: canonical or None}, in panel order."""
    return {marker: canonical(marker) for marker in markers}


def origin(name) -> str | None:
    """`shipped` or `learned` for a known marker's entry, else None."""
    found = canonical(name)
    return (load()["entries"][found].get("origin") or "shipped") if found else None


def plain_name(name) -> str:
    """A panel column's marker name without its acquisition position or a
    fluorophore / cycle suffix (`03_CDX2_AF488` -> `CDX2`): what a learned
    entry is called, so every spelling of it folds onto it."""
    text = str(name).strip()
    base = _POSITION.sub("", text)
    for candidate in (_SUFFIX.sub("", _FLUOR_SUFFIX.sub("", base)), base, text):
        candidate = candidate.strip(" _-.")
        if candidate and fold(candidate):
            return candidate
    return text


def _said(origin) -> dict:
    """An overlay item's `origin` as the learned layer recorded who said so."""
    origin = dict(origin or {})
    source = origin.pop("source", "agent")
    origin.pop("product", None)
    origin.pop("layer", None)
    return {"source": "ai" if source == "agent" else source, **origin}


def learned() -> list:
    """The markers Plexora knows from the overlays alone, as entries, each with
    `learned` ({source, at, agent, project, ...}: who said so)."""
    from biocognia_markers import overlay

    loaded = load()
    by_id = {e["id"]: e for e in loaded["entries"].values() if e.get("origin") == "learned"}
    out = []
    for path in overlay_paths():
        for mid, item in overlay.read(path).get("markers", {}).items():
            if mid in by_id:
                out.append({**{k: v for k, v in by_id.pop(mid).items() if k != "origin"},
                            "learned": _said(item.get("origin"))})
    return out


def _node(entry) -> dict:
    """A learned entry as a graph marker: its class and tier from its role, and
    the role itself as Plexora's own word for how it gates it."""
    cls, tier = ROLE_CLASS[entry["role"]]
    node = {"canonical": entry["canonical"], "synonyms": list(entry.get("synonyms") or ()),
            "class": cls, "products": {PRODUCT: {"role": entry["role"]}}}
    if tier:
        node["tier"] = tier
    for field, key in (("compartment", "compartment"), ("lineage", "lineage_text"),
                       ("binary", "binary"), ("morphology", "morphology"),
                       ("caveats", "caveats"), ("expected_fraction", "expected_fraction")):
        if entry.get(field) not in (None, [], ""):
            node[key] = entry[field]
    return node


def _edges(mid, entry, ids) -> list:
    """A learned entry's partners as graph edges (partners Plexora cannot name
    a marker for are left out: an edge joins two markers)."""
    out, group = [], []
    for partner in entry.get("partners") or ():
        other = ids.get(fold(partner.get("marker") or ""))
        if other is None or other == mid:
            continue
        grade = GRADE_BACK[partner.get("confidence") or DEFAULT_CONFIDENCE]
        relation = partner["relation"]
        if relation == "subset":
            out.append({"id": f"subset:{mid}:{other}", "type": "subset", "from": mid, "to": other,
                        "confidence": grade})
        elif relation in ("exclusive", "coexpressed"):
            a, b = sorted([mid, other])
            out.append({"id": f"{relation}:{a}|{b}", "type": relation, "from": a, "to": b,
                        "confidence": grade})
        else:
            group.append((other, grade))
    if len(group) > 1:
        grade = max((g for _o, g in group), key=_graph.CONFIDENCE.index)      # the weakest
        out.append({"id": f"subset_any:{mid}", "type": "subset_any", "from": mid,
                    "to": sorted({o for o, _g in group}), "confidence": grade})
    elif group:
        other, grade = group[0]
        out.append({"id": f"subset:{mid}:{other}", "type": "subset", "from": mid, "to": other,
                    "confidence": grade})
    return out


def _held_by(path, mid):
    """Who wrote marker `mid` into the overlay at `path` (`user`, `agent`), or None."""
    from biocognia_markers import overlay

    item = overlay.read(path).get("markers", {}).get(mid)
    return ((item or {}).get("origin") or {}).get("source")


def learn(entries, *, source="ai", about=None) -> list:
    """Add markers the shipped graph does not let Plexora gate to the overlay.

    `entries`: [{canonical, role, compartment?, lineage?, binary?, partners?
    [{marker (a canonical name), relation, confidence}], caveats?,
    expected_fraction?, synonyms?, morphology?}]. A name Plexora knows from the
    shipped graph is never written; one already learned is replaced only by
    the user's word over a model's (`LEARNED_SOURCES`). A marker the graph
    holds without a compartment (CDX2) is filled in, never changed. `about`
    (who, from which session, project) is kept with each item, beside an
    entry's own `about` when it has one. Partners may name each other within
    one call. Returns the names written."""
    from biocognia_markers import graph as graphmod, overlay
    from biocognia_markers.resolve import marker_key

    if source not in LEARNED_SOURCES:
        raise ValueError(f"source is one of {LEARNED_SOURCES}")
    shipped = _shipped()
    graph = graphmod.load(overlays=False)
    by_name = {fold(item["canonical"]): mid for mid, item in graph.markers.items()}
    fields = ("role", "compartment", "lineage", "binary", "partners", "caveats",
              "expected_fraction", "synonyms", "morphology")
    origin = _ORIGIN[source]
    written = []
    with _LEARN_LOCK:
        path = learned_path()
        loaded = load()
        name_ids = {name: mid for mid, name in loaded["ids"].items()}
        ids = dict(by_name)
        ids.update({key: name_ids[name] for key, name in loaded["lookup"].items()
                    if name in name_ids})
        batch = []
        for raw in entries or []:
            name = str(raw.get("canonical") or "").strip()
            if not name or not raw.get("role") or fold(name) in shipped["lookup"]:
                continue
            entry = {"canonical": name, **{k: raw[k] for k in fields
                                           if raw.get(k) not in (None, [], "")}}
            entry.setdefault("binary", True)
            for partner in entry.get("partners") or ():
                partner.setdefault("confidence", DEFAULT_CONFIDENCE)
            check(entry)
            mid = by_name.get(fold(name)) or f"m:{marker_key(name)}"
            held = _held_by(path, mid)
            if held is not None and overlay.RANK.get(held, -1) >= overlay.RANK[origin]:
                continue          # the first account stands, unless the user's
            ids.setdefault(fold(name), mid)
            batch.append((name, mid, entry, {**(about or {}), **(raw.get("about") or {})},
                          held is not None))
        if not batch:
            return []
        for name, mid, entry, said, replaced in batch:
            node = _node(entry)
            if mid in graph.markers:
                # Held without what Plexora gates with: filled in, never changed.
                fills, _clashes = graphmod.marker_fills(graph.markers[mid], node)
                node = {"canonical": graph.markers[mid]["canonical"],
                        "synonyms": [s for s in node["synonyms"] if s != graph.markers[mid]["canonical"]]
                        + ([name] if name != graph.markers[mid]["canonical"] else []),
                        **fills}
            if replaced:
                # The relations it was learned with before go with the old
                # account (never another product's, nor another's account).
                stale = [f"edges.{e['id']}" for e in overlay.read(path).get("edges", ())
                         if (e.get("origin") or {}).get("layer") == LAYER
                         and mid in _graph.endpoints(e)]
                overlay.forget(stale, source=origin, path=path)
            report = overlay.write({"markers": {mid: node}, "edges": _edges(mid, entry, ids)},
                                   source=origin,
                                   about={**said, "product": PRODUCT, "layer": LAYER},
                                   path=path, graph=graph)
            if f"markers.{mid}" in report["written"]:
                written.append(name)
    if written:
        clear_cache()
    return written


def forget(name) -> bool:
    """Take a learned marker off the overlay, with the relations Plexora
    learned with it (so the next session asks about it again). False when it
    was not there."""
    from biocognia_markers import overlay

    with _LEARN_LOCK:
        path = learned_path()
        held = overlay.read(path)
        key = fold(name)
        mids = [mid for mid, item in held.get("markers", {}).items()
                if key in {fold(item.get("canonical") or ""), fold(mid[2:])}
                or key in {fold(s) for s in item.get("synonyms") or ()}]
        if not mids:
            return False
        labels = [f"markers.{mid}" for mid in mids] + [
            f"edges.{e['id']}" for e in held.get("edges", ())
            if (e.get("origin") or {}).get("layer") == LAYER
            and mids[0] in _graph.endpoints(e)]
        removed = overlay.forget(labels, source="user", path=path)["removed"]
    clear_cache()
    return any(label.startswith("markers.") for label in removed)


def _migrate_legacy():
    """Move a learned file from before the graph into the overlay, once, and
    keep the old file beside it as `<name>.migrated`."""
    import yaml

    if getattr(_MIGRATING, "on", False):
        return
    legacy = _legacy_path()
    if not legacy.is_file():
        return
    with _MIGRATE_LOCK:
        if not legacy.is_file():
            return
        try:
            raw = yaml.safe_load(legacy.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            return
        entries = [dict(e) for e in raw.get("markers") or () if isinstance(e, dict)]
        _MIGRATING.on = True
        try:
            _learn_legacy(entries)
        finally:
            _MIGRATING.on = False
        try:
            os.replace(legacy, legacy.with_name(legacy.name + ".migrated"))
        except OSError:
            pass
        _signature.update(at=0.0, value=None)


def _learn_legacy(entries):
    for said in ("ai", "user"):                               # the user's word last: it wins
        group = []
        for entry in entries:
            if ((entry.get("learned") or {}).get("source") or "ai") != said:
                continue
            about = {k: v for k, v in (entry.get("learned") or {}).items()
                     if k not in ("source",)}
            group.append({**{k: v for k, v in entry.items() if k != "learned"},
                          "about": {"learned_at" if k == "at" else k: v for k, v in about.items()}})
        try:
            learn(group, source=said)
        except (OSError, ValueError):
            for entry in group:                               # one bad line: the rest still move
                try:
                    learn([entry], source=said)
                except (OSError, ValueError):
                    continue
