"""What a gate's provenance says about how the agent reached it (`detail.review`).

`build` reads one unit's record and returns a short, bounded digest the viewer
shows beside the gate under review: which other markers the decision drew on
and how (`supporting`), a few sentences of notes, and what stays uncertain.
The viewer switches the supporting channels on beside the marker and shows
the notes over the image; both read this one record.

Only what the unit recorded as drawn or leaned on is named: the condition a
gate was fitted under, the carriers a state marker's start was drawn from,
the partner tiles and plots its looks showed (`unit["partners_drawn"]`,
`Engine.note_partners_drawn`), the references its purity check ran against,
the lineages it was checked on, the partners a scored start weighed. Never
the vocabulary's partners wholesale, nor a partner the plan only considered
(`evidence_plan`): those that were left out and why go to `not_used`.

Pure: no engine, no dataset. Every number is a plain float/int/bool, because
the provenance store serialises with `default=str` and would keep a numpy
scalar as a string.
"""

from __future__ import annotations

import math

from plexora.plugins.gating.server.autogate import schemas

VERSION = 1
MAX_SUPPORTING = 6
MAX_NOTES = 600
MAX_UNCERTAINTY = 4
MAX_NOT_USED = 4
MAX_INFLUENCE = 200
MAX_LINE = 200

#: The kind of relationship a vocabulary relation is, as the viewer names it.
KIND = {"subset": "parent_child", "subset_any": "parent_child",
        "coexpressed": "coexpression", "exclusive": "mutual_exclusion"}

#: What a partner did for the gate, most decisive first.
ROLE_ORDER = ("condition", "carrier", "tile_expected", "tile_not_expected", "reference_panel",
              "plot", "purity_check", "lineage_check", "scored_start")

#: Roles a reviewer checks by eye, so the viewer switches the channel on;
#: a scored start's partners only weighed a number.
DISPLAY_ROLES = ROLE_ORDER[:-1]

_LEAD = {"parent_child": "Parent lineage", "coexpression": "Co-expressed with",
         "mutual_exclusion": "Should exclude"}


def build(unit, *, state, method, confidence=None, channel_of=None) -> dict:
    """The review record of `unit` as it is written with `state`, `method` and
    `confidence` (passed in: an accepted gate's reason is set after its
    write). `channel_of` maps a marker to its image channel, or None."""
    marker = unit.get("marker")
    looks = _looks(unit)
    needs_review = bool(unit.get("needs_review") or method == "needs_review"
                        or state in schemas.REVIEW_WRITE_STATES)
    supporting = _supporting(unit, channel_of)
    uncertainty = _uncertainty(unit, needs_review)
    return {
        "version": VERSION, "marker": marker, "method": method, "state": state,
        "confidence": confidence, "looks": looks,
        "supporting": supporting,
        "notes": _notes(unit, supporting, uncertainty, state=state, method=method,
                        confidence=confidence, looks=looks, needs_review=needs_review),
        "uncertainty": uncertainty,
        "needs_review": needs_review,
        "not_used": _not_used(unit, {s["marker"] for s in supporting}),
    }


# -- supporting ---------------------------------------------------------------


