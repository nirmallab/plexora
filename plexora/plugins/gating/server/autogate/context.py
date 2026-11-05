"""A panel's biology, resolved once: what each marker is and who its partners are.

Built from the shipped vocabulary (`plexora.ai.vocabulary`), then whatever the
project or dataset metadata and the user say, and only last from an agent --
and only for markers the vocabulary does not know. What an agent or the user
says about such a marker is also taught to the vocabulary's learned layer
(`learn_from`), so the next panel that has it -- any panel, on any project
using this data root -- knows it without asking. Two rules keep an agent's
biology from doing harm:

- **Vocabulary wins.** An agent's entry for a marker the vocabulary knows (the
  shipped layer, or the learned one: the first account stands) is kept as a
  suggestion and not used.
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
import re
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
#: Precedence, highest first: `learned` is the vocabulary's learned layer --
#: an earlier session's account of a marker the shipped file does not know.
SOURCES = ("user", "metadata", "vocabulary", "learned", "ai")
#: Who may fill a panel entry through the tools: the scientist, or an agent.
TOOL_SOURCES = ("user", "ai")
#: Bounds on what one answer or call may say about a panel.
MAX_PARTNERS = 6
MAX_ENTRIES = 60
#: References shown beside a marker at most.
MAX_REFERENCES = 2
_LOCK = threading.Lock()


def panel_hash(markers) -> str:
    # The shipped layer only: a marker learned later must not move a panel's
    # stored context to a new file (and leave the user's entries behind).
    names = sorted({(vocabulary.canonical(m, learned=False) or vocabulary.fold(m))
                    for m in markers})
    return hashlib.sha1("|".join(names).encode("utf-8")).hexdigest()[:16]


def root() -> Path:
    from plexora import paths

    return paths.agent_root() / "gating" / "panels"


def _path(hash_):
    return root() / hash_ / "panel_context.json"


def _entry_from_vocabulary(marker, canonical, panel_by_canonical):
    found = vocabulary.load()["entries"][canonical]
    partners, missing = [], []
    for partner in found.get("partners") or []:
        here = [m for m in panel_by_canonical.get(partner["marker"], []) if m != marker]
        if not here and partner["relation"] == "subset_any":
            missing.append(partner["marker"])
        for panel_marker in here:
            partners.append({"marker": panel_marker, "relation": partner["relation"],
                             "confidence": partner.get("confidence") or vocabulary.DEFAULT_CONFIDENCE})
    entry = {
        "marker": marker, "canonical": canonical, "role": found.get("role"),
        "compartment": found.get("compartment"), "lineage": found.get("lineage"),
        "expression": found.get("expression"),
        "binary": bool(found.get("binary", True)), "partners": partners,
        "caveats": list(found.get("caveats") or []),
        "expected_fraction": found.get("expected_fraction"),
        "morphology": found.get("morphology") or "cell",
        "source": "learned" if found.get("origin") == "learned" else "vocabulary",
    }
    if missing:
        # A lineage the group names that this panel does not mark: positives
        # outside the members here are expected, so the group is not drawn.
        entry["subset_any_missing"] = missing
    return entry


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
                               "compartment": None, "lineage": None, "expression": None,
                               "binary": True,
                               "partners": [], "caveats": [], "expected_fraction": None,
                               "morphology": None, "source": None}
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
          "expected_fraction", "morphology")
MORPHOLOGIES = vocabulary.MORPHOLOGIES


def learn_from(context, markers, *, source, about=None) -> list:
    """Teach the vocabulary's learned layer what was just applied (by
    `source`, `ai` or `user`) for those of `markers` it did not know, partners
    named by their canonical names (`vocabulary.learn`). Returns the names
    learned; never raises -- a panel answer is applied whether or not the
    file could be written. Only a person's entries are learned: a reader's
    stay with the project they were given for until someone confirms them."""
    if source != "user":
        return []
    teach = []
    for marker in markers or ():
        entry = (context.get("entries") or {}).get(marker) or {}
        if entry.get("canonical") or entry.get("source") != source or not entry.get("role"):
            continue
        name = vocabulary.plain_name(marker)
        partners = [{"marker": vocabulary.canonical(p["marker"])
                     or vocabulary.plain_name(p["marker"]),
                     "relation": p["relation"], "confidence": p.get("confidence", "low")}
                    for p in entry.get("partners") or [] if p.get("marker")]
        teach.append({"canonical": name, "role": entry["role"],
                      "compartment": entry.get("compartment"), "lineage": entry.get("lineage"),
                      "binary": entry.get("binary"), "partners": partners,
                      "caveats": entry.get("caveats"),
                      "expected_fraction": entry.get("expected_fraction"),
                      "morphology": entry.get("morphology"),
                      "synonyms": [marker] if name != marker else None})
    if not teach:
        return []
    try:
        return vocabulary.learn(teach, source=source, about=about)
    except (OSError, ValueError):
        return []


def apply_entry(context, marker, fields, *, source) -> dict:
    """Merge one marker's fields from `source`, by precedence.

    user > metadata > vocabulary > learned > ai. An `ai` entry for a marker
    the vocabulary knows (either layer) is kept under `ai_suggestions` and not
    applied.
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
    if fields.get("morphology") is not None and fields["morphology"] not in MORPHOLOGIES:
        raise ValueError(f"morphology is one of {MORPHOLOGIES}")
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


