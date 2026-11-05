"""Engine v4: a dataset's images gated as groups of aligned images.

The v1/v3 dataset path gates one reference image in full and carries each of
its gates to the rest; a reference marker that ends in review sends every
image to its own full gating, and nothing the other images show is ever read.
v4 decides each marker once per *group* instead:

1. Once every image's unit of a marker is profiled (`due`), the images are
   aligned on their background (`reference.align_bg`: the mixture's
   background component, or the median and IQR of a one-mode image), and
   partitioned (`partition`, pure): the medoid image is an origin; an image
   whose background maps onto it within `member_shift` sds joins its group,
   one with no positives on the same background joins and is carried
   (`no_positives`); the rest gather around their own medoid in the next
   round. An image left alone under `cell_floor` cells is attached to the
   nearest group; the others (a failed stain, a changed distribution) are
   gated on their own. A group of four or more splits once at the largest
   gap in its shifts (`reference.batch_split`).
2. Each group has a scale image S (the user's reference image when it is a
   member, else the medoid) and up to `anchors_max` anchors. S's own unit
   decides the group: it runs the v3 loop unchanged, but its looks rotate
   over the anchors -- each an ordinary single-image cell panel, its
   intervals carried into that image, its cells' values carried back onto
   S's axis (`hypotheses.packet`), so every read is pooled evidence for one
   decision. Other anchors' own estimates join S's candidates (`peer:`).
3. When S's unit closes (`carry`), its gate is carried into every member.
   A member whose positive fraction at the carried gate sits with the
   group's and inside its own guard band is accepted at T1 for the audit
   sheet (`group_aligned`); the worst few that do not are checked with a
   `transfer_check` look (at most `checks_min`, or `checks_share` of the
   group), the rest accepted flagged `group_check_unverified` at low
   confidence. A check read too low or too high gives that image its own
   decision, for at most `own_looks` images of a group; past that, and past
   the marker's packet ceiling (`packets_base` + images), it closes for
   review at the carried gate. S closed for review closes its members for
   review at the carried gate -- never one full gating per image.

Every carried gate keeps its group, scale image, alignment and class in
provenance (`detail.group`, `detail.transfer`).
"""

from __future__ import annotations

import math

import numpy as np

from plexora.plugins.gating.server.autogate import reference, schemas, tableops
from plexora.plugins.gating.server.autogate.engine import TERMINAL, unit_key

#: Bumped whenever the same profiles would group differently or the same
#: answers carry a different gate; stored on every group.
VERSION = "1"

#: The provenance method of a gate carried from its group's decision.
METHOD = "group_aligned"

#: [cal] UNMEASURED. `cell_floor`: an image with fewer cells (a small TMA
#: core) is attached to its nearest group instead of decided alone;
#: `anchors_max`: the most images a group's looks rotate over; the checks a
#: group may ask (`checks_min`, or `checks_share` of its members); members
#: given their own decision after a check (`own_looks`); a marker's packets
#: past which nothing more is asked (`packets_base` + images, what the
#: reference-first path spends at best); a member's positive fraction is off
#: when farther from the group's median than `pf_mad` robust sds, `pf_abs`
#: and `pf_rel` of the median (against S's alone below `pf_min_members`).
GROUPS = {"cell_floor": 1500, "anchors_max": 6, "checks_min": 3, "checks_share": 0.1,
          "own_looks": 5, "packets_base": 3, "pf_mad": 3.0, "pf_abs": 0.05, "pf_rel": 0.30,
          "pf_min_members": 5, "no_positive_sd": 2.0, "looks_cap": 8}

#: Why a group member could not be accepted without a look.
OPENING_FAILURES = ("guard_band", "group_pf")
#: What a member's group record says about how its gate was carried
#: (`unit["group"]["flags"]`, provenance `detail.group.flags`): attached to
#: a group it did not align with because it has too few cells; carried with
#: no positives of its own; failed its opening check with no check left for
#: it (the group's share, or the marker's packet ceiling).
FLAGS = ("attached_under_floor", "carried_no_positives", "group_check_unverified",
         "packet_ceiling")

ONE_MODE = ("degenerate", "unimodal")


