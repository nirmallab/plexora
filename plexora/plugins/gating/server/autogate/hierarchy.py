"""The panel as a tree, and which gated markers may be trusted as evidence.

A gate that leans on a partner is only as good as the partner's gate: CD8
scored against a CD45 gate that failed inherits the failure. So the run keeps
an evidence graph, not a list of independent gates.

**The tree** (`build`) is made from the panel's own relations (the shipped
vocabulary, then the user's, metadata's or an agent's entries -- never a list
of marker names here). A marker's *parents* are the markers its positives
must be positive for (`subset`); it is *placed* under the deepest of those,
or of a `coexpressed` partner from an earlier role bucket (CD4 under CD3,
CD163 under CD68). Depth along that placement gives the stage:

- `broad` -- no parent in the panel (CD45, a pan-tumour or stromal marker);
- `lineage` -- one level down (CD3, CD20, CD68, CD11c);
- `subtype` -- deeper (CD4, CD8, CD163);
- `state` -- the state and signalling roles (activation, checkpoint,
  proliferation), whatever their parents;
- `unplaced` -- nothing known about it (no role, no relation); gated last,
  from its own data, until the panel context says more.

The stage orders the gating (`context.order`, partners still before the
markers that need them) and says which markers are informative for which.
It is a guide, never a gate: a child is gated when its parent failed, from
other references and its own data.

**Relations** (`relations`) are the marker's direct partners in both
directions, plus two the tree implies: a subset of a subset is a subset
(FOXP3 within CD4 within CD45), and a subset of a marker is exclusive of
whatever that marker excludes (CD8 within CD3, CD3 exclusive of CD20). A
derived relation counts for less (`DERIVED`) and is used only when no direct
one says the same.

**Reliability** (`grade`) is what a gate has earned: `high` / `moderate`
(accepted at that confidence, or the user's own gate), `low` (accepted at
low confidence), `failed` (closed for review -- even though a review gate is
written so every marker has a value -- failed QC, or no positive
population), `pending` (not decided yet).

**Selection** (`select`): each relation is weighed as relevance (the
relation, its stated confidence, direct or derived) times reliability. A
failed or pending reference is never used, and says why. A low-reliability
one is used only when no moderate-or-better reference of the same kind
(positive, negative, child) exists, and is marked down-weighted. A
biologically perfect partner with a failed gate is worse than a less direct
one with a reliable gate.

**Bidirectional.** Children (markers whose positives are a subset of this
one's) are evidence for a parent too: where several reliable children's
positives sit on the parent's axis says where the parent's positives begin
(`scoring`, the `children` candidate). A parent whose own distribution is
weak may wait for its children (`Engine.defer_for_children`).
"""

from __future__ import annotations

from plexora.ai import vocabulary

VERSION = "1"

#: The stages, in gating order.
STAGES = ("broad", "lineage", "subtype", "state", "unplaced")
STATE_ROLES = ("state", "signalling")

#: Relevance of a relation as evidence, before reliability.
RELEVANCE = {"subset": 1.0, "coexpressed": 0.8, "exclusive": 0.6}
#: The relation's stated confidence (vocabulary, user, agent).
STATED = {"high": 1.0, "moderate": 0.75, "low": 0.4}
#: A relation the tree implies rather than one stated.
DERIVED = 0.7
#: A relation the sample's tissue or disease names (`biology`): more telling
#: here than the generic one (MART1 with SOX10 in melanoma).
CONTEXT = 1.25

#: Reliability grades, worst first, and their weight as evidence.
GRADES = ("failed", "low", "moderate", "high")
RELIABILITY_WEIGHT = {"high": 1.0, "moderate": 0.7, "low": 0.35, "failed": 0.0}
USABLE = ("low", "moderate", "high")
RELIABLE = ("moderate", "high")

#: Unit states whose gate is not evidence, though a review gate is written.
FAILED_STATES = ("technically_failed", "no_positive_population",
                 "manual_review_recommended", "not_binary", "insufficient_information")
ACCEPTED = ("accepted", "accepted_t1", "accepted_low_confidence")
KEPT = ("skipped_manual", "skipped_locked")

#: What a reference is used as.
KINDS = ("positive", "negative", "child")


def _role_rank(entry):
    role = (entry or {}).get("role")
    return vocabulary.ROLES.index(role) if role in vocabulary.ROLES else len(vocabulary.ROLES)


def _stated_ok(partner):
    return STATED.get(partner.get("confidence") or "moderate", 0) >= STATED["moderate"]