# -- proposed partners and the partner review --------------------------------------

#: Roles a planner is never asked about: a counterstain or control, and the
#: signalling readouts (gated only on request).
_NOT_ASKED = ("context", "signalling")
#: The most a proposed relation may claim (the vocabulary's own word is
#: `high`), and how long its basis may be.
PROPOSED_CONFIDENCE = "moderate"
MAX_BASIS = 200
#: A planner's word on a relation the panel states, in the sample's context.
CHECK_VERDICTS = ("holds", "weaker_here", "does_not_hold_here")
#: Who must have named the tissue or disease before a stated relation may
#: change: a person or the project's metadata, never an inference from the
#: panel. Without one the review still fills empty partner slots.
REVIEW_SOURCES = ("user", "metadata")
#: The partner tile a relation fills: expected (`co`) or not expected (`anti`).
SLOTS = {"subset": "co", "coexpressed": "co", "subset_any": "co", "exclusive": "anti"}
#: Entry sources whose relations a review may change: the shipped and the
#: learned vocabulary -- never what a person or metadata stated.
REVIEWABLE_SOURCES = ("vocabulary", "learned")
#: Markers per review packet, and how many times each is asked: a change is
#: made only when both answers give it (`settle_review`).
REVIEW_BATCH = 12
REVIEW_DRAWS = (1, 2)
#: The most one context may change of a panel: more agreed changes than
#: max(n, share x the stated relations) and none is made -- a planner
#: rewriting the panel is confused, and the vocabulary stands.
MAX_CHANGES = (3, 0.25)
#: Words of a note too common to tie a change to a context.
_COMMON = {"sample", "samples", "tissue", "tissues", "from", "with", "the", "and", "this",
           "that", "cells", "cell", "patient", "biopsy", "section", "slide", "human"}
#: A state marker's carriers: the lineages of this panel whose cells carry it
#: (PD-1: T cells, B cells, activated myeloid cells), read from these roles,
#: at most this many; what its looks and its start are drawn from beside the
#: whole population (`Engine.carrier_populations`).
CARRIER_ROLES = ("lineage_reliable", "lineage_other", "tumour_stromal")
MAX_CARRIERS = 4
#: What a stored review holds: a review kept before carriers were asked for
#: (1), or before they were asked only where the marker is common (2), is
#: asked again (`review_done`), once.
REVIEW_VERSION = "3"


def _askable(entry) -> bool:
    return bool(entry.get("canonical")) and entry.get("role") not in _NOT_ASKED \
        and not entry.get("no_image_channel")


def empty_slots(context, marker) -> set:
    """The partner tiles nothing stated fills for `marker`: `co` when it
    names no subset, co-expressed or group partner, `anti` when no exclusion
    names it either way (PD-L1: both; CD45: `co`). What the review may fill
    whatever the context (`settle_review`)."""
    entries = context.get("entries") or {}
    filled = {SLOTS[p["relation"]] for p in (entries.get(marker) or {}).get("partners") or []
              if not p.get("proposed") and p.get("relation") in SLOTS}
    if any(p.get("marker") == marker and p.get("relation") == "exclusive"
           and not p.get("proposed")
           for name, entry in entries.items() if name != marker
           for p in entry.get("partners") or []):
        filled.add("anti")
    return {"co", "anti"} - filled


def stated_context(biology_record) -> bool:
    """Whether a person or the metadata named the sample's tissue or disease
    (an inference from the panel is not one): what a change to a stated
    relation needs."""
    record = biology_record or {}
    return record.get("source") in REVIEW_SOURCES and bool(
        record.get("said") or record.get("contexts"))


def review_markers(context, biology_record, markers) -> list:
    """The markers of `markers` the partner review covers: every askable one,
    whatever the context. Each may have empty slots to fill (PD-L1 names no
    partner, CD45 no expected one), and a named tissue or disease may break
    a relation the vocabulary states in general."""
    entries = context.get("entries") or {}
    wanted = set(markers)
    return [m for m in context.get("markers") or entries
            if m in wanted and _askable(entries.get(m) or {})]


