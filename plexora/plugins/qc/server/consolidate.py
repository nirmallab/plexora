"""The last step of a session: one ROI per category and action, not one per
finding -- a handful of regions a user excludes cells by.

Every check and detector finds its own problem, and two of them can find the
same place -- a misregistered field that is also out of focus, a fold the
artifact detector and a dark-region detector both outlined, the same class
found in several pieces. Written as they were found, the same tissue carries
several ROIs, and a reader sees one problem counted many times.

So once every decision is in, the session's findings are laid over each other
and the tissue they cover is PARTITIONED: each place goes to the best-fit
explanation that covers it (`rank`: physical damage and failed segmentation,
then registration, then focus, then signal and staining, then the rest, then
"needs review"; within a rank the stronger action, then the agent's
confidence, then the larger region), and every finding of the same category
(blur / focus, registration, segmentation, tissue / acquisition, staining /
signal, or needs review) and the same action (exclude, warn, noted) becomes
ONE ROI, however many pieces it has. The result is the smallest set of
non-overlapping ROIs that describes what was found: one per action at most
in each category.

Nothing found is lost:

- each consolidated ROI lists every finding that touches it (`findings`),
  with its class, channels and the part of the ROI it covers, so its
  description names them all and a click opens every channel involved;
- the cells follow each FINDING, inside its own outline (clipped to the ROI
  as it stands), with its own class, channels and action
  (`roi_link.membership_meta`) -- a CD3 aggregate still flags CD3 where it
  is, whichever explanation the ROI is shown as;
- the findings stay in the result (`consolidated_into`), with their own
  outlines and provenance.

Every write is receipted like any other QC write, so the session's undo walks
back through it. Regions the user drew, edited, approved or locked are never
touched, and nothing runs when there is nothing to consolidate: findings that
neither overlap nor share a category and action stay exactly as written.
"""

from __future__ import annotations

import hashlib

VERSION = "2"

#: Overlaps smaller than this share of the smaller region (or slivers left
#: over after the partition) are edge noise, not a shared place.
MIN_OVERLAP_SHARE = 0.05
#: A piece of a partition under this many full-resolution pixels is dropped.
MIN_PIECE_PX = 400.0

ACTION_RANK = {"exclude": 0, "warn": 1, "ignore": 2}
#: The ROI state each action is written in.
ACTION_STATE = {"exclude": "confirmed_exclude", "warn": "confirmed_warn",
                "ignore": "confirmed_noted"}
#: [cal] A consolidated ROI holds every region of its category: room for
#: many traced outlines before any is simplified (the ROI plugin's own cap is
#: far above this).
LAYER_MAX_VERTICES = 20_000
CONFIDENCE_RANK = {"sure": 0, "fairly_sure": 1, "unsure": 2}
#: A primary finding's decision, kept on the ROI as the stored record keeps it.
_DECISION_KEYS = ("verdict", "artifact_class", "severity", "confidence", "boundary", "scope",
                  "exclude_recommended", "manual_review", "source")


def rank(klass) -> int:
    """The precedence of a class: lower explains a place first."""
    from plexora.plugins.qc.server import schemas

    if klass in schemas.WHOLE_CELL_CLASSES:
        return 0
    if klass == "cross_cycle_registration_error":
        return 1
    if klass == "out_of_focus":
        return 2
    if klass in schemas.SIGNAL_RAISING_CLASSES or klass in (
            "staining_artifact", "empty_or_failed_channel", "illumination_or_shading",
            "stitching_or_tile_seam"):
        return 3
    if klass == "uncertain_manual_review":
        return 5
    return 4


def _class_of(unit):
    return (unit.get("decision") or {}).get("artifact_class") or unit.get("class") \
        or unit.get("class_hint") or "other_technical"


def _category(unit):
    """The category a finding is grouped under: one of the five, or
    "review" for one the agent could not settle."""
    from plexora.plugins.qc.server import schemas

    if unit["state"] == "manual_review_recommended":
        return schemas.REVIEW["id"]
    return schemas.category_of_class(_class_of(unit))


def _key(unit):
    """One ROI per category and action: a user excludes cells by a handful of
    regions, not by tens. Each finding's own class, channels and outline stay
    in the ROI's `findings`, and the cells follow each finding
    (`roi_link.membership_meta`), so a CD3 aggregate still flags only CD3."""
    return (_category(unit), _action(unit))


