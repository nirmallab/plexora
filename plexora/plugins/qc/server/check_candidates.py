"""A check's flagged regions as the session's candidates.

A region at a check's bar (`score_fields.regions`) becomes a candidate unit
like a detector's, with three differences:

- its outline is the check's own grid (`fine_grid`): the envelope is the
  region's polygon, never re-drawn on the coarser scan grid, so a
  registration region is as snug as the ~6.5 um mismatch map;
- `trace` says how the written outline is made: `method` (Blur QC's
  regions are traced at pixel level inside that envelope, as any blur),
  `map` (registration: the map is the outline -- there is no brighter or
  darker pixel to trace), `none` (a whole-tissue region is its tissue
  outline);
- `metrics` hold the score, the threshold, its source and steps, so every
  region says what bar it crossed (`provenance`).

The id is a content hash over the check, its scores' fingerprint, the bar
and the region's rank: the same review gives the same candidates, and the
answer memo recognises them.

What the review said is carried: a region the settle creates (decided, to
confirm, or for manual review) takes the review's `artifact_class` as its
`class_hint` and its severity as `review_hint`, so the confirm sheet, the
manual-review warning and the decision all name what the agent saw (a
registration review that answered `cycle_specific_tissue_loss` is not sent on
as a registration error).

Two more kinds of check region live here:

- **one-cycle places** (`one_cycle_units`): the Registration Check's map
  split off the places where one cycle has nuclei and the other (almost)
  none. They are tissue lost in a cycle (or debris on one), never a
  registration error, so they become `cycle_specific_tissue_loss` candidates
  scoped to the cycle that lost them -- and are never sampled by the
  registration review.
- **probes** (`hold_for_probes`, `settle_held`): a check's to-confirm regions
  are one population judged against one bar. Its `check_confirm_probe`
  strongest are looked at first, the rest held. When every probe comes back
  the same -- all `not_artifact`, or all artifacts of one class -- the rest
  take that verdict without a look (`extrapolated`, recording the probes and
  the rule); any disagreement (or a probe that ended in manual review)
  releases the rest to be confirmed one by one. Deterministic: the probes are
  the highest scores, ties by id.
"""

from __future__ import annotations

import hashlib

from plexora.plugins.qc.server import candidates as cand
from plexora.plugins.qc.server import polygons, schemas

#: `object`: the Artifact Detector's own traced outline is the region.
TRACE = {"blur": "method", "registration": "map", "artifacts": "object"}


def _version(check):
    from plexora.plugins.qc.server import artifacts, blur, registration

    return {"blur": blur.VERSION, "registration": registration.VERSION,
            "artifacts": artifacts.VERSION}.get(check, "1")


def _id(project, check_unit, bar, key):
    blob = f"{project}|{check_unit['id']}|{check_unit.get('fingerprint')}|{bar:.6g}|{key}"
    return "cand_" + hashlib.sha1(blob.encode("utf-8")).hexdigest()[:10]


def _scope(engine, check_unit, lost_in=None, reference=None, region=None):
    """(scope, channels, cycles) a check's region is about; a one-cycle
    region is about the cycle that lost its nuclei (`lost_in` "reference":
    the reference channel's cycle). An artifact object is about the
    channels it shows in: a saturated patch its own channel, the rest every
    channel."""
    check = check_unit["check"]
    if check == "artifacts":
        region = region or {}
        channels = list(region.get("channels") or [])
        if region.get("category") == "saturation" and region.get("source_channel"):
            meta = engine.scan(check_unit["project"]).channel(region["source_channel"]) or {}
            cycle = meta.get("cycle")
            return "channel", [region["source_channel"]], [int(cycle)] if cycle else []
        return "all_channels", channels, []
    channel = check_unit.get("channel")
    if lost_in == "reference" and reference:
        channel = reference
    scan = engine.scan(check_unit["project"])
    meta = scan.channel(channel) if channel else {}
    cycle = (meta or {}).get("cycle")
    if check == "registration":
        # A cycle out of register takes every channel imaged in it.
        members = [c["name"] for c in scan.channels if cycle and c.get("cycle") == cycle] \
            or [channel]
        return "cycle", members, [int(cycle)] if cycle else []
    return "channel", [channel], [int(cycle)] if cycle else []


def _audit(engine, check_unit, channels):
    project = check_unit["project"]
    names = [c for c in (check_unit.get("channel"), *channels)
             if c and engine.channel_unit(c, project) is not None]
    return list(dict.fromkeys(names))