def active(engine) -> bool:
    """Whether the session gates its images as groups: v4, a dataset, more
    than one image."""
    record = engine.record
    return record.get("scope") == "dataset" and schemas.is_v4(engine.options["engine"]) \
        and len(record.get("images") or ()) > 1


def group_of(engine, unit) -> dict | None:
    """The group record `unit` belongs to, or None."""
    gid = (unit.get("group") or {}).get("id")
    if not gid:
        return None
    for group in ((engine.record.get("groups") or {}).get(unit["marker"]) or {}).get(
            "groups") or ():
        if group["id"] == gid:
            return group
    return None


def pool_for(engine, unit) -> dict | None:
    """The anchors a scale image's looks rotate over while its group is
    deciding ({group, anchors, alignments, spaces}), or None: not a scale
    image, a group of one anchor, or the group already carried."""
    info = unit.get("group") or {}
    if info.get("role") != "scale":
        return None
    group = group_of(engine, unit)
    if group is None or group.get("state") != "deciding" or len(group["anchors"]) < 2:
        return None
    units = engine.record["units"]
    spaces = {p: (units.get(unit_key(p, unit["marker"])) or {}).get("fit_space")
              for p in group["anchors"]}
    return {"group": group["id"], "anchors": list(group["anchors"]),
            "alignments": {p: group["alignments"][p] for p in group["anchors"]},
            "spaces": spaces}


# -- partition (pure) ------------------------------------------------------------


def _n_cells(profile) -> int:
    return int((profile.get("summary") or {}).get("n_cells") or 0)


def _distance(a, b) -> float:
    """A symmetric background distance between two profiles: the shift of
    their background means in background sds, plus twice the log of their
    contrast ratio (a background twice as wide is as far as 1.4 sds off);
    infinite when either background cannot be read."""
    ba, bb = reference.background(a), reference.background(b)
    if ba is None or bb is None:
        return math.inf
    return abs(ba["mu"] - bb["mu"]) / math.sqrt(ba["sd"] * bb["sd"])         + 2.0 * abs(math.log(ba["sd"] / bb["sd"]))


def _medoid(names, profiles):
    """The image nearest all the others by background (ties: most cells,
    then order)."""
    def cost(p):
        return sum(min(_distance(profiles[p], profiles[q]), 1e6) for q in names if q != p)

    return min(names, key=lambda p: (cost(p), -_n_cells(profiles[p]), names.index(p)))


def _failed(profile) -> bool:
    return any(f in reference.FAILED_FLAGS for f in profile.get("flags") or ())


def _core(profile) -> bool:
    """An image whose own positives can be read: two modes, a background, a
    stain that did not fail."""
    return profile.get("class") not in ONE_MODE and not _failed(profile) \
        and reference.background(profile) is not None


def _anchors(scale, candidates, shifts, k_max) -> list:
    """S first, then candidates evenly by shift rank (both extremes first),
    `k_max` in all."""
    ranked = sorted(candidates, key=lambda p: (shifts[p], p))
    want = min(k_max - 1, len(ranked))
    if want <= 0:
        return [scale]
    if want == len(ranked):
        picks = ranked
    elif want == 1:
        picks = [max(ranked, key=lambda p: (abs(shifts[p]), p))]
    else:
        idx = sorted({round(i * (len(ranked) - 1) / (want - 1)) for i in range(want)})
        picks = [ranked[i] for i in idx]
    return [scale] + picks


def _members_of(names, origin, profiles):
    """(members, classes): the images of `names` that join a group formed
    around `origin` (origin first), and each other image's class against it."""
    members, classes = [origin], {}
    for p in names:
        if p == origin:
            continue
        al = reference.align_bg(profiles[p], profiles[origin])
        classes[p] = reference.classify_group(al, profiles[p], profiles[origin])
        if classes[p] in reference.MEMBER_CLASSES:
            members.append(p)
    return members, classes


def _split(members, origin, profiles, flags):
    """`members` cut once at the largest gap in their shifts from `origin`
    (`reference.batch_split`, four readable members or more), and the cut."""
    shift = {p: 0.0 if p == origin else
             (reference.align_bg(profiles[p], profiles[origin]) or {}).get("shift_bg_sd") or 0.0
             for p in members}
    core = [p for p in members if _core(profiles[p]) and p not in flags]
    cut = reference.batch_split([shift[p] for p in core]) if len(core) >= 4 else None
    if cut is None:
        return [members], None
    low = [p for p in members if shift[p] <= cut]
    high = [p for p in members if shift[p] > cut]
    return [part for part in (low, high) if part], cut


