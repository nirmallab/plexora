"""One QC decision packet per kind: the question, the numbers, at most two images.

Every packet is self-contained, carries numbers only in JSON (never a value
the agent would have to read off a picture), and carries no strictness: what
the agent judges is the same whatever preset the session runs under, which is
what lets a preset change reuse every answer. Each builder returns
`(packet, images)` with `images = [(bytes, format, (width, height))]` and the
packet's `_image_meta` aligned with them, or None when it closed the unit(s).
"""

from __future__ import annotations

import hashlib
import json

import numpy as np

from plexora.agent.errors import AgentError
from plexora.plugins.qc.server import candidates as cand
from plexora.plugins.qc.server import polygons, schemas, sheets

MAX_IMAGES = 2
SIGNIFICANT = 4

READING_GUIDE = {
    "audit": ("the audit sheet: tile 1 is the nuclear stain with the tissue outlined (cyan); "
              "every other tile is one channel, whole tissue, white at its calibrated "
              "window, captioned `number | channel | cycle | flags`; dashed magenta outlines "
              "labelled c1, c2... are regions the scan found. A channel is `clean` when "
              "nothing technical is wrong at this scale -- a stain that is simply sparse, "
              "dim or patterned by the tissue is clean. `suspicious` names the outlines "
              "that worry you (or `elsewhere`); `uncertain` asks for a closer look"),
    "confirm": ("the confirm sheet: the channel with the candidate outlined in magenta, "
                "its neighbourhood (nuclear stain in blue, the channel in yellow), a close "
                "crop at the strongest point, and the detector's own map (bright = "
                "suspicious) or, at deeper looks, the nuclear stain in the same crop and a "
                "clean field of the same size for comparison. An artifact is technical: "
                "folds, blur, bubbles, debris, aggregates, saturation, seams, lost or "
                "shifted tissue. Real biology (a lymphoid follicle, a vessel, necrosis) is "
                "`not_artifact`"),
    "scope": "the scope sheet: the same neighbourhood in up to nine channels, outline "
             "on each; choose the option whose channels show the artifact",
    "localize": "outlines A (tight) to E (bounding box) of one region, then all together; "
                "choose the one that covers the artifact and little else",
    "grid": "a labelled grid over the region: name every square the artifact covers",
    "review": ("the review sheet: every QC region on the whole tissue, filled when its "
               "cells are excluded, dashed when they are only warned, one colour per class. "
               "Consistent means: nothing obviously missed, nothing obviously real "
               "excluded, and no region much larger than the artifact it names"),
    "cells": ("a cell collage: one row per group -- far beyond the proposed cutoff, just "
              "beyond it, just inside it (kept); per cell the channel alone and a merge with "
              "its outline, captioned with its value. A cutoff is right when the cells beyond "
              "it are debris, blur, broken or merged segments or lost cells, and those inside "
              "look like the rest of the tissue's cells"),
    "classes": {k: v for k, v in schemas.CLASS_WORDS.items()},
    "severity": "minor: cells there are still readable; moderate: some markers unreliable; "
                "severe: nothing there can be trusted",
}


def reading_guide() -> dict:
    from plexora.plugins.qc.server import answers

    guide = dict(READING_GUIDE)
    guide["answer_schemas"] = {kind: answers.schema_for(kind) for kind in answers.KINDS}
    guide["answer_with"] = "qc_answer {session_id, packet_id, answer: {kind, ...}}"
    return guide