def _supporting(unit, channel_of):
    marker = unit.get("marker")
    found = {}

    def add(name, role, relation, clause, *, channel=None, **extra):
        if not name or name == marker:
            return
        entry = found.setdefault(name, {"marker": name, "roles": [], "clauses": {},
                                        "relation": None, "channel": None, "rank": len(ROLE_ORDER)})
        rank = ROLE_ORDER.index(role)
        if role not in entry["roles"]:
            entry["roles"].append(role)
        entry["clauses"].setdefault(role, clause)
        if rank < entry["rank"] and relation:
            entry["rank"], entry["relation"] = rank, relation
        elif entry["relation"] is None:
            entry["relation"] = relation
        if channel and not entry["channel"]:
            entry["channel"] = channel
        for key, value in extra.items():
            if value is not None:
                entry.setdefault(key, value)

    relation_of = _relations(unit)
    condition = unit.get("condition") or {}
    if condition.get("within"):
        n_in, n_out = (_num(condition.get("n_positive_within")),
                       _num(condition.get("n_positive_outside")))
        counts = (f" ({n_in} positives inside, {n_out} outside)"
                  if n_in is not None and n_out is not None else "")
        add(condition["within"], "condition",
            condition.get("relation") or relation_of.get(condition["within"]) or "subset",
            f"the gate was fitted within {condition['within']}+ cells only{counts}")

    carriers = unit.get("carriers") or {}
    start = carriers.get("start") or {}
    for c in carriers.get("chosen") or ():
        name = c.get("marker")
        enrichment = _num(c.get("enrichment"))
        clause = (f"its {name}+ cells carry {marker} x{enrichment:.1f} more often than the "
                  "average cell" if enrichment is not None
                  else f"its {name}+ cells were where {marker} was read")
        fit = c.get("fit") or {}
        split = _num(fit.get("low"))
        if fit.get("usable") and split is not None:
            joined = _num(start.get("low"))
            clause += (f"; its split at {_fmt(split)} joined the start ({_fmt(joined)})"
                       if joined is not None else
                       f"; its split at {_fmt(split)} was offered as a candidate")
        elif fit:
            clause += "; its own split was not usable" + (
                f" ({str(fit['why'])[:80]})" if fit.get("why") else "")
        add(name, "carrier", c.get("relation") or relation_of.get(name) or "subset", clause,
            grade=c.get("grade") if isinstance(c.get("grade"), str) else None,
            enrichment=enrichment)

    tiles, plots, channels = {}, {}, {}
    for entry in _drawn(unit):
        for tile in entry.get("tiles") or ():
            name = tile.get("marker")
            role = {"co": "tile_expected", "anti": "tile_not_expected",
                    "reference": "reference_panel"}.get(tile.get("panel"))
            if not name or role is None:
                continue
            seen = tiles.setdefault((name, role), {"looks": set(), "tile": tile})
            seen["looks"].add(entry.get("look"))
            channels.setdefault(name, tile.get("channel"))
        plot = entry.get("plot") or {}
        if plot.get("marker"):
            seen = plots.setdefault(plot["marker"], {"looks": set(), "plot": plot})
            seen["looks"].add(entry.get("look"))
    for (name, role), seen in tiles.items():
        n, tile = len(seen["looks"]), seen["tile"]
        clause = {"tile_expected": f"shown beside the cells as the expected partner in "
                                   f"{_count(n, 'look')}",
                  "tile_not_expected": f"shown beside the cells as the not-expected partner "
                                       f"in {_count(n, 'look')}",
                  "reference_panel": f"its channel was the reference panel in "
                                     f"{_count(n, 'look')}"}[role]
        others = [m for m in tile.get("members") or () if m != name]
        if others:
            clause += f", drawn together with {_join(others)}"
        add(name, role, tile.get("relation") or relation_of.get(name)
            or ("exclusive" if role == "tile_not_expected" else "subset"),
            clause, channel=tile.get("channel"))
    for name, seen in plots.items():
        plot = seen["plot"]
        relation = plot.get("relation") or relation_of.get(name)
        clause = (f"plotted against {marker}" + (f" ({relation})" if relation else "")
                  + f" in {_count(len(seen['looks']), 'picture')}")
        if plot.get("why"):
            clause += f": {str(plot['why'])[:100]}"
        add(name, "plot", relation, clause)

    regression = unit.get("regression") or {}
    if regression:
        impure = {}
        for check in regression.get("checks") or ():
            if check.get("name") == "partners" and check.get("ok") is False:
                for row in check.get("value") or ():
                    if isinstance(row, dict) and row.get("partner"):
                        impure[row["partner"]] = _num(row.get("excess"))
        for ref in unit.get("reference_gates") or ():
            name = ref.get("marker")
            gate = _num(ref.get("gate"))
            words = ", ".join(w for w in (_fmt(gate) if gate is not None else None,
                                          ref.get("confidence")
                                          if isinstance(ref.get("confidence"), str) else None)
                              if w)
            clause = (f"its gate{f' ({words})' if words else ''} was a reference of the "
                      "purity check")
            if name in impure:
                excess = impure[name]
                clause += (" -- the check failed on it" if excess is None else
                           f" -- the check failed on it ({excess:.2f} excess)")
            add(name, "purity_check", ref.get("relation") or relation_of.get(name), clause,
                grade=ref.get("confidence") if isinstance(ref.get("confidence"), str) else None)

    for row in _lineage_rows(unit, bool(regression)):
        name = row.get("partner")
        at_top, prevalence = _num(row.get("at_top")), _num(row.get("prevalence"))
        if row.get("contaminated") and at_top is not None:
            clause = (f"{at_top:.0%} of the brightest {marker} positives sit on {name}"
                      + (f", against {prevalence:.0%} of all cells" if prevalence is not None
                         else "") + ": the population is suspect")
        else:
            excess = _num(row.get("excess"))
            clause = ("checked as a lineage that should exclude it"
                      + (f" ({excess:.2f} excess at the gate)" if excess is not None else ""))
        add(name, "lineage_check", "exclusive", clause)

    scoring = unit.get("scoring") or {}
    if (unit.get("start") or {}).get("source") == "scoring":
        basis = scoring.get("basis")
        for p in scoring.get("partners") or ():
            weight = _num(p.get("weight"))
            clause = ("its gate was weighed into the scored start"
                      + (f" (weight {weight:.2f}" + (", down-weighted" if p.get("down_weighted")
                                                     else "") + ")" if weight is not None else "")
                      + (f", {basis}" if isinstance(basis, str) and basis else ""))
            add(p.get("marker"), "scored_start", p.get("relation") or relation_of.get(
                p.get("marker")), clause)

    out = []
    for name, entry in found.items():
        roles = [r for r in ROLE_ORDER if r in entry["roles"]]
        relation = entry["relation"] or relation_of.get(name) or "subset"
        looks = sum(1 for e in _drawn(unit)
                    if any(t.get("marker") == name for t in e.get("tiles") or ())
                    or (e.get("plot") or {}).get("marker") == name)
        channel = entry["channel"] or channels.get(name)
        if not channel and channel_of is not None:
            try:
                channel = channel_of(name)
            except Exception:
                channel = None
        row = {"marker": name, "channel": channel if isinstance(channel, str) else None,
               "relation": relation, "kind": KIND.get(relation, "parent_child"),
               "roles": roles, "display": any(r in DISPLAY_ROLES for r in roles),
               "looks": looks,
               "influence": _clip("; ".join(entry["clauses"][r] for r in roles[:2]),
                                  MAX_INFLUENCE)}
        if entry.get("grade"):
            row["grade"] = entry["grade"]
        if entry.get("enrichment") is not None:
            row["enrichment"] = round(float(entry["enrichment"]), 3)
        out.append(row)
    out.sort(key=lambda r: (not r["display"], ROLE_ORDER.index(r["roles"][0]), -r["looks"],
                            r["marker"]))
    return out[:MAX_SUPPORTING]