def partition(profiles, reference_image=None, order=None, *, params=None) -> dict:
    """{groups, singletons}: the images of one marker (`profiles`, each a
    profiled unit's class, metrics, summary, fit space and flags) in groups
    of aligned images. Each group: members, its scale image S, the origin it
    was formed around, anchors (S first), each member's alignment onto S and
    class against it, flags, and the shift it was split at. `singletons`:
    {image: class} of the images gated on their own.

    Groups form in rounds: the medoid of the images left (those with
    positives of their own first) gathers the ones aligned with it; the rest
    try again around their own medoid, so two batches far apart are two
    groups, not one group and a crowd of singletons. A round whose medoid
    gathers nothing leaves it alone. An image under `cell_floor` cells left
    alone is attached to the group whose origin it is nearest to."""
    params = {**GROUPS, **(params or {})}
    order = [p for p in (order or sorted(profiles)) if p in profiles]
    singles, classes, flags = {}, {}, {}
    left = []
    for p in order:
        if reference.background(profiles[p]) is None:
            singles[p] = "distribution_change"
        elif _failed(profiles[p]):
            singles[p] = "staining_failure"
        else:
            left.append(p)
    rounds, alone = [], []
    while left:
        pool = [p for p in left if _core(profiles[p])]
        if not pool:
            alone.extend(left)
            break
        origin = _medoid(pool, profiles)
        members, seen = _members_of(left, origin, profiles)
        for p, klass in seen.items():
            classes.setdefault(p, klass)
        if len(members) < 2:
            alone.append(origin)
            left.remove(origin)
            continue
        rounds.append({"origin": origin, "members": members})
        left = [p for p in left if p not in members]
    for p in alone:
        near = min(rounds, key=lambda r: _distance(profiles[p], profiles[r["origin"]]),
                   default=None)
        al = reference.align_bg(profiles[p], profiles[near["origin"]]) if near else None
        if near is not None and _n_cells(profiles[p]) < params["cell_floor"] \
                and reference.mappable(al):
            near["members"].append(p)
            flags[p] = ["attached_under_floor"]
        elif p in classes:
            singles[p] = classes[p]
        else:
            singles[p] = reference.classify_group(al, profiles[p], profiles[near["origin"]])                 if near is not None else "image_specific_shift"
    groups = []
    for r in rounds:
        parts, cut = _split(r["members"], r["origin"], profiles, flags)
        for part in parts:
            if len(part) == 1:
                singles[part[0]] = classes.get(part[0]) or "stable"
                continue
            group = _group(part, r["origin"] if r["origin"] in part
                           else _medoid(part, profiles), profiles, reference_image, flags,
                           params)
            for p in group.pop("dropped"):
                singles[p] = group["classes"].pop(p)
            if len(group["members"]) < 2:
                for p in group["members"]:
                    singles[p] = classes.get(p) or "stable"
                continue
            group["split"] = cut
            groups.append(group)
    return {"groups": groups, "singletons": {p: singles[p] for p in order if p in singles}}


def _group(part, origin, profiles, reference_image, flags, params) -> dict:
    """One group: its scale image, every member re-aligned onto it, anchors."""
    eligible = [p for p in part if _core(profiles[p]) and p not in flags]
    if reference_image in eligible:
        scale = reference_image
    else:
        scale = _medoid(eligible or part, profiles)
    alignments, classes, kept, dropped = {}, {}, [], []
    for p in part:
        if p == scale:
            alignments[p] = reference.identity_alignment()
            classes[p] = "stable"
            kept.append(p)
            continue
        al = reference.align_bg(profiles[p], profiles[scale])
        klass = reference.classify_group(al, profiles[p], profiles[scale])
        classes[p] = klass
        if al is not None and reference.mappable(al) and (
                klass in reference.MEMBER_CLASSES or p in flags):
            alignments[p] = al
            kept.append(p)
        else:
            dropped.append(p)
    shifts = {p: alignments[p]["shift_bg_sd"] for p in kept}
    candidates = [p for p in kept if p != scale and _core(profiles[p]) and p not in flags]
    return {"members": kept, "scale": scale, "origin": origin,
            "anchors": _anchors(scale, candidates, shifts, int(params["anchors_max"])),
            "alignments": alignments, "classes": classes,
            "flags": {p: flags[p] for p in kept if p in flags}, "dropped": dropped}


