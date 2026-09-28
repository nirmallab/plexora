"""What an external agent may ask the QC plugin to do.

Two halves with one store. The session tools (`capabilities_session.py`) and
the analysis tools here (`ai:qc:analytics`) are Paid; everything that reads,
changes or exports what a session -- or a user drawing QC regions by hand --
produced is Free, and stays usable after a licence lapses:

    get_qc_results       the active result: channels, regions, cells, provenance
    list_qc_results      every result this project has had
    activate_qc_result   make an earlier result the active one (receipted)
    set_qc_strictness    lenient / standard / strict / custom: every region's
                         action and every cell's call re-derived, no packet
    approve_qc_roi       pin a region's action (and lock it)
    set_qc_cycles        say which channels were imaged together
    export_qc            CSV / GeoJSON / JSON files
    write_qc_to_source   the calls into the user's own AnnData / table file
    reset_qc, restore_qc clear QC (snapshotting first) and put it back
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import Field

from plexora.agent.errors import AgentError
from plexora.agent.limits import MAX_IDS, MAX_LIST
from plexora.agent.receipts import make_receipt
from plexora.agent.registry import Capability
from plexora.agent.schemas import AgentModel, ProjectInput

OWNER = "qc"
STATE = "plugin_store:qc"
TAGS = ("qc", "quality", "artifact", "exclude", "regions", "cells", "control")


def _results():
    from plexora.plugins.qc.server import results

    return results


def _open_session_on(project):
    from plexora.plugins.qc.server import schemas
    from plexora.plugins.qc.server.engine import store

    for record in store().list(limit=50):
        if project in (record.get("images") or []) and \
                record.get("state") not in schemas.FINISHED_STATES:
            return record.get("session_id")
    return None


# -- reading -----------------------------------------------------------------------------


class ResultsInput(ProjectInput):
    result_id: str | None = Field(None, description="Default: the active result.")
    include_cells: bool = Field(False, description="Also list failing cell ids (bounded).")
    include_regions: bool = Field(True, description="Every QC region with its class and "
                                  "action.")
    max_ids: int = Field(200, ge=0, le=MAX_IDS)


def get_results(call, inp):
    from plexora.plugins.qc.server import roi_link

    results = _results()
    ds = call.session.image_data(inp.project)
    document = results.load(inp.project)
    sync = roi_link.sync(ds, document, save=False)
    result = results.get_result(inp.project, document, inp.result_id) if inp.result_id \
        else results.active(document)
    out = {"project": inp.project, "revision": results.revision(inp.project),
           "active_result_id": document.get("active_result_id"),
           "strictness": document.get("strictness"), "sync": sync,
           "cycles_override": document.get("cycles_override")}
    if result is None:
        out["result"] = None
        out["note"] = "no QC result yet: run a QC session or draw regions in a QC: category"
        return out
    out["summary"] = results.summary(result)
    out["channels"] = [{k: c.get(k) for k in ("name", "cycle", "status", "flags", "reason")}
                       for c in result.get("channels") or []][:MAX_LIST]
    if inp.include_regions:
        regions = []
        for candidate in (result.get("candidates") or {}).values():
            user = candidate.get("user_state") or {}
            regions.append({k: candidate.get(k) for k in (
                "id", "roi_id", "class", "action", "scope", "channels", "cycles", "severity",
                "detector", "created_by", "state")}
                | {"tissue_fraction": (candidate.get("measurement") or {}).get(
                    "tissue_fraction"),
                   "action_by_strictness": candidate.get("action_by_strictness"),
                   "user": {k: v for k, v in user.items() if v}})
        out["regions"] = regions[:MAX_LIST]
        out["regions_truncated"] = len(regions) > MAX_LIST
    out["cells"] = {k: v for k, v in (result.get("cells") or {}).items() if k != "modules"}
    out["cell_modules"] = {name: {k: entry.get(k) for k in ("state", "reason", "decision",
                                                             "cutoffs")}
                           for name, entry in ((result.get("cells") or {}).get("modules")
                                               or {}).items()}
    if inp.include_cells:
        cells = results.cells(inp.project)
        if cells is not None and cells.height:
            failing = cells.filter(~cells["pass"])
            out["failing_cell_ids"] = failing["cell_id"].head(inp.max_ids).to_list()
            out["failing_truncated"] = failing.height > inp.max_ids
    out["provenance"] = {k: result.get(k) for k in (
        "result_id", "session_id", "created_at", "finished_at", "detector_versions",
        "scan_version", "scan_fingerprint", "cycles_method", "agent", "software_version",
        "mode", "origin")}
    out["residual"] = result.get("residual")
    out["warnings"] = (result.get("warnings") or [])[-20:]
    return out


def list_results(call, inp):
    results = _results()
    document = results.load(inp.project)
    rows = [{"result_id": r["result_id"], "session_id": r.get("session_id"),
             "created_at": r.get("created_at"), "finished_at": r.get("finished_at"),
             "origin": r.get("origin"), "rolled_back": bool(r.get("rolled_back")),
             "active": r["result_id"] == document.get("active_result_id"),
             "summary": results.summary(r)}
            for r in (document.get("results") or {}).values()]
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return {"project": inp.project, "results": rows, "archived": document.get("archived") or [],
            "revision": results.revision(inp.project, document)}


# -- changing ----------------------------------------------------------------------------


class StrictnessInput(ProjectInput):
    preset: Literal["lenient", "standard", "strict", "custom"]
    custom_thresholds: dict[str, float] | None = Field(
        None, description="With preset=custom: strictness keys, each within the "
                          "lenient..strict band.")
    expected_revision: str | None = None


def apply_strictness(call, project, preset, custom):
    """Re-derive every region's action and every cell's call under a preset;
    returns (before_rev, after_rev, renamed, skipped, previous)."""
    from plexora.plugins.qc.server import roi_link, strictness
    from plexora.plugins.qc.server.cells import calls

    results = _results()
    table = strictness.thresholds(preset, custom if preset == "custom" else None)
    ds = call.session.image_data(project)
    renamed, skipped = [], []
    with results.lock(project):
        document = results.load(project)
        before = results.revision(project, document)
        previous = dict(document.get("strictness") or {})
        sync = roi_link.sync(ds, document)
        result = results.active(document)
        if result is not None:
            meta_rows = []
            for candidate in (result.get("candidates") or {}).values():
                user = candidate.get("user_state") or {}
                if not candidate.get("roi_id") or user.get("deleted") or \
                        user.get("removed_from_qc"):
                    continue
                action = strictness.action_for(candidate, table)
                if action == candidate.get("action"):
                    continue
                if roi_link.user_wins(candidate) and not user.get("approved_action"):
                    skipped.append({"roi_id": candidate["roi_id"], "why": "the user's region"})
                    continue
                try:
                    roi_link.rename(ds, candidate["roi_id"], action, candidate)
                except Exception as exc:
                    skipped.append({"roi_id": candidate["roi_id"], "why": str(exc)})
                    continue
                renamed.append({"roi_id": candidate["roi_id"], "from": candidate.get("action"),
                                "to": action})
                candidate["action"] = action
                meta_rows.append(candidate)
            if meta_rows:
                meta = results.roi_meta(project)
                rows = {r["roi_id"]: r for r in meta.to_dicts()} if meta.height else {}
                updates = []
                for candidate in meta_rows:
                    row = rows.get(candidate["roi_id"])
                    if row is not None:
                        row["action"] = candidate["action"]
                        row["strictness_used"] = preset
                        updates.append(row)
                results.upsert_roi_meta(project, updates)
            result["strictness"] = {"preset": preset,
                                    "thresholds": custom if preset == "custom" else None}
            results.put_result(document, result)
        document["strictness"] = {"preset": preset,
                                  "thresholds": custom if preset == "custom" else None}
        after = results.save(project, document)
    if renamed:
        roi_link.tell_roi_panel(call, project, "update")
    if result is not None and call.session.project(project).has_table:
        calls.write_for_active(call, project, refresh_regions=any(
            sync.get(k) for k in ("adopted", "edited", "deleted", "relabelled", "removed")))
        after = results.revision(project)
    return before, after, renamed, skipped, previous


def set_strictness(call, inp):
    session_id = _open_session_on(inp.project)
    if session_id:
        raise AgentError("conflict", "a QC session is open on this project; finish it before "
                         "changing the strictness", detail={"session_id": session_id},
                         retryable=True)
    results = _results()
    if inp.expected_revision is not None and \
            results.revision(inp.project) != inp.expected_revision:
        raise AgentError("conflict", "the QC results changed since they were read",
                         detail={"current_revision": results.revision(inp.project)},
                         retryable=True)
    before, after, renamed, skipped, previous = apply_strictness(
        call, inp.project, inp.preset, inp.custom_thresholds)
    receipt = make_receipt(
        call, changed=before != after, before=previous,
        after={"preset": inp.preset, "thresholds": inp.custom_thresholds},
        revision_before=before, revision_after=after, persistent_state=STATE,
        undo_hint={"tool": "set_qc_strictness", "arguments": {
            "project": inp.project, "preset": previous.get("preset") or "standard",
            "custom_thresholds": previous.get("thresholds"), "expected_revision": after}})
    document = results.load(inp.project)
    return {"receipt": receipt.model_dump(mode="json"), "renamed": renamed,
            "skipped": skipped, "summary": results.summary(results.active(document))}


class ActivateInput(ProjectInput):
    result_id: str
    expected_revision: str | None = None


def activate_result(call, inp):
    from plexora.plugins.qc.server.cells import calls

    results = _results()
    with results.lock(inp.project):
        document = results.load(inp.project)
        before = results.revision(inp.project, document)
        if inp.expected_revision is not None and before != inp.expected_revision:
            raise AgentError("conflict", "the QC results changed since they were read",
                             detail={"current_revision": before}, retryable=True)
        found = results.get_result(inp.project, document, inp.result_id)
        if found is None:
            raise AgentError("invalid_input", f"no QC result {inp.result_id!r}",
                             detail={"results": sorted(document.get("results") or {})})
        previous = document.get("active_result_id")
        results.put_result(document, found, activate=True)
        after = results.save(inp.project, document)
    if call.session.project(inp.project).has_table:
        calls.write_for_active(call, inp.project)
        after = results.revision(inp.project)
    receipt = make_receipt(
        call, changed=previous != inp.result_id, before={"active_result_id": previous},
        after={"active_result_id": inp.result_id}, revision_before=before,
        revision_after=after, persistent_state=STATE,
        reversible=previous is not None,
        undo_hint={"tool": "activate_qc_result", "arguments": {
            "project": inp.project, "result_id": previous, "expected_revision": after}}
        if previous else None)
    return {"receipt": receipt.model_dump(mode="json"), "active_result_id": inp.result_id}


class ApproveInput(ProjectInput):
    roi_id: str
    action: Literal["exclude", "warn", "ignore"] | None = Field(
        None, description="Pin this action whatever the strictness (default: its current "
                          "one).")
    lock: bool = Field(True, description="Also lock the region's shape in the ROI panel.")


def approve_roi(call, inp):
    from plexora.plugins.qc.server import roi_link
    from plexora.plugins.qc.server.cells import calls
    from plexora.plugins.roi.server import service

    results = _results()
    ds = call.session.image_data(inp.project)
    with results.lock(inp.project):
        document = results.load(inp.project)
        before = results.revision(inp.project, document)
        found = roi_link.sync(ds, document)
        result = results.active(document)
        candidate = next((c for c in ((result or {}).get("candidates") or {}).values()
                          if c.get("roi_id") == inp.roi_id), None)
        if candidate is None:
            raise AgentError("invalid_input", f"{inp.roi_id!r} is not a QC region of the "
                             "active result")
        action = inp.action or candidate.get("action") or "exclude"
        user = candidate.setdefault("user_state", {})
        previous = {"approved": bool(user.get("approved")),
                    "approved_action": user.get("approved_action"),
                    "action": candidate.get("action")}
        user.update(approved=True, approved_action=action)
        if action != candidate.get("action"):
            roi_link.rename(ds, inp.roi_id, action, candidate)
            candidate["action"] = action
        if inp.lock:
            service.update_roi(ds, inp.roi_id, locked=True)
            user["locked"] = True
        meta = results.roi_meta(inp.project)
        rows = [r for r in meta.to_dicts() if r["roi_id"] == inp.roi_id] if meta.height else []
        for row in rows:
            row.update(approved=True, approved_action=action, action=action,
                       locked=bool(inp.lock) or bool(row.get("locked")))
        results.upsert_roi_meta(inp.project, rows)
        results.put_result(document, result)
        after = results.save(inp.project, document)
    if call.session.project(inp.project).has_table:
        calls.write_for_active(call, inp.project, refresh_regions=any(
            found.get(k) for k in ("adopted", "edited", "deleted", "relabelled", "removed")))
        after = results.revision(inp.project)
    receipt = make_receipt(call, changed=True, before=previous,
                           after={"approved": True, "approved_action": action,
                                  "locked": inp.lock},
                           revision_before=before, revision_after=after,
                           persistent_state=STATE, reversible=False)
    return {"receipt": receipt.model_dump(mode="json"), "roi_id": inp.roi_id,
            "action": action}


class CyclesInput(ProjectInput):
    cycles: list[list[str]] | None = Field(None, description="Channel names, one list per "
                                           "cycle, in imaging order.")
    period: int | None = Field(None, ge=1, le=100, description="Or: every N channels is one "
                               "cycle.")
    clear: bool = Field(False, description="Forget the override; infer the cycles again.")


def set_cycles(call, inp):
    results = _results()
    if not inp.clear and not inp.cycles and not inp.period:
        raise AgentError("invalid_input", "give cycles, period, or clear=true")
    record = call.session.project(inp.project)
    names = [c.get("fullname") or c.get("name") for c in record.image.real_channels]
    if inp.cycles:
        unknown = sorted({n for group in inp.cycles for n in group} - set(names))
        if unknown:
            raise AgentError("invalid_input", f"not channels of this image: {unknown}",
                             detail={"channels": names[:100]})
    with results.lock(inp.project):
        document = results.load(inp.project)
        before = results.revision(inp.project, document)
        previous = document.get("cycles_override")
        document["cycles_override"] = None if inp.clear else (
            {"cycles": inp.cycles} if inp.cycles else {"period": inp.period})
        after = results.save(inp.project, document)
    from plexora.plugins.qc.server import cycles as cycle_rules

    inferred = cycle_rules.infer(names, override=document["cycles_override"])
    receipt = make_receipt(
        call, changed=previous != document["cycles_override"], before=previous,
        after=document["cycles_override"], revision_before=before, revision_after=after,
        persistent_state=STATE, reversible=True,
        undo_hint={"tool": "set_qc_cycles", "arguments": {
            "project": inp.project, **({"clear": True} if previous is None else
                                       previous)}})
    return {"receipt": receipt.model_dump(mode="json"), "cycles": inferred["cycles"],
            "method": inferred["method"],
            "note": "the next QC session scans with these cycles"}


def refresh(call, inp):
    """Take in the user's edits in the ROI panel (new QC regions, reshaped or
    deleted ones) and recompute the cells' calls from them."""
    from plexora.plugins.qc.server import roi_link
    from plexora.plugins.qc.server.cells import calls

    results = _results()
    ds = call.session.image_data(inp.project)
    with results.lock(inp.project):
        document = results.load(inp.project)
        before = results.revision(inp.project, document)
        report = roi_link.sync(ds, document)
        if results.active(document) is not None:
            results.put_result(document, results.active(document))
        results.save(inp.project, document)
    cells = None
    if call.session.project(inp.project).has_table:
        cells = calls.write_for_active(call, inp.project)
    after = results.revision(inp.project)
    receipt = make_receipt(call, changed=before != after, before={"revision": before},
                           after={"sync": report, "cells": cells}, revision_before=before,
                           revision_after=after, persistent_state=STATE, reversible=False)
    return {"receipt": receipt.model_dump(mode="json"), "sync": report, "cells": cells,
            "summary": results.summary(results.active(results.load(inp.project)))}