def proposal_key(biology_record) -> str:
    """What proposed partners and reviews are kept under: the tissue and
    disease the session was told (folded) and the contexts they name. The
    same panel in another context is asked again."""
    record = biology_record or {}
    said = record.get("said") or {}
    parts = [f"{k}={vocabulary.fold(said[k])}" for k in ("tissue", "disease") if said.get(k)]
    parts += [f"context={c}" for c in sorted(record.get("contexts") or ())]
    return ";".join(parts) or "unspecified"


def context_words(biology_record) -> set:
    """The words of the sample's context a change must name to be tied to it:
    the tissue, disease and note as said, the user's own note, the contexts."""
    record = biology_record or {}
    said = record.get("said") or {}
    text = " ".join([*(str(v) for v in said.values() if v), str(record.get("original") or ""),
                     *(str(c) for c in record.get("contexts") or ())])
    return {w for w in re.findall(r"[a-z0-9]+", text.casefold())
            if len(w) >= 4 and w not in _COMMON}


def names_context(basis, words) -> bool:
    return bool(words) and bool(set(re.findall(r"[a-z0-9]+", str(basis or "").casefold()))
                                & set(words))


def proposals_for(context, biology_record) -> dict:
    """{marker: [partner]} a settled review left for the tiles only: what it
    proposed for an empty slot without being sure."""
    return dict((context.get("proposed") or {}).get(proposal_key(biology_record)) or {})


def store_proposals(context, biology_record, proposals):
    """Keep tiles-only `proposals` ({marker: [partner]}) with the panel under
    the context's key. The caller saves the context."""
    context.setdefault("proposed", {}).setdefault(proposal_key(biology_record), {}).update(
        {m: [dict(p) for p in ps] for m, ps in proposals.items()})


def apply_proposals(context, proposals):
    """Lay tiles-only proposals over a panel context (a copy), each tagged
    `proposed`, beside what the entry states and only into a slot still
    empty: what a look's partner tiles draw -- after every stated partner of
    their kind (`partners.partner_plan`) -- never what weighs a gate
    (`hierarchy` leaves them out)."""
    entries = context.get("entries") or {}
    for marker, partners in (proposals or {}).items():
        entry = entries.get(marker)
        if entry is None or not partners:
            continue
        empty = empty_slots(context, marker)
        stated = {p.get("marker") for p in entry.get("partners") or []}
        entry["partners"] = list(entry.get("partners") or []) + [
            {**p, "proposed": True} for p in partners
            if p.get("marker") in entries and p.get("marker") not in stated
            and SLOTS.get(p.get("relation")) in empty]
    return context


def _confidence(word, cap=None):
    rank = CONFIDENCE_RANK.get(word, 0)
    if cap is not None:
        rank = min(rank, CONFIDENCE_RANK[cap])
    return vocabulary.CONFIDENCE[len(vocabulary.CONFIDENCE) - 1 - rank]


def _stated(context, marker) -> dict:
    """{partner: relation} the panel states between `marker` and another
    marker, either way round (a planner's tiles-only proposals left out)."""
    from plexora.plugins.gating.server.autogate import hierarchy

    entries = context.get("entries") or {}
    out = {}
    for p in (entries.get(marker) or {}).get("partners") or []:
        if not p.get("proposed"):
            out.setdefault(p["marker"], p["relation"])
    for r in hierarchy.relations(context, marker):
        if not r["derived"]:
            out.setdefault(r["marker"], r["relation"])
    return out