def build(context) -> dict:
    """{version, nodes: {marker: {stage, depth, placed_under, parents,
    children}}, stages: {stage: [markers]}} for every gated marker of the
    panel context (role `context` is left out)."""
    entries = context.get("entries") or {}
    gated = [m for m in context.get("markers") or entries if
             (entries.get(m) or {}).get("role") != "context"]
    present = set(gated)
    parents, placement = {}, {}
    for marker in gated:
        entry = entries[marker]
        hard, soft = [], []
        for partner in entry.get("partners") or []:
            ref = partner.get("marker")
            if ref not in present or ref == marker or not _stated_ok(partner):
                continue
            if partner["relation"] == "subset":
                hard.append(ref)
            elif partner["relation"] == "coexpressed" \
                    and _role_rank(entries.get(ref)) <= _role_rank(entry):
                soft.append(ref)
        parents[marker] = sorted(set(hard))
        placement[marker] = sorted(set(hard) | set(soft))

    depth, under = {}, {}

    def walk(marker, trail):
        if marker in depth:
            return depth[marker]
        best, chosen = 0, None
        for ref in placement[marker]:
            if ref in trail:          # a mutual relation (HLA-ABC and B2M): not a level
                continue
            d = walk(ref, trail | {marker}) + 1
            if d > best or (d == best and chosen is not None
                            and _role_rank(entries[ref]) < _role_rank(entries[chosen])):
                best, chosen = d, ref
        depth[marker], under[marker] = best, chosen
        return best

    for marker in gated:
        walk(marker, frozenset())
    children = {m: [] for m in gated}
    for marker in gated:
        for ref in parents[marker]:
            children[ref].append(marker)
    nodes, stages = {}, {s: [] for s in STAGES}
    for marker in gated:
        entry = entries[marker]
        if entry.get("role") in STATE_ROLES:
            stage = "state"
        elif entry.get("role") is None and not placement[marker] and not children[marker] \
                and not _named_by_others(entries, marker):
            stage = "unplaced"
        else:
            stage = STAGES[min(depth[marker], 2)]
        nodes[marker] = {"stage": stage, "depth": depth[marker], "placed_under": under[marker],
                         "parents": parents[marker], "children": sorted(children[marker])}
        stages[stage].append(marker)
    return {"version": VERSION, "nodes": nodes, "stages": stages}


def _named_by_others(entries, marker):
    return any(p.get("marker") == marker for e in entries.values()
               for p in e.get("partners") or [])


def stage_rank(tree, marker) -> int:
    node = (tree.get("nodes") or {}).get(marker)
    return STAGES.index(node["stage"]) if node else len(STAGES)


# -- relations ------------------------------------------------------------------


def _direct(context, marker):
    """[(other, relation, direction, stated confidence)] stated for `marker`
    in either direction. Forward: the marker's own partners; reverse: a
    marker that names this one -- `subset` reverse is a child, a reverse
    co-expressed or exclusive relation means the same forward."""
    entries = context.get("entries") or {}
    own = entries.get(marker) or {}
    out = []
    for partner in own.get("partners") or []:
        if partner.get("marker") in entries and partner["marker"] != marker:
            out.append(_Rel(partner["marker"], partner["relation"], "forward",
                            partner.get("confidence") or "high", partner.get("context")))
    for other, entry in entries.items():
        if other == marker:
            continue
        for partner in entry.get("partners") or []:
            if partner.get("marker") != marker:
                continue
            direction = "reverse" if partner["relation"] == "subset" else "forward"
            out.append(_Rel(other, partner["relation"], direction,
                            partner.get("confidence") or "high", partner.get("context")))
    return out


class _Rel(tuple):
    """(other, relation, direction, confidence), unpacking as four; `context`
    names the tissue or disease that stated it, if one did."""

    def __new__(cls, other, relation, direction, confidence, context=None):
        rel = super().__new__(cls, (other, relation, direction, confidence))
        rel.context = context
        return rel


def _down(confidence):
    order = vocabulary.CONFIDENCE          # high, moderate, low
    i = order.index(confidence) if confidence in order else len(order) - 1
    return order[min(i + 1, len(order) - 1)]


def _weaker(a, b):
    order = vocabulary.CONFIDENCE
    return a if order.index(a) >= order.index(b) else b