# -- files -----------------------------------------------------------------------------


class ExportInput(ProjectInput):
    what: Literal["rois", "cells", "both"] = "both"
    result_id: str | None = None


def export_qc(call, inp):
    from plexora.plugins.qc.server import export

    return export.export(call, inp.project, what=inp.what, result_id=inp.result_id)


class WriteSourceInput(ProjectInput):
    confirm: Literal[True] = Field(description="Must be true, and only once the user has "
                                   "explicitly asked for the QC calls to be written into "
                                   "their file.")
    replace: bool = Field(False, description="Overwrite QC columns an earlier write left "
                          "(never anything else).")


def write_source(call, inp):
    from plexora.plugins.qc.server import source_write

    ds = call.data
    try:
        written = source_write.write(ds, replace=inp.replace)
    except source_write.Exists as exc:
        raise AgentError("conflict", "the file already holds QC columns; pass replace=true "
                         "to overwrite them", detail={"existing": exc.existing}) from exc
    except LookupError as exc:
        raise AgentError("precondition_missing", str(exc)) from exc
    except (OverflowError, ValueError) as exc:
        raise AgentError("invalid_input", str(exc)) from exc
    source = ds.table.source
    receipt = make_receipt(call, changed=True, before=None, after=written,
                           persistent_state="source_file", source_file_modified=True,
                           source_path=source.path if source else None, reversible=False)
    return {"receipt": receipt.model_dump(mode="json"), "written": written}


