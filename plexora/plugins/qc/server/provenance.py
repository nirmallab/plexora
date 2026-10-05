"""Why each region and cell was flagged: one record per finding, one builder.

The panel shows a finding's category, the details view its subtype and the
steps that led to it, and the export everything. All three read the records
built here, so the words in the viewer and the columns in a notebook cannot
drift apart:

- `region_record` -- one QC region: category and subtype, the tool that
  found it (detector or image check, its version), the score and the
  threshold it crossed with where that threshold came from, the agent's
  judgment (and its notes, in its own words), how its outline was made, how many cells it removes, and who
  made it. Derived from the candidate the result holds and its `roi_meta`
  row; the geometry itself stays in the GeoJSON (`geometry_ref`).
- `cell_reason_records` -- one per cell reason that flagged any cell: the
  module (or the regions) behind it, the cutoffs and the verdicts, the
  offsets and where they came from, how many cells it excluded and warned.
  `cells_source` says whether the reason was read on the cell itself
  ("direct") or inherited from a region the cell sits in ("roi").
- `document` -- `qc_provenance.json`: the vocabulary, the checks, every
  region and reason, the categories summarised, the software version.
- `findings_rows` -- the same, flat, for `qc_findings.csv`.

Pure reads: nothing here writes, and a record never guesses a value it does
not hold (a missing one is None).
"""

from __future__ import annotations

from plexora.plugins.qc.server import schemas

SCHEMA = "plexora.qc.provenance/1"

#: What a region's `score` measures, by the tool that found it.
SCORE_KINDS = {"blur": "blur_score", "registration": "mismatch_share",
               "segmentation": "flagged_cell_density", "artifacts": "artifact_score"}

FINDINGS_COLUMNS = ("kind", "id", "category", "category_words", "subtype", "subtype_words",
                    "action", "level", "tool", "tool_version", "score", "score_kind",
                    "threshold", "threshold_source", "offset_steps", "method", "channels",
                    "cycles",
                    "ai_verdict", "ai_confidence", "ai_notes", "n_cells", "n_excluded", "n_warned",
                    "cells_source", "created_by", "user_state", "geometry_ref")


def method_of(candidate) -> str | None:
    """How the region's current outline was made (schemas.REGION_METHODS), or
    None when nothing says. The candidate's own `method` wins (a region drawn
    in a panel, or by the agent's magic select); otherwise its refinement
    says: magic select, QC's tracer, or a check's score map; a grid answer;
    else the detector's untraced envelope."""
    candidate = candidate or {}
    method = candidate.get("method")
    refinement = candidate.get("refinement") or {}
    edited = (candidate.get("user_state") or {}).get("edited")
    if method in schemas.REGION_METHODS and (edited or not refinement
                                             or method in ("sam", "sam_agent")):
        return method
    if refinement.get("status") == "refined":
        traced = refinement.get("method")
        if traced == "sam":
            # Traced with the segmentation model, automatically (the tracer).
            return "sam_agent"
        return "map" if traced == "map" or candidate.get("trace") == "map" else "traced"
    if method in schemas.REGION_METHODS:
        return method
    if candidate.get("trace") == "map":
        return "map"
    if candidate.get("grid_squares"):
        return "grid"
    detector = candidate.get("detector")
    if detector and detector != "user":
        return "envelope"
    return None


def _tool(candidate):
    detector = candidate.get("detector") or "user"
    return {"name": detector, "version": candidate.get("detector_version"),
            "origin": candidate.get("origin") or ("user" if detector == "user" else "detector"),
            "created_by": candidate.get("created_by")
            or (candidate.get("user_state") or {}).get("created_by") or "agent",
            "method": method_of(candidate)}


#: The longest notes text a record carries (each answer's notes are at most
#: 300 characters; a region looked at many times keeps its first words).
MAX_NOTES_CHARS = 600


def notes_text(notes) -> str | None:
    """The agent's notes (a list, one per answer) as one line, or None."""
    if isinstance(notes, str):
        notes = [notes]
    seen = list(dict.fromkeys(" ".join(str(n).split()) for n in notes or [] if n))
    text = " ".join(n for n in seen if n)
    if not text:
        return None
    return text if len(text) <= MAX_NOTES_CHARS else text[:MAX_NOTES_CHARS - 1] + "…"