# -- the session's groups --------------------------------------------------------


def _profile(unit) -> dict:
    return {k: unit.get(k) for k in ("class", "metrics", "summary", "fit_space", "flags")}


def due(engine, marker) -> bool:
    """Whether `marker` can be grouped: not grouped yet, and every image's
    unit of it profiled (or closed by the bulk pass)."""
    if marker in (engine.record.get("groups") or {}):
        return False
    units = [engine.record["units"].get(unit_key(p, marker)) for p in engine.record["images"]]
    return all(u is None or u["state"] != "pending" for u in units)


def maintain(engine):
    """Group every marker that is due, then carry every group whose scale
    image has closed. Called before each decision is chosen (`ready_units`,
    `next_unit`) and at the end of the bulk pass."""
    if not active(engine):
        return
    for marker in engine.record["order"]:
        if due(engine, marker):
            build(engine, marker)
    for entry in (engine.record.get("groups") or {}).values():
        for group in entry.get("groups") or ():
            if group.get("state") == "deciding":
                scale = engine.record["units"].get(unit_key(group["scale"], group["marker"]))
                if scale is None or scale["state"] in TERMINAL:
                    carry(engine, group)


def build(engine, marker):
    """Group one marker's images (`partition`), freeze the groups, and make
    the first decisions: each scale image's and each singleton's, as v3
    makes them for an image alone (`bulk.decide_first`)."""
    from plexora.plugins.gating.server.autogate import bulk

    record = engine.record
    waiting = {}
    for project in record["images"]:
        unit = record["units"].get(unit_key(project, marker))
        if unit is not None and unit["state"] == "group_pending":
            waiting[project] = unit
    out = partition({p: _profile(u) for p, u in waiting.items()},
                    record.get("reference_image"), list(waiting))
    built = []
    for n, part in enumerate(out["groups"], 1):
        group = {"id": f"group:{marker}:{n}", "marker": marker, "version": VERSION,
                 "state": "deciding", **part, "checks": {}, "own_looks": 0}
        built.append(group)
        for project in group["members"]:
            role = "scale" if project == group["scale"] else (
                "anchor" if project in group["anchors"] else "member")
            waiting[project]["group"] = {
                "id": group["id"], "scale": group["scale"], "role": role,
                "class": group["classes"][project], "alignment": group["alignments"][project],
                **({"flags": list(group["flags"][project])} if project in group["flags"]
                   else {})}
    record.setdefault("groups", {})[marker] = {"groups": built, "singletons": out["singletons"],
                                               "version": VERSION}
    engine.log(event="grouped", marker=marker,
               groups=[{"id": g["id"], "scale": g["scale"], "members": len(g["members"]),
                        "anchors": g["anchors"], "split": g.get("split")} for g in built],
               singletons=out["singletons"])
    for group in built:
        bulk.decide_first(engine, waiting[group["scale"]])
    for project, klass in out["singletons"].items():
        unit = waiting[project]
        unit["group"] = {"id": None, "role": "alone", "class": klass}
        if klass == "staining_failure":
            unit["flags"] = sorted(set(unit.get("flags") or []) | {"staining_failure"})
        bulk.decide_first(engine, unit)


def _pf(engine, project, unit, gate) -> float | None:
    ds = engine.call.session.data(project)
    counted = tableops.local_or_node(ds, "gating.autogate.gates_at", {
        "marker": unit["marker"], "lows": [float(gate)], "high": unit.get("high")})
    return counted["n_positive"][0] / max(1, counted["n_finite"])


def marker_packets(engine, marker) -> int:
    """The packets the marker's units have been shown so far, every image."""
    seen = set()
    for project in engine.record["images"]:
        unit = engine.record["units"].get(unit_key(project, marker)) or {}
        seen.update(unit.get("packets") or ())
    return len(seen)


