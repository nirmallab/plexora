"""What an external agent may ask the QC plugin to do.

Two halves with one store. The session tools (`capabilities_session.py`) and
the analysis tools here (`ai:qc:analytics`) are Paid; everything that reads,
changes or exports what a session -- or a user drawing QC regions by hand --
produced is Free, and stays usable after a licence lapses:

    get_qc_results       the active result: channels, regions, cells, provenance
    get_qc_exclusions    which cells estimation leaves out (automatic gating's
                         fits, samples and fields), counted by reason and marker
    list_qc_results      every result this project has had
    activate_qc_result   make an earlier result the active one (receipted)
    set_qc_strictness    lenient / standard / strict / custom: every region's
                         action and every cell's call re-derived, no packet
    approve_qc_roi       pin a region's action (and lock it)
    dismiss_qc_finding   set aside a cell reason, marker flag or channel verdict
    set_qc_cycles        say which channels were imaged together
    export_qc            CSV / GeoJSON / JSON files
    write_qc_to_source   the calls into the user's own AnnData / table file
    reset_qc, restore_qc clear QC (snapshotting first) and put it back

plus the image checks the panel runs, Registration Check and Segmentation QC
(`capabilities_checks.py`), also Free;

and, Paid: `profile_image_qc`, `render_qc_overview`, `sample_qc_examples` (places
sampled across a check's score distribution, one row per part of it, to judge
by eye) and `refine_qc_roi` (trace a QC region's artifact at pixel level inside
its outline).
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
#: Residual-artifact rows `get_qc_results` brief keeps (largest first).
BRIEF_RESIDUAL_ROWS = 10
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
    include_cells: bool = Field(False, description="Also list failing cell ids, and the cells "
                                                   "with an unreliable marker (bounded).")
    include_regions: bool = Field(True, description="Every QC region with its category, "
                                  "subtype (class), action, score, threshold and "
                                  "threshold_source, and the agent's judgment.")
    max_ids: int = Field(200, ge=0, le=MAX_IDS, description="With include_cells: at most "
                         "this many cell ids per list.")
    detail: Literal["brief", "full"] = Field(
        "brief", description="brief: counts, categories, regions, cell reasons and each "
                             "check's bar; full: also every histogram and quantile, each "
                             "region's cell propagation and each call's raw evidence.")


#: What `detail: brief` leaves out, at any depth: the bulk of a large
#: image's result (a 200k-cell, 50-region result was 50k characters).
BULKY = ("distribution", "histogram", "quantiles", "propagation", "evidence",
         "marker_evidence", "field_stats", "summary_by_level")


def _brief(value):
    if isinstance(value, dict):
        return {k: _brief(v) for k, v in value.items() if k not in BULKY}
    if isinstance(value, list):
        return [_brief(v) for v in value]
    return value


def _checks(call, project):
    """The free image checks' state (Registration Check, Segmentation QC,
    Blur QC): kept beside the QC document, never in it, so reading them moves
    no revision."""
    from plexora.plugins.qc.server import blur, registration

    from plexora.plugins.qc.server.segqc import run as segqc

    out = {}
    try:
        out["registration"] = registration.public_state(project, call.session.project(project))
    except AgentError as exc:
        out["registration"] = {"error": exc.to_problem()}
    try:
        out["segmentation"] = segqc.public_status(call.session, project)
    except AgentError as exc:
        out["segmentation"] = {"error": exc.to_problem()}
    try:
        out["blur"] = blur.public_status(call.session, project)
    except AgentError as exc:
        out["blur"] = {"error": exc.to_problem()}
    try:
        from plexora.plugins.qc.server import artifacts

        out["artifacts"] = artifacts.public_status(call.session, project)
    except AgentError as exc:
        out["artifacts"] = {"error": exc.to_problem()}
    return out


def get_results(call, inp):
    from plexora.plugins.qc.server import provenance, roi_link, schemas

    results = _results()
    ds = call.session.image_data(inp.project)
    document = results.load(inp.project)
    sync = roi_link.sync(ds, document, save=False)
    result = results.get_result(inp.project, document, inp.result_id) if inp.result_id \
        else results.active(document)
    out = {"project": inp.project, "revision": results.revision(inp.project),
           "active_result_id": document.get("active_result_id"),
           "strictness": document.get("strictness"), "sync": sync,
           "cycles_override": document.get("cycles_override"),
           "checks": _checks(call, inp.project)}
    if result is None:
        out["result"] = None
        out["note"] = "no QC result yet: run a QC session or draw regions in a QC: category"
        return out
    out["summary"] = results.summary(result)
    out["channels"] = [{k: c.get(k) for k in ("name", "cycle", "status", "flags", "reason",
                                               "reached_by", "user_state") if c.get(k) is not None}
                       for c in result.get("channels") or []][:MAX_LIST]
    if inp.include_regions:
        regions = []
        for candidate in (result.get("candidates") or {}).values():
            user = candidate.get("user_state") or {}
            record = provenance.region_summary(candidate, checks=result.get("checks"))
            klass = candidate.get("class") or "other_technical"
            regions.append({k: candidate.get(k) for k in (
                "id", "roi_id", "class", "action", "scope", "channels", "cycles", "severity",
                "detector", "created_by", "state")}
                | {"category": schemas.category_of_class(klass),
                   "class_words": schemas.CLASS_WORDS.get(klass, klass),
                   "score": record["score"], "score_kind": record["score_kind"],
                   "threshold": record["threshold"],
                   "threshold_source": record["threshold_source"],
                   "offset_steps": record["offset_steps"], "ai_decision": record["ai"],
                   "origin": record["tool"]["origin"],
                   # How the outline was made (schemas.REGION_METHODS).
                   "method": record["tool"]["method"],
                   "tissue_fraction": (candidate.get("measurement") or {}).get(
                    "tissue_fraction"),
                   "refined_fraction": (candidate.get("measurement") or {}).get(
                       "refined_fraction"),
                   "refinement": {k: (candidate.get("refinement") or {}).get(k) for k in (
                       "status", "method", "kept_fraction", "reason")}
                   if candidate.get("refinement") else None,
                   "action_by_strictness": candidate.get("action_by_strictness"),
                   "user": {k: v for k, v in user.items() if v}})
        out["regions"] = regions[:MAX_LIST]
        out["regions_truncated"] = len(regions) > MAX_LIST
    try:
        live = provenance.regions(ds, inp.project, result)
    except Exception:  # an unreadable ROI document: the summary without its regions
        live = []
    reasons = provenance.cell_reason_records(result)
    out["categories"] = provenance.categories_summary(
        live, reasons, provenance.marker_reason_records(result),
        n_cells=int((result.get("cells") or {}).get("n") or 0))
    out["cell_reasons"] = [{k: r.get(k) for k in (
        "reason", "category", "words", "tool", "channels", "cutoffs", "verdicts",
        "offset_steps", "threshold_source", "notes", "n_excluded", "n_warned", "denominator",
        "cells_source")} for r in reasons]
    if result.get("checks"):
        out["session_checks"] = result["checks"]
        from plexora.plugins.qc.server import checks_result

        table = checks_result.registration_table(result)
        if table:
            out["registration"] = table
    out["cells"] = {k: v for k, v in (result.get("cells") or {}).items() if k != "modules"}
    retention = result.get("dna_retention")
    if retention is None:
        # A result without a session's measure: the free tool's current one.
        from plexora.plugins.qc.server import dna_retention as dna_rules

        try:
            retention = dna_rules.current(inp.project)
        except Exception:  # no QC store yet: nothing measured
            retention = None
    if retention:
        from plexora.plugins.qc.server import dna_retention as dna_rules

        out["dna_retention"] = {
            "digest": dna_rules.digest_line(retention),
            **{k: retention.get(k) for k in (
                "reference", "reference_cycle", "reliable_through_cycle", "n_cells",
                "n_nucleated", "n_no_nucleus", "fingerprint", "cycles")}}
    if inp.include_cells:
        cells = results.cells(inp.project)
        if cells is not None and cells.height:
            failing = cells.filter(~cells["pass"])
            out["failing_cell_ids"] = failing["cell_id"].head(inp.max_ids).to_list()
            out["failing_truncated"] = failing.height > inp.max_ids
            if "unreliable_markers" in cells.columns:
                marked = cells.filter(cells["unreliable_markers"].list.len() > 0)
                out["unreliable_marker_cells"] = [
                    {"cell_id": int(r["cell_id"]), "markers": list(r["unreliable_markers"])}
                    for r in marked.head(inp.max_ids).iter_rows(named=True)]
                out["unreliable_marker_truncated"] = marked.height > inp.max_ids
    out["provenance"] = {k: result.get(k) for k in (
        "result_id", "session_id", "created_at", "finished_at", "detector_versions",
        "scan_version", "scan_fingerprint", "cycles_method", "agent", "software_version",
        "mode", "origin")}
    out["residual"] = result.get("residual")
    if result.get("user_dismissed"):
        out["user_dismissed"] = result["user_dismissed"]
    out["warnings"] = (result.get("warnings") or [])[-20:]
    if inp.detail == "brief":
        for key in ("checks", "session_checks", "cells"):
            if key in out:
                out[key] = _brief(out[key])
        # Brief was 58k characters on a 40-channel image (live run lsp11385):
        # candidate ids per channel, every residual row. Counts and states
        # here; `detail="full"` has the rest.
        for channel in out.get("channels") or []:
            if channel.get("reached_by") is not None:
                channel["n_reached_by"] = len(channel.pop("reached_by"))
        residual = out.get("residual") or []
        if len(residual) > BRIEF_RESIDUAL_ROWS:
            out["residual"] = residual[:BRIEF_RESIDUAL_ROWS]
            out["residual_truncated"] = len(residual)
        channels = out.get("channels") or []
        quiet = [c for c in channels if c.get("status") == "clean" and not c.get("flags")
                 and not c.get("user_state")]
        if len(quiet) > 8:
            out["channels"] = [c for c in channels if c not in quiet]
            out["clean_channels"] = [c["name"] for c in quiet]
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


def apply_strictness(call, project, preset, custom, *, expected_revision=None):
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
        # Checked under the lock, so no write can land between the check
        # and this one.
        if expected_revision is not None and before != expected_revision:
            raise AgentError("conflict", "the QC results changed since they were read",
                             detail={"current_revision": before}, retryable=True)
        previous = dict(document.get("strictness") or {})
        sync = roi_link.sync(ds, document)
        result = results.active(document)
        if result is not None:
            meta_rows = []
            for candidate in (result.get("candidates") or {}).values():
                user = candidate.get("user_state") or {}
                if candidate.get("consolidated_into") and not candidate.get("roi_id") \
                        and not user.get("deleted"):
                    # A finding drawn as part of a consolidated ROI: no ROI
                    # of its own to rename, but its cells follow its action.
                    candidate["action"] = strictness.action_for(candidate, table)
                    continue
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
    before, after, renamed, skipped, previous = apply_strictness(
        call, inp.project, inp.preset, inp.custom_thresholds,
        expected_revision=inp.expected_revision)
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
    action: Literal["exclude", "warn", "note", "ignore"] | None = Field(
        None, description="Pin this action whatever the strictness (default: its current "
                          "one). `note` keeps the cells and records them (the Background "
                          "ROI's own); `exclude` on the Background ROI removes its cells.")
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


class ViewChannel(AgentModel):
    name: str = Field(max_length=200)
    color: str | None = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    range: list[float] | None = Field(None, min_length=2, max_length=2)


class ViewChannelState(ViewChannel):
    visible: bool | None = None


class Viewport(AgentModel):
    x: float
    y: float
    width: float = Field(gt=0)
    height: float = Field(gt=0)


class ViewState(AgentModel):
    """Everything on screen when a region was drawn: where the view was, how
    far in, HD mode, and the channels with their colours and windows."""

    sample: str | None = Field(None, max_length=200)
    viewport: Viewport | None = None
    zoom: float | None = None
    hd_mode: bool | None = None
    channels: list[ViewChannelState] = Field(default_factory=list, max_length=MAX_LIST)
    captured_at: str | None = Field(None, max_length=40)


class RefreshInput(ProjectInput):
    views: dict[str, list[ViewChannel] | ViewState] | None = Field(
        None, max_length=MAX_LIST,
        description="{roi_id: view} -- what was on screen when a region was drawn by "
                    "hand: either the channels (name, colour, window), or the whole "
                    "view (viewport, zoom, HD mode, channels). Kept with the region so "
                    "clicking it puts that view back. Only a hand-drawn region's first "
                    "view is kept.")


def _keep_views(result, views):
    """Put each hand-drawn region's drawing-time view on its candidate --
    once: a later refresh never replaces what the region was drawn under.

    A channel list fills `view_channels` (as before); a whole view fills
    `view` too, and `view_channels` mirrors its visible channels so every
    older reader keeps working."""
    if not result or not views:
        return []
    by_roi = {c.get("roi_id"): c for c in (result.get("candidates") or {}).values()
              if c.get("roi_id")}
    kept = []
    for roi_id, given in views.items():
        candidate = by_roi.get(roi_id)
        if candidate is None or candidate.get("created_by") != "user" \
                or candidate.get("view_channels") or candidate.get("view") or not given:
            continue
        if isinstance(given, ViewState):
            view = given.model_dump(exclude_none=True)
            view["channels"] = view.get("channels", [])[:8]
            if not view["channels"] and not view.get("viewport"):
                continue
            candidate["view"] = view
            candidate["view_channels"] = [
                {k: v for k, v in channel.items() if k != "visible"}
                for channel in view["channels"] if channel.get("visible", True)]
        else:
            candidate["view_channels"] = [channel.model_dump(exclude_none=True)
                                          for channel in given[:8]]
        kept.append(roi_id)
    return kept


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
        viewed = _keep_views(results.active(document), getattr(inp, "views", None))
        if viewed:
            report["viewed"] = viewed
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


class DismissInput(ProjectInput):
    finding: Literal["cell_reason", "marker", "channel"] = Field(
        description="What was wrong: a whole-cell reason (every cell it flagged or "
                    "excluded), one marker's reason (that marker's value kept in those "
                    "cells), or a channel's audit verdict (the channel called clean).")
    reason: str | None = Field(None, description="The cell or marker reason, as "
                               "get_qc_results names it (cell_reason, marker).")
    marker: str | None = Field(None, description="The marker (marker).")
    channel: str | None = Field(None, description="The channel's name (channel).")
    restore: bool = Field(False, description="Put a dismissed finding back.")


def _dismissal_key(inp):
    if inp.finding == "channel":
        if not inp.channel:
            raise AgentError("invalid_input", "a channel finding needs `channel`")
        return {"finding": "channel", "channel": inp.channel}
    if not inp.reason:
        raise AgentError("invalid_input", f"a {inp.finding} finding needs `reason`")
    if inp.reason.startswith("region:") and inp.finding == "cell_reason":
        raise AgentError("invalid_input", "a region's cells follow the region: delete the "
                         "region (delete_roi) or change its action (approve_qc_roi) instead",
                         detail={"reason": inp.reason})
    if inp.finding == "marker":
        if not inp.marker:
            raise AgentError("invalid_input", "a marker finding needs `marker`")
        return {"finding": "marker", "reason": inp.reason, "marker": inp.marker}
    return {"finding": "cell_reason", "reason": inp.reason}


def dismiss_finding(call, inp):
    """The user's "this is wrong": a cell reason, a marker's flag or a channel's
    verdict set aside in the active result -- recorded, never deleted, so the
    provenance keeps what QC found and who overruled it."""
    from plexora.plugins.qc.server.cells import calls

    results = _results()
    key = _dismissal_key(inp)
    with results.lock(inp.project):
        document = results.load(inp.project)
        before = results.revision(inp.project, document)
        result = results.active(document)
        if result is None:
            raise AgentError("not_found", "no QC result yet")
        dismissed = result.setdefault("user_dismissed", [])
        same = [d for d in dismissed if {k: d.get(k) for k in key} == key]
        if inp.finding == "channel":
            channel = next((c for c in result.get("channels") or []
                            if c.get("name") == inp.channel), None)
            if channel is None:
                raise AgentError("invalid_input", f"{inp.channel!r} is not a channel of the "
                                 "active result")
            if inp.restore:
                for entry in same:
                    channel["status"] = entry.get("was") or channel.get("status")
                channel.pop("user_state", None)
            elif not same:
                key["was"] = channel.get("status")
                channel["status"] = "clean"
                channel["user_state"] = {"dismissed": True, "was": key["was"], "by": "user",
                                         "at": results.now_iso()}
        elif not inp.restore and not same:
            known = set((result.get("cells") or {}).get("by_reason") or {}) | \
                set((result.get("cells") or {}).get("warn_by_reason") or {})
            if inp.finding == "marker":
                known = set((((result.get("cells") or {}).get("marker_flags") or {})
                             .get(inp.marker) or {}))
            if inp.reason not in known:
                raise AgentError("invalid_input", f"{inp.reason!r} flags no cells in the "
                                 "active result", detail={"allowed": sorted(known)})
        changed = bool(same) if inp.restore else not same
        if inp.restore:
            result["user_dismissed"] = [d for d in dismissed if d not in same]
        elif not same:
            dismissed.append({**key, "by": "user", "at": results.now_iso()})
        results.put_result(document, result)
        results.save(inp.project, document)
    cells = None
    if changed and inp.finding != "channel" and call.session.project(inp.project).has_table:
        cells = calls.write_for_active(call, inp.project, refresh_regions=False)
    after = results.revision(inp.project)
    arguments = {"project": inp.project, "finding": inp.finding, "reason": inp.reason,
                 "marker": inp.marker, "channel": inp.channel, "restore": not inp.restore}
    receipt = make_receipt(call, changed=changed, before={"dismissed": inp.restore},
                           after={"dismissed": not inp.restore}, revision_before=before,
                           revision_after=after, persistent_state=STATE, reversible=changed,
                           undo_hint={"tool": "dismiss_qc_finding", "arguments": {
                               k: v for k, v in arguments.items() if v is not None}}
                           if changed else None)
    return {"receipt": receipt.model_dump(mode="json"), "dismissed": not inp.restore,
            "finding": key, "cells": cells}


# -- files -----------------------------------------------------------------------------


class ExportInput(ProjectInput):
    what: Literal["rois", "cells", "provenance", "both"] = Field(
        "both", description="rois: qc_regions.geojson; cells: cells.csv; provenance: "
                            "qc_provenance.json (every region, cell reason and marker "
                            "reason with its category, subtype, tool, score, threshold "
                            "and threshold_source, and the agent's judgment) and "
                            "qc_findings.csv (the same, one row each), with the GeoJSON "
                            "they point into; both: all of them.")
    result_id: str | None = Field(None, description="Default: the active result.")


def export_qc(call, inp):
    from plexora.plugins.qc.server import export

    return export.export(call, inp.project, what=inp.what, result_id=inp.result_id)


class SegThresholds(AgentModel):
    """Segmentation QC's thresholds, as the panel's sliders set them."""

    flag_under: float | None = Field(None, ge=0.3, le=0.99, description="Under's score bar.")
    flag_over: float | None = Field(None, ge=0.3, le=0.99, description="Over's score bar.")
    z_large: float | None = Field(None, ge=1.0, le=8.0, description="Robust SDs above the "
                                  "median log area.")
    z_small: float | None = Field(None, ge=1.0, le=8.0, description="Robust SDs below the "
                                  "median log area.")
    z_irregular: float | None = Field(None, ge=1.0, le=8.0, description="Robust SDs below "
                                      "the median circularity.")


class WriteSourceInput(ProjectInput):
    confirm: Literal[True] = Field(description="Must be true, and only once the user has "
                                   "explicitly asked for the QC calls to be written into "
                                   "their file.")
    replace: bool = Field(False, description="Overwrite QC columns an earlier write left "
                          "(never anything else).")
    what: Literal["both", "qc", "segmentation"] = Field(
        "both", description="The QC cell calls, Segmentation QC's calls, or both (whichever "
        "exist).")
    seg_thresholds: SegThresholds | None = Field(
        None, description="Segmentation QC's thresholds for the written calls; default the "
        "run's own flag and 3 robust SDs for large / small / irregular.")


def write_source(call, inp):
    from plexora.plugins.qc.server import source_write

    ds = call.data
    thresholds = inp.seg_thresholds.model_dump(exclude_none=True) if inp.seg_thresholds \
        else None
    try:
        written = source_write.write(ds, replace=inp.replace, what=inp.what,
                                     thresholds=thresholds)
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
                          "summary": c.get("summary") or {}} for c in result.channels],
            "candidates": [{"id": c.id, "class_hint": c.class_hint, "channels": list(c.channels),
                            "scope_hint": c.scope_hint, "severity": c.severity,
                            "detector": c.detector, "alternatives": c.alternatives,
                            "bbox": bbox_fullres(result.grid, c.mask),
                            "tissue_fraction": cand.area_fraction(c.mask, result)}
                           for c in built["ranked"]][:MAX_LIST],
            "residual": built["residual"], "skipped_detectors": skipped}


class RefineRoiInput(ProjectInput):
    roi_id: str | None = Field(None, description="The QC region to trace (an ROI id).")
    all: bool = Field(False, description="Trace every QC region of the active result instead "
                                        "(regions the user edited or locked are skipped "
                                        "unless force).")
    margin_um: float | None = Field(None, ge=0.0, le=50.0, description="The margin grown "
                                    "round the trace, microns (default: the class's own).")
    force: bool = Field(False, description="Also retrace a region the user reshaped (their "
                                           "outline is then the envelope). A locked region "
                                           "is never retraced: unlock it first.")
    method: Literal["auto", "classical", "sam"] = Field(
        "auto", description="auto: the segmentation model's outline for a physical artifact "
                            "(debris, a fold, a bubble, torn tissue) or a blurred patch when it "
                            "passes its guards, "
                            "else the classical trace; classical: never the model; sam: the "
                            "model for any class (refused when it is not set up).")


def _scan_of(call, project, result):
    """The scan a result was made from, else the image's default scan, else
    the newest stored scan of the same image; None when there is none."""
    from plexora.plugins.qc.server import report
    from plexora.plugins.qc.server import scan as scanmod

    found = report._scan_for(result) if result.get("scan_fingerprint") else None
    if found is not None:
        return found
    override = _results().load(project).get("cycles_override")
    fp, context = scanmod.plan(call.session, project, override=override)
    found = scanmod._MEMORY.get((project, fp)) or scanmod.load(project, fp)
    if found is not None:
        return found
    folder = scanmod._folder(project)
    if folder.is_dir():
        for meta in sorted(folder.glob("scan_*.json"), key=lambda p: p.stat().st_mtime,
                           reverse=True):
            other = scanmod.load(project, meta.stem[len("scan_"):])
            if other is not None and other.meta.get("identity") == context["identity"]:
                return other
    return None


def refine_roi(call, inp):
    """Trace QC regions at pixel level: each region's outline (or the envelope
    QC judged it in) becomes the search area, and what is written is the
    artifact's own pixels inside it. Receipted per region, each undoable."""
    import dataclasses

    from plexora.plugins.qc.server import polygons, refine, roi_link
    from plexora.plugins.roi.server.repository import ROIRepository
    from plexora.server.utils import pixel_scale, source_image

    if bool(inp.roi_id) == bool(inp.all):
        raise AgentError("invalid_input", "give a roi_id, or all: true")
    from plexora.plugins.qc.server import refine_sam

    if inp.method == "sam" and not refine_sam.available():
        from plexora.vision import sam as sam_model

        raise AgentError("capability_unavailable", "the segmentation model is not set up on "
                         "this server; use method: auto or classical",
                         detail={"segment": sam_model.status().to_dict()})
    session_id = _open_session_on(inp.project)
    if session_id:
        raise AgentError("conflict", "a QC session is open on this project; finish it before "
                         "retracing its regions", detail={"session_id": session_id},
                         retryable=True)
    results = _results()
    ds = call.session.image_data(inp.project)
    pixel = pixel_scale.pixel_size(call.session.project(inp.project))
    pixel_um = float(pixel["value"]) if pixel else None
    options = {"margin_um": inp.margin_um} if inp.margin_um is not None else None
    refined, skipped, receipts = [], [], []
    with results.lock(inp.project):
        document = results.load(inp.project)
        before = results.revision(inp.project, document)
        roi_link.sync(ds, document)
        result = results.active(document)
        candidates = [c for c in ((result or {}).get("candidates") or {}).values()
                      if c.get("roi_id")]
        if inp.roi_id:
            candidates = [c for c in candidates if c["roi_id"] == inp.roi_id]
            if not candidates:
                raise AgentError("invalid_input", f"{inp.roi_id!r} is not a QC region of the "
                                 "active result")
        live = {r["roi_id"]: r for r in roi_link.live_regions(ds, result)}
        targets = []
        for candidate in candidates:
            user = candidate.get("user_state") or {}
            if candidate["roi_id"] not in live:
                skipped.append({"roi_id": candidate["roi_id"], "why": "the region is gone"})
                continue
            if user.get("locked"):
                # A lock is the ROI plugin's promise that the shape stays.
                if inp.roi_id:
                    raise AgentError("invalid_input", f"{inp.roi_id} is locked: unlock it in "
                                     "the ROI panel to retrace it")
                skipped.append({"roi_id": candidate["roi_id"], "why": "it is locked"})
                continue
            if user.get("edited") and not inp.force:
                if inp.roi_id:
                    raise AgentError("invalid_input", f"{inp.roi_id} is the user's: they "
                                     "reshaped it; pass force: true to retrace it anyway")
                skipped.append({"roi_id": candidate["roi_id"], "why": "the user reshaped it"})
                continue
            targets.append(candidate)
        scan = _scan_of(call, inp.project, result) if targets else None
        if targets and scan is None:
            raise AgentError("precondition_missing", "scan the image first "
                             "(`profile_image_qc`, a job) or run a QC session",
                             detail={"project": inp.project})
        tissue_px = float(((scan.meta if scan else {}).get("tissue") or {}).get("area_px")
                          or 0.0)
        seq = 0
        if targets:
            # One region at a time, each read inside the reader lock and
            # inferred outside it (refine_sam.trace): a tile request never
            # waits behind a model.
            for candidate in targets:
                    roi_id = candidate["roi_id"]
                    user = candidate.get("user_state") or {}
                    feature_geometry = live[roi_id]["geometry"]
                    theirs = user.get("edited") or candidate.get("created_by") == "user" \
                        or user.get("created_by") == "user"
                    envelope = feature_geometry if theirs else (
                        candidate.get("envelope_geometry") or feature_geometry)
                    if candidate.get("trace") in ("map", "none") \
                            and candidate.get("envelope_geometry"):
                        # A check's map region: its outline IS the score map
                        # (there are no pixels of a misregistration or a
                        # cluster to trace). A retrace puts that back.
                        trace = _map_trace(candidate, feature_geometry, live[roi_id]["class"],
                                           pixel_um)
                        envelope = candidate["envelope_geometry"]
                    else:
                        mask = polygons.geometry_to_grid(envelope, scan.grid, touch=True)
                        about = {**candidate, "class": live[roi_id]["class"]}
                        if inp.method == "classical":
                            with source_image.SHELF.reader(ds) as source:
                                trace = refine.refine(about, mask, scan, source,
                                                      pixel_um=pixel_um, envelope=envelope,
                                                      options=options)
                        elif inp.method == "sam":
                            trace = _sam_any_class(about, mask, scan, ds, pixel_um=pixel_um,
                                                   envelope=envelope, options=options)
                        else:
                            trace = refine_sam.trace(about, mask, scan, ds, pixel_um=pixel_um,
                                                     envelope=envelope, options=options)
                    if not trace.refined:
                        skipped.append({"roi_id": roi_id, "why": trace.reason,
                                        "status": trace.status})
                        continue
                    record = trace.to_record()
                    record["margin_um"] = inp.margin_um
                    candidate["refinement"] = record
                    changed = roi_link.retrace(ds, roi_id, candidate, trace.geometry)
                    candidate["envelope_geometry"] = envelope
                    candidate["geometry"] = trace.geometry
                    measurement = candidate.setdefault("measurement", {})
                    if tissue_px > 0:
                        measurement["refined_fraction"] = min(1.0, trace.area_px2 / tissue_px)
                    user.pop("edited", None)
                    candidate["user_state"] = user
                    refined.append({"roi_id": roi_id, "method": trace.method,
                                    "kept_fraction": trace.kept_fraction,
                                    "area_um2": trace.area_um2, "parts": trace.parts})
                    if changed is None:
                        continue
                    undo, partial = roi_link.undo_arguments(inp.project, changed["before"],
                                                            changed["revision_after"],
                                                            reshaped=True)
                    seq += 1
                    child = dataclasses.replace(call, operation_id=f"{call.operation_id}."
                                                                   f"{seq:03d}",
                                                receipted=False, extras=dict(call.extras),
                                                # One ROI-panel notice for them
                                                # all, below; none per region.
                                                notify=None)
                    receipt = make_receipt(
                        child, changed=True,
                        before={"roi_id": roi_id, "geometry_hash": polygons.geometry_hash(
                            changed["before"].get("geometry"))},
                        after={"roi_id": roi_id, "method": trace.method,
                               "kept_fraction": trace.kept_fraction},
                        revision_before=changed["revision_before"],
                        revision_after=changed["revision_after"],
                        persistent_state="plugin_store:roi", reversible=not partial,
                        undo_hint={"tool": "update_roi", "arguments": undo,
                                   **({"partial": True} if partial else {})},
                        extra={"parent_operation_id": call.operation_id,
                               "candidate_id": candidate["id"]})
                    receipts.append(receipt.model_dump(mode="json"))
        if refined:
            from plexora.plugins.qc.server import strictness

            for candidate in targets:
                candidate["action_by_strictness"] = strictness.actions_by_preset(candidate)
            results.put_result(document, result)
            results.save(inp.project, document)
        strictness_now = dict(document.get("strictness") or {})
    after = results.revision(inp.project)
    if refined:
        roi_link.tell_roi_panel(call, inp.project, "update")
        # The traced areas can change what excludes (the large-region rule)
        # and which cells a region holds: every action and call re-derived.
        _b, after, _renamed, _skip, _prev = apply_strictness(
            call, inp.project, strictness_now.get("preset") or "standard",
            strictness_now.get("thresholds"))
    parent = make_receipt(
        call, changed=bool(receipts), before={"revision": before},
        after={"refined": [r["roi_id"] for r in refined]}, revision_before=before,
        revision_after=after, persistent_state=STATE, reversible=len(receipts) == 1,
        undo_hint=receipts[0]["undo_hint"] if len(receipts) == 1 else None,
        extra={"children": [r["operation_id"] for r in receipts]})
    return {"receipt": parent.model_dump(mode="json"), "receipts": receipts,
            "refined": refined, "skipped": skipped}