def _metrics(check_unit, bar, region=None, *, cell_um=None):
    """A check region's numbers. Its size is in score-grid squares
    (`score_cells`, each `score_cell_um` across) and in µm² -- a bare
    `cells` read as segmented cells."""
    out = {"check": check_unit["check"], "threshold": bar["value"],
           "auto_threshold": bar["auto"], "offset_steps": bar["offset_steps"],
           "step": bar["step"],
           "threshold_source": check_unit.get("threshold_source") or "auto",
           "fingerprint": check_unit.get("fingerprint")}
    if region is not None:
        out.update(score_cells=region["cells"], score_cell_um=cell_um,
                   mean=region["mean"], max=region["max"],
                   area_um2=region.get("area_um2"), weight_pct=region.get("weight_pct"))
    return out


def review_hint(answer, check):
    """{artifact_class, severity, source} a score review's answer gives every
    region its settle creates (None without a class or severity)."""
    klass = getattr(answer, "artifact_class", None)
    severity = getattr(answer, "severity", None)
    if not klass and not severity:
        return None
    return {"artifact_class": klass or schemas.CHECK_CLASS[check], "severity": severity,
            "source": "score_review"}


def on_tissue(engine, project, regions) -> tuple:
    """(regions, n_on_glass): the regions with at least `VISUAL.min_on_tissue`
    of their area on the tissue (feathered, holes filled), and how many were
    dropped -- a check's region on the glass affects no cell and is no
    finding, the same rule a drawn outline meets in `segment_qc_roi`."""
    from plexora.plugins.qc.server import tissue

    if not regions:
        return regions, 0
    try:
        found = tissue.for_project(engine.call.session, project)
    except Exception:  # noqa: BLE001 -- no tissue: nothing to hold them to
        return regions, 0
    floor = float(schemas.VISUAL["min_on_tissue"])
    kept = [r for r in regions if r.get("geometry") is None
            or tissue.share_on_tissue(found, r["geometry"])["on_tissue_fraction"] >= floor]
    return kept, len(regions) - len(kept)


def unit_for(engine, check_unit, field, found, index, bar, *, hint=None, lost_in=None,
             key=None):
    """The candidate unit of region `index` of `found` at `bar`. `hint` (a
    `review_hint`) names the class the review saw; `lost_in` makes it a
    one-cycle region (`cycle_specific_tissue_loss`) of the cycle named."""
    project = check_unit["project"]
    scan = engine.scan(project)
    region = found["regions"][index]
    geometry = region["geometry"]
    mask = polygons.geometry_to_grid(geometry, scan.grid, touch=True)
    reference = check_unit.get("reference") or getattr(field, "reference", None)
    scope, channels, cycles = _scope(engine, check_unit, lost_in, reference, region)
    audit = _audit(engine, check_unit, channels)
    check = check_unit["check"]
    klass = region.get("class") or schemas.CHECK_CLASS[check]
    if lost_in:
        klass = "cycle_specific_tissue_loss"
    elif hint and hint.get("artifact_class") in schemas.CLASS_WORDS and not (
            region.get("class") and hint["artifact_class"] == schemas.CHECK_CLASS[check]):
        # The class the agent named; the check's generic one (a review that
        # named none) never replaces a region's own (an Artifact Detector
        # category: a tear is tissue damage, not "a tissue artifact").
        klass = hint["artifact_class"]
    unit = {"type": "candidate", "project": project,
            "id": _id(project, check_unit, bar["value"], key or region["id"]),
            "channel": check_unit.get("channel") or region.get("source_channel")
            or (audit[0] if audit else None),
            "audit_channel": audit[0] if audit else None, "audit_channels": audit,
            "channels": channels, "cycles": cycles, "detector": check,
            **({"reference": reference} if reference else {}),
            "detector_version": _version(check), "class_hint": klass, "alternatives": [],
            "scope_hint": scope, "score": float(region["max"]), "severity": float(region["max"]),
            "metrics": _metrics(check_unit, bar, region,
                                cell_um=getattr(field, "cell_um", None)),
            "primary_metric": "",
            "merged_from": [], "mask": cand.encode_mask(mask),
            "bbox": [float(v) for v in region["bbox"]], "peak": list(region["peak"]),
            "measurement": {"tissue_fraction": cand.area_fraction(mask, scan),
                            "cells": int(region["cells"]),
                            "area_um2": region.get("area_um2")},
            "envelope_geometry": geometry, "fine_grid": True,
            "variants": {"standard": {"geometry": geometry,
                                      "area_um2": region.get("area_um2")}},
            "trace": TRACE[check], "origin": "check", "check_unit": check_unit["id"],
            "cell_um": field.cell_um, "level": 0, "state": "awaiting_confirm"}
    if hint and not lost_in:
        unit["review_hint"] = dict(hint)
    if lost_in:
        unit["one_cycle"] = {"lost_in": lost_in, "bar": bar["value"]}
        unit["metrics"].update(one_cycle=True, lost_in=lost_in, threshold_source="auto",
                               offset_steps=0)
    return unit