def _ai(candidate, *, fallback_notes=None):
    decision = candidate.get("ai_decision") or {}
    notes = notes_text(candidate.get("notes")) or notes_text(fallback_notes)
    if not notes and not any(decision.get(k) for k in ("verdict", "artifact_class",
                                                       "severity", "confidence")):
        return None
    return {k: decision.get(k) for k in ("verdict", "artifact_class", "severity",
                                         "confidence", "boundary", "scope", "source",
                                         "manual_review")} | {"notes": notes}


def check_notes(candidate, checks):
    """The notes of the image check that found a region (its score review's
    looks), for a region whose own looks left none."""
    if (candidate or {}).get("origin") != "check" or not checks:
        return None
    unit = candidate.get("check_unit")
    name = candidate.get("detector")
    entries = (checks or {}).get(name) or {}
    if isinstance(unit, str) and ":" in unit:
        key = unit.split(":", 1)[1]
        if key in entries:
            return (entries[key] or {}).get("notes")
    keys = ["calls"] if name == "segmentation" else [
        *(candidate.get("channels") or []), "image"]
    for key in keys:
        if key in entries:
            return (entries[key] or {}).get("notes")
    return None


def region_summary(candidate, *, checks=None) -> dict:
    """The provenance fields of one region, as the panel's region rows carry
    them (the details view's lines). `checks` (the result's) lends a check's
    region its score review's notes when it has none of its own."""
    candidate = candidate or {}
    metrics = candidate.get("metrics") or {}
    tool = _tool(candidate)
    return {"score": candidate.get("score"),
            "score_kind": SCORE_KINDS.get(tool["name"])
            or ("detector_score" if candidate.get("score") is not None else None),
            "threshold": metrics.get("threshold"),
            "auto_threshold": metrics.get("auto_threshold"),
            "threshold_source": metrics.get("threshold_source")
            or ("auto" if metrics.get("threshold") is not None else None),
            "offset_steps": metrics.get("offset_steps"),
            "tool": tool, "ai": _ai(candidate, fallback_notes=check_notes(candidate, checks)),
            "cycles": [int(c) for c in candidate.get("cycles") or []],
            "scope": candidate.get("scope")}


def _user_state(candidate, row):
    user = candidate.get("user_state") or {}
    row = row or {}
    if user.get("deleted") or row.get("deleted"):
        return "deleted"
    if user.get("removed_from_qc") or row.get("removed_from_qc"):
        return "removed_from_qc"
    if user.get("approved") or row.get("approved"):
        return "approved"
    if user.get("edited") or row.get("user_edited"):
        return "edited"
    if user.get("relabelled"):
        return "relabelled"
    return None