# -- reset / restore -----------------------------------------------------------------------


class ResetInput(ProjectInput):
    confirm: Literal[True] = Field(description="Must be true.")
    delete_agent_rois: bool = Field(True, description="Also delete the regions QC wrote that "
                                    "nobody edited, approved or locked.")
    expected_revision: str | None = None


def reset(call, inp):
    from plexora.plugins.qc.server import roi_link
    from plexora.plugins.roi.server.repository import ROIRepository

    results = _results()
    session_id = _open_session_on(inp.project)
    if session_id:
        raise AgentError("conflict", "a QC session is open on this project",
                         detail={"session_id": session_id}, retryable=True)
    ds = call.session.image_data(inp.project)
    with results.lock(inp.project):
        document = results.load(inp.project)
        before = results.revision(inp.project, document)
        if inp.expected_revision is not None and before != inp.expected_revision:
            raise AgentError("conflict", "the QC results changed since they were read",
                             detail={"current_revision": before}, retryable=True)
        roi_link.sync(ds, document)
        deleted, kept = [], []
        if inp.delete_agent_rois:
            repo = ROIRepository(ds.name)
            state = repo.load()
            features = {f["id"]: f for f in roi_link._features(state)}
            for candidate in ((results.active(document) or {}).get("candidates") or {}).values():
                roi_id = candidate.get("roi_id")
                if not roi_id or roi_id not in features:
                    continue
                if roi_link.user_wins(candidate) or features[roi_id].get("locked"):
                    kept.append(roi_id)
                    continue
                deleted.append(features[roi_id])
        label = f"reset_{call.operation_id}"
        snapshot = results.snapshot(inp.project, label)
        body = results.read_snapshot(snapshot)
        body["deleted_rois"] = deleted
        Path(snapshot).write_text(json.dumps({k: v for k, v in body.items() if k != "frames"},
                                             default=str), encoding="utf-8")
        if deleted:
            repo = ROIRepository(ds.name)
            state = repo.load()
            repo.apply(state["revision"], [{"op": "roi.bulk_delete",
                                            "ids": [f["id"] for f in deleted]}])
        document["active_result_id"] = None
        after = results.save(inp.project, document)
        import polars as pl

        results.put_table(inp.project, "qc_cells", pl.DataFrame({"cell_id": pl.Series(
            [], dtype=pl.Int64)}))
        results.put_table(inp.project, "qc_cell_rois", pl.DataFrame(
            schema=results.CELL_ROIS_SCHEMA))
        results.drop_roi_meta(inp.project, [f["id"] for f in deleted])
        after = results.revision(inp.project)
    receipt = make_receipt(call, changed=True, before={"revision": before},
                           after={"deleted_rois": [f["id"] for f in deleted], "kept": kept},
                           revision_before=before, revision_after=after,
                           persistent_state=STATE,
                           undo_hint={"tool": "restore_qc", "arguments": {
                               "project": inp.project, "snapshot": snapshot,
                               "expected_revision": after}})
    return {"receipt": receipt.model_dump(mode="json"), "snapshot": snapshot,
            "deleted_rois": [f["id"] for f in deleted], "kept_rois": kept}