def check_proposal(context, marker, partners) -> tuple:
    """(accepted, refused) for the partners a planner proposed for `marker`:
    the rules a prompt cannot be trusted to keep.

    A partner is another gated marker of this panel the panel does not
    already relate to `marker` (a stated relation is reviewed, never stated
    twice), with a relation and a `basis` (the note's words and the lineage
    that justify it); one relation per partner, applied at most at
    `PROPOSED_CONFIDENCE` (`said` keeps the planner's own word). Two `subset`
    partners that exclude each other cannot both hold -- the positives would
    sit in both; `subset_any` says either. An `exclusive` partner is never
    one the positives sit in, nor a parent of one, stated or proposed (PD-L1
    on CD11c cells cannot exclude CD45). A lone `subset_any` is a `subset`."""
    from plexora.plugins.gating.server.autogate import hierarchy

    entries = context.get("entries") or {}
    stated = _stated(context, marker)
    accepted, refused, seen = [], [], set()

    def refuse(partner, why):
        refused.append({"marker": partner.get("marker"), "relation": partner.get("relation"),
                        "reason": why})

    for raw in partners or ():
        partner = dict(raw or {})
        name, relation = partner.get("marker"), partner.get("relation")
        basis = str(partner.get("basis") or "").strip()
        if name not in entries or name == marker or entries[name].get("role") == "context":
            refuse(partner, "not another gated marker of this panel")
        elif relation not in RELATIONS:
            refuse(partner, f"the relation is one of {RELATIONS}")
        elif not basis:
            refuse(partner, "no basis: the note's words and the lineage that justify it")
        elif name in stated:
            refuse(partner, f"the panel already states {stated[name]} {name}: a stated "
                            "relation is reviewed (`checks`), not proposed again")
        elif name in seen:
            refuse(partner, "one relation per partner")
        else:
            seen.add(name)
            said = _confidence(partner.get("confidence"))
            accepted.append({"marker": name, "relation": relation,
                             "confidence": _confidence(said, PROPOSED_CONFIDENCE),
                             "said": said, "basis": basis[:MAX_BASIS]})

    def rels(name):
        return hierarchy.relations(context, name)

    def parents(name):
        return {r["marker"] for r in rels(name)
                if r["relation"] == "subset" and r["direction"] == "forward"}

    subsets = [p["marker"] for p in accepted if p["relation"] == "subset"]
    clash = {a for a in subsets for b in subsets if a != b and any(
        r["marker"] == b and r["relation"] == "exclusive" for r in rels(a))}
    above = set()
    positives = [n for n, r in stated.items() if r in ("subset", "coexpressed", "subset_any")]
    positives += [p["marker"] for p in accepted
                  if p["relation"] != "exclusive" and p["marker"] not in clash]
    for name in positives:
        above |= {name} | parents(name)
    kept = []
    for p in accepted:
        if p["marker"] in clash:
            refuse(p, "two subset partners that exclude each other: the positives cannot sit "
                      "in both (subset_any says either)")
        elif p["relation"] == "exclusive" and p["marker"] in above:
            refuse(p, "excludes a lineage the positives sit in, or a parent of one")
        else:
            kept.append(p)
    group = [p for p in kept if p["relation"] == "subset_any"]
    if len(group) == 1:
        group[0]["relation"] = "subset"
    return kept, refused


def wants_carriers(entry) -> bool:
    """A state marker (PD-1, Ki-67, FOXP3): carried by cells of other
    lineages, so its looks and start lean on theirs. A lineage marker is its
    own carrier."""
    return (entry or {}).get("role") == "state"


def _is_carrier(context, name) -> bool:
    entry = (context.get("entries") or {}).get(name) or {}
    return entry.get("role") in CARRIER_ROLES and not entry.get("no_image_channel")


def check_carriers(context, marker, names) -> tuple:
    """(kept, refused) for the carriers a planner named for `marker`: each a
    lineage marker of this panel with an image channel, not the marker, not
    one the panel states it excludes, once; at most `MAX_CARRIERS`."""
    entries = context.get("entries") or {}
    if not wants_carriers(entries.get(marker)):
        return [], [{"marker": n, "reason": "only a state marker has carriers"}
                    for n in names or ()]
    stated = _stated(context, marker)
    kept, refused = [], []
    for name in names or ():
        if name not in entries or name == marker or not _is_carrier(context, name):
            refused.append({"marker": name, "reason": "not a lineage marker of this panel "
                                                      f"({', '.join(CARRIER_ROLES)})"})
        elif stated.get(name) == "exclusive":
            refused.append({"marker": name, "reason": "the panel states it excludes this marker"})
        elif name in kept:
            refused.append({"marker": name, "reason": "named twice"})
        elif len(kept) >= MAX_CARRIERS:
            refused.append({"marker": name, "reason": f"at most {MAX_CARRIERS} carriers"})
        else:
            kept.append(name)
    return kept, refused


def most_specific(context, names) -> list:
    """`names` less any a narrower one of them sits in (CD45 goes when CD3,
    a subset of CD45, is there too): the carrier is the lineage itself."""
    from plexora.plugins.gating.server.autogate import hierarchy

    names = list(dict.fromkeys(names))
    wider = set()
    for name in names:
        wider |= {r["marker"] for r in hierarchy.relations(context, name)
                  if r["relation"] == "subset" and r["direction"] == "forward"}
    return [n for n in names if n not in wider]


def carriers_for(context, marker) -> list:
    """[{marker, source}]: the lineages whose cells carry `marker` -- its
    stated `subset`/`subset_any` partners of a lineage role (`vocabulary`)
    and those a settled review named (`planner`, laid on by
    `apply_carriers`), the narrowest only. [] for a marker that is not a
    state marker."""
    entry = (context.get("entries") or {}).get(marker) or {}
    if not wants_carriers(entry):
        return []
    stated = [p["marker"] for p in entry.get("partners") or []
              if p.get("relation") in ("subset", "subset_any") and not p.get("proposed")
              and _is_carrier(context, p["marker"])]
    planned = [n for n in entry.get("planner_carriers") or () if _is_carrier(context, n)]
    source = {n: "planner" for n in planned}
    source.update({n: "vocabulary" for n in stated})
    return [{"marker": n, "source": source[n]}
            for n in most_specific(context, [*stated, *planned])]


