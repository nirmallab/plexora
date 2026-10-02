"""The sample's biology -- tissue and disease -- as a prior for one session.

"A melanoma from the skin" carries two contexts, and each changes how a
marker should be read: MART1 and SOX10 together mean much more in melanoma;
SOX9 also marks hair follicles in skin, so place and shape matter; keratin
along the surface is epidermis, not infiltrate. The knowledge lives in
`plexora/ai/knowledge/biology.yaml`, never in code; this module resolves
what the user said against it and applies it:

- **Panel** (`overlay`): the context's relations are laid over the panel's,
  for the session only -- added, or their confidence set, and tagged
  `context` so `hierarchy` weighs them more when choosing references. An
  entry the user or metadata stated is never changed.
- **Looks** (`brief`): each look is told the tissue and disease, who said so,
  what the marker (and its references) also mark here, and the structures
  that look alike -- so the vision step reads the pattern in the right
  tissue.
- **Confidence** (`engine.confidence_for`): a look says whether what it saw
  fits the expected biology; a conflict or an ambiguity (two structures
  could explain it) caps the gate's confidence, agreement of every line of
  evidence lets it rise.

**A prior, not a rule.** Context never moves a gate, never forces a
population to exist, never overrides a stated relation. Intensity and the
image stay primary.

**Who said so** (`SOURCES`): `user` (the request), `metadata` (the
project's own fields), `inferred` (the panel's markers alone -- every
indicator of a disease present; weakest, and always reported as inferred).
"""

from __future__ import annotations

import copy
import functools
from pathlib import Path

from plexora.ai import vocabulary

PATH = Path(vocabulary.__file__).parent / "knowledge" / "biology.yaml"
SOURCES = ("user", "metadata", "inferred")
KINDS = ("tissue", "disease")
#: What a look is told of the Context Interpreter's reading (beside `said`).
INTERPRETATION_KEYS = ("populations_of_interest", "markers_mentioned_for_context",
                       "ambiguities", "confidence")
#: Project metadata fields that may name a tissue or a disease.
METADATA_FIELDS = ("tissue", "organ", "site", "disease", "diagnosis", "tumour_type",
                   "tumor_type", "cancer_type", "sample_type", "description")


def project_metadata(call, project) -> dict:
    """The free-text fields a project (and its dataset) carries that may name
    its tissue or disease: a dataset's description, a metadata mapping."""
    out = {}
    try:
        record = call.session.project(project)
    except Exception:
        return out
    for obj in (record, getattr(record, "dataset", None)):
        if obj is None:
            continue
        text = getattr(obj, "description", None)
        if isinstance(text, str) and text.strip():
            out.setdefault("description", text)
        meta = getattr(obj, "metadata", None)
        if isinstance(meta, dict):
            for key, value in meta.items():
                if isinstance(value, str):
                    out.setdefault(str(key), value)
    return out