def region_record(candidate, row=None, live=None, *, n_cells=None, segmentation=None,
                  checks=None) -> dict:
    """The whole record of one QC region (see the module docstring)."""
    from plexora.plugins.qc.server import class_rules, polygons

    candidate = candidate or {}
    row = row or {}
    live = live or {}
    klass = live.get("class") or candidate.get("class") or row.get("class") \
        or "other_technical"
    category = live.get("category") or schemas.category_of_class(klass)
    level, level_channels = class_rules.region_level(
        klass, candidate.get("scope") or row.get("scope"),
        candidate.get("channels") or row.get("channels") or [], segmentation=segmentation)
    refinement = candidate.get("refinement") or {}
    geometry = live.get("geometry") or candidate.get("geometry")
    roi_id = live.get("roi_id") or candidate.get("roi_id") or row.get("roi_id")
    summary = region_summary(candidate, checks=checks)
    return {
        "roi_id": roi_id, "candidate_id": candidate.get("id") or row.get("candidate_id"),
        "name": live.get("name"),
        "category": category, "category_words": schemas.category_words(category),
        "class": klass, "class_words": schemas.CLASS_WORDS.get(klass, klass),
        "action": live.get("action") or candidate.get("action") or row.get("action"),
        "level": level, "level_channels": level_channels,
        "channels": list(candidate.get("channels") or row.get("channels") or []),
        "cycles": summary["cycles"] or [int(c) for c in row.get("cycles") or []],
        "scope": candidate.get("scope") or row.get("scope"),
        **{k: summary[k] for k in ("score", "score_kind", "threshold", "auto_threshold",
                                   "threshold_source", "offset_steps", "tool", "ai")},
        "severity": (candidate.get("ai_decision") or {}).get("severity")
        or candidate.get("severity"),
        "evidence_artifacts": list(candidate.get("evidence_artifacts")
                                   or row.get("evidence_artifacts") or []),
        "refinement": {"status": refinement.get("status"), "method": refinement.get("method"),
                       "kept_fraction": refinement.get("kept_fraction"),
                       "reason": refinement.get("reason"),
                       "margin_um": refinement.get("margin_um"),
                       "refine_um": refinement.get("refine_um")} if refinement else None,
        "measurement": {k: v for k, v in (candidate.get("measurement") or {}).items()
                        if isinstance(v, (int, float, str, bool)) or v is None},
        "method": summary["tool"]["method"],
        "view": candidate.get("view"),
        "geometry_hash": polygons.geometry_hash(geometry) if geometry else None,
        "geometry_ref": f"qc_regions.geojson#{roi_id}" if roi_id else None,
        "user_state": _user_state(candidate, row),
        "created_by": _tool(candidate)["created_by"] if candidate else row.get("created_by"),
        "created_at": candidate.get("created_at") or row.get("created_at"),
        "session_id": candidate.get("session_id") or row.get("session_id"),
        "strictness_used": row.get("strictness_used"),
        "action_by_strictness": candidate.get("action_by_strictness"),
        "n_cells": n_cells, "cells_source": "roi"}


def cells_per_region(project, result) -> dict:
    """{roi_id: cells the region flagged} from the active result's cell calls
    (a cell counts for every region it is flagged by); {} without them."""
    from plexora.plugins.qc.server import results

    try:
        frame = results.cells(project)
    except Exception:  # an unreadable store: no counts
        return {}
    if frame is None or not frame.height or "roi_ids" not in frame.columns:
        return {}
    if "result_id" in frame.columns and frame["result_id"][0] != (result or {}).get(
            "result_id"):
        return {}
    exploded = frame.select("roi_ids").explode("roi_ids", empty_as_null=True) \
        .drop_nulls("roi_ids")
    if not exploded.height:
        return {}
    counts = exploded.group_by("roi_ids").len()
    return {str(r): int(n) for r, n in counts.iter_rows()}


def _module_source(entry):
    """How a module's cutoffs were set: its own rule, or moved after a look."""
    decision = (entry or {}).get("decision") or {}
    if decision.get("threshold_source"):
        return decision["threshold_source"]
    moved = any(((decision.get(side) or {}).get("offset_steps") or 0)
                for side in ("low", "high") if isinstance(decision.get(side), dict))
    return "agent_refined" if moved else "auto"


def cell_reason_records(result) -> list:
    """One record per cell reason that flagged any cell (see the module
    docstring), in `PRIMARY_ORDER`."""
    cells = (result or {}).get("cells") or {}
    evidence = cells.get("evidence") or {}
    modules = cells.get("modules") or {}
    by_reason = cells.get("by_reason") or {}
    warn_by_reason = cells.get("warn_by_reason") or {}
    order = {r: i for i, r in enumerate(schemas.PRIMARY_ORDER)}
    reasons = sorted(set(by_reason) | set(warn_by_reason), key=lambda r: order.get(r, 999))
    out = []
    for reason in reasons:
        ev = evidence.get(reason) or {}
        module = ev.get("module")
        entry = modules.get(module) or {}
        decision = entry.get("decision") or {}
        category = schemas.category_of_reason(reason)
        region = reason.startswith("region:")
        out.append({
            "reason": reason, "category": category,
            "category_words": schemas.category_words(category),
            "words": schemas.REASON_WORDS.get(reason) or (
                f"In {schemas.CLASS_WORDS.get(reason.split(':', 1)[1], reason)}"
                if region else reason),
            "definition": schemas.REASON_DEFINITIONS.get(reason, ""),
            "level": ev.get("level") or "cell",
            "tool": module or ("regions" if region else None),
            "tool_version": entry.get("version"),
            "measurement": ev.get("column"),
            "channels": list(ev.get("channels") or []),
            "cutoffs": ev.get("cutoffs"), "verdicts": ev.get("verdicts"),
            "offset_steps": ev.get("offsets") or {
                side: (decision.get(side) or {}).get("offset_steps")
                for side in ("low", "high") if isinstance(decision.get(side), dict)} or None,
            "threshold_source": None if region else (ev.get("threshold_source")
                                                     or _module_source(entry)),
            "fingerprint": ev.get("fingerprint"),
            "rois": list(ev.get("rois") or []),
            "notes": notes_text(entry.get("notes")),
            "n_excluded": int(by_reason.get(reason) or 0),
            "n_warned": int(warn_by_reason.get(reason) or 0),
            "denominator": int(cells.get("n") or 0),
            "strictness": (result or {}).get("strictness"),
            "cells_source": "roi" if region else "direct"})
    return out


