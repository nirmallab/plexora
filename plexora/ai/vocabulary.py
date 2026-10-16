"""The marker vocabulary, and the one rule for recognising a marker name.

Panels name the same antibody a dozen ways -- CD8, CD8a, CD8A, cd8_AF488,
CD8-2 -- and nothing downstream should care. `canonical(name)` folds a name to
the vocabulary's entry (case, punctuation, a fluorophore or cycle suffix, a
trailing replicate number) or returns None when it is not a known marker; the
entry says what the marker is, where it stains, whether it is binary and how
it relates to other markers.

Two layers. The shipped one (`plexora/ai/knowledge/markers.yaml`) is curated
and always wins. The learned one (`learned_path()`: a file under the data
root, or wherever PLEXORA_VOCABULARY_PATH points -- a lab's shared folder, so
every install there learns together) holds what a model or the user said
about a marker the shipped one does not know, written the first time it was
said (`learn`, from a gating session's panel context). A marker learned once
is known from then on, in every panel, and is not asked about again.
"""

from __future__ import annotations

import functools
import hashlib
import os
import re
import threading
import time
from pathlib import Path

PATH = Path(__file__).parent / "knowledge" / "markers.yaml"

#: Where the learned layer is kept when PLEXORA_VOCABULARY_PATH is not set:
#: under the data root.
LEARNED_FILE = Path("vocabulary") / "learned_markers.yaml"
ENV_LEARNED = "PLEXORA_VOCABULARY_PATH"
#: Who may teach the learned layer, in order of trust: the user's word
#: replaces a model's, a model's never replaces anything.
LEARNED_SOURCES = ("user", "ai")
#: How often (s) the learned file is looked at for a change made elsewhere
#: (another process, another install sharing it).
_RECHECK_S = 1.0
_LEARN_LOCK = threading.Lock()
_signature = {"at": 0.0, "value": None}

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


def learned_path() -> Path:
    """The learned layer's file (it may not exist yet). PLEXORA_VOCABULARY_PATH
    names the file, or a folder to keep `learned_markers.yaml` in."""
    configured = os.environ.get(ENV_LEARNED, "").strip()
    if configured:
        path = Path(configured).expanduser()
        return path / LEARNED_FILE.name if path.is_dir() or not path.suffix else path
    from plexora import paths

    return paths.data_root() / LEARNED_FILE


def _learned_signature():
    """What the learned file is now (path, mtime, size), looked at no more
    than once a second: `load` is called for every name it resolves."""
    now = time.monotonic()
    path = learned_path()
    held = _signature["value"]
    if held is not None and held[0] == str(path) and now - _signature["at"] < _RECHECK_S:
        return held
    try:
        stat = path.stat()
        value = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        value = (str(path), None, None)
    _signature.update(at=now, value=value)
    return value


def load() -> dict:
    """{version, entries, lookup, shipped_lookup}: both layers, the shipped
    one winning every name they share."""
    return _load(_learned_signature())


@functools.lru_cache(maxsize=1)
def _shipped() -> dict:
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
        entry["origin"] = "shipped"
        entries[entry["canonical"]] = entry
        for alias in [entry["canonical"], *entry["synonyms"]]:
            lookup.setdefault(fold(alias), entry["canonical"])
    return {"version": str(raw.get("version") or "0"), "entries": entries, "lookup": lookup}