def _sam_any_class(about, mask, scan, ds, *, pixel_um, envelope, options):
    """`method: sam`: the model's outline whatever the class (its guards
    still apply), the classical trace kept as the alternative."""
    from plexora.plugins.qc.server import refine, refine_sam
    from plexora.server.utils import source_image

    with source_image.SHELF.reader(ds) as source:
        classical = refine.refine(about, mask, scan, source, pixel_um=pixel_um,
                                  envelope=envelope, options=options)
        full_h, full_w = source.level_shape(0)
        job = refine_sam.prepare(about, mask, scan, source, pixel_um=pixel_um,
                                 envelope=envelope, image_size=(int(full_w), int(full_h)),
                                 any_class=True,
                                 hint=classical.geometry if classical.refined else None)
    return refine_sam.run(job, classical)


def _map_trace(candidate, current, klass, pixel_um):
    """A retrace of a check region whose outline is its score map: the map's
    outline again when the region was reshaped, else nothing to do."""
    from types import SimpleNamespace

    from plexora.plugins.qc.server import polygons, refine

    envelope = candidate["envelope_geometry"]
    method = refine.MAP_METHODS.get(klass, "score_map") \
        if candidate.get("trace") == "map" else None
    same = polygons.geometry_hash(current) == polygons.geometry_hash(envelope)
    area = polygons.area_of(envelope)
    record = {"status": "map" if method else "not_applicable", "method": method,
              "kept_fraction": 1.0, "refine_um": candidate.get("cell_um"),
              "reason": "the check's score map is the outline"}
    return SimpleNamespace(
        refined=not same, status=record["status"], method=method, kept_fraction=1.0,
        geometry=envelope, area_px2=area, parts=None,
        area_um2=area * pixel_um ** 2 if pixel_um else None,
        reason="its outline is already the check's score map" if same else None,
        to_record=lambda: dict(record))


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