class RestoreInput(ProjectInput):
    snapshot: str
    expected_revision: str | None = None


def restore(call, inp):
    from plexora.plugins.qc.server import roi_link
    from plexora.plugins.roi.server.repository import ROIRepository

    results = _results()
    body = results.read_snapshot(inp.snapshot)
    ds = call.session.image_data(inp.project)
    with results.lock(inp.project):
        before = results.revision(inp.project)
        if inp.expected_revision is not None and before != inp.expected_revision:
            raise AgentError("conflict", "the QC results changed since the reset",
                             detail={"current_revision": before}, retryable=True)
        deleted = body.get("deleted_rois") or []
        if deleted:
            repo = ROIRepository(ds.name)
            state = repo.load()
            existing = {f["id"] for f in roi_link._features(state)}
            missing = [f for f in deleted if f["id"] not in existing]
            if missing:
                repo.apply(state["revision"], [{"op": "roi.bulk_create",
                                                "features": missing}])
        results.restore_snapshot(inp.project, body)
        after = results.revision(inp.project)
    receipt = make_receipt(call, changed=True, before={"revision": before},
                           after={"revision": after, "restored_rois": len(deleted)},
                           revision_before=before, revision_after=after,
                           persistent_state=STATE, reversible=False)
    return {"receipt": receipt.model_dump(mode="json"), "restored_rois": len(deleted)}