def _eligible(engine, result):
    """This session's written findings the user has not made theirs."""
    from plexora.plugins.qc.server import roi_link, schemas

    stored = (result or {}).get("candidates") or {}
    out = []
    for unit in engine.units_of("candidate"):
        if not unit.get("roi_id") or unit.get("channel_level") or unit.get("findings"):
            continue
        if unit["state"] not in schemas.CONFIRMED_STATES + ("manual_review_recommended",):
            continue
        record = stored.get(unit["id"]) or {}
        if roi_link.user_wins(record) or (record.get("user_state") or {}).get("deleted"):
            continue
        if not unit.get("geometry"):
            continue
        out.append(unit)
    return out


def plan(findings) -> list:
    """[(key, piece shape, [finding units of that key])] -- the partition, in
    precedence order; empty when nothing would change."""
    from shapely.geometry import shape
    from shapely.ops import unary_union

    shapes = {u["id"]: shape(u["geometry"]).buffer(0) for u in findings}
    by_key = {}
    for unit in findings:
        by_key.setdefault(_key(unit), []).append(unit)

    def order(item):
        key, units = item
        precedence = min(rank(_class_of(u)) for u in units) \
            if key[0] != "review" else rank("uncertain_manual_review")
        confidence = min(CONFIDENCE_RANK.get((u.get("decision") or {}).get("confidence"), 1)
                         for u in units)
        area = sum(shapes[u["id"]].area for u in units)
        return (precedence, ACTION_RANK.get(key[1], 1), confidence, -area, key)

    ordered = sorted(by_key.items(), key=order)
    shared = any(len(units) > 1 for _k, units in ordered)
    if not shared:
        for i, a in enumerate(findings):
            for b in findings[i + 1:]:
                sa, sb = shapes[a["id"]], shapes[b["id"]]
                if not sa.intersects(sb):
                    continue
                smaller = min(sa.area, sb.area) or 1.0
                if sa.intersection(sb).area >= MIN_OVERLAP_SHARE * smaller:
                    shared = True
                    break
            if shared:
                break
    if not shared:
        return []
    claimed = None
    pieces = []
    for key, units in ordered:
        whole = unary_union([shapes[u["id"]] for u in units]).buffer(0)
        piece = whole if claimed is None else whole.difference(claimed)
        claimed = whole if claimed is None else claimed.union(whole)
        piece = _drop_slivers(piece, whole)
        if piece is not None:
            pieces.append((key, piece, units))
    return pieces


def _drop_slivers(piece, whole):
    """`piece` without the small fragments the partition cut off a larger
    region -- a small finding left whole (a speck of aggregate) stays."""
    from plexora.plugins.qc.server import polygons

    polygonal = polygons._polygonal(piece)
    if polygonal is None:
        return None
    parts = list(getattr(polygonal, "geoms", [polygonal]))
    originals = list(getattr(whole, "geoms", [whole]))

    def uncut(part):
        return any(abs(o.area - part.area) <= 0.01 * max(o.area, 1.0)
                   and o.intersection(part).area >= 0.99 * part.area for o in originals)

    kept = [p for p in parts if p.area >= MIN_PIECE_PX or uncut(p)]
    if not kept:
        return None
    from shapely.ops import unary_union

    return unary_union(kept)


def _action(unit):
    """The finding's action, in the schema's words (exclude / warn /
    ignore). A region left for manual review keeps the action it was written
    with -- warn, or ignore when it covers too much tissue to warn every cell
    of (`strictness.manual_review_action`)."""
    by_state = {"confirmed_exclude": "exclude", "confirmed_warn": "warn",
                "confirmed_noted": "ignore"}
    if unit["state"] in by_state:
        return by_state[unit["state"]]
    action = unit.get("action")
    return action if action in ACTION_RANK else "warn"