def wider_carriers(context, marker) -> list:
    """[{marker, source}]: the stated carriers `carriers_for` left out as
    wider than one it kept, narrowest first (PD-1: CD3, then CD45). Where
    `Engine.carrier_populations` climbs when none of the narrowest carries
    the marker more often than the average cell: it is concentrated in its
    narrowest carriers, diluted in the wider ones."""
    entry = (context.get("entries") or {}).get(marker) or {}
    if not wants_carriers(entry):
        return []
    kept = {c["marker"] for c in carriers_for(context, marker)}
    out = []
    for p in entry.get("partners") or []:
        if (p.get("relation") in ("subset", "subset_any") and not p.get("proposed")
                and _is_carrier(context, p["marker"]) and p["marker"] not in kept
                and p["marker"] not in {c["marker"] for c in out}):
            out.append({"marker": p["marker"], "source": "vocabulary"})
    return out


def carriers_planned(context, biology_record) -> dict:
    """{marker: [carrier]} a settled review named in this context."""
    return dict(review_for(context, biology_record).get("carriers") or {})


def apply_carriers(context, carriers):
    """Lay a settled review's carriers on a panel context (a copy) as
    `planner_carriers`: read by `carriers_for` and `Engine._links` only --
    never a relation that weighs a gate."""
    entries = context.get("entries") or {}
    for marker, names in (carriers or {}).items():
        if marker in entries and names:
            entries[marker]["planner_carriers"] = [n for n in names if n in entries]
    return context


def reviewable(context, marker) -> list:
    """[{marker, relation, confidence}] the review may change for `marker`:
    the relations its own entry states, when the vocabulary stated them --
    never a person's, the metadata's or a tissue context's (`biology`),
    which already speak for this sample."""
    entry = (context.get("entries") or {}).get(marker) or {}
    if entry.get("source") not in REVIEWABLE_SOURCES:
        return []
    return [{"marker": p["marker"], "relation": p["relation"],
             "confidence": p.get("confidence") or vocabulary.DEFAULT_CONFIDENCE}
            for p in entry.get("partners") or []
            if not (p.get("proposed") or p.get("planner") or p.get("context"))]


def check_review(context, marker, checks) -> tuple:
    """(kept, refused) for a planner's verdicts on `marker`'s relations: each
    names a relation `reviewable` lists, once, with a verdict and -- unless it
    holds -- a basis. A `holds` is dropped (the relation stands)."""
    stated = {p["marker"]: p["relation"] for p in reviewable(context, marker)}
    kept, refused, seen = [], [], set()
    for raw in checks or ():
        check = dict(raw or {})
        name, verdict = check.get("marker"), check.get("verdict")
        basis = str(check.get("basis") or "").strip()

        def refuse(why):
            refused.append({"marker": name, "verdict": verdict, "reason": why})

        if name not in stated:
            refuse("not a relation the vocabulary states for this marker")
        elif verdict not in CHECK_VERDICTS:
            refuse(f"the verdict is one of {CHECK_VERDICTS}")
        elif name in seen:
            refuse("one verdict per relation")
        elif verdict == "holds":
            seen.add(name)
        elif not basis:
            refuse("no basis: the context's words that make it hold less here")
        else:
            seen.add(name)
            kept.append({"marker": name, "relation": stated[name], "verdict": verdict,
                         "said": _confidence(check.get("confidence")),
                         "basis": basis[:MAX_BASIS]})
    return kept, refused


def review_for(context, biology_record) -> dict:
    """The partner review of this panel in this context: {version, markers,
    draws: {draw: {marker: {checks, partners, carriers}}}, done, changes,
    unsure, note, tiles, carriers}."""
    return dict((context.get("reviewed") or {}).get(proposal_key(biology_record)) or {})


def review_done(context, biology_record, markers) -> bool:
    review = review_for(context, biology_record)
    return bool(review.get("done")) and review.get("version") == REVIEW_VERSION \
        and set(markers) <= set(review.get("markers") or ())


def review_queue(markers) -> list:
    """[{batch, draw, markers}]: every batch asked once per draw."""
    batches = [list(markers[i:i + REVIEW_BATCH])
               for i in range(0, len(markers), REVIEW_BATCH)]
    return [{"batch": b, "draw": d, "markers": batch}
            for b, batch in enumerate(batches) for d in REVIEW_DRAWS]


def store_review_draw(context, biology_record, draw, answers, markers, inferred=None):
    """Keep one draw's checked answers ({marker: {checks, partners,
    carriers}}) for a batch of `markers`, and the context it read off the
    panel when none was named (`inferred`: {tissue, disease, confidence,
    basis}). The caller saves the context."""
    review = context.setdefault("reviewed", {}).setdefault(proposal_key(biology_record), {})
    if review.get("done") or review.get("version") != REVIEW_VERSION:
        # Asked again (the panel gained markers, or a review kept before
        # carriers were asked for): a fresh review, nothing of the settled
        # one carried into it.
        review.clear()
    review["version"] = REVIEW_VERSION
    review.setdefault("markers", [])
    review["markers"] = list(dict.fromkeys([*review["markers"], *markers]))
    review.setdefault("draws", {}).setdefault(str(draw), {}).update(
        {m: answers.get(m) or {"checks": [], "partners": [], "carriers": []} for m in markers})
    if inferred:
        review.setdefault("inferred", {}).setdefault(str(draw), dict(inferred))
    review["done"] = False