def _relations(unit) -> dict:
    """{partner: relation} from the panel entry's partners (the vocabulary's
    words for a pair, used only to name a partner something else chose)."""
    out = {}
    for p in (unit.get("context") or {}).get("partners") or ():
        if isinstance(p, dict) and p.get("marker") and p.get("relation") in KIND:
            out.setdefault(p["marker"], p["relation"])
    return out


def _drawn(unit):
    """The looks' records of what they drew, only for looks that were sent."""
    sent = len(unit.get("packets") or ())
    return [e for e in unit.get("partners_drawn") or ()
            if isinstance(e, dict) and isinstance(e.get("look"), int) and e["look"] <= sent]


def _lineage_rows(unit, checked):
    """The exclusive lineages the population was checked on: the contaminated
    ones always, the clean ones only when the gate's own checks ran (at most
    two, the largest excess first)."""
    rows = [r for r in unit.get("lineage") or () if isinstance(r, dict) and r.get("partner")]
    worst = [r for r in (unit.get("lineage_contamination"), unit.get("lineage_suspect"))
             if isinstance(r, dict) and r.get("partner")]
    bad, seen = [], set()
    for r in worst + [r for r in rows if r.get("contaminated")]:
        if r["partner"] not in seen:
            seen.add(r["partner"])
            bad.append({**r, "contaminated": True})
    clean = []
    if checked:
        clean = sorted((r for r in rows if not r.get("contaminated")
                        and r["partner"] not in seen),
                       key=lambda r: -(_num(r.get("excess")) or 0.0))[:2]
    return bad + clean