# -- analysis (Paid) ------------------------------------------------------------------------


class ProfileInput(ProjectInput):
    detectors: list[str] | None = None
    map_cell_um: float | None = Field(None, ge=5.0, le=500.0)


def profile_image(call, inp):
    """The scan and the detectors, without a session: every channel's numbers
    and the candidates a session would pursue."""
    from plexora.plugins.qc.server import candidates as cand
    from plexora.plugins.qc.server import scan as scanmod
    from plexora.plugins.qc.server.detectors import DetectorContext, run_all

    results = _results()
    override = results.load(inp.project).get("cycles_override")
    params = {"cell_um": float(inp.map_cell_um)} if inp.map_cell_um else None
    result, reused = scanmod.load_or_run(call.session, inp.project, params=params,
                                         override=override,
                                         progress=lambda **k: call.progress(**k),
                                         cancelled=call.cancelled)
    raw, skipped = run_all(DetectorContext(result, project=inp.project),
                           enabled=inp.detectors)
    built = cand.build(raw, result, project=inp.project)
    from plexora.plugins.qc.server.scan import bbox_fullres

    return {"project": inp.project, "reused_scan": reused, "grid": result.grid,
            "tissue": result.meta.get("tissue"), "cycles": result.meta.get("cycles"),
            "cross_cycle": {k: v for k, v in (result.meta.get("cross_cycle") or {}).items()
                            if k != "cycles"},
            "channels": [{"name": c["name"], "cycle": c.get("cycle"), "flags": c["flags"],
                          "summary": {k: v for k, v in (c.get("summary") or {}).items()
                                      if k != "seams"}} for c in result.channels],
            "candidates": [{"id": c.id, "class_hint": c.class_hint, "channels": list(c.channels),
                            "scope_hint": c.scope_hint, "severity": c.severity,
                            "detector": c.detector, "alternatives": c.alternatives,
                            "bbox": bbox_fullres(result.grid, c.mask),
                            "tissue_fraction": cand.area_fraction(c.mask, result)}
                           for c in built["ranked"]][:MAX_LIST],
            "residual": built["residual"], "skipped_detectors": skipped}