def marker_reason_records(result) -> list:
    cells = (result or {}).get("cells") or {}
    out = []
    for entry in cells.get("marker_evidence") or []:
        reason = entry.get("reason") or ""
        category = schemas.category_of_reason(reason)
        out.append({
            "marker": entry.get("marker"), "reason": reason, "category": category,
            "category_words": schemas.category_words(category),
            "status": entry.get("status"), "tool": entry.get("module")
            or ("regions" if reason.startswith("region:") else None),
            "roi_id": entry.get("roi_id"), "test": entry.get("test"),
            "borne_out": entry.get("borne_out"), "why": entry.get("why"),
            "cutoffs": entry.get("cutoffs"), "verdicts": entry.get("verdicts"),
            "n_flagged": int(entry.get("n_flagged") or 0),
            "denominator": int(cells.get("n") or 0),
            "cells_source": "roi" if reason.startswith("region:") else "direct"})
    return out


def categories_summary(regions, cell_reasons, marker_reasons, *, n_cells=0) -> list:
    """Per category (the five, then review, then custom ones present): how
    many regions by action, cells excluded and warned by its reasons, marker
    flags, and the subtypes seen."""
    order = [*schemas.CATEGORY_IDS, schemas.REVIEW["id"]]
    extra = [r["category"] for r in regions if r["category"] not in order]
    out = []
    for category in [*order, *dict.fromkeys(extra)]:
        mine = [r for r in regions if r["category"] == category]
        reasons = [r for r in cell_reasons if r["category"] == category]
        markers = [m for m in marker_reasons if m["category"] == category]
        actions = {}
        for region in mine:
            actions[region.get("action") or "ignore"] = actions.get(
                region.get("action") or "ignore", 0) + 1
        subtypes = list(dict.fromkeys(
            [r["class_words"] for r in mine]
            + [r["words"] for r in reasons if not r["reason"].startswith("region:")]))
        out.append({"category": category, "words": schemas.category_words(category),
                    "color": schemas.category_color(category), "regions": actions,
                    "n_regions": len(mine),
                    "cells_excluded_by_reason": sum(r["n_excluded"] for r in reasons),
                    "cells_warned_by_reason": sum(r["n_warned"] for r in reasons),
                    "marker_flags": sum(m["n_flagged"] for m in markers),
                    "denominator_cells": int(n_cells or 0),
                    "subtypes": subtypes})
    return out


def regions(ds, project, result, *, rows=None) -> list:
    """Every live region's record, in category order."""
    from plexora.plugins.qc.server import class_rules, results, roi_link

    if rows is None:
        meta = results.roi_meta(project)
        rows = {r["roi_id"]: r for r in meta.to_dicts()} if meta.height else {}
    candidates = (result or {}).get("candidates") or {}
    counts = cells_per_region(project, result)
    try:
        segmentation = class_rules.segmentation_channel(ds, (result or {}).get("cycles"))
    except Exception:
        segmentation = None
    out = []
    for live in roi_link.live_regions(ds, result):
        candidate = candidates.get(live.get("candidate_id")) or {}
        out.append(region_record(candidate, rows.get(live["roi_id"]), live,
                                 n_cells=counts.get(live["roi_id"]),
                                 segmentation=segmentation,
                                 checks=(result or {}).get("checks")))
    rank = {k: i for i, k in enumerate((*schemas.CATEGORY_IDS, schemas.REVIEW["id"]))}
    out.sort(key=lambda r: rank.get(r["category"], len(rank)))
    return out


