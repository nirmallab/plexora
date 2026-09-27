"""A panel's biology, resolved once: what each marker is and who its partners are.

Built from the shipped vocabulary (`plexora.ai.vocabulary`), then whatever the
project or dataset metadata and the user say, and only last from an agent --
and only for markers the vocabulary does not know. Two rules keep an agent's
biology from doing harm:

- **Vocabulary wins.** An agent's entry for a marker the vocabulary knows is
  kept as a suggestion and not used.
- **Context never moves a gate.** It chooses references, orders the markers,
  sets expectations -- and can lower a confidence or route a marker to review.
  Every threshold is still decided from the marker's own data and pixels.

Keyed by a hash of the panel's canonical names, so every image and every
dataset that shares a panel shares one context, and it is asked about once.
Stored under `<data_root>/.agent/gating/panels/<hash>/panel_context.json`.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

from plexora.ai import vocabulary

VERSION = "1"

#: The vocabulary's schema (`plexora.ai.vocabulary`), which a panel entry --
#: shipped, the user's, or an agent's -- is held to.
ROLES = vocabulary.ROLES
COMPARTMENTS = vocabulary.COMPARTMENTS
RELATIONS = vocabulary.RELATIONS
UNKNOWN_ROLE_RANK = len(ROLES)
CONFIDENCE_RANK = {c: len(vocabulary.CONFIDENCE) - 1 - i
                   for i, c in enumerate(vocabulary.CONFIDENCE)}
SOURCES = ("user", "metadata", "vocabulary", "ai")
#: Who may fill a panel entry through the tools: the scientist, or an agent.
TOOL_SOURCES = ("user", "ai")
#: Bounds on what one answer or call may say about a panel.
MAX_PARTNERS = 6
MAX_ENTRIES = 60
#: References shown beside a marker at most.
MAX_REFERENCES = 2
_LOCK = threading.Lock()


def panel_hash(markers) -> str:
    names = sorted({(vocabulary.canonical(m) or vocabulary.fold(m)) for m in markers})
    return hashlib.sha1("|".join(names).encode("utf-8")).hexdigest()[:16]


def root() -> Path:
    from plexora import paths

    return paths.agent_root() / "gating" / "panels"


def _path(hash_):
    return root() / hash_ / "panel_context.json"


def _entry_from_vocabulary(marker, canonical, panel_by_canonical):
    found = vocabulary.load()["entries"][canonical]
    partners = []
    for partner in found.get("partners") or []:
        for panel_marker in panel_by_canonical.get(partner["marker"], []):
            if panel_marker != marker:
                partners.append({"marker": panel_marker, "relation": partner["relation"],
                                 "confidence": partner.get("confidence", "moderate")})
    return {
        "marker": marker, "canonical": canonical, "role": found.get("role"),
        "compartment": found.get("compartment"), "lineage": found.get("lineage"),
        "binary": bool(found.get("binary", True)), "partners": partners,
        "caveats": list(found.get("caveats") or []),
        "expected_fraction": found.get("expected_fraction"),
        "source": "vocabulary",
    }


def build(markers, *, overrides=None, image_channels=None) -> dict:
    """The panel context for `markers` (the table's marker columns).

    `overrides` is {marker: {field: value}} from the user or metadata
    (`source` says which); `image_channels` lets a marker without an image
    channel be flagged, since it can never be looked at.
    """
    markers = list(markers)
    resolved = vocabulary.resolve_panel(markers)
    by_canonical = {}
    for marker, canonical in resolved.items():
        if canonical:
            by_canonical.setdefault(canonical, []).append(marker)
    entries = {}
    unresolved = []
    for marker in markers:
        canonical = resolved[marker]
        if canonical:
            entries[marker] = _entry_from_vocabulary(marker, canonical, by_canonical)
        else:
            entries[marker] = {"marker": marker, "canonical": None, "role": None,
                               "compartment": None, "lineage": None, "binary": True,
                               "partners": [], "caveats": [], "expected_fraction": None,
                               "source": None}
            unresolved.append(marker)
        if image_channels is not None and marker not in image_channels:
            entries[marker]["no_image_channel"] = True
    duplicates = {c: ms for c, ms in by_canonical.items() if len(ms) > 1}
    context = {"version": VERSION, "vocabulary_version": vocabulary.version(),
               "panel_hash": panel_hash(markers), "markers": markers, "entries": entries,
               "unresolved": unresolved, "duplicates": duplicates, "ai_suggestions": {},
               "order": [], "order_basis": {}}
    for marker, fields in (overrides or {}).items():
        apply_entry(context, marker, fields, source=fields.get("source", "user"))
    from plexora.agent.presets import nuclear_channel

    context["nuclear_channel"] = nuclear_channel(markers) or nuclear_channel(
        image_channels or [])
    context["order"], context["order_basis"] = order(context)
    return context


FIELDS = ("role", "compartment", "lineage", "binary", "partners", "caveats",
          "expected_fraction")


def apply_entry(context, marker, fields, *, source) -> dict:
    """Merge one marker's fields from `source`, by precedence.

    user > metadata > vocabulary > ai. An `ai` entry for a marker the
    vocabulary knows is kept under `ai_suggestions` and not applied.
    Partners must name markers of this panel.
    """
    if source not in SOURCES:
        raise ValueError(f"source is one of {SOURCES}")
    if marker not in context["entries"]:
        raise KeyError(f"{marker!r} is not a marker of this panel")
    entry = context["entries"][marker]
    current = entry.get("source")
    fields = {k: v for k, v in fields.items() if k in FIELDS}
    if "partners" in fields:
        clean = []
        for partner in fields["partners"] or []:
            name = partner.get("marker")
            if name not in context["entries"] or name == marker:
                raise ValueError(f"partner {name!r} is not another marker of this panel")
            if partner.get("relation") not in RELATIONS:
                raise ValueError(f"a partner's relation is one of {RELATIONS}")
            clean.append({"marker": name, "relation": partner["relation"],
                          "confidence": partner.get("confidence", "low")})
        fields["partners"] = clean
    if "role" in fields and fields["role"] not in ROLES:
        raise ValueError(f"role is one of {ROLES}")
    rank = {s: i for i, s in enumerate(SOURCES)}
    if source == "ai" and entry.get("canonical"):
        # The vocabulary knows this marker: an agent's account of it is kept
        # for the record and not used.
        context["ai_suggestions"][marker] = fields
        return entry
    if current and rank.get(current, len(SOURCES)) < rank[source]:
        context.setdefault("lower_precedence", {})[marker] = {"source": source, **fields}
        return entry
    entry.update(fields)
    entry["source"] = source
    if marker in context["unresolved"] and entry.get("role"):
        context["unresolved"] = [m for m in context["unresolved"] if m != marker]
    context["order"], context["order_basis"] = order(context)
    return entry


def role_rank(entry) -> int:
    role = (entry or {}).get("role")
    return ROLES.index(role) if role in ROLES else UNKNOWN_ROLE_RANK


def _edges(context):
    """partner -> dependent: the partner is gated first. Only for partners of
    moderate or high confidence, from a bucket no later than the dependent's,
    and within a bucket only when the dependent is a subset of the partner."""
    entries = context["entries"]
    edges = {}
    for marker, entry in entries.items():
        for partner in entry.get("partners") or []:
            if CONFIDENCE_RANK.get(partner.get("confidence"), 0) < 1:
                continue
            ref = partner["marker"]
            if ref not in entries:
                continue
            a, b = role_rank(entries[ref]), role_rank(entry)
            if a < b or (a == b and partner["relation"] == "subset"):
                edges.setdefault(ref, set()).add(marker)
    return edges


def order(context, t1_scores=None):
    """(gating order, basis) -- role buckets, partners before dependents,
    ties by how many dependents a marker unlocks, then T1 score, then name.
    `context` markers are left out (never gated)."""
    entries = context["entries"]
    t1_scores = t1_scores or {}
    gated = [m for m in context["markers"] if entries[m].get("role") != "context"]
    edges = _edges(context)
    indegree = {m: 0 for m in gated}
    for ref, dependents in edges.items():
        for dependent in dependents:
            if dependent in indegree and ref in indegree:
                indegree[dependent] += 1
    unlocks = {m: len(edges.get(m, ())) for m in gated}

    def priority(m):
        return (role_rank(entries[m]), -unlocks[m], -(t1_scores.get(m) or 0.0), m)

    ready = sorted([m for m in gated if indegree[m] == 0], key=priority)
    out = []
    basis = {}
    remaining = set(gated)
    while remaining:
        if not ready:
            # A cycle: break it at the earliest bucket, best score.
            stuck = sorted(remaining, key=priority)[0]
            ready = [stuck]
            basis[stuck] = "cycle broken"
        marker = ready.pop(0)
        if marker not in remaining:
            continue
        remaining.discard(marker)
        out.append(marker)
        basis.setdefault(marker, (entries[marker].get("role") or "unknown role"))
        for dependent in sorted(edges.get(marker, ())):
            if dependent in indegree:
                indegree[dependent] -= 1
                if indegree[dependent] == 0 and dependent in remaining:
                    ready.append(dependent)
        ready.sort(key=priority)
    return out, basis


def references_for(context, marker, gated, *, limit=MAX_REFERENCES):
    """Partners that may serve as references for `marker` in this run.

    `gated` is {marker: confidence} for markers already gated in this run
    (`high`, `moderate`, `low`, ...). A partner qualifies when the vocabulary
    (or the user) gives the relation at moderate or high confidence, it was
    gated at moderate or better, and it is not itself a state or signalling
    readout.
    """
    entry = context["entries"].get(marker) or {}
    choices = []
    for partner in entry.get("partners") or []:
        ref = partner["marker"]
        if CONFIDENCE_RANK.get(partner.get("confidence"), 0) < 1:
            continue
        if gated.get(ref) not in ("high", "moderate"):
            continue
        if (context["entries"].get(ref) or {}).get("role") in ("state", "signalling"):
            continue
        rank = RELATIONS.index(partner["relation"])
        choices.append((rank, -CONFIDENCE_RANK[partner["confidence"]], ref, partner))
    choices.sort()
    return [c[3] for c in choices[:limit]]


# -- storage -------------------------------------------------------------------


def load(hash_) -> dict | None:
    path = _path(hash_)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save(context) -> str:
    path = _path(context["panel_hash"])
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(context, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    return revision(context)


def revision(context) -> str:
    blob = json.dumps(context, sort_keys=True).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()[:16]


def for_project(ds, *, create=True) -> dict:
    """The stored context for a project's panel, or a fresh one (saved when
    `create`), refreshed if the vocabulary changed since."""
    markers = list(ds.table.markers)
    channels = [c.get("fullname") or c.get("name") for c in ds.project.image.real_channels]
    hash_ = panel_hash(markers)
    stored = load(hash_)
    if stored is not None and stored.get("vocabulary_version") == vocabulary.version() \
            and stored.get("markers") == markers:
        return stored
    overrides = {}
    if stored is not None:
        for marker, entry in (stored.get("entries") or {}).items():
            if entry.get("source") in ("user", "metadata", "ai") and marker in markers:
                overrides[marker] = {**{k: entry.get(k) for k in FIELDS
                                        if entry.get(k) is not None},
                                     "source": entry["source"]}
    context = build(markers, overrides=overrides, image_channels=channels)
    if create:
        save(context)
    return context