@functools.lru_cache(maxsize=1)
def load() -> dict:
    import yaml

    with open(PATH, encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    contexts, lookup = {}, {}
    for entry in raw.get("contexts") or []:
        entry = {"structures": [], "expect": {}, "relations": [], "indicators": [],
                 "synonyms": [], **entry}
        if entry.get("kind") not in KINDS:
            raise ValueError(f"biology.yaml context {entry.get('name')!r}: kind is one of "
                             f"{KINDS}")
        for rel in entry["relations"]:
            if rel.get("relation") not in vocabulary.RELATIONS:
                raise ValueError(f"biology.yaml context {entry['name']!r}: relation "
                                 f"{rel.get('relation')!r}")
            if rel.get("confidence", "moderate") not in vocabulary.CONFIDENCE:
                raise ValueError(f"biology.yaml context {entry['name']!r}: confidence "
                                 f"{rel.get('confidence')!r}")
        contexts[entry["name"]] = entry
        for alias in [entry["name"], *entry["synonyms"]]:
            lookup.setdefault(vocabulary.fold(alias), entry["name"])
    return {"version": str(raw.get("version") or "0"), "contexts": contexts,
            "lookup": lookup}


def version() -> str:
    return load()["version"]


def match(text) -> list:
    """The known contexts named anywhere in `text` (a request, a metadata
    value), longest names first so `cutaneous melanoma` is not read as two."""
    if not text:
        return []
    folded = vocabulary.fold(text)
    found = []
    for alias in sorted(load()["lookup"], key=len, reverse=True):
        if alias and alias in folded:
            name = load()["lookup"][alias]
            if name not in found:
                found.append(name)
            folded = folded.replace(alias, " ")
    return found


def resolve(tissue=None, disease=None, notes=None, *, source="user", original=None,
            interpretation=None) -> dict:
    """The session's biology record: what was said, which known contexts it
    names (`contexts`), and what is not known (`unmatched`) -- an unknown
    tissue is kept as the user's words and passed to the looks, it just
    brings no relations. `original` is the user's note as written when the
    fields were interpreted from it (`plexora.ai.context`); it is kept, and
    shown to every look, beside the interpretation."""
    said = {k: v for k, v in (("tissue", tissue), ("disease", disease), ("notes", notes))
            if v}
    names, unmatched = [], []
    for field in ("tissue", "disease", "notes"):
        hits = match(said.get(field))
        names.extend(n for n in hits if n not in names)
        if said.get(field) and not hits and field != "notes":
            unmatched.append(said[field])
    if original and not names:
        # The interpreter may have kept a word the vocabulary knows only in
        # the user's spelling of it: the note itself is searched last.
        names.extend(n for n in match(original) if n not in names)
    record = {"version": version(), "source": source, "said": said, "contexts": names,
              "unmatched": unmatched}
    if original:
        record["original"] = str(original)
    if interpretation:
        record["interpretation"] = {k: interpretation[k] for k in INTERPRETATION_KEYS
                                    if interpretation.get(k)}
    return record


def infer(panel, metadata=None) -> dict | None:
    """Biology from the project's metadata fields, else from the panel's
    markers (a disease whose every indicator is in the panel), else None.
    Never as strong as the user's word: `source` says which."""
    values = []
    for key, value in (metadata or {}).items():
        if str(key).casefold() in METADATA_FIELDS and isinstance(value, str):
            values.append(value)
    if values:
        record = resolve(notes=" ; ".join(values), source="metadata")
        if record["contexts"]:
            return record
    canon = {e.get("canonical") for e in (panel.get("entries") or {}).values()}
    inferred = [name for name, ctx in load()["contexts"].items()
                if ctx["indicators"] and set(ctx["indicators"]) <= canon]
    if not inferred:
        return None
    return {"version": version(), "source": "inferred", "said": {},
            "contexts": inferred, "unmatched": [],
            "basis": {n: list(load()["contexts"][n]["indicators"]) for n in inferred}}


def contexts_of(record):
    known = load()["contexts"]
    return [known[n] for n in (record or {}).get("contexts") or [] if n in known]


def overlay(panel, record) -> dict:
    """The panel context with the session's biology laid over it (a copy;
    the stored panel is shared by every session of the panel and is never
    changed). Relations a context names are added, or set to its
    confidence, on vocabulary entries and tagged `context`; an entry the
    user, metadata or an agent stated is left as it is."""
    ctxs = contexts_of(record)
    if not ctxs:
        return panel
    out = copy.deepcopy(panel)
    entries = out.get("entries") or {}
    by_canonical = {}
    for marker, entry in entries.items():
        by_canonical.setdefault(entry.get("canonical") or marker, []).append(marker)
    changed = []
    for ctx in ctxs:
        for rel in ctx["relations"]:
            for a in by_canonical.get(rel["a"], []):
                entry = entries[a]
                if entry.get("source") not in (None, "vocabulary"):
                    continue
                for b in by_canonical.get(rel["b"], []):
                    if b == a:
                        continue
                    partners = entry.setdefault("partners", [])
                    hit = next((p for p in partners if p["marker"] == b
                                and p["relation"] == rel["relation"]), None)
                    confidence = rel.get("confidence", "moderate")
                    if hit is None:
                        partners.append({"marker": b, "relation": rel["relation"],
                                         "confidence": confidence, "context": ctx["name"]})
                    else:
                        hit.update(confidence=confidence, context=ctx["name"])
                    changed.append({"a": a, "b": b, "relation": rel["relation"],
                                    "confidence": confidence, "context": ctx["name"]})
    from plexora.plugins.gating.server.autogate import context

    out["biology"] = {"contexts": [c["name"] for c in ctxs], "relations": changed,
                      "source": (record or {}).get("source")}
    out["order"], out["order_basis"] = context.order(out)
    return out


def brief(record, panel, marker, references=()) -> dict | None:
    """What a look is told about the sample's biology for `marker` (and its
    references): the tissue and disease and who said so, what each of them
    marks here, and the structures they share -- the frame the image is read
    in. None when the session has no biology."""
    if not record or not (record.get("contexts") or record.get("said") or record.get("original")):
        return None
    entries = (panel or {}).get("entries") or {}

    def canon(m):
        return (entries.get(m) or {}).get("canonical") or m

    wanted = [marker, *[r for r in references if r != marker]]
    names = {canon(m): m for m in wanted}
    expect, structures = {}, []
    for ctx in contexts_of(record):
        for name, note in ctx["expect"].items():
            if name in names:
                expect.setdefault(names[name], []).append(f"{ctx['name']}: {note}")
        for structure in ctx["structures"]:
            if canon(marker) in structure["markers"]:
                structures.append({"name": structure["name"], "context": ctx["name"],
                                   "markers": [m for m in structure["markers"]],
                                   "where": structure["where"]})
    out = {"source": record.get("source"),
           "contexts": [f"{c['name']} ({c['kind']})" for c in contexts_of(record)]}
    if record.get("said"):
        out["said"] = record["said"]
    if record.get("original"):
        out["original"] = record["original"]
    if record.get("interpretation"):
        out["interpretation"] = record["interpretation"]
    if record.get("unmatched"):
        out["unmatched"] = record["unmatched"]
    if expect:
        out["expect"] = expect
    if structures:
        out["structures"] = structures
        if len(structures) > 1:
            out["ambiguity"] = ("this marker is expected in more than one structure here: "
                                "tell them apart by place, shape and the companion markers "
                                "before calling the positives one population")
    return out