def vocabulary() -> dict:
    return {"categories": [{k: c[k] for k in ("id", "words", "color", "default_class")}
                           | {"groups": list(c["groups"])} for c in schemas.CATEGORIES]
            + [{"id": schemas.REVIEW["id"], "words": schemas.REVIEW["words"],
                "color": schemas.REVIEW["color"], "default_class": schemas.REVIEW["class"],
                "groups": []}],
            "classes": {k: schemas.CLASS_CATEGORY[k] for k in schemas.ARTIFACT_CLASSES},
            "reasons": {r: schemas.category_of_reason(r)
                        for r in (*schemas.REASONS, *schemas.MARKER_REASONS)},
            "threshold_sources": list(schemas.THRESHOLD_SOURCES),
            "methods": list(schemas.REGION_METHODS),
            "origins": list(schemas.REGION_ORIGINS)}


def document(ds, project, result, *, checks=None, files=None) -> dict:
    """The body of `qc_provenance.json`."""
    from plexora.plugins.qc.server import results

    region_list = regions(ds, project, result)
    reasons = cell_reason_records(result)
    markers = marker_reason_records(result)
    n_cells = int(((result or {}).get("cells") or {}).get("n") or 0)
    return {"schema": SCHEMA, "project": project,
            "result_id": (result or {}).get("result_id"),
            "session_id": (result or {}).get("session_id"),
            "generated_at": results.now_iso(),
            "software_version": (result or {}).get("software_version")
            or results._software_version(),
            "strictness": (result or {}).get("strictness"),
            "vocabulary": vocabulary(),
            "checks": checks if checks is not None else (result or {}).get("checks") or {},
            "regions": region_list, "cell_reasons": reasons, "marker_reasons": markers,
            "categories": categories_summary(region_list, reasons, markers, n_cells=n_cells),
            "summary": results.summary(result), "files": files or {}}


def _joined(values):
    return ";".join(str(v) for v in values or [] if v is not None and v != "")