def ceiling(engine) -> int:
    return int(GROUPS["packets_base"]) + len(engine.record["images"])


def carry(engine, group):
    """The group's decision into every member, once its scale image closed:
    accepted -> carried and checked (`_carry_accepted`); closed for review
    with a gate -> every member closed for review at the carried gate;
    anything else (no gate reached) -> each member decided on its own."""
    from plexora.plugins.gating.server.autogate import bulk

    marker = group["marker"]
    units = engine.record["units"]
    scale = units.get(unit_key(group["scale"], marker))
    members = [p for p in group["members"] if p != group["scale"]]
    state = (scale or {}).get("state")
    gate = (scale or {}).get("final")
    if gate is None:
        gate = (scale or {}).get("proposed")
    if scale is not None and state in schemas.ACCEPTED_STATES and scale.get("final") is not None:
        group["state"] = "carried"
        group["gate"] = float(scale["final"])
        _carry_accepted(engine, group, scale)
        return
    if scale is not None and state in schemas.REVIEW_WRITE_STATES and gate is not None:
        group["state"] = "closed"
        group["gate"] = float(gate)
        group["outcome"] = "review"
        for project in members:
            unit = units[unit_key(project, marker)]
            carried = _carried(group, scale, unit, project, float(gate))
            _set_transfer(unit, group, scale, carried, None)
            engine.close(unit, "manual_review_recommended",
                         f"{group['id']}: its scale image {group['scale']} closed for review "
                         f"({scale.get('reason') or state}); written at its gate carried here",
                         proposed=carried, unlooked="group_review")
        return
    group["state"] = "closed"
    group["outcome"] = "own"
    for project in members:
        unit = units[unit_key(project, marker)]
        if unit["state"] != "group_pending":
            continue
        unit["group"] = {**(unit.get("group") or {}), "fallback": f"the scale image ended {state}"}
        bulk.decide_first(engine, unit)


def _carried(group, scale, unit, project, gate) -> float:
    al = group["alignments"][project]
    return float(reference.carry(al, gate, scale.get("fit_space"), unit.get("fit_space")))


def _set_transfer(unit, group, scale, carried, pf):
    """What `transfer.transfer_packet` and the provenance read of a carried
    gate: the scale image as the reference, the alignment, the class."""
    info = unit.get("group") or {}
    unit["transfer"] = {"reference": group["scale"], "reference_gate": group.get("gate"),
                        "reference_confidence": scale.get("confidence"),
                        "aligned_gate": carried, "alignment": info.get("alignment"),
                        "shift_bg_sd": (info.get("alignment") or {}).get("shift_bg_sd"),
                        "class": info.get("class"), "pf_aligned": pf,
                        "pf_reference": group.get("pf_scale"), "own_gmm": unit.get("gmm"),
                        "group": group["id"]}


