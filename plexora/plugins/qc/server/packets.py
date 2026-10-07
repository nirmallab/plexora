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
              "that worry you (or `elsewhere`); `uncertain` asks for a closer look at the "
              "outlines it names (naming none: every one on its tile). An "
              "outline seen in several channels is one candidate, drawn with the same label "
              "on each of their tiles. `clean` settles the outlines a whole-tissue tile shows "
              "well (shading, background, a failed stain); a strong small one (a "
              "little blur or fold) is still looked at closer, so name it in "
              "`where` when it worries you. A staining problem (aggregates, high "
              "background, autofluorescence, bleed-through) is settled on the row "
              "itself with `class_hint`, never outlined: `scan_hints` says what the scan "
              "saw in that channel. A failed stain listed on a row (`channel_level`, no "
              "outline) is decided by naming it: its marker is then unreliable in every "
              "cell. Each candidate is described once, in `candidates` keyed by label; a "
              "row lists only the labels on its tile"),
    "confirm": ("the confirm sheet: the channel with the candidate outlined in magenta, "
                "its neighbourhood (nuclear stain in blue, the channel in yellow), a close "
                "crop at the strongest point, and the detector's own map (bright = "
                "suspicious) or, at deeper looks, the nuclear stain in the same crop and a "
                "clean field of the same size for comparison. An artifact is technical: "
                "folds, blur, bubbles, debris, aggregates, saturation, lost or "
                "shifted tissue. Real biology (a lymphoid follicle, a vessel, necrosis) is "
                "`not_artifact`. A sheet of several candidates has one row per candidate, "
                "labelled (c3 | ...), the same panels smaller and no second look: answer "
                "`verdicts` keyed by label; `need_more_evidence` on one gives that one its "
                "own closer sheet. The magenta outline is a search envelope: Plexora traces "
                "the artifact's own pixels inside it and writes only those, keeping the "
                "normal tissue around them. So `boundary: covers` means the whole artifact "
                "lies inside the outline (it may be much larger); `too_small` that part "
                "lies outside; `too_large` that it takes in other tissue the trace could "
                "mistake for the artifact (another bright or dark structure)"),
    "scope": "the scope sheet: the same neighbourhood in up to nine channels, outline "
             "on each; choose the option whose channels show the artifact",
    "localize": ("outlines A (tight) to E (bounding box) of one region, then all together. "
                 "Each outline is a search envelope (thin, dashed) with the artifact "
                 "Plexora traced inside it drawn solid and filled: that trace is what is "
                 "written. Choose the smallest envelope whose trace holds the whole "
                 "artifact and nothing else. When no trace does -- each misses part of the "
                 "artifact, spills onto clean tissue, or joins two objects -- answer "
                 "`redraw` (when allowed): the segmentation model outlines it again, once, "
                 "and the same outlines come back with its trace"),
    "grid": ("a labelled grid over the region: name every square the artifact touches; "
             "its pixels are traced inside them"),
    "review": ("the review sheet: every QC region on the whole tissue as its traced "
               "outline, filled when its "
               "cells are excluded, dashed when they are only warned, one colour per class. "
               "Consistent means: nothing obviously missed, nothing obviously real "
               "excluded, and no region much larger than the artifact it names. The "
               "regions come grouped by category and action, largest first, each group "
               "naming its largest few in `top` and counting the rest in `more`; every "
               "region on the sheet is labelled, so a concern may name any label"),
    "score": ("a score-review sheet: one image check (blur, registration mismatch, "
              "segmentation problems) scored the tissue; each row holds places from one "
              "part of that score's distribution -- FINE, JUST BELOW and JUST ABOVE the "
              "bar, FAR ABOVE it, IN REGIONS (the heart of the largest flagged areas) -- "
              "the scored square dashed cyan, the score in the caption; the last row is "
              "the whole tissue with the regions at the bar outlined, and the score map "
              "with each place marked. Judge each row by what its tiles show, not by the "
              "number: a stain that is sparse or dim is not blur; nuclei moved a cell or "
              "two in a few places are a local mismatch, a whole field shifted is a cycle "
              "shift; dense tumour is not under-segmentation, small lymphocytes are not "
              "fragments, big macrophages are not merges. Registration tiles show the "
              "reference red and the comparison green: yellow where they agree. The bar "
              "is right (`accept`) when the rows beyond it are artifacts and the "
              "borderline rows are what a bar should split; `too_lenient` when just "
              "below already shows the problem, `too_aggressive` when just above looks "
              "normal: each moves it one step and shows the places again. Answer "
              "`whole_tissue` only when `global.possible` is true"),
    "visual": ("the visual overview: the whole tissue in four views -- the brightest DNA "
               "across cycles (folds are doubled tissue, debris is bright), the first "
               "cycle's DNA cyan under the last's red (cyan only: tissue lost or lifted "
               "later; red only: debris gained or a shifted cycle; red along one side and "
               "cyan along the other of the same piece: it tore loose and moved between "
               "cycles -- the whole piece is the artifact), the mean of the channels "
               "(autofluorescent folds, debris bright everywhere), and where the channels "
               "are bright (red) or dark (blue) together. The tissue is outlined cyan; "
               "regions already written are solid and labelled r-numbers, the detector's "
               "pending objects dashed and labelled d-numbers; a grid names places. Only "
               "large, obvious artifacts are yours to outline -- a long fold, a tear, a "
               "lifted corner, a smear of debris, a bubble -- and only what no solid "
               "outline already covers. Thin solid outlines along an edge do not cover the "
               "piece inside them: zoom wherever outlines or dashed objects cluster, and at "
               "every tip and corner of the tissue, before saying nothing is there. Biology is not an artifact: a necrotic area, a "
               "lymphoid aggregate, fat, mucin, cartilage, a vessel, a biopsy's edge. For "
               "each place: inspect_artifact_channels on a box, then segment_qc_roi with "
               "preview true, refine, and write with the session's id. Answer `done` when "
               "the pass is over (or `nothing_found`), naming in `left` what you saw but "
               "did not outline"),
    # Only the classes an answer may name: the generic hand-drawn ones (and
    # anything else outside AGENT_CLASSES) would be refused if used.
    "classes": {k: schemas.CLASS_WORDS.get(k, k) for k in schemas.AGENT_CLASSES},
    "severity": "minor: cells there are still readable; moderate: some markers unreliable; "
                "severe: nothing there can be trusted",
}