def findings_rows(body) -> list:
    """`qc_findings.csv`'s rows: one per region, cell reason and marker
    reason of a provenance document."""
    rows = []
    for region in body.get("regions") or []:
        tool = region.get("tool") or {}
        ai = region.get("ai") or {}
        rows.append({
            "kind": "region", "id": region.get("roi_id"), "category": region["category"],
            "category_words": region["category_words"], "subtype": region["class"],
            "subtype_words": region["class_words"], "action": region.get("action"),
            "level": region.get("level"), "tool": tool.get("name"),
            "tool_version": tool.get("version"), "score": region.get("score"),
            "score_kind": region.get("score_kind"), "threshold": region.get("threshold"),
            "threshold_source": region.get("threshold_source"),
            "offset_steps": region.get("offset_steps"), "method": region.get("method"),
            "channels": _joined(region.get("channels")), "cycles": _joined(region.get("cycles")),
            "ai_verdict": ai.get("verdict"), "ai_confidence": ai.get("confidence"),
            "ai_notes": ai.get("notes"),
            "n_cells": region.get("n_cells"), "n_excluded": None, "n_warned": None,
            "cells_source": "roi", "created_by": tool.get("created_by"),
            "user_state": region.get("user_state"), "geometry_ref": region.get("geometry_ref")})
    for reason in body.get("cell_reasons") or []:
        offsets = reason.get("offset_steps") or {}
        rows.append({
            "kind": "cell_reason", "id": reason["reason"], "category": reason["category"],
            "category_words": reason["category_words"], "subtype": reason["reason"],
            "subtype_words": reason["words"],
            "action": "exclude" if reason["n_excluded"] else "warn",
            "level": reason.get("level"), "tool": reason.get("tool"),
            "tool_version": reason.get("tool_version"), "score": None,
            "score_kind": reason.get("measurement"), "threshold": None,
            "threshold_source": reason.get("threshold_source"),
            "offset_steps": _joined(f"{k}:{v}" for k, v in offsets.items()),
            "channels": _joined(reason.get("channels")), "cycles": "",
            "ai_verdict": _joined(f"{k}:{v}" for k, v in (reason.get("verdicts") or {}).items()),
            "ai_confidence": None, "ai_notes": reason.get("notes"),
            "n_cells": reason["n_excluded"] + reason["n_warned"],
            "n_excluded": reason["n_excluded"], "n_warned": reason["n_warned"],
            "cells_source": reason.get("cells_source"), "created_by": None,
            "user_state": None, "geometry_ref": None})
    for marker in body.get("marker_reasons") or []:
        rows.append({
            "kind": "marker_reason", "id": f"{marker.get('marker')}|{marker['reason']}",
            "category": marker["category"], "category_words": marker["category_words"],
            "subtype": marker["reason"], "subtype_words": schemas.REASON_WORDS.get(
                marker["reason"], marker["reason"]),
            "action": marker.get("status"), "level": "marker", "tool": marker.get("tool"),
            "tool_version": None, "score": None, "score_kind": marker.get("test"),
            "threshold": None, "threshold_source": None, "offset_steps": None,
            "channels": marker.get("marker") or "", "cycles": "",
            "ai_verdict": _joined(f"{k}:{v}" for k, v in (marker.get("verdicts") or {}).items()),
            "ai_confidence": None, "ai_notes": None, "n_cells": marker["n_flagged"], "n_excluded": None,
            "n_warned": None, "cells_source": marker.get("cells_source"), "created_by": None,
            "user_state": None,
            "geometry_ref": f"qc_regions.geojson#{marker['roi_id']}"
            if marker.get("roi_id") else None})
    return rows


# -- one cell ---------------------------------------------------------------------
#
# What the viewer's hover card says of the cell under the pointer: why it was
# flagged, on what value, against which bar -- the cell's own row of
# `qc_cells` read against the result's evidence. The card shows a few lines of
# it; the panel and the exports keep the rest.

#: The `qc_cells` column each cell module's value is stored in (`cells/calls.py`
#: writes a module's `m_*` measures).
MODULE_MEASURE = {"seg_under": "m_seg_under", "seg_over": "m_seg_over",
                  "seg_size": "m_seg_size", "seg_shape": "m_seg_shape"}

#: The side of a module's cutoffs each reason lies beyond.
REASON_SIDE = {"seg_under": "high", "seg_over": "high", "seg_small": "low",
               "seg_large": "high", "seg_irregular": "low"}


def _finite(value):
    import math

    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def measure_of(module, reason=None) -> str | None:
    """The `qc_cells` column a module's value is in."""
    if not module:
        return None
    return MODULE_MEASURE.get(module)


def _region_brief(region, fraction=None):
    region = region or {}
    out = {k: region.get(k) for k in ("roi_id", "name", "class", "class_words", "category",
                                      "category_words", "color", "action", "created_by")}
    if fraction is not None:
        out["fraction"], out["method"] = fraction
    return out