def _carry_accepted(engine, group, scale):
    from plexora.plugins.gating.server.autogate import candidates

    marker = group["marker"]
    units = engine.record["units"]
    gate = group["gate"]
    group["pf_scale"] = _pf(engine, group["scale"], scale, gate)
    rows = []
    for project in group["members"]:
        if project == group["scale"]:
            continue
        unit = units[unit_key(project, marker)]
        if unit["state"] != "group_pending":
            continue
        carried = _carried(group, scale, unit, project, gate)
        ds = engine.call.session.data(project)
        pf = _pf(engine, project, unit, carried)
        _set_transfer(unit, group, scale, carried, pf)
        rows.append({"project": project, "unit": unit, "gate": carried, "pf": pf,
                     "inside": candidates.inside_guard(ds, marker, carried),
                     "class": group["classes"].get(project)})
    core = [r for r in rows if r["class"] != "no_positives"
            and "attached_under_floor" not in (group["flags"].get(r["project"]) or ())]
    pfs = [group["pf_scale"]] + [r["pf"] for r in core]
    med = float(np.median(pfs))
    mad = float(np.median(np.abs(np.asarray(pfs) - med))) * 1.4826
    if len(pfs) >= int(GROUPS["pf_min_members"]):
        centre = med
        bar = max(GROUPS["pf_mad"] * mad, GROUPS["pf_abs"], GROUPS["pf_rel"] * med)
    else:
        centre = group["pf_scale"]
        bar = max(GROUPS["pf_abs"], GROUPS["pf_rel"] * centre)
    group["pf"] = {"median": round(med, 5), "mad": round(mad, 5), "centre": round(centre, 5),
                   "bar": round(bar, 5)}
    failing = []
    for row in rows:
        why, severity = [], 0.0
        if row["class"] == "no_positives":
            if not _no_positive_ok(row, pfs):
                why.append("no_positives_check")
        else:
            if row["inside"] is False:
                why.append("guard_band")
                severity += 1.0
            off = abs(row["pf"] - centre)
            if off > bar:
                why.append("group_pf")
                severity += off / max(bar, 1e-9)
        row["why"] = why
        row["severity"] = severity
        if why and row["class"] != "no_positives":
            failing.append(row)
    budget = max(int(GROUPS["checks_min"]),
                 math.ceil(float(GROUPS["checks_share"]) * len(group["members"])))
    room = max(0, ceiling(engine) - marker_packets(engine, marker))
    checks = sorted(failing, key=lambda r: (-r["severity"], r["project"]))[:min(budget, room)]
    checked = {r["project"] for r in checks}
    level = scale.get("confidence") if scale.get("confidence") in ("high", "moderate") else "low"
    for row in rows:
        unit = row["unit"]
        group["checks"][row["project"]] = {"why": row["why"], "pf": round(row["pf"], 5),
                                           "gate": row["gate"],
                                           "outcome": "check" if row["project"] in checked
                                           else ("unverified" if row["why"] else "auto")}
        if row["project"] in checked:
            unit["candidate"] = float(row["gate"])
            unit["method"] = METHOD
            unit["state"] = "transfer_check"
            continue
        flags = []
        confidence = "high" if level == "high" and row["class"] == "stable" else \
            ("moderate" if level in ("high", "moderate") else "low")
        if row["class"] == "no_positives":
            flags.append("carried_no_positives")
            confidence = "low"
        if row["why"]:
            flags.append("packet_ceiling" if room < budget and row in failing
                         and len(checks) < len(failing) else "group_check_unverified")
            confidence = "low"
        accept(engine, unit, row["gate"], confidence, flags)
    engine.log(event="group_carried", group=group["id"], gate=gate,
               checks=sorted(checked), pf=group["pf"],
               unverified=sorted(p for p, c in group["checks"].items()
                                 if c["outcome"] == "unverified"))


def _no_positive_ok(row, pfs) -> bool:
    """A member with no positives of its own: its share at the carried gate
    no more than the group's largest, and the gate above its background."""
    unit = row["unit"]
    if row["pf"] > max(pfs):
        return False
    bg = reference.background(unit)
    if bg is None:
        return True
    fit = reference.to_fit(row["gate"], unit.get("fit_space"))
    return fit >= bg["mu"] + GROUPS["no_positive_sd"] * bg["sd"]


def accept(engine, unit, gate, confidence, flags=()):
    """A carried gate accepted: written at T1 for the audit sheet (or
    through the numeric checks without one), as a stable transfer is."""
    unit["candidate"] = float(gate)
    unit["path"] = "transfer"
    unit["method"] = METHOD
    unit["transfer_confidence"] = confidence
    if flags:
        info = unit.setdefault("group", {})
        info["flags"] = sorted(set(info.get("flags") or []) | set(flags))
    if engine.options["audit_sheet"]:
        engine.write(unit, unit["candidate"], method=METHOD, confidence=confidence,
                     state="accepted_t1", tier="T1")
        if unit["state"] not in TERMINAL:
            unit["state"] = "accepted_t1"
    else:
        unit["state"] = "awaiting_regression"
        engine.settle(unit)