def guide_version() -> str:
    blob = json.dumps(reading_guide(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def _round(value):
    if isinstance(value, bool) or not isinstance(value, float):
        return value
    if value == 0 or value != value:
        return value
    return float(f"{value:.{SIGNIFICANT}g}")


def _lean_value(value):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            item = _lean_value(item)
            if item is None or item == [] or item == {}:
                continue
            out[key] = item
        return out
    if isinstance(value, list):
        return [_lean_value(v) for v in value]
    if isinstance(value, (np.floating,)):
        return _round(float(value))
    if isinstance(value, (np.integer,)):
        return int(value)
    return _round(value)


def lean(packet, options) -> dict:
    packet["evidence"] = _lean_value(packet.get("evidence") or {})
    if options.get("reading") == "once":
        packet["answer_schema"] = {"see": f"reading_guide.answer_schemas.{packet['kind']}"}
        packet.pop("answer_with", None)
    packet["images"] = [{k: v for k, v in image.items() if k != "estimated_vision_tokens"}
                        for image in packet.get("images") or []]
    return packet


def _reading(engine, keys, specific=()):
    guide = READING_GUIDE
    specific = [s for s in specific if s]
    if engine.options.get("reading") == "every_packet":
        texts = [guide[k] for k in keys if isinstance(guide.get(k), str)]
        return {"how_to_read": "; ".join(texts + specific)}
    out = {"guide": list(keys)}
    if specific:
        out["how_to_read"] = "; ".join(specific)
    return out


def _fmt(engine):
    return engine.options["image_format"]


def _image(rendered, role, caption):
    w, h = rendered["size"]
    return ((rendered["image"], rendered["format"], (int(w), int(h))),
            {"role": role, "caption": caption,
             "artifact_id": (rendered.get("artifact") or {}).get("id")})


def _images(packet, pairs):
    images = []
    for rendered, role, caption in pairs:
        image, meta = _image(rendered, role, caption)
        images.append(image)
        packet["_image_meta"].append(meta)
    return images


def _bbox_um(box, pixel):
    if not pixel:
        return None
    v = float(pixel["value"])
    return [round(b * v, 1) for b in box]


def _record_artifact(units, rendered):
    art = (rendered.get("artifact") or {}).get("id")
    if art:
        for unit in units:
            unit.setdefault("artifacts", []).append(art)


def _variants(engine, unit):
    if not unit.get("variants"):
        scan = engine.scan(unit["project"])
        unit["variants"] = polygons.geometry_variants(engine.mask_of(unit), scan.grid,
                                                      pixel_um=scan.meta.get("pixel_um"))
    return unit["variants"]


def _brief(unit, pixel):
    return {"id": unit["id"], "label": unit.get("label"), "class_hint": unit.get("class_hint"),
            "alternatives": unit.get("alternatives"), "scope_hint": unit.get("scope_hint"),
            "channels": unit.get("channels"), "cycles": unit.get("cycles"),
            "detector": unit.get("detector"), "score": unit.get("score"),
            "tissue_fraction": (unit.get("measurement") or {}).get("tissue_fraction"),
            "bbox_px": unit.get("bbox"), "bbox_um": _bbox_um(unit.get("bbox") or [], pixel),
            "metrics": unit.get("metrics")}


# -- the channel audit ------------------------------------------------------------------


def channel_audit(engine, units):
    project = units[0]["project"]
    scan = engine.scan(project)
    pixel = engine.pixel_for(project)
    calibration = engine.calibration(project)
    per_sheet = int(schemas.ENGINE["audit_batch"])
    pending_cands = [u for u in engine.units_of("candidate", project)
                     if u["state"] == "awaiting_audit"]
    rows = []
    label = 0
    for unit in units:
        mine = [c for c in pending_cands if c.get("audit_channel") == unit["id"]]
        mine.sort(key=lambda c: (-float(c.get("score") or 0), c["id"]))
        listed = []
        for candidate in mine:
            label += 1
            candidate["label"] = f"c{label}"
            variants = _variants(engine, candidate)
            listed.append({"id": candidate["id"], "label": candidate["label"],
                           "class_hint": candidate.get("class_hint"),
                           "score": candidate.get("score"),
                           "tissue_fraction": (candidate.get("measurement") or {}).get(
                               "tissue_fraction"),
                           "geometry": (variants.get("standard") or {}).get("geometry")})
        meta = scan.channel(unit["id"])
        summary = meta.get("summary") or {}
        rows.append({"number": unit["order"] + 1, "channel": unit["id"],
                     "cycle": meta.get("cycle"), "flags": meta.get("flags"),
                     "candidates": listed,
                     "overview": {k: summary.get(k) for k in (
                         "saturation_fraction", "tissue_ratio", "dynamic_range_decades",
                         "zero_fraction", "focus_rel_p10", "bright_compact_fraction")},
                     "illumination_r2": (summary.get("illumination") or {}).get("r2")})
    pages = [rows[i:i + per_sheet] for i in range(0, len(rows), per_sheet)]
    audited = engine.record.setdefault("audit_pages", 0)
    total_pages = audited + len(pages) + int(np.ceil(
        len([u for u in engine.units_of("channel", project)
             if u["state"] == "pending"]) / per_sheet))
    packet = {"question": ("For every channel row: is anything technically wrong -- a fold, "
                           "blur, bubble, debris, aggregates, saturation, uneven "
                           "illumination, seams, lost tissue, a failed stain? Answer "
                           "`clean`, `suspicious` (name the outlined candidates that worry "
                           "you, or `elsewhere`) or `uncertain`, per channel."),
              "evidence": {"project": project,
                           "rows": [{k: v for k, v in r.items() if k != "candidates"}
                                    | {"candidates": [{k: c[k] for k in (
                                        "id", "label", "class_hint", "score",
                                        "tissue_fraction")} for c in r["candidates"]]}
                                    for r in rows],
                           "tissue": scan.meta.get("tissue"),
                           "cell_um": scan.grid.get("cell_um"),
                           **_reading(engine, ["audit"])},
              "allowed": ["clean", "suspicious", "uncertain"], "_image_meta": []}
    pairs = []
    for index, page in enumerate(pages, start=1):
        rendered = sheets.audit_sheet(engine.call.session, project, scan, page,
                                      index=audited + index, total=max(1, total_pages),
                                      fmt=_fmt(engine), pixel=pixel, calibration=calibration)
        _record_artifact([u for u in units if u["id"] in {r["channel"] for r in page}],
                         rendered)
        pairs.append((rendered, "audit_sheet", f"channels {page[0]['number']}-"
                                               f"{page[-1]['number']}"))
    engine.record["audit_pages"] = audited + len(pages)
    for unit in units:
        unit["state"] = "awaiting_audit"
    return packet, _images(packet, pairs)


# -- candidates -------------------------------------------------------------------------


def artifact_confirm(engine, units):
    unit = units[0]
    project = unit["project"]
    scan = engine.scan(project)
    pixel = engine.pixel_for(project)
    level = int(unit.get("level") or 0)
    _variants(engine, unit)
    rendered = sheets.confirm_sheet(engine.call.session, project, scan, unit,
                                    engine.mask_of(unit), level=level, fmt=_fmt(engine),
                                    pixel=pixel, calibration=engine.calibration(project))
    _record_artifact([unit], rendered)
    words = schemas.CLASS_WORDS.get(unit.get("class_hint"), unit.get("class_hint"))
    neighbours = []
    mask = engine.mask_of(unit)
    for other in engine.units_of("candidate", project):
        if other is unit or other["state"] in ("dismissed", "merged"):
            continue
        theirs = engine.mask_of(other)
        inter = np.logical_and(mask, theirs).sum()
        if inter:
            neighbours.append({"candidate": other["id"], "channels": other.get("channels"),
                               "iou": float(inter / np.logical_or(mask, theirs).sum()),
                               "state": other["state"]})
    packet = {"question": (f"The scan flagged a possible {words} in "
                           f"{', '.join(unit.get('channels') or [])[:80]} (outlined). Is it a "
                           "technical artifact? Give its class and severity, whether the "
                           "outline covers it, and whether its cells should be excluded. "
                           "`need_more_evidence` shows it closer."),
              "evidence": {"candidate": _brief(unit, pixel), "look": level + 1,
                           "looks": int(schemas.ENGINE["confirm_levels"]),
                           "neighbours": neighbours[:6],
                           "scope_options": list(schemas.SCOPES),
                           **_reading(engine, ["confirm", "severity"])},
              "allowed": list(schemas.ARTIFACT_CLASSES), "_image_meta": []}
    return packet, _images(packet, [(rendered, "confirm_sheet",
                                     f"{words} candidate, look {level + 1}")])


def scope_options(engine, unit):
    scan = engine.scan(unit["project"])
    lead = unit.get("channel")
    options = [{"id": "s1", "scope": "channel", "channels": [lead]}]
    if len(unit.get("channels") or []) > 1:
        options.append({"id": "s2", "scope": "channels", "channels": list(unit["channels"])})
    cycles = (scan.meta.get("cycles") or {}).get("cycles") or []
    cycle = scan.channel(lead).get("cycle") if lead else None
    if cycle and len(cycles) > 1:
        members = next((c["channels"] for c in cycles if c["index"] == cycle), [])
        options.append({"id": "s3", "scope": "cycle", "channels": list(members),
                        "cycle": cycle})
        later = [c for cy in cycles if cy["index"] >= cycle for c in cy["channels"]]
        if len(later) > len(members):
            options.append({"id": "s4", "scope": "cycles", "channels": later,
                            "cycles": [cy["index"] for cy in cycles if cy["index"] >= cycle]})
    options.append({"id": "s5", "scope": "all_channels",
                    "channels": [c["name"] for c in scan.channels]})
    return options


def artifact_scope(engine, units):
    unit = units[0]
    project = unit["project"]
    scan = engine.scan(project)
    pixel = engine.pixel_for(project)
    rendered = sheets.scope_sheet(engine.call.session, project, scan, unit, fmt=_fmt(engine),
                                  pixel=pixel, calibration=engine.calibration(project))
    _record_artifact([unit], rendered)
    options = scope_options(engine, unit)
    unit["scope_options"] = options
    words = schemas.CLASS_WORDS.get((unit.get("decision") or {}).get("artifact_class")
                                    or unit.get("class_hint"), "artifact")
    packet = {"question": f"Which channels does this {words} affect? Choose one option.",
              "evidence": {"candidate": _brief(unit, pixel), "options": options,
                           **_reading(engine, ["scope"])},
              "allowed": [o["id"] for o in options] + ["cannot_tell"], "_image_meta": []}
    return packet, _images(packet, [(rendered, "scope_sheet", "the region across channels")])


def artifact_localize(engine, units):
    unit = units[0]
    project = unit["project"]
    scan = engine.scan(project)
    pixel = engine.pixel_for(project)
    variants = _variants(engine, unit)
    rendered = sheets.localize_sheet(engine.call.session, project, scan, unit, variants,
                                     fmt=_fmt(engine), pixel=pixel,
                                     calibration=engine.calibration(project))
    _record_artifact([unit], rendered)
    alternatives = [{"id": sheets.VARIANT_LETTERS[name], "variant": name,
                     "area_um2": v.get("area_um2"), "area_px2": v.get("area_px2"),
                     "vertices": v.get("vertices")}
                    for name, v in variants.items() if name in sheets.VARIANT_LETTERS]
    packet = {"question": "Which outline covers the artifact best? A letter, `current` (the "
                          "standard outline, B), or `none_fits` for a grid.",
              "evidence": {"candidate": _brief(unit, pixel), "alternatives": alternatives,
                           "boundary_said": (unit.get("decision") or {}).get("boundary"),
                           **_reading(engine, ["localize"])},
              "allowed": [a["id"] for a in alternatives] + ["current", "none_fits"],
              "_image_meta": []}
    return packet, _images(packet, [(rendered, "localize_sheet", "candidate outlines")])


def artifact_grid(engine, units):
    unit = units[0]
    project = unit["project"]
    scan = engine.scan(project)
    pixel = engine.pixel_for(project)
    if unit.get("grid_spec") is None:
        region = cand.decode_mask(unit["grid_region"]) if unit.get("grid_region") \
            else engine.mask_of(unit)
        unit["grid_spec"] = polygons.localisation_grid(region, scan.grid,
                                                       side=int(schemas.ENGINE["grid_side"]))
    spec = unit["grid_spec"]
    rendered = sheets.grid_sheet(engine.call.session, project, scan, unit, spec,
                                 fmt=_fmt(engine), pixel=pixel,
                                 calibration=engine.calibration(project))
    _record_artifact([unit], rendered)
    packet = {"question": "List every grid square the artifact covers (e.g. B3, B4). "
                          "`refine: true` asks for a finer grid inside them.",
              "evidence": {"candidate": _brief(unit, pixel),
                           "grid": {"rows": spec["rows"], "columns": spec["columns"],
                                    "square_um": [round(v * float(pixel["value"]), 1)
                                                  for v in spec["square_px"]]
                                    if pixel else None,
                                    "square_px": spec["square_px"]},
                           "pre_selected": [s["id"] for s in spec["squares"]
                                            if s["covered"] >= 0.5],
                           "round": int(unit.get("grid_rounds") or 0) + 1,
                           **_reading(engine, ["grid"])},
              "allowed": [s["id"] for s in spec["squares"]], "_image_meta": []}
    return packet, _images(packet, [(rendered, "grid_sheet", "the region on a grid")])


# -- the final review -------------------------------------------------------------------


def final_qc_review(engine, units):
    project = engine.project
    scan = engine.scan(project)
    pixel = engine.pixel_for(project)
    regions = []
    for unit in engine.units_of("candidate", project):
        if unit["state"] not in schemas.WRITTEN_STATES or not unit.get("geometry"):
            continue
        regions.append({"label": f"r{len(regions) + 1}", "candidate": unit["id"],
                        "class": unit.get("class") or unit.get("class_hint"),
                        "action": unit.get("action"), "geometry": unit["geometry"],
                        "channels": unit.get("channels"),
                        "tissue_fraction": (unit.get("measurement") or {}).get(
                            "tissue_fraction")})
    if not regions and not engine.units_of("cells", project):
        units[0]["state"] = "reviewed"
        units[0]["reason"] = "nothing was confirmed: no review needed"
        return None
    rendered = sheets.review_sheet(engine.call.session, project, scan, regions,
                                   fmt=_fmt(engine), pixel=pixel,
                                   calibration=engine.calibration(project))
    _record_artifact(units, rendered)
    units[0]["regions"] = [{k: r[k] for k in ("label", "candidate")} for r in regions]
    excluded = [r for r in regions if r["action"] == "exclude"]
    channels = {}
    for unit in engine.units_of("channel", project):
        channels[unit["state"]] = channels.get(unit["state"], 0) + 1
    packet = {"question": ("Here is every QC region. Is the picture consistent: nothing "
                           "obvious missed, nothing real excluded, no region far larger than "
                           "its artifact? Name concerns by region label."),
              "evidence": {"regions": [{k: v for k, v in r.items() if k != "geometry"}
                                       for r in regions],
                           "excluded_tissue_fraction": float(sum(
                               r.get("tissue_fraction") or 0 for r in excluded)),
                           "channels": channels,
                           "cells": engine.record.get("cells_summary"),
                           **_reading(engine, ["review"])},
              "allowed": ["consistent", "inconsistent", "cannot_tell"], "_image_meta": []}
    return packet, _images(packet, [(rendered, "review_sheet", "every QC region")])


def _cells(engine, units):
    from plexora.plugins.qc.server.cells import packets as cell_packets

    return cell_packets.build(engine, units)


BUILDERS = {"channel_audit": channel_audit, "artifact_confirm": artifact_confirm,
            "artifact_scope": artifact_scope, "artifact_localize": artifact_localize,
            "artifact_grid": artifact_grid, "final_qc_review": final_qc_review,
            "cell_intensity": _cells, "cell_area": _cells, "cycle_stability": _cells,
            "channel_outlier": _cells}


def narrate(packet) -> str:
    kind = packet.get("kind")
    evidence = packet.get("evidence") or {}
    candidate = evidence.get("candidate") or {}
    template = schemas.NARRATION.get(kind) or ""
    channel = (candidate.get("channels") or [""])[0] if candidate else \
        (evidence.get("marker") or "")
    words = schemas.CLASS_WORDS.get(candidate.get("class_hint"), "artifact") \
        if candidate else ""
    try:
        return template.format(n=len(evidence.get("rows") or []), channel=channel,
                               class_words=words)
    except (KeyError, IndexError):
        return template


def evidence_label(packet) -> str:
    images = packet.get("images") or []
    role = images[0].get("role") if images else None
    return schemas.EVIDENCE_LABELS.get(role) or ""


def undrawable(exc):
    return AgentError("internal_error", f"evidence could not be drawn: {exc}")