def relations(context, marker) -> list:
    """Every relation `marker` has as evidence: direct ones, and the ones
    the tree implies (`derived`, with the chain in `via`). Each is {marker,
    relation, direction, confidence, derived, via, kind, relevance}; a
    relation stated directly is never repeated as a derived one."""
    entries = context.get("entries") or {}
    out, seen = [], set()

    def add(other, relation, direction, confidence, via=None, context=None):
        key = (other, relation, direction)
        if other == marker or key in seen or other not in entries:
            return
        if (entries.get(other) or {}).get("role") == "context":
            return
        if direction == "reverse" and (entries.get(other) or {}).get("role") in STATE_ROLES:
            # A state readout within the marker (PD-1 within CD45) is not a
            # population that says where the marker's positives begin.
            return
        seen.add(key)
        kind = ("child" if direction == "reverse"
                else "negative" if relation == "exclusive" else "positive")
        relevance = RELEVANCE[relation] * STATED.get(confidence, STATED["low"])
        if via:
            relevance *= DERIVED
        if context:
            relevance *= CONTEXT
        out.append({"marker": other, "relation": relation, "direction": direction,
                    "confidence": confidence, "derived": bool(via), "via": via,
                    "kind": kind, "relevance": round(relevance, 4),
                    **({"context": context} if context else {})})

    direct = _direct(context, marker)
    for rel in direct:
        other, relation, direction, confidence = rel
        add(other, relation, direction, confidence, context=rel.context)
    # Ancestors: a subset of a subset is a subset; the exclusions of an
    # ancestor hold for the marker. Only through reliable stated relations.
    ancestors, frontier = {}, [(o, c) for o, r, d, c in direct
                               if r == "subset" and d == "forward" and c != "low"]
    while frontier:
        nxt = []
        for anc, conf in frontier:
            if anc in ancestors or anc == marker:
                continue
            ancestors[anc] = conf
            for o, r, d, c in _direct(context, anc):
                if r == "subset" and d == "forward" and c != "low":
                    nxt.append((o, _weaker(conf, c)))
        frontier = nxt
    stated = {(o, r, d) for o, r, d, _c in direct}
    for anc, conf in ancestors.items():
        if (anc, "subset", "forward") not in stated:
            add(anc, "subset", "forward", _down(conf), via=_chain(context, marker, anc))
        for o, r, d, c in _direct(context, anc):
            if r == "exclusive" and c != "low" and (o, "exclusive", "forward") not in stated:
                add(o, "exclusive", "forward", _down(_weaker(conf, c)), via=[anc])
    # Descendants: a subset of a child is a child too.
    kids = [(o, c) for o, r, d, c in direct if r == "subset" and d == "reverse" and c != "low"]
    seen_kids = {o for o, _c in kids}
    while kids:
        nxt = []
        for kid, conf in kids:
            for o, r, d, c in _direct(context, kid):
                if r == "subset" and d == "reverse" and c != "low" and o not in seen_kids \
                        and o != marker:
                    seen_kids.add(o)
                    add(o, "subset", "reverse", _down(_weaker(conf, c)), via=[kid])
                    nxt.append((o, _weaker(conf, c)))
        kids = nxt
    return out


def _chain(context, marker, ancestor):
    """The markers between `marker` and a derived ancestor, for the record."""
    from collections import deque

    queue, back = deque([marker]), {marker: None}
    while queue:
        cur = queue.popleft()
        if cur == ancestor:
            break
        for o, r, d, c in _direct(context, cur):
            if r == "subset" and d == "forward" and o not in back:
                back[o] = cur
                queue.append(o)
    path, cur = [], back.get(ancestor)
    while cur is not None and cur != marker:
        path.append(cur)
        cur = back.get(cur)
    return list(reversed(path)) or None


# -- reliability ----------------------------------------------------------------


def grade(unit) -> tuple:
    """(grade, why) for one session unit: what its gate has earned as
    evidence for others."""
    state = unit.get("state")
    if state in ACCEPTED:
        if unit.get("needs_review"):
            return "failed", "written for manual review"
        if state == "accepted_t1":
            return "high", "accepted by the deterministic pass"
        confidence = unit.get("confidence")
        if state == "accepted_low_confidence" or confidence not in RELIABLE:
            return "low", "accepted at low confidence"
        return confidence, f"accepted at {confidence} confidence"
    if state in KEPT:
        if unit.get("thresholded") and unit.get("seen"):
            return "high", "the user's own gate"
        return None, "not gated"
    if state in FAILED_STATES:
        why = {"technically_failed": "failed QC",
               "no_positive_population": "no positive population"}.get(
            state, "closed for manual review")
        return "failed", why
    if state and state.startswith("skipped"):
        return None, "not gated"
    return "pending", "not decided yet"