def reading_guide() -> dict:
    from plexora.plugins.qc.server import answers

    guide = dict(READING_GUIDE)
    guide["answer_schemas"] = _deduplicated({kind: answers.schema_for(kind)
                                             for kind in answers.KINDS})
    # The classes an answer may name (the generic hand-drawn ones are not).
    guide["agent_classes"] = list(schemas.AGENT_CLASSES)
    guide["answer_with"] = "qc_answer {session_id, packet_id, answer: {kind, ...}}"
    return guide


def _deduplicated(by_kind) -> dict:
    """The answer schemas once each: a kind whose fields are another's says
    `same_as`, a nested copy of a kind's fields says `fields_of`, and the class
    list is named once (`agent_classes`) -- the start was ~9k tokens of repeats."""
    classes = sorted(schemas.AGENT_CLASSES)

    def strip(value):
        if isinstance(value, dict):
            if isinstance(value.get("enum"), list) and sorted(value["enum"]) == classes:
                value = {k: v for k, v in value.items() if k != "enum"}
                value["one_of"] = "reading_guide.agent_classes"
            return {k: strip(v) for k, v in value.items()}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    def fields(schema):
        return {k: v for k, v in schema["properties"].items() if k != "kind"}

    out, seen = {}, {}
    for kind, schema in by_kind.items():
        schema = strip(schema)
        key = json.dumps([fields(schema), schema["required"]], sort_keys=True)
        if key in seen:
            out[kind] = {"kind": kind, "same_as": seen[key]}
            continue
        seen[key] = kind
        out[kind] = schema
    known = {json.dumps(fields(schema), sort_keys=True): kind
             for kind, schema in out.items() if "properties" in schema}

    def refer(value):
        if isinstance(value, dict):
            props = value.get("properties")
            if isinstance(props, dict):
                name = known.get(json.dumps({k: v for k, v in props.items() if k != "kind"},
                                            sort_keys=True))
                if name and value is not out.get(name):
                    return {k: v for k, v in value.items() if k != "properties"} | {
                        "fields_of": name}
            return {k: refer(v) for k, v in value.items()}
        return value

    for kind in list(out):
        if "properties" in out[kind]:
            out[kind] = {**out[kind], "properties": {k: refer(v) for k, v in
                                                     out[kind]["properties"].items()}}
    return out


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
        if list(packet.get("allowed") or ()) == list(schemas.AGENT_CLASSES):
            # The classes are the answer schema's enum, read once with the
            # guide: thirty-odd names were repeated on every confirm.
            packet["allowed"] = {"see": f"reading_guide.answer_schemas.{packet['kind']}"}
    packet["images"] = [{k: v for k, v in image.items() if k != "estimated_vision_tokens"}
                        for image in packet.get("images") or []]
    return packet


def _size(packet) -> int:
    return len(json.dumps(packet, default=str))


def _drop_nested(evidence, key):
    """`key` removed from the evidence and from every candidate, row and
    region in it."""
    evidence.pop(key, None)
    for group in ("candidates", "rows", "regions"):
        for item in evidence.get(group) or ():
            if isinstance(item, dict):
                item.pop(key, None)
    if isinstance(evidence.get("candidate"), dict):
        evidence["candidate"].pop(key, None)


#: What a QC packet loses first when it is over `budget.PACKET_CHAR_LIMIT`:
#: context the agent can do without, before anything it answers by.
TRIM_ORDER = ("neighbours", "metrics", "previous", "tissue", "bbox_px", "overview",
              "illumination_r2", "alternatives")


def trim(packet) -> dict:
    """A QC packet brought inside one tool result: optional evidence dropped,
    least useful first, then long lists cut to what still fits (marked
    `truncated`). The gating keys `budget.trim` knows are never in one."""
    from plexora.agent.sessions import budget

    limit = budget.PACKET_CHAR_LIMIT
    evidence = packet.get("evidence")
    if not isinstance(evidence, dict) or _size(packet) <= limit:
        return packet
    # The final review's grouped table gives up its named regions first, one
    # per group in turn: every group keeps its count and tissue share.
    if isinstance(evidence.get("regions"), dict) and _shrink_top(packet, evidence, limit):
        return packet
    for key in TRIM_ORDER:
        _drop_nested(evidence, key)
        if _size(packet) <= limit:
            return packet
    # The final review lists every region, the smallest last; a sheet's rows
    # and candidates are answered by name, so those are never cut.
    regions = evidence.get("regions")
    while isinstance(regions, list) and regions and _size(packet) > limit:
        regions.pop()
        evidence["truncated"] = {"regions": True}
    return packet