def run(engine, action="close") -> dict | None:
    """Consolidate the session's written findings; returns what was done, or
    None when there was nothing to do (or the session only proposed, and is
    not committing what it proposed)."""
    from plexora.plugins.qc.server import polygons, results

    if engine.options.get("mode") != "apply" and action != "commit":
        return None
    project = engine.project
    result = results.get_result(project, results.load(project), engine.record["result_id"])
    findings = _eligible(engine, result)
    if len(findings) < 2:
        return None
    pieces = plan(findings)
    if not pieces:
        return None
    from shapely.geometry import shape

    shapes = {u["id"]: shape(u["geometry"]).buffer(0) for u in findings}
    written = []
    from plexora.plugins.qc.server import schemas

    for key, piece, primaries in pieces:
        category, action = key
        # The layer's class is its category's: the finest class only when
        # every finding in it shares one (a layer of folds stays "fold").
        classes = {_class_of(u) for u in primaries}
        if category == schemas.REVIEW["id"]:
            klass = schemas.REVIEW["class"]
        elif len(classes) == 1:
            klass = classes.pop()
        else:
            klass = schemas.default_class(category)
        listed = []
        for unit in findings:
            part = shapes[unit["id"]].intersection(piece)
            # A small finding counts where most of it lies, however small.
            floor = min(MIN_PIECE_PX, 0.5 * shapes[unit["id"]].area)
            if part.is_empty or part.area < floor:
                continue
            geometry = polygons.to_geojson(polygons._polygonal(part), simplify_px=0,
                                           max_vertices=polygons.MAX_VERTICES,
                                           min_area_px=1.0)
            if geometry is None:
                continue
            listed.append({"candidate_id": unit["id"], "class": _class_of(unit),
                           "channels": list(unit.get("channels") or []),
                           "scope": (unit.get("decision") or {}).get("scope")
                           or unit.get("scope_hint"),
                           "action": _action(unit),
                           "primary": _key(unit) == key,
                           "share": round(part.area / max(piece.area, 1e-9), 4),
                           "geometry": geometry,
                           # What the ROI's action is re-derived from under
                           # another preset (`strictness.action_for`).
                           **({"state": unit["state"],
                               "ai_decision": {k: (unit.get("decision") or {}).get(k)
                                               for k in _DECISION_KEYS},
                               "measurement": dict(unit.get("measurement") or {})}
                              if _key(unit) == key else {})})
        if not listed:
            continue
        if len(listed) == 1 and listed[0]["primary"] and len(primaries) == 1 \
                and abs(piece.area - shapes[primaries[0]["id"]].area) \
                <= 0.01 * max(piece.area, 1.0):
            # One finding, alone in its place and uncut: its ROI stays as written.
            continue
        top = primaries[0]
        every_channel = list(dict.fromkeys(c for f in listed for c in f["channels"]))
        geometry = polygons.to_geojson(piece, simplify_px=0,
                                       max_vertices=LAYER_MAX_VERTICES, min_area_px=1.0)
        if geometry is None:
            continue
        ident = hashlib.sha1(repr((engine.id, category, action,
                                   sorted(f["candidate_id"] for f in listed))).encode()
                             ).hexdigest()[:10]
        unit = {"type": "candidate", "project": project, "id": f"cons_{ident}",
                "channel": top.get("channel"), "audit_channel": top.get("audit_channel"),
                "audit_channels": list(top.get("audit_channels") or []),
                "channels": every_channel, "cycles": list(top.get("cycles") or []),
                "detector": "consolidated", "detector_version": VERSION,
                "class_hint": klass, "alternatives": [], "scope_hint":
                    (top.get("decision") or {}).get("scope") or top.get("scope_hint"),
                "score": top.get("score"), "severity": top.get("severity"),
                "metrics": {"consolidated": True, "findings": len(listed)},
                "decision": {**(top.get("decision") or {}), "artifact_class": klass,
                             "source": "consolidation"},
                "notes": [n for u in primaries for n in (u.get("notes") or [])],
                "geometry": geometry, "envelope_geometry": geometry,
                "origin": "consolidated", "findings": listed,
                "consolidated_from": [f["candidate_id"] for f in listed],
                "state": ACTION_STATE[action], "action": action, "category": category}
        engine.record["units"][engine.unit_key_of(unit)] = unit
        engine.write_candidate(unit, klass=klass, action=action)
        if unit.get("roi_id"):
            written.append(unit)
    if not written:
        return None
    # The findings' own ROIs give way to the consolidated ones: receipted
    # deletes, the findings kept in the result with where they went.
    into = {}
    for unit in written:
        for cid in unit["consolidated_from"]:
            into.setdefault(cid, []).append(unit["id"])
    removed = 0
    for finding in findings:
        if finding["id"] not in into:
            continue
        if engine.remove_roi(finding, reason="consolidated"):
            removed += 1
        finding["consolidated_into"] = into[finding["id"]]
        engine.restore_record(finding, consolidated_into=into[finding["id"]])
    summary = {"version": VERSION, "findings": len(findings), "rois": len(written),
               "removed": removed,
               "pieces": [{"id": u["id"], "category": u["category"], "action": u["action"],
                           "class": u["class_hint"], "channels": u["channels"],
                           "findings": len(u["findings"]),
                           "roi_id": u["roi_id"]} for u in written]}
    engine.record["consolidation"] = summary
    return summary