class SampleInput(ProjectInput):
    check: Literal["blur", "registration", "segmentation", "artifacts"] = Field(
        description="The image check whose scores to sample: Blur QC, the Registration "
                    "Check, Segmentation QC or the Artifact Detector. It must have run "
                    "(run_blur_check, compute_registration_mismatch, run_segmentation_qc, "
                    "run_artifact_check).")
    category: Literal["fold", "tear", "debris", "saturation"] | None = Field(
        None, description="artifacts: the category whose objects to sample.")
    channel: str | None = Field(None, max_length=200, description="blur: the channel "
                                "(default the first listed).")
    comparison: str | None = Field(None, max_length=200, description="registration: the "
                                   "channel compared to the reference (default the "
                                   "current comparison).")
    module: Literal["seg_under", "seg_over", "seg_large", "seg_small",
                    "seg_irregular"] | None = Field(
        None, description="segmentation: sample cells by one reason's score (each tile "
                          "centred on a cell, the mask's outlines drawn); absent: the map "
                          "of where flagged cells cluster.")
    threshold: float | None = Field(None, ge=0, description="Preview the rows at this bar "
                                    "(nothing is stored). Prefer `adjust`.")
    adjust: Literal["tighter", "looser"] | None = Field(
        None, description="Preview the rows one step tighter (a lower bar: more flagged) "
                          "or looser than the bar in force; nothing is stored -- "
                          "set_blur_check / write_registration_regions store a step.")
    strata: list[Literal["clear_good", "borderline_below", "borderline_above",
                         "strongly_abnormal", "clustered"]] | None = Field(
        None, max_length=5, description="The rows to draw (default every row that has "
                                        "places): clearly fine, just below / just above "
                                        "the bar, far above it, inside the largest "
                                        "regions.")
    per_stratum: int = Field(6, ge=1, le=6, description="Places per row.")
    format: Literal["webp", "png"] = Field("webp", description="The sheet's format: webp "
                                           "(the same pixels, fewer bytes) or png.")
    seed: int = Field(0, description="The same seed, field and bar give the same places.")