# -- not used ------------------------------------------------------------------


def _not_used(unit, supporting):
    relation_of = _relations(unit)
    out, seen = [], set(supporting)

    def put(name, relation, why):
        if name and name not in seen and name != unit.get("marker"):
            seen.add(name)
            out.append({"marker": name, "relation": relation or relation_of.get(name),
                        "why": _clip(str(why or ""), 120)})

    for s in (unit.get("carriers") or {}).get("skipped") or ():
        if isinstance(s, dict):
            put(s.get("marker"), s.get("relation"), s.get("why"))
    plan = unit.get("evidence_plan") or {}
    for a in plan.get("avoided") or ():
        if isinstance(a, dict):
            put(a.get("marker"), a.get("relation"), a.get("why"))
    for name in plan.get("not_gated_yet") or ():
        put(name, None, "not gated yet")
    return out[:MAX_NOT_USED]


# -- uncertainty ----------------------------------------------------------------


def _uncertainty(unit, needs_review):
    out = []

    def put(text):
        text = _clip(" ".join(str(text or "").split()), MAX_LINE)
        if text and not any(text in o or o in text for o in out):
            out.append(text)

    reason = unit.get("reason")
    if needs_review and reason:
        put(reason)
    regression = unit.get("regression") or {}
    failed = [str(f) for f in regression.get("failed") or ()]
    if failed:
        put(f"failed its own checks: {', '.join(failed)}"
            + (" (confirmed by a whole-image look)" if unit.get("regression_confirmed") else ""))
    state = unit.get("hypotheses") or {}
    if state:
        from plexora.plugins.gating.server.autogate.hypotheses import plateau_note

        try:
            put(plateau_note(state).lstrip("; "))
        except Exception:  # an older record's posterior: the reason says it
            pass
        if state.get("isolated_only"):
            put("read on the isolated cells alone: most crowded cells could not be read")
    c = unit.get("lineage_contamination") or unit.get("lineage_suspect")
    if isinstance(c, dict) and c.get("partner") and _num(c.get("at_top")) is not None:
        put(f"{_num(c['at_top']):.0%} of the brightest positives sit on {c['partner']}, "
            "which should exclude them")
    for note in unit.get("confidence_notes") or ():
        put(note)
    question = unit.get("question") or {}
    if isinstance(question, dict) and question.get("question"):
        put(f"needs the user: {question['question']}")
    if unit.get("closed_unlooked"):
        put("closed without a look at cells")
    if needs_review and unit.get("notes"):
        put(f"the reader's last note: {_clip(str(unit['notes'][-1]), 160)}")
    return out[:MAX_UNCERTAINTY]


# -- notes ----------------------------------------------------------------------