def global_unit(engine, check_unit, field, bar):
    """One region over the whole tissue: the check's problem everywhere."""
    project = check_unit["project"]
    scan = engine.scan(project)
    tissue = scan.tissue()
    geometry = polygons.mask_to_geometry(tissue, scan.grid)
    check = check_unit["check"]
    scope, channels, cycles = _scope(engine, check_unit)
    if check == "blur" and cycles:
        # A whole channel out of focus is how its cycle was acquired.
        members = [c["name"] for c in scan.channels if c.get("cycle") == cycles[0]]
        scope, channels = "cycle", members or channels
    audit = _audit(engine, check_unit, channels)
    from plexora.plugins.qc.server.scan import bbox_fullres

    box = bbox_fullres(scan.grid, tissue) or (0, 0, *scan.grid["image_size"])
    unit = {"type": "candidate", "project": project,
            "id": _id(project, check_unit, bar["value"], "whole_tissue"),
            "channel": check_unit.get("channel") or (audit[0] if audit else None),
            "audit_channel": audit[0] if audit else None, "audit_channels": audit,
            "channels": channels, "cycles": cycles, "detector": check,
            **({"reference": check_unit["reference"]} if check_unit.get("reference") else {}),
            "detector_version": _version(check), "class_hint": schemas.CHECK_CLASS[check],
            "alternatives": [], "scope_hint": scope, "score": None, "severity": 1.0,
            "metrics": {**_metrics(check_unit, bar), "whole_tissue": True},
            "primary_metric": "", "merged_from": [], "mask": cand.encode_mask(tissue),
            "bbox": [float(v) for v in box], "peak": [(box[0] + box[2]) / 2,
                                                      (box[1] + box[3]) / 2],
            "measurement": {"tissue_fraction": 1.0, "cells": int(tissue.sum())},
            "envelope_geometry": geometry, "fine_grid": True, "trace": "none",
            "origin": "check", "check_unit": check_unit["id"], "whole_tissue": True,
            "level": 0, "state": "awaiting_confirm"}
    if check == "registration":
        # A cycle out of register everywhere is a verdict on the channel, not
        # a place: no ROI over the tissue, its markers unreliable in every
        # cell (`cells.calls`), the shift said in microns.
        stats = field.stats or {}
        unit["channel_level"] = True
        unit["metrics"].update({k: stats.get(k) for k in (
            "global_shift_um", "global_shift_px", "pattern", "highlighted_pct")
            if stats.get(k) is not None})
    return unit


# -- one-cycle places ------------------------------------------------------------------------


def one_cycle_found(entry, *, pixel_um, fingerprint, image_size, reference=None,
                    comparison=None) -> list:
    """[(lost_in, field, found, bar)]: the one-cycle regions of a registration
    field at their automatic bar (no pixel read; geometry included)."""
    from plexora.plugins.qc.server import score_fields

    out = []
    fields = score_fields.one_cycle_fields(entry, pixel_um=pixel_um, fingerprint=fingerprint,
                                           image_size=image_size, reference=reference,
                                           comparison=comparison)
    limit = int(schemas.ENGINE["check_one_cycle_regions"])
    for lost_in, field in fields.items():
        bar = score_fields.bar(field)
        found = score_fields.regions(field, bar["value"],
                                     min_cells=int(schemas.ENGINE["check_one_cycle_min_cells"]),
                                     max_regions=limit)
        if found["regions"]:
            out.append((lost_in, field, found, bar))
    return out


def one_cycle_units(engine, check_unit, found_list) -> list:
    """The `cycle_specific_tissue_loss` candidates of `one_cycle_found`,
    largest first, at most `check_one_cycle_regions` in all."""
    limit = int(schemas.ENGINE["check_one_cycle_regions"])
    # Tissue "lost" on the glass is debris that came or went there: no cell.
    found_list = [(lost_in, field, {**found, "regions": on_tissue(
        engine, check_unit["project"], found["regions"])[0]}, bar)
        for lost_in, field, found, bar in found_list]
    pairs = [(lost_in, field, found, bar, i) for lost_in, field, found, bar in found_list
             for i in range(len(found["regions"]))]
    pairs.sort(key=lambda p: (-p[2]["regions"][p[4]]["cells"], p[0], p[4]))
    return [unit_for(engine, check_unit, field, found, index, bar, lost_in=lost_in,
                     key=f"one_cycle:{lost_in}:{found['regions'][index]['id']}")
            for lost_in, field, found, bar, index in pairs[:limit]]


def one_cycle_summary(found_list) -> dict:
    """What a registration check unit keeps of its one-cycle places."""
    return {"n_regions": sum(f["n_regions"] for _l, _f, f, _b in found_list),
            "by_cycle_lost": {lost_in: {"n_regions": found["n_regions"],
                                        "flagged_pct": found["flagged_pct"],
                                        "bar": bar["value"]}
                              for lost_in, _field, found, bar in found_list},
            "class": "cycle_specific_tissue_loss"}