def _stored_steps(call, project, inp):
    """(steps, source) of the bar a check holds now."""
    if inp.check == "blur":
        from plexora.plugins.qc.server import blur

        label = blur.resolve(call.session, project, inp.channel)
        bar = blur.threshold_of(project, label)
        if bar["source"] == "user":
            return None, "user", bar["value"]
        return int(bar.get("offset_steps") or 0), bar["source"], None
    if inp.check == "artifacts":
        from plexora.plugins.qc.server import artifacts

        bar = artifacts.threshold_of(project, inp.category or "fold")
        if bar["source"] == "user":
            return None, "user", bar["value"]
        return int(bar.get("offset_steps") or 0), bar["source"], None
    if inp.check == "registration":
        from plexora.plugins.qc.server import registration

        state = registration.resolve(registration.load_state(project),
                                     registration.channel_names(call.session.project(project)))
        comparison = inp.comparison or state.get("comparison")
        steps = int((state.get("offsets") or {}).get(comparison) or 0)
        return steps, "user_relative" if steps else "auto", None
    return 0, "auto", None


def sample_examples(call, inp):
    from plexora.agent.core.visual import with_image
    from plexora.plugins.qc.server import schemas, score_review

    if inp.threshold is not None and inp.adjust is not None:
        raise AgentError("invalid_input", "give `threshold` or `adjust`, not both")
    if inp.module and inp.check != "segmentation":
        raise AgentError("invalid_input", "`module` samples Segmentation QC's cells: use it "
                         "with check segmentation")
    project = inp.project
    if inp.check == "segmentation" and inp.module:
        subject = score_review.cells_for(call.session, project, inp.module)
    else:
        subject = score_review.field_for(call.session, project, inp.check,
                                         channel=inp.channel, comparison=inp.comparison,
                                         category=inp.category)
    steps, source, value = _stored_steps(call, project, inp)
    preview = inp.threshold is not None or inp.adjust is not None
    if inp.threshold is not None:
        value, steps, source = float(inp.threshold), None, "preview"
    elif inp.adjust is not None:
        from plexora.plugins.qc.server import score_fields

        base = int(steps or 0)
        steps = base + schemas.ADJUST[inp.adjust]
        if abs(steps) > int(schemas.ENGINE["adjust_max_steps"]):
            raise AgentError("invalid_input", "that is past the steps a bar may move from "
                             "its automatic one", detail={
                                 "offset_steps": base,
                                 "max_steps": schemas.ENGINE["adjust_max_steps"]})
        steps = score_fields.clamp_steps(steps)
        source = "preview"
    look = score_review.review(call.session, project, subject, offset_steps=steps or 0,
                               threshold=value, strata=inp.strata,
                               per_row=inp.per_stratum, seed=inp.seed, fmt=inp.format,
                               source=source)
    sheet = look["sheet"]
    bar = look["bar"]
    out = {"project": project, "check": inp.check, "module": inp.module,
           "channel": subject.channel, "reference": getattr(subject, "reference", None),
           "manifest": score_review.manifest_of(look),
           "distribution": look["distribution"],
           "threshold": {"value": bar["value"], "auto": bar["auto"], "source": source,
                         "offset_steps": bar["offset_steps"], "step": bar["step"],
                         "preview": preview},
           "evaluation_at_threshold": look["at"], "global": look["global"],
           "rows": {k: [p["score"] for p in v] for k, v in look["strata"].items()},
           "artifact": sheet.get("artifact"),
           "next": "judge each row by its tiles: artifact, normal or mixed. To move the "
                   "bar, adjust it one step (set_blur_check / write_registration_regions), "
                   "then write the regions."}
    if inp.format == "png":
        return with_image(out, sheet["image"])
    out["_images"] = [{"data": sheet["image"], "format": sheet["format"]}]
    out["image_inline"] = True
    return out