def _notes(unit, supporting, uncertainty, *, state, method, confidence, looks, needs_review):
    marker = unit.get("marker")
    reason = " ".join(str(unit.get("reason") or "").split())
    final = _num(unit.get("final"))
    path = unit.get("path")
    sentences = []
    empty = state in schemas.EMPTY_GATE_STATES or method in ("failed_marker",
                                                            "no_positive_population")
    if needs_review:
        at = f" ({_fmt(final)})" if final is not None else ""
        sentences.append(f"Written for review at the best gate reached{at}"
                         + (f": {reason}" if reason else ""))
    elif state == "technically_failed" or method == "failed_marker":
        sentences.append("The stain failed" + (f": {reason}" if reason else ""))
    elif state == "no_positive_population" or method == "no_positive_population":
        sentences.append("No positive population" + (f": {reason}" if reason else ""))
    elif method == "ai_conditional" and (unit.get("condition") or {}).get("within"):
        sentences.append(f"Fitted within {unit['condition']['within']}+ cells only, after "
                         f"{_count(looks, 'look')}")
    elif path == "panel" or method == "ai_panel":
        hyp = unit.get("hypotheses") or {}
        n_obs = len(hyp.get("observations") or ()) or looks
        sentences.append(f"Decided by {_count(n_obs, 'blind look')} at numbered cells"
                         + (f": {hyp['reason']}" if hyp.get("reason") else ""))
    elif path == "t1" or method == "gmm":
        sentences.append("Accepted at the Auto gate: the distribution split cleanly"
                         + (", and the audit strip confirmed it" if unit.get("audited")
                            else ", with no look at cells"))
    elif method in ("transfer_aligned", "group_aligned") or path == "transfer":
        transfer = unit.get("transfer") or {}
        gate = _num(transfer.get("reference_gate"))
        source = transfer.get("reference")
        sentences.append("Carried over from " + (f"{source}" if isinstance(source, str)
                                                  else "another image of the dataset")
                         + (f"'s gate ({_fmt(gate)})" if gate is not None else "")
                         + " through their alignment")
    elif looks:
        refs = [r for r in unit.get("references") or () if isinstance(r, str)]
        sentences.append(f"Decided by {_count(looks, 'look')} at cells around the gate"
                         + (f" beside its reference {_join(refs[:2])}"
                            if path == "t3" and refs else "")
                         + (" and a walk over candidate thresholds" if path == "t4" else ""))
    else:
        sentences.append("Decided without a look at cells")

    if supporting:
        for kind in ("parent_child", "coexpression", "mutual_exclusion"):
            group = [s for s in supporting if s["kind"] == kind]
            if not group:
                continue
            lead = _LEAD[kind]
            if kind == "parent_child" and all(s["relation"] == "subset_any" for s in group):
                lead = "Expected in any of"
            sentences.append(f"{lead} {_join([s['marker'] for s in group[:3]])}: "
                             f"{group[0]['influence']}")
    elif not empty:
        sentences.append(f"No partner was drawn or leaned on: decided from {marker}'s own "
                         "distribution" + (f" and {_count(looks, 'look')} at cells" if looks
                                           else ", without a look"))
    rest = [u for u in uncertainty if not (reason and u == _clip(reason, MAX_LINE))]
    if rest:
        if needs_review:
            sentences.append(f"Needs review: {rest[0]}")
        elif confidence not in (None, "high"):
            sentences.append(f"Confidence {confidence}: {rest[0]}")

    text = ""
    for sentence in sentences:
        sentence = sentence.rstrip(" .") + "."
        candidate = f"{text} {sentence}".strip()
        if len(candidate) > MAX_NOTES:
            if not text:
                text = _clip(sentence, MAX_NOTES)
            break
        text = candidate
    return text


# -- formatting -------------------------------------------------------------------


def _looks(unit) -> int:
    used = _num((unit.get("used") or {}).get("packets"))
    if isinstance(used, (int, float)) and not isinstance(used, bool):
        return int(used)
    return len(unit.get("packets") or ())


def _num(value):
    """A plain float, int or bool (a numpy scalar's value), or None."""
    if value is None:
        return None
    item = getattr(value, "item", None)
    if callable(item) and not isinstance(value, (bool, int, float)):
        try:
            value = item()
        except (TypeError, ValueError):
            return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _fmt(value) -> str:
    value = _num(value)
    if value is None:
        return "?"
    return f"{value:.0f}" if abs(value) >= 100 else f"{value:.3g}"


def _count(n, noun) -> str:
    n = int(n or 0)
    return f"{n} {noun}" + ("" if n == 1 else "s")


def _join(names) -> str:
    names = [str(n) for n in names]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _clip(text, limit) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    cut = text[:limit - 1].rsplit(" ", 1)[0].rstrip(",;:")
    return cut + "..."