def _shrink_top(packet, evidence, limit) -> bool:
    """The review table's `top` lists cut round-robin -- the last-ranked
    group first, the smallest named region of each -- into its `more` count,
    until the packet fits. True when it fits."""
    groups = [g for g in (evidence["regions"].get("groups") or ()) if isinstance(g, dict)]
    while _size(packet) > limit:
        cut = False
        for group in reversed(groups):
            top = group.get("top") or []
            if not top:
                continue
            gone = top.pop()
            group["more"] = int(group.get("more") or 0) + 1
            group["more_tissue_fraction"] = _round(float(
                group.get("more_tissue_fraction") or 0) + float(
                gone.get("tissue_fraction") or 0))
            evidence["truncated"] = {"top": True}
            cut = True
            if _size(packet) <= limit:
                return True
        if not cut:
            return False
    return True


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
    if unit.get("channel_level"):
        return {}  # a verdict on the channel: never outlined
    if not unit.get("variants"):
        scan = engine.scan(unit["project"])
        unit["variants"] = polygons.geometry_variants(engine.mask_of(unit), scan.grid,
                                                      pixel_um=scan.meta.get("pixel_um"))
    return unit["variants"]


def _channel_token(channels, scan):
    """`schemas.channel_token` of a scan: its channel names and cycles."""
    if not channels or scan is None:
        return channels
    return schemas.channel_token(channels, [c["name"] for c in scan.channels],
                                 (scan.meta.get("cycles") or {}).get("cycles") or [])


def _brief(unit, pixel, scan=None):
    return {"id": unit["id"], "label": unit.get("label"), "class_hint": unit.get("class_hint"),
            "alternatives": unit.get("alternatives"), "scope_hint": unit.get("scope_hint"),
            "channels": _channel_token(unit.get("channels"), scan), "cycles": unit.get("cycles"),
            "detector": unit.get("detector"), "score": unit.get("score"),
            "tissue_fraction": (unit.get("measurement") or {}).get("tissue_fraction"),
            "bbox_px": unit.get("bbox"), "bbox_um": _bbox_um(unit.get("bbox") or [], pixel),
            "metrics": unit.get("metrics")}


def label_of(engine, unit, *, fresh=False):
    """A candidate's label (c1, c2...), unique in the session: what the audit
    rows, the confirm sheet's rows and a batched answer's keys call it."""
    if unit.get("label") and not fresh:
        return unit["label"]
    seq = int(engine.record.get("label_seq") or 0) + 1
    engine.record["label_seq"] = seq
    unit["label"] = f"c{seq}"
    return unit["label"]


# -- the channel audit ------------------------------------------------------------------


AUDIT_CANDIDATE_KEYS = ("id", "class_hint", "score", "tissue_fraction", "channel_level")
#: An audit row's whole-channel numbers, sent as one list per row in this
#: order (`overview_columns`): the names on every row were a fifth of an
#: audit's text.
AUDIT_OVERVIEW = ("saturation_fraction", "tissue_ratio", "dynamic_range_decades",
                  "zero_fraction", "focus_rel_p10", "bright_compact_fraction",
                  "illumination_r2")


def _audit_evidence(rows, scan=None) -> dict:
    """The audit's rows and candidates, each candidate described once: a
    candidate merged across channels sits on every one of its rows, and
    repeating its description on each was most of a crowded audit's text.
    `{"rows": [... "candidates": [label, ...]], "candidates": {label: {...}}}`."""
    described, out_rows = {}, []
    for row in rows:
        labels = []
        for c in row.get("candidates") or ():
            label = c.get("label") or c.get("id")
            labels.append(label)
            if label not in described:
                entry = {k: c.get(k) for k in AUDIT_CANDIDATE_KEYS}
                if c.get("channels") is not None:
                    entry["channels"] = _channel_token(c.get("channels"), scan)
                described[label] = entry
        numbers = {**(row.get("overview") or {}), "illumination_r2": row.get("illumination_r2")}
        listed = [numbers.get(k) for k in AUDIT_OVERVIEW]
        out_rows.append({k: v for k, v in row.items()
                         if k not in ("candidates", "overview", "illumination_r2")}
                        | ({"overview": listed} if any(v is not None for v in listed) else {})
                        | {"candidates": labels})
    return {"rows": out_rows, "candidates": described,
            "overview_columns": list(AUDIT_OVERVIEW)}