# -- the table -------------------------------------------------------------------------------


class ExclusionsInput(ProjectInput):
    mode: Literal["strict", "exclude"] = Field(
        "strict", description="strict: cells called exclude or warn, and per marker the cells "
                              "flagged unreliable in it (what gating leaves out by default); "
                              "exclude: warn calls kept.")


def get_exclusions(call, inp):
    """What anything that estimates from this table leaves out (plexora/agent/
    cell_exclusions.py): counts by reason and by marker, the QC result they
    come from and a fingerprint that changes with them -- never cell ids.
    Calls derived before a region was drawn or reshaped are re-derived first."""
    from plexora.plugins.qc.server import exclusions

    ds = call.session.data(inp.project)
    if not ds.table.available:
        return {"project": inp.project, "mode": inp.mode, "applied": False,
                "reason": "this project has no cell table: QC regions mark the image only"}
    out = exclusions.summary(ds, inp.mode)
    out["project"] = inp.project
    out["consumers"] = ("automatic gating leaves these cells out of every fit, sample, "
                        "collage and validation field (gating_session_start qc=...); the "
                        "gates still apply to every cell")
    return out


def capabilities():
    from plexora.plugins.qc import (capabilities_checks, capabilities_segment,
                                    capabilities_session, capabilities_visual)

    def free(**kwargs):
        kwargs.setdefault("tags", TAGS)
        return Capability(owner=OWNER, version="1", **kwargs)

    def paid(**kwargs):
        kwargs.setdefault("tags", TAGS)
        kwargs.setdefault("entitlement", "ai:qc:analytics")
        return Capability(owner=OWNER, version="1", **kwargs)

    return [
        *capabilities_session.capabilities(),
        *capabilities_checks.capabilities(free),
        free(name="qc.get_results", tool_name="get_qc_results",
             purpose="A project's QC: every channel's status, every QC region (category, "
                     "subtype, action, score and threshold with its source, the agent's "
                     "judgment, who made it, the user's edits), a summary per category, the "
                     "cells' pass/fail counts by reason with where each came from, the "
                     "strictness, the provenance. Read this first.",
             permission="read", input_model=ResultsInput, handler=get_results,
             egress="aggregates", reads=("qc", "rois")),
        free(name="qc.get_exclusions", tool_name="get_qc_exclusions",
             purpose="Which cells QC leaves out of estimation -- automatic gating's fits, "
                     "samples, collages and validation fields -- counted by reason and by "
                     "marker, with the QC result they come from. Re-derives calls made "
                     "stale by a region drawn since.",
             permission="read", input_model=ExclusionsInput, handler=get_exclusions,
             egress="aggregates", reads=("qc", "rois", "table")),
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
             permission="reversible_write", input_model=RefreshInput, handler=refresh,
             writes=("qc",), persistent=True, reversible=False, reads=("rois", "table", "mask")),
        free(name="qc.dismiss_finding", tool_name="dismiss_qc_finding",
             purpose="Set aside a QC finding the user judged wrong: a whole-cell reason "
                     "(its cells stop failing for it), one marker's flag, or a channel's "
                     "audit verdict (called clean). Recorded with who and when, and "
                     "restorable; a region's cells follow the region, so delete that "
                     "instead.",
             permission="reversible_write", input_model=DismissInput,
             handler=dismiss_finding, writes=("qc",), persistent=True,
             reads=("qc", "table")),
        free(name="qc.set_cycles", tool_name="set_qc_cycles",
             purpose="Say which channels were imaged together (cycles), when the names do "
                     "not: the next scan checks registration and tissue loss per cycle.",
             permission="reversible_write", input_model=CyclesInput, handler=set_cycles,
             writes=("qc",), persistent=True),
        free(name="qc.export", tool_name="export_qc",
             purpose="Write a project's QC to files: cells.csv (pass, reasons, "
                     "qc_category, flag_source, regions, and Segmentation QC's seg_qc_* "
                     "columns when it has run), qc_regions.geojson (category, subtype, "
                     "score, threshold, verdict per region), qc_provenance.json and "
                     "qc_findings.csv (why every region and cell was flagged), "
                     "summary.json (with `segmentation_qc`), result.json.",
             permission="read", input_model=ExportInput, handler=export_qc,
             egress="aggregates", reads=("qc", "rois"),
             tags=TAGS + ("export", "csv", "geojson", "download")),
        free(name="qc.write_source", tool_name="write_qc_to_source",
             purpose="Write the QC calls into the user's own table: AnnData obs/obsm/uns "
                     "(plexora_qc_*, and Segmentation QC's plexora_seg_qc_* when it has run), "
                     "or columns of a CSV/Parquet. Modifies the user's file: needs "
                     "--allow-source-writes and confirm: true, only on request.",
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
        paid(name="qc.refine_roi", tool_name="refine_qc_roi",
             purpose="Trace a QC region's artifact at pixel level: its outline becomes the "
                     "search area, and the region is rewritten as the artifact's own pixels "
                     "inside it (aggregate specks, a fold's band, the blurred patch), so the "
                     "normal tissue it took in is kept. A registration region retraces to "
                     "its mismatch map and a segmentation cluster to its density grid (their "
                     "map is the outline); a blur region to the blur trace. One region, or "
                     "all of them; each receipted and undoable; locked regions are left "
                     "alone. Needs the image scanned (a QC session or profile_image_qc).",
             permission="reversible_write", input_model=RefineRoiInput, handler=refine_roi,
             writes=("qc", "rois"), persistent=True, reads=("image", "qc", "rois", "table")),
        capabilities_segment.capability(paid),
        *capabilities_visual.capabilities(paid),
        paid(name="qc.sample_examples", tool_name="sample_qc_examples",
             purpose="Look at an image check the way a QC session does: places sampled "
                     "across its score distribution -- a row each of clearly fine, just "
                     "below and just above the bar, far above it, inside the largest "
                     "flagged regions -- and the whole tissue with the regions at the bar "
                     "and the score map; each tile's position in the manifest. For "
                     "Segmentation QC, sample cells by one reason (`module`). Judge "
                     "artifact against normal variation by eye before writing anything; "
                     "`adjust` previews the bar a step away.",
             permission="read", input_model=SampleInput, handler=sample_examples,
             visual_output=True, egress="rendered_pixels", reads=("image", "qc", "mask"),
             tags=TAGS + ("blur", "registration", "segmentation", "sample", "threshold")),
        paid(name="qc.render_overview", tool_name="render_qc_overview",
             purpose="A channel audit sheet: up to eight channels, whole tissue, at the "
                     "project's calibrated windows, beside the nuclear stain.",
             permission="read", input_model=OverviewInput, handler=render_overview,
             visual_output=True, egress="rendered_pixels", reads=("image",)),
    ]