def review_context(review, biology_record) -> dict | None:
    """{source, words, ...}: the context a change to a stated relation must be
    tied to, or None. The one a person or the metadata named; else the one
    the panel's markers imply (`biology.infer`: every indicator of a context
    present); else the one both draws read off the panel at `high` with the
    same tissue and disease. Always said to be inferred when it is."""
    record = biology_record or {}
    if stated_context(record):
        return {"source": record.get("source"), "words": sorted(context_words(record)),
                **{k: record[k] for k in ("said", "contexts") if record.get(k)}}
    if record.get("source") == "inferred" and record.get("contexts"):
        return {"source": "inferred from the panel's markers",
                "words": sorted(context_words(record)), "contexts": record["contexts"]}
    inferred = (review or {}).get("inferred") or {}
    a, b = (inferred.get(str(d)) or {} for d in REVIEW_DRAWS)

    def named(x):
        return tuple(vocabulary.fold(x.get(k) or "") for k in ("tissue", "disease"))

    if a.get("confidence") == "high" == b.get("confidence") and named(a) == named(b) \
            and any(named(a)):
        said = {k: a[k] for k in ("tissue", "disease") if a.get(k)}
        return {"source": "inferred by the planner from the panel (both draws)",
                "words": sorted(context_words({"said": said})), "said": said,
                "basis": a.get("basis")}
    return None


def dropped_pairs(context) -> set:
    """{(marker, partner, relation)} a settled review dropped, both ways round
    for a relation that runs both ways."""
    out = set()
    for a, b, relation in context.get("dropped") or ():
        out.add((a, b, relation))
        if relation != "subset":
            out.add((b, a, relation))
    return out


def changes_for(context, biology_record) -> dict:
    """{drop, weaken, add}: what a settled review changes in this context."""
    return dict(review_for(context, biology_record).get("changes") or {})


def apply_changes(context, changes):
    """Lay a settled review's changes over a panel context (a copy): a
    dropped relation leaves both entries that state it, a weakened one goes
    one confidence step down, an addition joins at `PROPOSED_CONFIDENCE`
    tagged `planner` -- evidence like a vocabulary relation, never boosted
    like a tissue context's. Only relations `reviewable` would list are
    touched."""
    entries = context.get("entries") or {}
    symmetric = ("exclusive", "coexpressed")

    def touched(a, b, relation):
        hits = []
        for x, y in ((a, b), (b, a)):
            if (x, y) != (a, b) and relation not in symmetric:
                continue
            entry = entries.get(x) or {}
            if entry.get("source") not in REVIEWABLE_SOURCES:
                continue
            hits += [p for p in entry.get("partners") or [] if p.get("marker") == y
                     and p.get("relation") == relation
                     and not (p.get("proposed") or p.get("planner") or p.get("context"))]
        return hits

    for change in (changes or {}).get("drop") or ():
        gone = {id(p) for p in touched(change["marker"], change["partner"], change["relation"])}
        for name in (change["marker"], change["partner"]):
            entry = entries.get(name)
            if entry is not None:
                entry["partners"] = [p for p in entry.get("partners") or [] if id(p) not in gone]
        # Nor derived back through the tree (CD68 within CD45, CD45
        # exclusive of Pan-CK): `hierarchy.relations` and the partner plan
        # read this.
        context.setdefault("dropped", []).append(
            [change["marker"], change["partner"], change["relation"]])
    for change in (changes or {}).get("weaken") or ():
        for p in touched(change["marker"], change["partner"], change["relation"]):
            p["confidence"] = vocabulary.CONFIDENCE[min(
                vocabulary.CONFIDENCE.index(p.get("confidence") or vocabulary.DEFAULT_CONFIDENCE) + 1,
                len(vocabulary.CONFIDENCE) - 1)]
    for change in (changes or {}).get("add") or ():
        entry = entries.get(change["marker"])
        if entry is None or change["partner"] not in entries:
            continue
        entry.setdefault("partners", []).append(
            {"marker": change["partner"], "relation": change["relation"],
             "confidence": PROPOSED_CONFIDENCE, "planner": True,
             "basis": change.get("basis")})
    return context