def channel_audit(engine, units):
    project = units[0]["project"]
    scan = engine.scan(project)
    pixel = engine.pixel_for(project)
    calibration = engine.calibration(project)
    per_sheet = int(schemas.ENGINE["audit_batch"])
    pending_cands = [u for u in engine.units_of("candidate", project)
                     if u["state"] == "awaiting_audit"]
    # What the scan saw of a staining kind in each channel: never a candidate.
    hints = (engine.record.get("staining_hints") or {}).get(project) or {}
    rows = []
    for unit in units:
        # A candidate merged across channels is on every one of its rows,
        # under one label (session-unique: a later sheet goes on counting).
        mine = [c for c in pending_cands if unit["id"] in cand.audit_channels(c)]
        mine.sort(key=lambda c: (-float(c.get("score") or 0), c["id"]))
        listed = []
        for candidate in mine:
            label_of(engine, candidate)
            variants = _variants(engine, candidate)
            listed.append({"id": candidate["id"], "label": candidate["label"],
                           "class_hint": candidate.get("class_hint"),
                           "score": candidate.get("score"),
                           "tissue_fraction": (candidate.get("measurement") or {}).get(
                               "tissue_fraction"),
                           "channel_level": bool(candidate.get("channel_level")) or None,
                           "geometry": (variants.get("standard") or {}).get("geometry")})
        meta = scan.channel(unit["id"])
        summary = meta.get("summary") or {}
        rows.append({"number": unit["order"] + 1, "channel": unit["id"],
                     "cycle": meta.get("cycle"), "flags": meta.get("flags"),
                     "candidates": listed, "scan_hints": hints.get(unit["id"]),
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
                           "illumination, lost tissue, a failed stain? Answer "
                           "`clean`, `suspicious` (name the outlined candidates that worry "
                           "you, or `elsewhere`) or `uncertain`, per channel. A staining "
                           "problem is settled on the row with `class_hint`, never "
                           "outlined."),
              "evidence": {"project": project,
                           **_audit_evidence(rows, scan),
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


def _neighbours(engine, unit, project):
    """Candidates whose mask overlaps this one's -- a count of channels, not
    the names: what matters for the decision is that they overlap, not which
    channels the other one is on."""
    neighbours = []
    mask = engine.mask_of(unit)
    for other in engine.units_of("candidate", project):
        if other is unit or other["state"] in ("dismissed", "merged"):
            continue
        theirs = engine.mask_of(other)
        inter = np.logical_and(mask, theirs).sum()
        if inter:
            neighbours.append({"candidate": other["id"],
                               "n_channels": len(other.get("channels") or []),
                               "iou": float(inter / np.logical_or(mask, theirs).sum()),
                               "state": other["state"]})
    return neighbours


def artifact_confirm(engine, units):
    if len(units) > 1:
        return _artifact_confirm_batch(engine, units)
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
    neighbours = _neighbours(engine, unit, project)
    packet = {"question": (f"The scan flagged a possible {words} in "
                           f"{', '.join(unit.get('channels') or [])[:80]} (outlined). Is it a "
                           "technical artifact? Give its class and severity, whether the "
                           "whole artifact lies inside the outline (its pixels are traced "
                           "inside it), and whether its cells should be excluded. "
                           "`need_more_evidence` shows it closer."),
              "evidence": {"candidate": _brief(unit, pixel, scan), "look": level + 1,
                           "looks": int(schemas.ENGINE["confirm_levels"]),
                           "neighbours": neighbours[:6],
                           "scope_options": list(schemas.SCOPES),
                           **_reading(engine, ["confirm", "severity"])},
              "allowed": list(schemas.AGENT_CLASSES), "_image_meta": []}
    return packet, _images(packet, [(rendered, "confirm_sheet",
                                     f"{words} candidate, look {level + 1}")])


def _shared_metrics(briefs) -> dict:
    """The `metrics` every brief carries with the same value (one check's
    bar, step and fingerprint on each of its regions), taken out of each and
    returned once; a brief keeps only its own."""
    rows = [b.get("metrics") for b in briefs]
    if len(rows) < 2 or not all(isinstance(r, dict) for r in rows):
        return {}
    shared = {k: v for k, v in rows[0].items()
              if all(k in r and r[k] == v for r in rows[1:])}
    for brief in briefs:
        brief["metrics"] = {k: v for k, v in brief["metrics"].items() if k not in shared}
    return shared


def _artifact_confirm_batch(engine, units):
    """First looks at several candidates on one sheet, a row each (the
    channel's whole tissue with the outline, the neighbourhood, a close crop,
    the detector's map), answered per candidate label."""
    project = units[0]["project"]
    scan = engine.scan(project)
    pixel = engine.pixel_for(project)
    for unit in units:
        label_of(engine, unit)
        _variants(engine, unit)
    rendered = sheets.confirm_batch_sheet(
        engine.call.session, project, scan, units, [engine.mask_of(u) for u in units],
        fmt=_fmt(engine), pixel=pixel, calibration=engine.calibration(project))
    _record_artifact(units, rendered)
    briefs = []
    for unit in units:
        brief = _brief(unit, pixel, scan)
        brief.pop("bbox_px", None)    # a click is on the sheet, never a coordinate
        brief["neighbours"] = _neighbours(engine, unit, project)[:4]
        briefs.append(brief)
    shared = _shared_metrics(briefs)
    labels = [u["label"] for u in units]
    packet = {"question": (f"The scan flagged {len(units)} possible artifacts, one sheet row "
                           f"each ({', '.join(labels)}). For each: is it a technical "
                           "artifact? Give its class and severity, whether the whole "
                           "artifact lies inside its outline (its pixels are traced inside "
                           "it), and whether its cells should be excluded -- as `verdicts`, "
                           "keyed by label. `need_more_evidence` on one shows that one "
                           "closer, on its own."),
              "evidence": {"candidates": briefs, "labels": labels, "look": 1,
                           "shared_metrics": shared or None,
                           "looks": int(schemas.ENGINE["confirm_levels"]),
                           "scope_options": list(schemas.SCOPES),
                           **_reading(engine, ["confirm", "severity"])},
              "allowed": list(schemas.AGENT_CLASSES), "_image_meta": []}
    return packet, _images(packet, [(rendered, "confirm_batch_sheet",
                                     f"{len(units)} candidates, first look")])


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
              "evidence": {"candidate": _brief(unit, pixel, scan), "options": options,
                           **_reading(engine, ["scope"])},
              "allowed": [o["id"] for o in options] + ["cannot_tell"], "_image_meta": []}
    return packet, _images(packet, [(rendered, "scope_sheet", "the region across channels")])


def _localize_trace(engine, unit, variants):
    """The artifact traced once inside every outline together, then clipped
    to each: {variant: {geometry, area_px2, area_um2, kept_fraction}}, and the
    trace's record (kept on the unit, so the outline chosen is written as it
    was drawn). {} when tracing is off, not possible, or fails."""
    from shapely.geometry import shape
    import shapely

    if not engine.options.get("refine", True) or not variants:
        return {}, None
    scan = engine.scan(unit["project"])
    klass = (unit.get("decision") or {}).get("artifact_class") or unit.get("class_hint")
    margin = engine.options.get("refine_margin_um")
    union = shapely.union_all([shape(v["geometry"]) for v in variants.values()])
    envelope = polygons.to_geojson(union, simplify_px=0)
    if envelope is None:
        return {}, None
    from plexora.plugins.qc.server import refine_sam

    held = unit.get("localize_trace") or {}
    version = refine_sam.model_version()
    if held.get("envelope_hash") != polygons.geometry_hash(envelope) \
            or held.get("class") != klass or held.get("margin_um") != margin \
            or held.get("sam_version") != version:
        pixel = engine.pixel_for(unit["project"])
        mask = polygons.geometry_to_grid(envelope, scan.grid, touch=True)
        try:
            # The model's outline, for a physical artifact when it is there,
            # competes with the classical trace under guards; either way every
            # lettered outline is the trace clipped to it.
            result = refine_sam.trace({**unit, "decision": {"artifact_class": klass}}, mask,
                                      scan, engine.call.session.image_data(unit["project"]),
                                      pixel_um=float(pixel["value"]) if pixel else None,
                                      envelope=envelope,
                                      options={"margin_um": margin}
                                      if margin is not None else None)
            record = result.to_record()
            record["geometry"] = result.geometry if result.refined else None
        except Exception as exc:  # noqa: BLE001 -- the sheet then shows envelopes only
            engine.log(event="refine_failed", unit=engine.key_of(unit), error=str(exc))
            record = {"status": "fallback", "reason": f"tracing failed: {exc}", "geometry": None}
        record.update(envelope_hash=polygons.geometry_hash(envelope), **{"class": klass},
                      margin_um=margin, sam_version=version)
        unit["localize_trace"] = held = record
    if held.get("status") != "refined" or not held.get("geometry"):
        return {}, held
    pixel_um = scan.meta.get("pixel_um")
    out = {}
    for name, variant in variants.items():
        clipped = polygons.clip_to(held["geometry"], variant["geometry"])
        if clipped is None:
            continue
        area = polygons.area_of(clipped)
        out[name] = {"geometry": clipped, "area_px2": area,
                     "area_um2": area * pixel_um * pixel_um if pixel_um else None,
                     "kept_fraction": area / variant["area_px2"] if variant.get("area_px2")
                     else None}
    return out, held


def can_redraw(engine, unit) -> bool:
    """Whether the agent may still ask the segmentation model to outline this
    region again: the model is installed, tracing is on, and the region has
    not used its `ENGINE["max_sam_redraws"]`."""
    from plexora.plugins.qc.server import refine_sam

    return bool(engine.options.get("refine", True)) and refine_sam.available() \
        and int(unit.get("sam_redraws") or 0) < int(schemas.ENGINE["max_sam_redraws"])


def redraw_with_sam(engine, unit) -> dict:
    """The region outlined again by the segmentation model, at the agent's
    ask, inside every outline together (as `_localize_trace` traces): the
    result becomes the localize trace, so the next localize packet draws it
    and the outline chosen is written as drawn. Returns what happened."""
    from shapely.geometry import shape
    import shapely

    from plexora.plugins.qc.server import refine_sam

    unit["sam_redraws"] = int(unit.get("sam_redraws") or 0) + 1
    variants = _variants(engine, unit)
    scan = engine.scan(unit["project"])
    klass = (unit.get("decision") or {}).get("artifact_class") or unit.get("class_hint")
    margin = engine.options.get("refine_margin_um")
    envelope = polygons.to_geojson(shapely.union_all([shape(v["geometry"])
                                                      for v in variants.values()]),
                                   simplify_px=0) if variants else None
    if envelope is None:
        outcome = {"status": "not_applicable", "reason": "the region has no outline to redraw"}
        unit["sam_redraw"] = outcome
        return outcome
    pixel = engine.pixel_for(unit["project"])
    try:
        result = refine_sam.redraw({**unit, "decision": {"artifact_class": klass}},
                                   polygons.geometry_to_grid(envelope, scan.grid, touch=True),
                                   scan, engine.call.session.image_data(unit["project"]),
                                   pixel_um=float(pixel["value"]) if pixel else None,
                                   envelope=envelope,
                                   options={"margin_um": margin} if margin is not None else None)
    except Exception as exc:  # noqa: BLE001 -- the outline in hand stands
        engine.log(event="sam_redraw_failed", unit=engine.key_of(unit), error=str(exc))
        outcome = {"status": "failed", "reason": str(exc)[:200]}
        unit["sam_redraw"] = outcome
        return outcome
    if result.method == "sam":
        record = result.to_record()
        record["geometry"] = result.geometry
        record.update(envelope_hash=polygons.geometry_hash(envelope), **{"class": klass},
                      margin_um=margin, sam_version=refine_sam.model_version())
        unit["localize_trace"] = record
        outcome = {"status": "redrawn", "reason": "outlined by the segmentation model; "
                                                  "drawn solid in every outline below"}
    else:
        sam = result.params.get("sam") or {}
        outcome = {"status": "kept", "reason": "the model's outline failed a guard ("
                   f"{sam.get('guard') or sam.get('reason') or sam.get('status')}); "
                   "the outline in hand is unchanged"}
    unit["sam_redraw"] = outcome
    engine.log(event="sam_redraw", unit=engine.key_of(unit), **outcome)
    return outcome


def artifact_localize(engine, units):
    unit = units[0]
    project = unit["project"]
    scan = engine.scan(project)
    pixel = engine.pixel_for(project)
    variants = _variants(engine, unit)
    traces, trace = _localize_trace(engine, unit, variants)
    rendered = sheets.localize_sheet(engine.call.session, project, scan, unit, variants,
                                     fmt=_fmt(engine), pixel=pixel,
                                     calibration=engine.calibration(project), traces=traces)
    _record_artifact([unit], rendered)
    alternatives = []
    for name, v in variants.items():
        if name not in sheets.VARIANT_LETTERS:
            continue
        entry = {"id": sheets.VARIANT_LETTERS[name], "variant": name,
                 "area_um2": v.get("area_um2"), "area_px2": v.get("area_px2"),
                 "vertices": v.get("vertices")}
        if name in traces:
            entry.update(refined_area_um2=traces[name]["area_um2"],
                         refined_area_px2=traces[name]["area_px2"],
                         kept_fraction=traces[name]["kept_fraction"])
        alternatives.append(entry)
    evidence = {"candidate": _brief(unit, pixel, scan), "alternatives": alternatives,
                "boundary_said": (unit.get("decision") or {}).get("boundary"),
                **_reading(engine, ["localize"])}
    if trace is not None:
        evidence["refinement"] = {k: trace.get(k) for k in ("status", "method", "reason")}
    redraw = can_redraw(engine, unit)
    if unit.get("sam_redraw"):
        evidence["redrawn"] = unit["sam_redraw"]
    packet = {"question": "Which outline should the artifact be traced inside? A letter, "
                          "`current` (the standard outline, B), or `none_fits` for a grid."
                          + (" `redraw`, once, when every solid trace misses part of the "
                             "artifact or spills onto clean tissue: the segmentation model "
                             "outlines it again and you choose again." if redraw else ""),
              "evidence": evidence,
              "allowed": [a["id"] for a in alternatives] + ["current", "none_fits"]
              + (["redraw"] if redraw else []),
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
              "evidence": {"candidate": _brief(unit, pixel, scan),
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


def _compact_region(region, scan):
    """One final-review region as the agent sees it: its channels collapsed
    (`_channel_token`) and its refinement down to the status word -- the
    method and kept_fraction are for the record, not the consistency
    judgment -- with no geometry and no `refined_fraction` (folded into
    `excluded_tissue_fraction` before this runs)."""
    out = {k: v for k, v in region.items()
          if k not in ("geometry", "refined_fraction", "refinement")}
    out["channels"] = _channel_token(region.get("channels"), scan)
    out["refinement"] = (region.get("refinement") or {}).get("status")
    return out


#: How many regions each (category, action) group of the final review names;
#: the rest are counted (`more`, `more_tissue_fraction`).
FINAL_REVIEW_TOP_PER_GROUP = 5


def _review_table(regions, scan, top=FINAL_REVIEW_TOP_PER_GROUP) -> dict:
    """The final review's regions as a table: one group per (category,
    action), excluding groups first then by tissue share, each with its
    count, tissue share, its classes when more than one, its `top` largest
    regions by label and the rest counted. Pure."""
    from plexora.plugins.qc.server import consolidate

    def share(r):
        return float(r.get("tissue_fraction") or 0)

    grouped = {}
    for region in regions:
        grouped.setdefault((region.get("category"), region.get("action")), []).append(region)
    groups = []
    for (category, action), members in grouped.items():
        members = sorted(members, key=lambda r: (-share(r), str(r.get("label"))))
        group = {"category": category, "action": action, "count": len(members),
                 "tissue_fraction": _round(sum(share(r) for r in members))}
        classes = {}
        for r in members:
            classes[r.get("class")] = classes.get(r.get("class"), 0) + 1
        if len(classes) > 1:
            group["classes"] = classes
        group["top"] = [{"label": r.get("label"), "class": r.get("class"),
                         "channels": _channel_token(r.get("channels"), scan),
                         "tissue_fraction": r.get("tissue_fraction"),
                         "refinement": (r.get("refinement") or {}).get("status")}
                        for r in members[:top]]
        rest = members[top:]
        if rest:
            group["more"] = len(rest)
            group["more_tissue_fraction"] = _round(sum(share(r) for r in rest))
        groups.append(group)
    groups.sort(key=lambda g: (consolidate.ACTION_RANK.get(g["action"], 1),
                               -float(g["tissue_fraction"] or 0), str(g["category"])))
    by_action = {}
    for region in regions:
        by_action[region.get("action")] = by_action.get(region.get("action"), 0) + 1
    return {"n": len(regions), "by_action": by_action, "groups": groups}


def _dna_digest(engine, project):
    """The session result's DNA retention digest line (`dna_retention`), or
    None when the result has none or the module is not there."""
    try:
        from plexora.plugins.qc.server import dna_retention, results
    except ImportError:
        return None
    digest_line = getattr(dna_retention, "digest_line", None)
    if digest_line is None:
        return None
    try:
        result = results.get_result(project, results.load(project),
                                     engine.record.get("result_id"))
        summary = (result or {}).get("dna_retention")
        return digest_line(summary) if summary else None
    except Exception:  # noqa: BLE001 -- a digest is context, never a reason to fail
        return None


def final_qc_review(engine, units):
    project = engine.project
    scan = engine.scan(project)
    pixel = engine.pixel_for(project)
    regions = []
    for unit in engine.units_of("candidate", project):
        if unit["state"] not in schemas.WRITTEN_STATES or not unit.get("geometry"):
            continue
        # A region keeps the label it was first shown under, so a second
        # review names the same regions as the first did.
        if not unit.get("review_label"):
            counter = int(engine.record.get("review_labels") or 0) + 1
            engine.record["review_labels"] = counter
            unit["review_label"] = f"r{counter}"
        klass = unit.get("class") or unit.get("class_hint")
        regions.append({"label": unit["review_label"], "candidate": unit["id"],
                        "class": klass,
                        "category": schemas.REVIEW["id"]
                        if unit["state"] == "manual_review_recommended"
                        else schemas.category_of_class(klass),
                        "action": unit.get("action"), "geometry": unit["geometry"],
                        "channels": unit.get("channels"),
                        "tissue_fraction": (unit.get("measurement") or {}).get(
                            "tissue_fraction"),
                        "refined_fraction": (unit.get("measurement") or {}).get(
                            "refined_fraction"),
                        "refinement": {k: (unit.get("refinement") or {}).get(k) for k in (
                            "status", "method", "kept_fraction")}
                        if unit.get("refinement") else None})
    if not regions:
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
    # Every region listed one by one was half a 250-region run's text: the
    # table groups them by category and action and names the largest few of
    # each; `units[0]["regions"]` above still resolves any label the sheet
    # shows, so a concern may name one the table only counts.
    table = _review_table(regions, scan)
    # What was not checked, and why: the last look must not read silence on a
    # check as a clean result.
    checks = {}
    for unit in engine.units_of("check", project):
        states = checks.setdefault(unit["check"], {})
        states[unit["state"]] = states.get(unit["state"], 0) + 1
    not_checked = [{"check": n["check"], "status": n["status"], "reason": n["reason"]}
                   for n in engine.record.get("planning_notes") or ()]
    evidence = {"regions": table,
                "excluded_tissue_fraction": float(sum(
                    (r.get("refined_fraction") if r.get("refined_fraction")
                     is not None else r.get("tissue_fraction")) or 0
                    for r in excluded)),
                "channels": channels, "checks": checks,
                "planning_notes": not_checked,
                **_reading(engine, ["review"])}
    digest = _dna_digest(engine, project)
    if digest:
        evidence["dna_retention"] = digest
    packet = {"question": ("Here is every QC region, grouped by category and action. Is "
                           "the picture consistent: nothing obvious missed, nothing real "
                           "excluded, no region far larger than its artifact? Name concerns "
                           "by region label."),
              "evidence": evidence,
              "allowed": ["consistent", "inconsistent", "cannot_tell"], "_image_meta": []}
    return packet, _images(packet, [(rendered, "review_sheet", "every QC region")])


# -- the image checks ----------------------------------------------------------------------


def score_review(engine, units):
    """One check on one channel: places sampled across its score, at the bar
    the session holds (the automatic one moved by the steps looks took)."""
    from plexora.plugins.qc.server import checks_bulk, checks_result
    from plexora.plugins.qc.server import score_review as review

    from plexora.plugins.qc.server import transitions

    unit = units[0]
    project = unit["project"]
    field = checks_bulk.field_of(engine, unit)
    if field is None:
        engine.close(unit, "manual_review_recommended",
                     "the check's scores are no longer cached; run the check again")
        checks_result.record(engine, unit)
        return None
    if transitions.settle_if_nothing_at_bar(engine, unit, field):
        return None
    look = review.review(engine.call.session, project, field,
                         offset_steps=int(unit.get("offset_steps") or 0),
                         seed=int(engine.options.get("seed") or 0), fmt=_fmt(engine),
                         scan=engine.scan(project),
                         source=unit.get("threshold_source") or "auto")
    bar = look["bar"]
    rows = list(look["strata"])
    unit["shown"] = {"strata": rows, "global": bool(look["global"]["possible"]),
                     "threshold": bar["value"], "offset_steps": bar["offset_steps"],
                     "step": bar["step"],
                     "places": {k: [{"x": p["x"], "y": p["y"], "score": p["score"]}
                                    for p in v] for k, v in look["strata"].items()}}
    unit["threshold"] = bar["value"]
    _record_artifact([unit], look["sheet"])
    rounds = int(unit.get("rounds") or 0)
    words = schemas.CHECK_WORDS.get(unit["check"], unit["check"])
    on = unit.get("channel") or "the segmentation mask"
    if unit["check"] == "artifacts":
        on = f"the {unit.get('category') or 'artifact'} objects"
    if unit["check"] == "registration":
        on = f"{unit.get('channel')} against {unit.get('reference')}"
    evidence = {**look["evidence"], "cycle": _cycle_of(engine, unit),
                "round": rounds + 1, "rounds": int(schemas.ENGINE["score_rounds"]),
                "previous": (unit.get("strata_verdicts") or [])[-1:] or None,
                **_reading(engine, ["score", "severity"])}
    if not rows:
        engine.close(unit, "decided", f"nothing to show: no place of the {words} score "
                                      "could be sampled")
        checks_result.record(engine, unit)
        return None
    shows = {"blur": "really out of focus",
             "registration": "nuclei out of register between the cycles (red and green "
                             "apart)",
             "segmentation": "cells far too large or too small for this mask (merged "
                             "nuclei, fragments, or debris segmented as cells)",
             "artifacts": "a fold, a tear, debris or a saturated patch (not normal tissue)"}
    packet = {"question": (f"The {words} check scored {on}; each row shows places from one "
                           f"part of its score. For each row: are its tiles "
                           f"{shows.get(unit['check'], 'the artifact')}, or normal tissue? "
                           "And does the bar sit right?"),
              "evidence": evidence,
              "allowed": {"strata": rows, "verdicts": list(schemas.SCORE_VERDICTS),
                          "threshold": list(schemas.THRESHOLD_VERDICTS),
                          "whole_tissue": ["artifact", "normal", "cannot_tell"]
                          if look["global"]["possible"] else None},
              "_image_meta": []}
    return packet, _images(packet, [(look["sheet"], "score_sheet",
                                     f"{words} in {on}, bar {bar['value']:.3g}")])


def _cycle_of(engine, unit):
    try:
        meta = engine.scan(unit["project"]).channel(unit.get("channel")) \
            if unit.get("channel") else {}
    except Exception:
        return None
    return (meta or {}).get("cycle")


# -- the visual pass -----------------------------------------------------------------------


def visual_scan(engine, units):
    """The whole tissue in four views, with every region the session has
    written outlined: the agent outlines large artifacts with its tools
    while this packet is out, then answers that the pass is done."""
    from plexora.agent.registry import tool_name_of
    from plexora.plugins.qc.server import overview, results

    unit = units[0]
    project = unit["project"]
    document = results.load(project)
    result = results.get_result(project, document, engine.record["result_id"])
    rendered = overview.build_sheet(engine.call.session, project, result=result,
                                    fmt=_fmt(engine))
    _record_artifact([unit], rendered)
    manifest = rendered["manifest"]
    written = []
    for candidate_id in unit.get("written") or []:
        held = engine.record["units"].get(engine.unit_key_of(
            {"project": project, "type": "candidate", "id": candidate_id}))
        if held is not None:
            written.append({"candidate": held["id"], "label": held.get("label"),
                            "class": held.get("class"), "action": held.get("action"),
                            "state": held["state"], "roi_id": held.get("roi_id")})
    evidence = {"session_id": engine.id,
                "tissue": manifest.get("tissue"),
                "panels": [p["words"] for p in manifest.get("panels") or []],
                **overview.for_agent(manifest),
                "channels": manifest.get("channels"),
                "max_regions": int(schemas.VISUAL["max_regions"]),
                "written": written,
                "tools": {"zoom": tool_name_of("qc.render_artifact_overview"),
                          "channels": tool_name_of("qc.inspect_artifact_channels"),
                          "outline": tool_name_of("qc.segment_roi")},
                "how": ("point on the picture ({artifact_id, px}); zoom with the overview "
                        "tool's region; pick channels on a box; preview the outline, refine "
                        "it, then write it with session_id; at most max_regions"),
                **_reading(engine, ["visual", "classes", "severity"])}
    packet = {"question": ("Here is the whole tissue in four views, with every region "
                           "already written outlined. Are there large, obvious artifacts "
                           "no outline covers -- a fold, a tear, lifted tissue, debris, a "
                           "bubble? Outline each with the tools, snug to the artifact, then "
                           "answer done; or nothing_found."),
              "evidence": evidence, "allowed": ["done", "nothing_found"], "_image_meta": []}
    return packet, _images(packet, [(rendered, "visual_overview",
                                     "the whole tissue in four views")])


BUILDERS = {"channel_audit": channel_audit, "artifact_confirm": artifact_confirm,
            "artifact_scope": artifact_scope, "artifact_localize": artifact_localize,
            "artifact_grid": artifact_grid, "final_qc_review": final_qc_review,
            "score_review": score_review, "visual_scan": visual_scan}


def _narrated_channel(channels) -> str:
    """The channel words of a candidate's brief: its first channel, or the
    words of a compacted token (`_channel_token`) -- indexing the token
    "cycle 2" narrated "a tissue loss in c."."""
    if isinstance(channels, str):
        return "every channel" if channels == "all_channels" else channels
    return (channels or ["this image"])[0]


def narrate(packet) -> str:
    kind = packet.get("kind")
    evidence = packet.get("evidence") or {}
    candidate = evidence.get("candidate") or {}
    template = schemas.NARRATION.get(kind) or ""
    if kind == "artifact_confirm" and evidence.get("candidates"):
        return schemas.NARRATION["artifact_confirm_batch"].format(
            n=len(evidence["candidates"]))
    if kind == "score_review":
        return template.format(check_words=schemas.CHECK_WORDS.get(evidence.get("check"),
                                                                    "check"),
                               channel=evidence.get("channel")
                               or evidence.get("reference") or "the image")
    channel = _narrated_channel(candidate.get("channels")) if candidate else \
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