def _read_learned(path) -> list:
    import yaml

    try:
        with open(path, encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except (OSError, ValueError):
        return []
    return [dict(e) for e in (raw.get("markers") or []) if isinstance(e, dict)]


@functools.lru_cache(maxsize=4)
def _load(signature) -> dict:
    shipped = _shipped()
    entries = dict(shipped["entries"])
    lookup = dict(shipped["lookup"])
    learned_entries = []
    if signature and signature[1] is not None:
        for entry in _read_learned(signature[0]):
            name = entry.get("canonical")
            if not name or fold(name) in shipped["lookup"]:
                continue          # the shipped layer knows it: never shadowed
            entry.setdefault("synonyms", [])
            entry.setdefault("partners", [])
            entry.setdefault("caveats", [])
            try:
                check(entry)
            except ValueError:
                continue          # a hand-edited line gone wrong: skipped, not fatal
            entry["origin"] = "learned"
            entries[name] = entry
            learned_entries.append(entry)
            for alias in [name, *entry["synonyms"]]:
                lookup.setdefault(fold(alias), name)
    version = shipped["version"]
    if learned_entries:
        digest = hashlib.sha1(repr(sorted(
            (e["canonical"], e.get("role"), e.get("compartment"), e.get("lineage"),
             e.get("binary"),
             tuple((p.get("marker"), p.get("relation")) for p in e["partners"]))
            for e in learned_entries)).encode("utf-8")).hexdigest()[:10]
        version = f"{version}+learned.{digest}"
    return {"version": version, "entries": entries, "lookup": lookup,
            "shipped_lookup": shipped["lookup"]}


def clear_cache():
    """Forget what was read: the next `load` reads both layers again."""
    _signature.update(at=0.0, value=None)
    _load.cache_clear()


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


def learned() -> list:
    """The learned layer's entries, as stored (what `learn` wrote)."""
    signature = _learned_signature()
    return _read_learned(signature[0]) if signature[1] is not None else []


def _write_learned(path, entries, note=None):
    import yaml

    body = {"version": "1",
            "note": note or ("Markers Plexora learned: what a model or the user said about "
                             "markers the shipped vocabulary does not know. The shipped "
                             "vocabulary always wins; delete an entry to have it asked "
                             "again."),
            "markers": sorted(entries, key=lambda e: fold(e["canonical"]))}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(yaml.safe_dump(body, sort_keys=False, allow_unicode=True),
                   encoding="utf-8")
    os.replace(tmp, path)


def learn(entries, *, source="ai", about=None) -> list:
    """Add markers the shipped layer does not know to the learned layer.

    `entries`: [{canonical, role, compartment?, lineage?, binary?, partners?
    [{marker (a canonical name), relation, confidence}], caveats?,
    expected_fraction?, synonyms?}]. A name the shipped layer knows is never
    written; one already learned is replaced only by the user's word over a
    model's (`LEARNED_SOURCES`). `about` (who, from which session, project)
    is kept with each entry. Returns the names written."""
    if source not in LEARNED_SOURCES:
        raise ValueError(f"source is one of {LEARNED_SOURCES}")
    shipped = _shipped()["lookup"]
    fields = ("role", "compartment", "lineage", "binary", "partners", "caveats",
              "expected_fraction", "synonyms")
    rank = {s: i for i, s in enumerate(LEARNED_SOURCES)}
    written = []
    with _LEARN_LOCK:
        path = learned_path()
        current = {e.get("canonical"): e for e in _read_learned(path) if e.get("canonical")}
        for raw in entries or []:
            name = str(raw.get("canonical") or "").strip()
            if not name or not raw.get("role") or fold(name) in shipped:
                continue
            entry = {"canonical": name, **{k: raw[k] for k in fields
                                           if raw.get(k) not in (None, [], "")}}
            entry.setdefault("binary", True)
            check(entry)
            held = current.get(name)
            held_source = ((held or {}).get("learned") or {}).get("source", "ai")
            if held is not None and rank.get(source, 9) >= rank.get(held_source, 9):
                continue          # the first account stands, unless the user's
            entry["learned"] = {"source": source,
                                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                **{k: v for k, v in (about or {}).items() if v is not None}}
            current[name] = entry
            written.append(name)
        if written:
            _write_learned(path, list(current.values()))
    if written:
        clear_cache()
    return written


def forget(name) -> bool:
    """Take a marker off the learned layer (so the next session asks about
    it again). False when it was not there."""
    with _LEARN_LOCK:
        path = learned_path()
        current = _read_learned(path)
        kept = [e for e in current if fold(e.get("canonical") or "") != fold(name)]
        if len(kept) == len(current):
            return False
        _write_learned(path, kept)
    clear_cache()
    return True