def settle_review(context, biology_record, view=None) -> dict:
    """Decide a review once every draw is in, and keep the outcome under the
    context's key in `context` (the stored panel): {changes, unsure, note}.
    `view` is the panel the session reads (its tissue's relations laid on,
    `biology.overlay`), what the rules are checked against; default
    `context`.

    A change is made only when it is sure: both draws give it identically,
    both at `high`, and it still passes the rules on the panel as changed so
    far (drops and weakenings first, then additions through
    `check_proposal`). Filling an empty slot (`empty_slots`) needs no more.
    Changing what the vocabulary states -- a drop, a weakening, an addition
    beside a stated partner of the same kind -- also needs a context
    (`review_context`) that each basis names: a relation that holds in
    general is broken only by a tissue or disease. More agreed changes than
    `MAX_CHANGES` allows and none is made. Whatever is not sure is listed in
    `unsure`, and the stated relations stand; draw 1's proposals for empty
    slots that were not made draw the tiles only (`store_proposals`)."""
    import copy

    view = context if view is None else view
    key = proposal_key(biology_record)
    review = (context.get("reviewed") or {}).get(key) or {}
    draws = review.get("draws") or {}
    first, second = (draws.get(str(d)) or {} for d in REVIEW_DRAWS)
    ctx = review_context(review, biology_record)
    words = set((ctx or {}).get("words") or ())

    def tied(item):
        return ctx is not None and names_context(item.get("basis"), words)

    def sure_check(marker, item):
        return item.get("said") == "high" and tied(item)

    def sure_add(marker, item):
        return item.get("said") == "high" and (
            SLOTS.get(item.get("relation")) in empty_slots(view, marker) or tied(item))

    def listed(marker, item, **more):
        return {"marker": marker, "partner": item.get("marker"),
                **{k: v for k, v in item.items() if k != "marker"}, **more}

    def agreed(kind, same, sure):
        out, unsure = [], []
        for marker in review.get("markers") or ():
            mine = (first.get(marker) or {}).get(kind) or []
            theirs = (second.get(marker) or {}).get(kind) or []
            for item in mine:
                twin = next((t for t in theirs if same(item, t)), None)
                if twin is not None and sure(marker, item) and sure(marker, twin):
                    out.append((marker, item))
                else:
                    unsure.append(listed(marker, item, draws=2 if twin is not None else 1))
            unsure += [listed(marker, t, draws=1) for t in theirs
                       if not any(same(t, i) for i in mine)]
        return out, unsure

    checks, unsure = agreed("checks", lambda a, b: (a["marker"], a["verdict"]) ==
                            (b["marker"], b["verdict"]), sure_check)
    adds, unsure_adds = agreed("partners", lambda a, b: (a["marker"], a["relation"]) ==
                               (b["marker"], b["relation"]), sure_add)
    unsure += unsure_adds
    changes = {"drop": [], "weaken": [], "add": []}
    for marker, item in checks:
        kind = "drop" if item["verdict"] == "does_not_hold_here" else "weaken"
        changes[kind].append({"marker": marker, "partner": item["marker"],
                              "relation": item["relation"], "basis": item["basis"]})
    draft = apply_changes(copy.deepcopy(view), changes)
    by_marker = {}
    for marker, item in adds:
        by_marker.setdefault(marker, []).append(item)
    for marker, items in by_marker.items():
        kept, refused = check_proposal(draft, marker, items)
        changes["add"] += [{"marker": marker, "partner": p["marker"],
                            "relation": p["relation"], "basis": p["basis"]} for p in kept]
        unsure += [listed(marker, r) for r in refused]
    n_stated = sum(len(reviewable(view, m)) for m in view.get("markers") or ())
    limit = max(MAX_CHANGES[0], MAX_CHANGES[1] * n_stated)
    n = sum(len(v) for v in changes.values())
    note = None
    if n > limit:
        note = (f"{n} agreed changes, more than {limit:g} for one context: none made, the "
                "vocabulary's partners stand")
        unsure += [{"marker": c["marker"], "partner": c["partner"], "change": kind}
                   for kind, items in changes.items() for c in items]
        changes = {"drop": [], "weaken": [], "add": []}
    # Empty slots the review was not sure of still get draw 1's partners,
    # for the tiles only.
    tiles = {}
    for m in review.get("markers") or ():
        empty = empty_slots(view, m)
        made = {c["partner"] for c in changes["add"] if c["marker"] == m}
        tiles[m] = [p for p in (first.get(m) or {}).get("partners") or []
                    if SLOTS.get(p.get("relation")) in empty and p["marker"] not in made]
    # Every reviewed marker's entry replaced: nothing of an earlier review stays.
    store_proposals(context, biology_record, tiles)
    # A state marker's carriers: those both draws name, in draw 1's order
    # (each already checked by `check_carriers` when its draw was kept).
    carriers = {}
    for m in review.get("markers") or ():
        theirs = set((second.get(m) or {}).get("carriers") or ())
        both = [n for n in (first.get(m) or {}).get("carriers") or () if n in theirs]
        if both:
            carriers[m] = both
    review.update(changes=changes, unsure=unsure, note=note, context=ctx, done=True,
                  tiles={m: ps for m, ps in tiles.items() if ps}, carriers=carriers)
    return {"changes": changes, "unsure": unsure, "note": note, "context": ctx,
            "tiles": review["tiles"], "carriers": carriers}