# -- probes: the strongest few judged, the verdict carried --------------------------------


def hold_for_probes(engine, check_unit, units, group="confirm") -> dict | None:
    """Keep the `check_confirm_probe` strongest of `units` (fresh to-confirm
    candidates of one check) asked; hold the rest until those are judged.
    Returns the group record (kept on the check unit under
    `confirm_groups`), or None when the group is no larger than the probes."""
    k = int(schemas.ENGINE.get("check_confirm_probe") or 0)
    units = [u for u in units if u.get("state") == "awaiting_confirm"
             and not u.get("held_for") and not u.get("probe_for")]
    if k <= 0 or len(units) <= k:
        return None
    order = sorted(units, key=lambda u: (-float(u.get("score") or 0.0), u["id"]))
    probes, held = order[:k], order[k:]
    gid = f"{check_unit['id']}:{group}"
    for unit in probes:
        unit["probe_for"] = gid
    for unit in held:
        unit["held_for"] = gid
    record = {"probes": [u["id"] for u in probes], "held": [u["id"] for u in held],
              "state": "probing", "rule": "check_confirm_probe", "k": k}
    check_unit.setdefault("confirm_groups", {})[gid] = record
    return record


def _verdict(unit):
    """What a settled probe came back as: ("not_artifact", None),
    ("artifact", class) or None (nothing to carry: manual review, the
    user's, a merge without a judgment)."""
    state = unit.get("state")
    if state == "dismissed":
        return ("not_artifact", None)
    decision = unit.get("decision") or {}
    if state in schemas.CONFIRMED_STATES or (state == "merged"
                                              and decision.get("verdict") == "artifact"):
        return ("artifact", unit.get("class") or decision.get("artifact_class"))
    return None


def _candidate(engine, project, cid):
    return engine.record["units"].get(engine.unit_key_of(
        {"project": project, "type": "candidate", "id": cid}))


def settle_held(engine, project=None) -> int:
    """Resolve every probe group whose probes are all settled: carry their
    common verdict to the held regions (`extrapolated`), or release them to
    be confirmed one by one. Returns how many held regions were touched."""
    from collections import Counter

    from plexora.plugins.qc.server.engine import TERMINAL

    touched = 0
    for check in engine.units_of("check", project):
        for gid, group in (check.get("confirm_groups") or {}).items():
            if group.get("state") != "probing":
                continue
            probes = [p for p in (_candidate(engine, check["project"], cid)
                                  for cid in group["probes"]) if p is not None]
            if any(p["state"] not in TERMINAL for p in probes):
                continue
            verdicts = [_verdict(p) for p in probes]
            held = [h for h in (_candidate(engine, check["project"], cid)
                                for cid in group["held"]) if h is not None]
            common = verdicts[0] if verdicts and verdicts[0] is not None \
                and all(v == verdicts[0] for v in verdicts) else None
            if common is None:
                for unit in held:
                    unit.pop("held_for", None)
                group.update(state="released",
                             verdicts=[list(v) if v else None for v in verdicts])
                touched += len(held)
                continue
            severities = Counter(((p.get("decision") or {}).get("severity") or "moderate")
                                 for p in probes)
            severity = sorted(severities.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            source = {"rule": "check_confirm_probe", "probes": [p["id"] for p in probes],
                      "verdict": common[0], "artifact_class": common[1],
                      "check_unit": check["id"]}
            carried = 0
            for unit in held:
                unit.pop("held_for", None)
                if unit["state"] != "awaiting_confirm" or int(unit.get("level") or 0):
                    continue
                unit["extrapolated"] = dict(source)
                if common[0] == "not_artifact":
                    engine.close(unit, "dismissed",
                                 f"extrapolated: the {len(probes)} strongest regions of this "
                                 "check were all judged not artifacts")
                else:
                    unit["decision"] = {"verdict": "artifact", "artifact_class": common[1],
                                        "severity": severity, "confidence": None,
                                        "boundary": "covers", "scope": unit.get("scope_hint"),
                                        "exclude_recommended": None, "source": "extrapolated",
                                        "extrapolated_from": source["probes"]}
                    engine.decide(unit)
                    from plexora.plugins.qc.server import transitions

                    transitions._absorb(engine, unit)
                carried += 1
            group.update(state="extrapolated", verdict=list(common), carried=carried)
            touched += carried
            regions = check.setdefault("regions", {})
            regions["extrapolated"] = int(regions.get("extrapolated") or 0) + carried
            from plexora.plugins.qc.server import checks_result

            checks_result.record(engine, check)
    return touched


def still_held(engine, unit) -> bool:
    """Whether a held candidate still waits for its probes (any group that
    is ready is resolved first)."""
    if not unit.get("held_for"):
        return False
    settle_held(engine, unit.get("project"))
    return bool(unit.get("held_for"))