class OverviewInput(ProjectInput):
    channels: list[str] | None = Field(None, max_length=8, description="Up to eight "
                                       "channels (default: the first eight).")
    format: Literal["webp", "png"] = "png"


def render_overview(call, inp):
    from plexora.agent.core.visual import with_image
    from plexora.agent.evidence import calibration
    from plexora.plugins.qc.server import scan as scanmod
    from plexora.plugins.qc.server import sheets
    from plexora.server.utils import pixel_scale

    results = _results()
    result, _reused = scanmod.load_or_run(call.session, inp.project,
                                          override=results.load(inp.project).get(
                                              "cycles_override"))
    names = [c["name"] for c in result.channels]
    chosen = [n for n in (inp.channels or names) if n in names][:8]
    rows = [{"number": names.index(n) + 1, "channel": n,
             "cycle": result.channel(n).get("cycle"), "flags": result.channel(n)["flags"],
             "candidates": []} for n in chosen]
    record = calibration.current(call.session, inp.project, names)
    rendered = sheets.audit_sheet(call.session, inp.project, result, rows, index=1, total=1,
                                  fmt="png", pixel=pixel_scale.pixel_size(
                                      call.session.project(inp.project)),
                                  calibration=record)
    return with_image({"manifest": rendered["manifest"], "artifact": rendered["artifact"]},
                      rendered["image"])