def review_brief(settled) -> dict | None:
    """What a session's status says of a settled review: each change made, in
    a line; the empty slots drawn from proposals it was not sure of; how many
    items were not sure; the context it went by (and whether it was named or
    inferred); and why nothing was made, if so."""
    if not settled:
        return None
    lines = [f"{kind} {c['relation']} {c['marker']} ~ {c['partner']}: {c.get('basis') or ''}"
             for kind, items in (settled.get("changes") or {}).items() for c in items]
    ctx = settled.get("context") or {}
    return {"applied": lines,
            "tiles_only": {m: [f"{p['relation']} {p['marker']}" for p in ps]
                           for m, ps in (settled.get("tiles") or {}).items()},
            "unsure": len(settled.get("unsure") or ()),
            **({"carriers": settled["carriers"]} if settled.get("carriers") else {}),
            "context": ({k: ctx[k] for k in ("source", "said", "contexts") if ctx.get(k)}
                        if ctx else "none: only empty slots could be filled"),
            **({"note": settled["note"]} if settled.get("note") else {})}


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
            # A planner's tiles-only proposal never orders the gating.
            if CONFIDENCE_RANK.get(partner.get("confidence"), 0) < 1 or partner.get("proposed"):
                continue
            ref = partner["marker"]
            if ref not in entries:
                continue
            a, b = role_rank(entries[ref]), role_rank(entry)
            if a < b or (a == b and partner["relation"] == "subset"):
                edges.setdefault(ref, set()).add(marker)
    return edges


def order(context, t1_scores=None):
    """(gating order, basis) -- the hierarchy's stages (`hierarchy`: broad,
    lineage, subtype, state, unplaced), then role buckets, partners before
    dependents, ties by how many dependents a marker unlocks, then T1 score,
    then name. `context` markers are left out (never gated)."""
    from plexora.plugins.gating.server.autogate import hierarchy

    entries = context["entries"]
    t1_scores = t1_scores or {}
    tree = hierarchy.build(context)
    gated = [m for m in context["markers"] if entries[m].get("role") != "context"]
    edges = _edges(context)
    indegree = {m: 0 for m in gated}
    for ref, dependents in edges.items():
        for dependent in dependents:
            if dependent in indegree and ref in indegree:
                indegree[dependent] += 1
    unlocks = {m: len(edges.get(m, ())) for m in gated}

    def priority(m):
        return (hierarchy.stage_rank(tree, m), role_rank(entries[m]), -unlocks[m],
                -(t1_scores.get(m) or 0.0), m)

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
        node = tree["nodes"].get(marker) or {}
        basis.setdefault(marker, f"{node.get('stage') or 'unplaced'} · "
                                 f"{entries[marker].get('role') or 'unknown role'}")
        for dependent in sorted(edges.get(marker, ())):
            if dependent in indegree:
                indegree[dependent] -= 1
                if indegree[dependent] == 0 and dependent in remaining:
                    ready.append(dependent)
        ready.sort(key=priority)
    return out, basis


def references_for(context, marker, gated, *, limit=MAX_REFERENCES):
    """Partners that may serve as references for `marker` in this run --
    the markers a look plots beside it, so the few channels most worth the
    picture (`hierarchy.select`, best relevance x reliability first).

    `gated` is {marker: grade} for markers already gated in this run
    (`high`, `moderate`, `low`, `failed`, ...). A partner qualifies when the
    relation (stated, or implied by the tree) is of moderate or high
    confidence, it was gated at moderate or better, and it is not itself a
    state or signalling readout.
    """
    from plexora.plugins.gating.server.autogate import hierarchy

    entries = context.get("entries") or {}
    rels = [r for r in hierarchy.relations(context, marker)
            if r["kind"] in ("positive", "negative")
            and CONFIDENCE_RANK.get(r["confidence"], 0) >= 1
            and (entries.get(r["marker"]) or {}).get("role") not in ("state", "signalling")]
    chosen = hierarchy.select(rels, gated, min_grade="moderate", limit=limit)
    return [{"marker": r["marker"], "relation": r["relation"], "confidence": r["confidence"],
             "grade": r["grade"], "weight": r["weight"], "derived": r["derived"]}
            for r in chosen["used"]]


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
    `create`), refreshed if the vocabulary changed since. An image-only
    project's markers are its image channels (an auto-contrast run's)."""
    channels = [c.get("fullname") or c.get("name") for c in ds.project.image.real_channels]
    markers = list(ds.table.markers) if getattr(ds.project, "has_table", True) else channels
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