def after_check(engine, unit, verdict) -> bool:
    """A group member's `transfer_check` answer; False when `unit` is not a
    group member (the v1/v3 transfer path decides it). `holds` goes through
    the numeric checks as any carried gate; `cannot_tell` keeps the carried
    gate, unverified; too low or too high gives the image its own decision
    while the group's `own_looks` and the marker's packet ceiling allow, else
    closes it for review at the carried gate."""
    group = group_of(engine, unit)
    if group is None:
        return False
    check = group["checks"].setdefault(unit["project"], {})
    check["verdict"] = verdict
    if verdict == "holds":
        unit["path"] = "transfer"
        unit["transfer_confidence"] = "moderate"
        unit["method"] = METHOD
        unit["state"] = "awaiting_regression"
        engine.settle(unit)
        return True
    if verdict not in ("too_low", "too_high"):
        accept(engine, unit, unit["candidate"], "low", ["group_check_unverified"])
        return True
    side = verdict.replace("_", " ")
    if int(group.get("own_looks") or 0) < int(GROUPS["own_looks"]) \
            and marker_packets(engine, unit["marker"]) < ceiling(engine):
        group["own_looks"] = int(group.get("own_looks") or 0) + 1
        check["outcome"] = "own"
        unit["transfer_verdict"] = verdict
        unit["method"] = None
        unit["path"] = None
        unit["reason"] = f"the gate carried from {group['scale']} read {side}; decided on its own"
        unit["state"] = "awaiting_t2"
        return True
    check["outcome"] = "review"
    engine.close(unit, "manual_review_recommended",
                 f"{group['id']}: the gate carried from {group['scale']} read {side}, and the "
                 "group has given its own looks out; written at the carried gate for review",
                 proposed=unit["candidate"])
    return True


def after_strip(engine, unit, verdict) -> bool:
    """A carried gate the audit sheet did not pass; False when `unit` is not
    a carried group member (its own look follows, as for any T1 gate). A
    member gets its own decision while the group's `own_looks` allow, else
    closes for review at the carried gate."""
    group = group_of(engine, unit)
    if group is None or (unit.get("group") or {}).get("role") == "scale"             or unit.get("path") != "transfer":
        return False
    check = group["checks"].setdefault(unit["project"], {})
    check["strip"] = verdict
    unit["audited"] = False
    if int(group.get("own_looks") or 0) < int(GROUPS["own_looks"]):
        group["own_looks"] = int(group.get("own_looks") or 0) + 1
        check["outcome"] = "own"
        unit["method"] = None
        unit["path"] = None
        unit["state"] = "awaiting_t2"
        unit["reason"] = (f"the audit sheet row of the gate carried from {group['scale']} "
                          f"looked {verdict.replace('_', ' ')}; decided on its own")
        return True
    check["outcome"] = "review"
    engine.close(unit, "manual_review_recommended",
                 f"{group['id']}: the audit sheet row of the gate carried from "
                 f"{group['scale']} looked {verdict.replace('_', ' ')}, and the group has "
                 "given its own looks out; written there for review",
                 proposed=unit["candidate"])
    return True


# -- what status and the report say ----------------------------------------------


def summary(engine) -> dict:
    """Per marker: its groups (scale image, members, anchors, gate, outcome,
    checks), the images gated alone and why, and the strategy that implies."""
    out = {}
    for marker, entry in (engine.record.get("groups") or {}).items():
        groups = entry.get("groups") or []
        singles = entry.get("singletons") or {}
        n = len(engine.record["images"])
        if len(groups) == 1 and not singles:
            strategy = reference.GLOBAL_ALIGNED
        elif groups and len(singles) / max(1, n) >= reference.THRESHOLDS["per_image_share"]:
            strategy = reference.PER_IMAGE
        elif len(groups) >= 2:
            strategy = reference.PER_BATCH
        elif groups:
            strategy = reference.INFORMED_PER_IMAGE
        else:
            strategy = reference.PER_IMAGE
        out[marker] = {
            "groups": [{"id": g["id"], "scale": g["scale"], "members": g["members"],
                        "anchors": g["anchors"], "state": g.get("state"),
                        "gate": g.get("gate"), "outcome": g.get("outcome"),
                        "split": g.get("split"), "pf": g.get("pf"),
                        "checks": {p: c.get("outcome") for p, c in (g.get("checks") or {}).items()},
                        "classes": g.get("classes")}
                       for g in groups],
            "singletons": singles, "strategy": strategy}
    return out


def brief(engine) -> dict:
    """{marker: {n_groups, per_image}} for the session's brief status."""
    return {marker: {"n_groups": len(entry.get("groups") or ()),
                     "per_image": len(entry.get("singletons") or ())}
            for marker, entry in (engine.record.get("groups") or {}).items()}