def cell_record(result, row, *, cell_id=None, regions=None, fractions=None, segqc=None,
                class_colors=None, reason_colors=None) -> dict:
    """One cell's QC record: {cell_id, calls, pass, action, primary_reason,
    reason_count, reasons, markers, regions, segqc, unreliable_markers,
    roi_method}.

    `row` is the cell's `qc_cells` row (None: QC flagged nothing on it).
    `regions` {roi_id: {roi_id, name, class, class_words, category,
    category_words, color, action}} are the live regions, `fractions`
    {roi_id: (fraction, method)} how much of the cell each holds, `segqc` the
    cell's Segmentation QC row (or None). Each reason names the value that
    crossed the bar and the bar itself (`value`, `cutoff`, `side`, in the
    cutoffs' `space`), the channels it was read on, the agent's verdict on
    that side and its notes; a region reason names the regions behind it
    (`via_regions`)."""
    from plexora.plugins.qc.server import viewer_data

    result = result or {}
    cells = result.get("cells") or {}
    evidence = cells.get("evidence") or {}
    modules = cells.get("modules") or {}
    regions = regions or {}
    fractions = fractions or {}
    row = row or {}
    cell_id = int(row.get("cell_id") if row.get("cell_id") is not None else cell_id)
    roi_ids = [r for r in row.get("roi_ids") or [] if r]
    excluded = set(row.get("excluded_by") or [])
    legacy = "excluded_by" not in row
    order = {r: i for i, r in enumerate(schemas.PRIMARY_ORDER)}
    primary = row.get("primary_reason") or ""
    reasons = sorted(row.get("reasons") or [], key=lambda r: (
        0 if r == primary else 1, order.get(r, 999), r))

    def status_of(reason):
        if not legacy:
            return "fail" if reason in excluded else "warn"
        return "fail" if row.get("action") == "exclude" and reason == primary else "warn"

    out_reasons = []
    for reason in reasons:
        ev = evidence.get(reason) or {}
        region = reason.startswith("region:")
        module = ev.get("module")
        entry = modules.get(module) or {}
        decision = entry.get("decision") or {}
        side = None if region else REASON_SIDE.get(reason)
        cutoffs = ev.get("cutoffs") or {}
        measure = None if region else measure_of(module, reason)
        category = schemas.category_of_reason(reason)
        record = {
            "reason": reason, "words": viewer_data.reason_words(reason),
            "definition": schemas.REASON_DEFINITIONS.get(reason, ""),
            "category": category, "category_words": schemas.category_words(category),
            "color": viewer_data.reason_color(reason, class_colors, reason_colors),
            "status": status_of(reason), "level": ev.get("level") or "cell",
            "tool": module or ("regions" if region else None),
            "channels": list(ev.get("channels") or []),
            "source_label": ev.get("column"), "measure": measure,
            "value": _finite(row.get(measure)) if measure else None,
            "side": side, "cutoff": _finite(cutoffs.get(side)) if side else None,
            "space": cutoffs.get("space"),
            "offset_steps": (ev.get("offsets") or {}).get(side) if side else None,
            "threshold_source": None if region else (ev.get("threshold_source")
                                                     or _module_source(entry)),
            "verdict": (decision.get(side) or {}).get("verdict")
            if side and isinstance(decision.get(side), dict) else None,
            "notes": None if region else notes_text(entry.get("notes")),
            "via_regions": []}
        if region:
            klass = reason.split(":", 1)[1]
            record["via_regions"] = [
                _region_brief(regions[r], fractions.get(r)) for r in roi_ids
                if r in regions and regions[r].get("class") == klass]
        out_reasons.append(record)

    marker_evidence = cells.get("marker_evidence") or []
    markers = []
    for flag in row.get("marker_flags") or []:
        parts = str(flag).split("|", 2)
        if len(parts) != 3:
            continue
        marker, reason, status = parts
        entry = {"marker": marker, "reason": reason,
                 "category": schemas.category_of_reason(reason),
                 "words": viewer_data.reason_words(reason),
                 "definition": schemas.MARKER_REASON_DEFINITIONS.get(reason, ""),
                 "color": viewer_data.reason_color(reason, class_colors, reason_colors),
                 "status": viewer_data.MARKER_STATUS.get(status, status),
                 "value": None, "cutoff": None, "space": None, "via_regions": []}
        if reason.startswith("region:"):
            rois = {e.get("roi_id") for e in marker_evidence
                    if e.get("marker") == marker and e.get("reason") == reason}
            entry["via_regions"] = [_region_brief(regions[r], fractions.get(r))
                                    for r in roi_ids if r in rois and r in regions]
        markers.append(entry)

    return {"cell_id": cell_id, "calls": True,
            "pass": bool(row.get("pass", True)),
            "action": row.get("action") or "pass",
            "primary_reason": primary or None,
            "reason_count": len(out_reasons),
            "reasons": out_reasons, "markers": markers,
            "regions": [_region_brief(regions[r], fractions.get(r))
                        for r in roi_ids if r in regions],
            "segqc": segqc,
            "unreliable_markers": list(row.get("unreliable_markers") or []),
            "roi_method": row.get("roi_method")}