def grade_from_provenance(row) -> str | None:
    """The grade of a gate written outside this session, from its provenance
    row (`autogate.provenance`): the user's own and approved or locked gates
    are trusted; a review write or failed gate is not."""
    if not row:
        return "high"          # a gate with no record: someone set it by hand
    if row.get("status") in ("approved", "locked"):
        return "high"
    method, confidence = row.get("method"), row.get("confidence")
    if method == "needs_review" or confidence in ("manual_review", "failed_qc") \
            or row.get("state") in FAILED_STATES:
        return "failed"
    if method == "manual":
        return "high"
    if confidence in RELIABLE:
        return confidence
    return "low"


def ledger(units) -> dict:
    """{marker: {grade, why, state, gate}} for one image's units."""
    out = {}
    for unit in units:
        g, why = grade(unit)
        if g is None:
            continue
        gate = unit.get("final") if unit.get("final") is not None else unit.get("candidate")
        if unit.get("state") in KEPT and unit.get("seen"):
            gate = unit["seen"][0]
        out[unit["marker"]] = {"grade": g, "why": why, "state": unit.get("state"),
                               "gate": None if gate is None else float(gate)}
    return out


# -- selection -----------------------------------------------------------------


def select(rels, grades, *, kinds=KINDS, limit=None, min_grade="low") -> dict:
    """{used: [...], avoided: [...]} from `relations` and {marker: grade}.

    Each used relation gains `grade`, `weight` (relevance x reliability) and
    `down_weighted`; each avoided one says why. Within a kind, a reference
    below moderate is used only when nothing moderate or better is; `limit`
    caps the used list per call (best weight first)."""
    floor = GRADES.index(min_grade)
    used, avoided = [], []
    by_kind = {}
    for rel in rels:
        if rel["kind"] not in kinds:
            continue
        g = grades.get(rel["marker"])
        if g is None or g == "pending":
            avoided.append({**_brief(rel), "why": "not gated yet"})
            continue
        if g == "failed":
            avoided.append({**_brief(rel), "grade": g,
                            "why": "its gate failed or is under review"})
            continue
        if GRADES.index(g) < floor:
            avoided.append({**_brief(rel), "grade": g, "why": f"{g} reliability"})
            continue
        by_kind.setdefault(rel["kind"], []).append({
            **rel, "grade": g,
            "weight": round(rel["relevance"] * RELIABILITY_WEIGHT[g], 4)})
    for kind, items in by_kind.items():
        reliable = [r for r in items if r["grade"] in RELIABLE]
        for r in items:
            if reliable and r["grade"] not in RELIABLE:
                avoided.append({**_brief(r), "grade": r["grade"],
                                "why": "low reliability, and a more reliable reference "
                                       "of the same kind is available"})
                continue
            used.append({**r, "down_weighted": r["grade"] not in RELIABLE})
    used.sort(key=lambda r: (-r["weight"], r["marker"]))
    if limit is not None:
        for r in used[limit:]:
            avoided.append({**_brief(r), "grade": r["grade"],
                            "why": "a stronger reference was chosen"})
        used = used[:limit]
    return {"used": used, "avoided": avoided}


def _brief(rel):
    return {k: rel[k] for k in ("marker", "relation", "direction", "derived") if k in rel}


def plan(context, tree, marker, grades, *, limit=None) -> dict:
    """What a packet (and the vision step) is told about the marker's place
    and its evidence: stage, parents, children, the references used with
    their weights and the ones avoided with why."""
    node = (tree.get("nodes") or {}).get(marker) or {}
    chosen = select(relations(context, marker), grades, limit=limit)
    # Not gated yet is the common case; names only, the reasons that matter
    # (failed, low, outranked) in full.
    waiting = sorted({a["marker"] for a in chosen["avoided"] if a["why"] == "not gated yet"})
    return {"stage": node.get("stage"), "depth": node.get("depth"),
            "placed_under": node.get("placed_under"),
            "parents": node.get("parents") or [], "children": node.get("children") or [],
            "used": [{k: r[k] for k in ("marker", "relation", "direction", "kind", "grade",
                                        "weight", "derived", "down_weighted", "context")
                      if k in r}
                     | ({"via": r["via"]} if r.get("via") else {})
                     for r in chosen["used"]],
            "avoided": [a for a in chosen["avoided"] if a["why"] != "not gated yet"],
            "not_gated_yet": waiting}