# -- the table -------------------------------------------------------------------------------


def capabilities():
    from plexora.plugins.qc import capabilities_session

    def free(**kwargs):
        kwargs.setdefault("tags", TAGS)
        return Capability(owner=OWNER, version="1", **kwargs)

    def paid(**kwargs):
        kwargs.setdefault("tags", TAGS)
        kwargs.setdefault("entitlement", "ai:qc:analytics")
        return Capability(owner=OWNER, version="1", **kwargs)

    return [
        *capabilities_session.capabilities(),
        free(name="qc.get_results", tool_name="get_qc_results",
             purpose="A project's QC: every channel's status, every QC region (class, "
                     "action, who made it, the user's edits), the cells' pass/fail counts by "
                     "reason, the strictness, the provenance. Read this first.",
             permission="read", input_model=ResultsInput, handler=get_results,
             egress="aggregates", reads=("qc", "rois")),
        free(name="qc.list_results", tool_name="list_qc_results",
             purpose="Every QC result this project has had (one per session), newest first.",
             permission="read", input_model=ProjectInput, handler=list_results,
             egress="metadata", reads=("qc",)),
        free(name="qc.activate_result", tool_name="activate_qc_result",
             purpose="Make an earlier QC result the active one (its regions and cell calls).",
             permission="reversible_write", input_model=ActivateInput,
             handler=activate_result, writes=("qc",), persistent=True, reads=("qc",)),
        free(name="qc.set_strictness", tool_name="set_qc_strictness",
             purpose="Change how readily QC excludes: lenient, standard, strict or custom. "
                     "Every region's action and every cell's call is re-derived from what "
                     "the session measured and judged -- nothing is asked again; the user's "
                     "own and approved regions keep theirs.",
             permission="reversible_write", input_model=StrictnessInput,
             handler=set_strictness, writes=("qc", "rois"), persistent=True,
             reads=("qc", "rois", "table")),
        free(name="qc.approve_roi", tool_name="approve_qc_roi",
             purpose="Approve a QC region: pin its action (exclude, warn, ignore) whatever "
                     "the strictness, and lock its shape.",
             permission="reversible_write", input_model=ApproveInput, handler=approve_roi,
             writes=("qc", "rois"), persistent=True, reads=("qc", "rois")),
        free(name="qc.refresh", tool_name="refresh_qc",
             purpose="Take in the user's QC regions from the ROI panel (drawn, reshaped, "
                     "deleted, moved between QC categories) and recompute every cell's call. "
                     "This is manual QC: no session, no licence.",
             permission="reversible_write", input_model=ProjectInput, handler=refresh,
             writes=("qc",), persistent=True, reversible=False, reads=("rois", "table", "mask")),
        free(name="qc.set_cycles", tool_name="set_qc_cycles",
             purpose="Say which channels were imaged together (cycles), when the names do "
                     "not: the next scan checks registration and tissue loss per cycle.",
             permission="reversible_write", input_model=CyclesInput, handler=set_cycles,
             writes=("qc",), persistent=True),
        free(name="qc.export", tool_name="export_qc",
             purpose="Write a project's QC to files: cells.csv (pass, reasons, regions), "
                     "qc_regions.geojson, summary.json, result.json.",
             permission="read", input_model=ExportInput, handler=export_qc,
             egress="aggregates", reads=("qc", "rois"),
             tags=TAGS + ("export", "csv", "geojson", "download")),
        free(name="qc.write_source", tool_name="write_qc_to_source",
             purpose="Write the QC calls into the user's own table: AnnData obs/obsm/uns "
                     "(plexora_qc_*), or columns of a CSV/Parquet. Modifies the user's file: "
                     "needs --allow-source-writes and confirm: true, only on request.",
             permission="source_file_write", input_model=WriteSourceInput,
             handler=write_source, writes=("source_file",), reversible=False,
             source_file_write=True, persistent=True,
             tags=TAGS + ("save", "write", "anndata", "export")),
        free(name="qc.reset", tool_name="reset_qc",
             purpose="Clear a project's QC (snapshotting it first): the active result, the "
                     "cell calls, and the regions QC wrote that nobody edited or locked.",
             permission="reversible_write", input_model=ResetInput, handler=reset,
             writes=("qc", "rois"), persistent=True),
        free(name="qc.restore", tool_name="restore_qc",
             purpose="Put back a QC snapshot (the undo of reset_qc).",
             permission="reversible_write", input_model=RestoreInput, handler=restore,
             writes=("qc", "rois"), persistent=True),
        paid(name="qc.profile_image", tool_name="profile_image_qc",
             purpose="Scan an image for QC without a session: every channel's global "
                     "numbers and flags, the tissue, the cycles, and the candidate artifact "
                     "regions the detectors found. A job.",
             permission="read", input_model=ProfileInput, handler=profile_image,
             execution="job", egress="aggregates", reads=("image",)),
        paid(name="qc.render_overview", tool_name="render_qc_overview",
             purpose="A channel audit sheet: up to eight channels, whole tissue, at the "
                     "project's calibrated windows, beside the nuclear stain.",
             permission="read", input_model=OverviewInput, handler=render_overview,
             visual_output=True, egress="rendered_pixels", reads=("image",)),
    ]
