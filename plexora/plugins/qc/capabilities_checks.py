"""The free image checks of the QC panel, as capabilities an agent calls too.

    detect_nuclear_channels        which channels are nuclear (the one shared rule)
    get_registration_check         the Registration Check's state and last numbers
    set_registration_check         turn it on/off, pick the pair, the rule, thresholds,
                                   overlay, flicker (the panel mirrors every change)
    step_registration_comparison   the comparison channel to the previous / next one
    compute_registration_mismatch  the blockwise mismatch field and `% highlighted`
    run_segmentation_qc            a job: DNA peaks against the mask's labels
    get_segmentation_qc            its summary (and where the per-cell table is)
    clear_segmentation_qc          forget it
    run_blur_check                 a job: multi-scale gradient focus, tile by tile
    get_blur_check                 its summary, the threshold, the blurred area
                                   (and the regions' outlines on request)
    set_blur_check                 a channel's threshold ("auto" for the automatic
                                   one) and colour, the channels listed, the
                                   smallest region
    clear_blur_check               forget one channel's result, or every one
    write_blur_regions             the blurred regions as "QC: Out of focus" ROIs

The QC panel calls exactly these (through `/plugins/qc/registration/...`,
`/plugins/qc/segmentation/...` and `/plugins/qc/blur/...`); nothing the panel
shows is computed anywhere else. None is licence-gated: they are the user's own checks in the viewer.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from plexora.agent.errors import AgentError
from plexora.agent.receipts import make_receipt
from plexora.agent.schemas import AgentModel, ProjectInput

STATE = "plugin_store:qc"
REG_TAGS = ("qc", "registration", "alignment", "cycles", "dna", "nuclear", "flicker")
SEG_TAGS = ("qc", "segmentation", "mask", "under-segmentation", "over-segmentation", "dna",
            "nuclei", "cells")
BLUR_TAGS = ("qc", "blur", "focus", "out-of-focus", "sharpness", "tenengrad")
_HEX = r"^#[0-9a-fA-F]{6}$"


def _notify(call, project, payload):
    if call.notify is None:
        return False
    try:
        return bool(call.notify(project, "qc", "qc.registration", payload))
    except Exception:
        return False


# -- Registration Check ------------------------------------------------------------------


class Rule(AgentModel):
    mode: Literal["auto", "manual"] = "auto"
    channels: list[str] | None = Field(None, max_length=200, description="manual: exactly "
                                       "these channels are the nuclear candidates.")
    pattern: str | None = Field(None, max_length=200, description="manual: channels whose "
                                "name matches this (case-insensitive) regular expression.")


class RegParams(AgentModel):
    threshold_um: float | None = Field(None, gt=0, le=100, description="A block is "
                                       "highlighted at or above this shift (microns).")
    threshold_px: float | None = Field(None, gt=0, le=500, description="The same, in "
                                       "full-resolution pixels, when the image has no "
                                       "pixel size.")
    block_px: int | None = Field(None, ge=24, le=256, description="Block edge, in pixels "
                                 "of the level measured.")
    min_tissue: float | None = Field(None, ge=0, le=1)
    min_confidence: float | None = Field(None, ge=0, le=1)


class RegColors(AgentModel):
    reference: str | None = Field(None, pattern=_HEX)
    comparison: str | None = Field(None, pattern=_HEX)


class DetectInput(ProjectInput):
    rule: Rule | None = Field(None, description="Preview a rule without storing it.")


class RegStatusInput(ProjectInput):
    include_overlay: bool = Field(False, description="Also the last computed block field.")
    include_map: bool = Field(False, description="Also the mismatch map's byte planes "
                              "(the panel's heatmap picture); `stats.mismatch_hotspots` "
                              "names its densest places without them.")


class RegSetInput(ProjectInput):
    active: bool | None = None
    reference: str | None = Field(None, max_length=200, description="Any channel of the "
                                  "image; default the first nuclear channel.")
    comparison: str | None = Field(None, max_length=200)
    rule: Rule | None = None
    params: RegParams | None = None
    overlay_visible: bool | None = None
    flicker: bool | None = Field(None, description="Flicker runs whenever the check is on "
                                 "(turned back on at each activation); false pauses it.")
    flicker_ms: int | None = Field(None, ge=100, le=2000)
    colors: RegColors | None = Field(None, description="Hex colours; a colour set here is "
                                     "the user's and is never overwritten.")
    channel_colors: dict[str, str | None] | None = Field(
        None, max_length=200, description="A comparison channel's own colour, by name "
        "(#rrggbb; null forgets it). Used whenever that channel is the comparison.")
    reset_colors: bool = False


class RegStepInput(ProjectInput):
    direction: Literal["prev", "next"]
    wrap: bool = True


class RegComputeInput(ProjectInput):
    force: bool = Field(False, description="Measure again even if the cached field fits.")
    include_overlay: bool = True
    include_map: bool = Field(False, description="Also the mismatch map's byte planes "
                              "(the panel's heatmap picture); `stats.mismatch_hotspots` "
                              "names its densest places without them.")
    comparison: str | None = Field(None, max_length=200, description="Measure this channel "
                                   "against the reference instead of the current comparison, "
                                   "without switching to it (a per-channel score).")


def _registration():
    from plexora.plugins.qc.server import registration

    return registration


def detect_nuclear_channels(call, inp):
    reg = _registration()
    record = call.session.project(inp.project)
    names = reg.channel_names(record)
    rule = inp.rule.model_dump() if inp.rule else reg.load_state(inp.project)["rule"]
    candidates, method = reg.candidates_for(names, rule)
    return {"project": inp.project, "channels": names, "candidates": candidates,
            "method": method, "rule": rule,
            "suggested": {"reference": candidates[0] if candidates else None,
                          "comparison": candidates[1] if len(candidates) > 1 else None}}


def get_registration(call, inp):
    reg = _registration()
    record = call.session.project(inp.project)
    out = {"project": inp.project, "registration": reg.public_state(inp.project, record)}
    if inp.include_overlay and out["registration"]["last"]:
        try:
            computed = reg.compute(call.session, inp.project, reg.load_state(inp.project))
            out["overlay"] = _lean(computed.get("overlay"), inp.include_map)
        except AgentError:
            pass
    return out


def _write(call, project, update):
    from plexora.plugins.qc.server import results

    reg = _registration()
    record = call.session.project(project)
    names = reg.channel_names(record)
    with results.lock(project):
        before = reg.load_state(project)
        after = update(before, names)
        changed = {k: v for k, v in after.items() if k != "updated_at"} != \
            {k: v for k, v in before.items() if k != "updated_at"}
        if changed:
            after = reg.save_state(project, after)
    public = reg.public_state(project, record, after)
    receipt = make_receipt(call, changed=changed, before=reg.undo_arguments(project, before),
                           after=public, persistent_state=STATE, reversible=True,
                           undo_hint={"tool": "set_registration_check",
                                      "arguments": reg.undo_arguments(project, before)})
    return {"receipt": receipt.model_dump(mode="json"), "registration": public}


def set_registration(call, inp):
    reg = _registration()
    fields = inp.model_dump(exclude={"project"}, exclude_none=True)
    if not fields.get("reset_colors"):
        fields.pop("reset_colors", None)
    if not fields:
        raise AgentError("invalid_input", "give at least one field to change")
    return _write(call, inp.project, lambda state, names: reg.apply_update(state, names,
                                                                          **fields))


def step_registration(call, inp):
    reg = _registration()
    return _write(call, inp.project,
                  lambda state, names: reg.step(state, names, inp.direction, wrap=inp.wrap))


def _lean(overlay, keep_map=False):
    """The overlay without the mismatch map's byte planes, which are the
    panel's picture: an agent reads `stats.mismatch_hotspots` instead."""
    if overlay and overlay.get("mismatch") and not keep_map:
        overlay = {**overlay, "mismatch": {k: v for k, v in overlay["mismatch"].items()
                                           if k not in ("share", "nucleus")}}
    return overlay


def compute_registration(call, inp):
    reg = _registration()
    state = reg.load_state(inp.project)
    out = reg.compute(call.session, inp.project, state, force=inp.force,
                      include_overlay=inp.include_overlay, comparison=inp.comparison)
    if "overlay" in out:
        out["overlay"] = _lean(out["overlay"], inp.include_map)
    # A score for another channel is not what the open tab is showing.
    if inp.comparison is None or inp.comparison == state.get("comparison"):
        _notify(call, inp.project, {"event": "computed", "reference": out["reference"],
                                    "comparison": out["comparison"],
                                    "fingerprint": out["fingerprint"]})
    return out


# -- Segmentation QC -----------------------------------------------------------------------


class SegParams(AgentModel):
    flag: float | None = Field(None, ge=0.3, le=0.95, description="A score at or above this "
                               "flags a cell (default 0.6).")
    unsure: float | None = Field(None, ge=0.1, le=0.9, description="Between this and `flag` "
                                 "a cell is ambiguous (default 0.4).")


class SegRunInput(ProjectInput):
    dna_channel: str | None = Field(None, max_length=200, description="Default: the one "
                                    "chosen before, else the first nuclear channel.")
    force: bool = Field(False, description="Run again even if a result for these inputs "
                        "is stored.")
    params: SegParams | None = None


class SegStatusInput(ProjectInput):
    pass


def _segqc():
    from plexora.plugins.qc.server.segqc import run

    return run


def _seg_notify(call, project, payload):
    if call.notify is None:
        return False
    try:
        return bool(call.notify(project, "qc", "qc.segmentation", payload))
    except Exception:
        return False


def run_segmentation(call, inp):
    seg = _segqc()
    params = inp.params.model_dump(exclude_none=True) if inp.params else None
    if call.job is not None:
        seg.note_running(inp.project, call.job["job_id"])
    try:
        summary, reused = seg.load_or_run(
            call.session, inp.project, dna_channel=inp.dna_channel, params=params,
            force=inp.force, progress=lambda **k: call.progress(**k),
            check_cancelled=call.check_cancelled)
    finally:
        if call.job is not None:
            seg.note_running(inp.project, None)
    _seg_notify(call, inp.project, {"event": "done", "fingerprint": summary["fingerprint"],
                                    "reused": reused})
    return {"project": inp.project, "reused": reused, "fingerprint": summary["fingerprint"],
            "summary": {k: v for k, v in summary.items() if k != "mask"},
            "next": "get_segmentation_qc for the summary; the open viewer shows the "
                    "under / over overlays"}


def get_segmentation(call, inp):
    seg = _segqc()
    return {"project": inp.project, "segmentation_qc": seg.public_status(call.session,
                                                                         inp.project)}


def clear_segmentation(call, inp):
    seg = _segqc()
    before = seg.current_fingerprint(inp.project)
    changed = seg.clear(inp.project)
    receipt = make_receipt(call, changed=changed, before={"fingerprint": before},
                           after={"fingerprint": None}, persistent_state=STATE,
                           reversible=True,
                           undo_hint={"tool": "run_segmentation_qc",
                                      "arguments": {"project": inp.project}})
    _seg_notify(call, inp.project, {"event": "cleared"})
    return {"receipt": receipt.model_dump(mode="json"), "cleared": changed}


# -- Blur QC ---------------------------------------------------------------------------------


_CHANNEL = Field(None, max_length=200, description="A channel of the image (the one to act "
                 "on). Default: every listed channel for a run, status or write; the first "
                 "listed for a set. Ignored on brightfield (the darkness plane).")


class BlurParams(AgentModel):
    analysis_um_px: float | None = Field(None, ge=0.2, le=4.0, description="The resolution "
                                         "the image is read at, µm per pixel (default 0.5).")
    cell_um: float | None = Field(None, ge=20, le=200, description="A grid cell's side in "
                                  "microns (default 40; each score covers 3 x 3 cells).")
    reference_fraction: float | None = Field(None, ge=0.05, le=0.5, description="The share "
                                             "of the sharpest cells that make the in-image "
                                             "sharp reference (default 0.15).")


class BlurRunInput(ProjectInput):
    channel: str | None = _CHANNEL
    channels: list[Annotated[str, Field(max_length=200)]] | None = Field(
        None, max_length=12, description="Run these (and list them). Default: the listed "
        "channels -- the ones picked, else the first three nuclear ones.")
    force: bool = Field(False, description="Run again even if a result for these inputs "
                        "is stored.")
    params: BlurParams | None = None


class BlurStatusInput(ProjectInput):
    channel: str | None = _CHANNEL
    include_regions: bool = Field(False, description="Also each blurred region's outline "
                                  "(GeoJSON, full-resolution pixels).")
    threshold: float | None = Field(None, ge=0, le=1, description="Evaluate at this "
                                    "threshold instead of each channel's stored one "
                                    "(nothing is stored).")
    min_region_tiles: int | None = Field(None, ge=1, le=1000)


class BlurSetInput(ProjectInput):
    channel: str | None = _CHANNEL
    threshold: Annotated[float, Field(ge=0, le=1)] | Literal["auto"] | None = Field(
        None, description="The channel's threshold: a tile is blurred at or above this Blur "
        "Score (0..1); \"auto\" goes back to its automatic threshold.")
    color: Annotated[str, Field(pattern=_HEX)] | Literal["auto"] | None = Field(
        None, description="The colour the channel's map and regions are drawn in (#rrggbb); "
        "\"auto\" goes back to the default.")
    channels: list[Annotated[str, Field(max_length=200)]] | Literal["default"] | None = Field(
        None, description="The channels listed in the panel, in order (at most 12); "
        "\"default\" goes back to the first three nuclear ones.")
    min_region_tiles: int | None = Field(None, ge=1, le=1000, description="A region needs "
                                         "at least this many flagged grid cells (default 4).")


class BlurClearInput(ProjectInput):
    channel: str | None = Field(None, max_length=200, description="Default: every channel's "
                                "result.")


class BlurWriteInput(ProjectInput):
    channel: str | None = _CHANNEL
    threshold: float | None = Field(None, ge=0, le=1, description="Only with `channel`. "
                                    "Default: each channel's stored threshold.")
    min_region_tiles: int | None = Field(None, ge=1, le=1000)


def _blur():
    from plexora.plugins.qc.server import blur

    return blur


def _blur_notify(call, project, payload):
    if call.notify is None:
        return False
    try:
        return bool(call.notify(project, "qc", "qc.blur", payload))
    except Exception:
        return False


def _blur_labels(call, project, channel=None, channels=None):
    """The channels a call acts on, checked against the image."""
    blur = _blur()
    if channels:
        names = blur.channel_names(call.session.project(project))
        wrong = [c for c in channels if c not in names]
        if wrong:
            raise AgentError("invalid_input", f"{wrong[0]!r} is not a channel of this image",
                             detail={"channels": names[:100]})
        if blur.shown(call.session, project, names) == [blur.BRIGHTFIELD]:
            return [blur.BRIGHTFIELD]
        return list(dict.fromkeys(channels))
    if channel is not None:
        return [blur.resolve(call.session, project, channel)]
    return blur.shown(call.session, project)


def run_blur(call, inp):
    from plexora.plugins.qc.server import results

    blur = _blur()
    params = inp.params.model_dump(exclude_none=True) if inp.params else None
    labels = _blur_labels(call, inp.project, inp.channel, inp.channels)
    if inp.channels and labels != [blur.BRIGHTFIELD]:
        with results.lock(inp.project):
            blur.save_settings(inp.project, channels=labels)
    if call.job is not None:
        blur.note_running(inp.project, call.job["job_id"], labels)
    out = []
    try:
        for i, label in enumerate(labels):
            def progress(done=0, total=None, message=None, i=i, label=label):
                share = (done / total) if total else 0.0
                call.progress(done=round(1000 * (i + share)), total=1000 * len(labels),
                              message=f"{label} · {message}" if len(labels) > 1 and message
                              else message)

            summary, reused = blur.load_or_run(
                call.session, inp.project, channel=label, params=params, force=inp.force,
                progress=progress, check_cancelled=call.check_cancelled)
            out.append({"channel": label, "reused": reused,
                        "fingerprint": summary["fingerprint"],
                        "summary": {k: v for k, v in summary.items() if k != "histogram"},
                        "evaluation": blur.public_evaluation(
                            blur.evaluation(inp.project, label, geometry=False))})
    finally:
        if call.job is not None:
            blur.note_running(inp.project, None)
    _blur_notify(call, inp.project, {"event": "done", "channels": labels})
    return {"project": inp.project, "results": out,
            "next": "get_blur_check for the blurred area and regions; write_blur_regions "
                    "turns them into QC ROIs"}


def get_blur(call, inp):
    blur = _blur()
    status = blur.public_status(call.session, inp.project, channel=inp.channel,
                                include_regions=inp.include_regions)
    if inp.threshold is not None or inp.min_region_tiles is not None:
        for entry in status["results"]:
            entry["evaluation"] = blur.public_evaluation(
                blur.evaluation(inp.project, entry["channel"], threshold=inp.threshold,
                                min_region_tiles=inp.min_region_tiles,
                                geometry=inp.include_regions), regions=inp.include_regions)
    return {"project": inp.project, "blur_qc": status}


def _blur_undo(project, label, before, fields):
    """set_blur_check's arguments that put back what `fields` changed."""
    mine = (before.get("per") or {}).get(label) or {}
    arguments = {"project": project}
    if "threshold" in fields or "color" in fields:
        arguments["channel"] = label
    if "threshold" in fields:
        arguments["threshold"] = mine["threshold"] if mine.get("threshold") is not None \
            else "auto"
    if "color" in fields:
        arguments["color"] = mine.get("color") or "auto"
    if "channels" in fields:
        arguments["channels"] = before.get("channels") or "default"
    if "min_region_tiles" in fields:
        arguments["min_region_tiles"] = int(before.get("min_region_tiles")
                                            or _blur().MIN_REGION_TILES)
    return arguments


def set_blur(call, inp):
    from plexora.plugins.qc.server import results

    blur = _blur()
    fields = inp.model_dump(exclude={"project", "channel"}, exclude_none=True)
    if not fields:
        raise AgentError("invalid_input", "give at least one field to change")
    if isinstance(fields.get("channels"), list):
        fields["channels"] = _blur_labels(call, inp.project, channels=fields["channels"])
        if fields["channels"] == [blur.BRIGHTFIELD]:
            fields.pop("channels")
    label = blur.resolve(call.session, inp.project, inp.channel)
    with results.lock(inp.project):
        before = blur.settings(inp.project)
        mine = (before.get("per") or {}).get(label) or {}
        own = {}
        if "threshold" in fields:
            own["threshold"] = None if fields["threshold"] == "auto" \
                else float(fields["threshold"])
        if "color" in fields:
            own["color"] = None if fields["color"] == "auto" else fields["color"].lower()
        top = {}
        if "channels" in fields:
            top["channels"] = None if fields["channels"] == "default" else fields["channels"]
        if "min_region_tiles" in fields:
            top["min_region_tiles"] = int(fields["min_region_tiles"])
        changed = any(mine.get(k) != v for k, v in own.items()) \
            or any(before.get(k) != v for k, v in top.items())
        if changed:
            if own:
                blur.save_channel_settings(inp.project, label, **own)
            if top:
                blur.save_settings(inp.project, **top)
    listed = blur.shown(call.session, inp.project)
    public = {"channel": label, "shown": listed,
              "threshold": blur.threshold_of(inp.project, label),
              "color": blur.color_of(inp.project, label,
                                     listed.index(label) if label in listed else 0),
              "min_region_tiles": blur.min_region_tiles_of(inp.project)}
    undo = _blur_undo(inp.project, label, before, fields)
    receipt = make_receipt(call, changed=changed, before=undo, after=public,
                           persistent_state=STATE, reversible=True,
                           undo_hint={"tool": "set_blur_check", "arguments": undo})
    if changed:
        _blur_notify(call, inp.project, {"event": "set", "channel": label})
    evaluation = blur.public_evaluation(blur.evaluation(inp.project, label, geometry=False))
    return {"receipt": receipt.model_dump(mode="json"), **public, "evaluation": evaluation}


def clear_blur(call, inp):
    blur = _blur()
    label = blur.resolve(call.session, inp.project, inp.channel) \
        if inp.channel is not None else None
    before = {name: blur.current_fingerprint(inp.project, name)
              for name in ([label] if label else list(blur._pointers(inp.project)))}
    gone = blur.clear(inp.project, label)
    changed = bool(gone)
    receipt = make_receipt(call, changed=changed, before={"results": before},
                           after={"results": {}}, persistent_state=STATE,
                           reversible=True,
                           undo_hint={"tool": "run_blur_check",
                                      "arguments": {"project": inp.project,
                                                    **({"channel": label} if label else {})}})
    _blur_notify(call, inp.project, {"event": "cleared", "channels": gone})
    return {"receipt": receipt.model_dump(mode="json"), "cleared": gone}


def _blur_candidate(fp, k, region, summary, evaluation):
    import hashlib

    from plexora.plugins.qc.server import results

    blur = _blur()
    channel = summary.get("channel")
    return {"id": "cand_" + hashlib.sha1(f"blur:{fp}:{k}".encode()).hexdigest()[:10],
            "detector": "blur", "detector_version": blur.VERSION, "class": "out_of_focus",
            "class_alternatives": [], "scope": "all_channels",
            "channels": [channel] if channel else [], "cycles": [],
            "geometry": region["geometry"], "envelope_geometry": region["geometry"],
            # A Blur Score is not a severity: the region row would print it as one.
            "severity": None, "score": region["max_blur"],
            "metrics": {"threshold": evaluation["threshold"], "tiles": region["tiles"],
                        "area_um2": region["area_um2"], "mean_blur": region["mean_blur"],
                        "max_blur": region["max_blur"], "fingerprint": fp,
                        "channel": blur.label_of(channel)},
            "measurement": {}, "ai_decision": None, "evidence_artifacts": [],
            "created_by": "blur", "state": "confirmed", "action": "exclude",
            "created_at": results.now_iso(),
            # Written on the user's request, so its action is pinned as an
            # approval pins one: a strictness change never renames it.
            "user_state": {"approved": True, "approved_action": "exclude"}}


def write_blur_regions(call, inp):
    """Each listed channel's regions (or one channel's) as ROIs in
    `qc_out_of_focus`: the blur ROIs of those channels that nobody has edited,
    locked, renamed or moved are replaced; the rest are kept."""
    import dataclasses

    from plexora.plugins.qc.server import results, roi_link, strictness
    from plexora.plugins.roi.server.repository import ConflictError, ROIRepository

    blur = _blur()
    project = inp.project
    if inp.threshold is not None and inp.channel is None:
        raise AgentError("invalid_input", "a threshold applies to one channel: give "
                         "`channel` with it")
    labels = _blur_labels(call, project, inp.channel)
    work = []
    for label in labels:
        summary = blur.current(project, label)
        evaluation = blur.evaluation(project, label, threshold=inp.threshold,
                                     min_region_tiles=inp.min_region_tiles) \
            if summary is not None else None
        if evaluation is not None:
            work.append((label, summary, evaluation))
    if not work:
        raise AgentError("precondition_missing", "run Blur QC first (run_blur_check): no "
                         "listed channel has a result with evaluable tissue")
    covered = {label for label, _s, _e in work}
    ds = call.session.image_data(project)
    written, kept, removed, receipts = [], [], [], []
    per_channel = {}
    with results.lock(project):
        document = results.load(project)
        roi_link.sync(ds, document)
        result = results.ensure_active(document, project)
        meta = results.roi_meta(project)
        repo = ROIRepository(ds.name)
        state = repo.load()
        revision_before = state["revision"]
        features = {f["id"]: f for f in roi_link._features(state)}
        replace = []
        for row in (meta.to_dicts() if meta.height else []):
            feature = features.get(row["roi_id"])
            if row.get("detector") != "blur" or row.get("deleted") or feature is None:
                continue
            channels = list(row.get("channels") or []) or [blur.BRIGHTFIELD]
            if not covered.intersection(channels):
                continue
            if row.get("user_edited") or row.get("locked") or feature.get("locked") \
                    or row.get("removed_from_qc") or row.get("class") != "out_of_focus" \
                    or row.get("approved_action") != "exclude":
                kept.append(row["roi_id"])
                continue
            replace.append(feature)
        if replace:
            ids = [f["id"] for f in replace]
            repo.apply(state["revision"], [{"op": "roi.bulk_delete", "ids": ids}])
            results.drop_roi_meta(project, ids)
            candidates = result.setdefault("candidates", {})
            for key in [k for k, c in candidates.items() if c.get("roi_id") in set(ids)]:
                del candidates[key]
            removed = [{"roi_id": f["id"], "name": f.get("name"),
                        "geometry": f.get("geometry")} for f in replace]
        rows = []
        k = 0
        for label, summary, evaluation in work:
            fp = summary["fingerprint"]
            mine = per_channel.setdefault(label, {"written": [],
                                                  "threshold": evaluation["threshold"],
                                                  "blurred_pct": evaluation["blurred_pct"],
                                                  "residual": evaluation["residual"]})
            for n, region in enumerate(evaluation["regions"], start=1):
                if not region.get("geometry"):
                    continue
                k += 1
                record = _blur_candidate(fp, n, region, summary, evaluation)
                try:
                    before, after, feature = roi_link.create(ds, record, action="exclude")
                except ConflictError:
                    before, after, feature = roi_link.create(ds, record, action="exclude")
                child = dataclasses.replace(call, operation_id=f"{call.operation_id}.{k:03d}",
                                            receipted=False, notify=None,
                                            extras=dict(call.extras))
                receipt = make_receipt(
                    child, changed=True, before=None,
                    after={"roi_id": feature["id"], "name": feature["name"],
                           "channel": label},
                    revision_before=before, revision_after=after,
                    persistent_state="plugin_store:roi",
                    undo_hint={"tool": "delete_roi", "arguments": {
                        "project": project, "roi_id": feature["id"], "confirm": True}},
                    extra={"parent_operation_id": call.operation_id,
                           "artifact_class": "out_of_focus", "action": "exclude"})
                receipts.append(receipt.operation_id)
                record["roi_id"] = feature["id"]
                record["action_by_strictness"] = strictness.actions_by_preset(record)
                result.setdefault("candidates", {})[record["id"]] = record
                rows.append({**roi_link.meta_row(record, feature, result=result,
                                                 session_id=None, action="exclude",
                                                 strictness=None, agent=None,
                                                 operation_id=receipt.operation_id,
                                                 created_by="blur"),
                             "approved": True, "approved_action": "exclude"})
                written.append(feature["id"])
                mine["written"].append(feature["id"])
        if rows:
            results.upsert_roi_meta(project, rows)
        results.put_result(document, result)
        results.save(project, document)
        revision_after = repo.load()["revision"]
    changed = bool(written or removed)
    if changed:
        roi_link.tell_roi_panel(call, project, "create")
        if call.session.project(project).has_table:
            from plexora.plugins.qc.server.cells import calls

            calls.write_for_active(call, project)
    receipt = make_receipt(
        call, changed=changed, before={"removed": [r["roi_id"] for r in removed]},
        after={"written": written, "kept": kept}, revision_before=revision_before,
        revision_after=revision_after, persistent_state="plugin_store:roi",
        reversible=True, undo_hint={"note": "each region has its own receipt (children): "
                                    "undo_operation on one deletes that region",
                                    "children": receipts},
        extra={"children": receipts})
    return {"receipt": receipt.model_dump(mode="json"), "written": written, "kept": kept,
            "removed": [r["roi_id"] for r in removed], "channels": per_channel}


# -- the table ---------------------------------------------------------------------------


def capabilities(free):
    return [
        free(name="qc.registration_channels", tool_name="detect_nuclear_channels",
             purpose="The image's nuclear (DNA) channels in channel order -- DAPI, DNA, "
                     "Hoechst, nuclear stains, with any cycle number, prefix or suffix -- "
                     "under the Registration Check's rule, or a rule to preview.",
             permission="read", input_model=DetectInput, handler=detect_nuclear_channels,
             egress="metadata", reads=("image",), tags=REG_TAGS),
        free(name="qc.registration_status", tool_name="get_registration_check",
             purpose="The Registration Check: on or off, the reference and comparison "
                     "channels, the candidates, thresholds, overlay and flicker, and the "
                     "last computed mismatch numbers for this pair.",
             permission="read", input_model=RegStatusInput, handler=get_registration,
             egress="aggregates", reads=("qc", "image"), tags=REG_TAGS),
        free(name="qc.registration_set", tool_name="set_registration_check",
             purpose="Turn the Registration Check on or off, or change its reference / "
                     "comparison channel, DNA rule, thresholds, overlay, flicker or colours. "
                     "The open viewer follows: the reference goes in channel 1, the "
                     "comparison in channel 2; channels 3+ are left alone.",
             permission="reversible_write", input_model=RegSetInput, handler=set_registration,
             writes=("qc",), persistent=True, reads=("qc",), tags=REG_TAGS),
        free(name="qc.registration_step", tool_name="step_registration_comparison",
             purpose="Move the Registration Check's comparison to the previous or next "
                     "nuclear channel (the Z / X keys).",
             permission="reversible_write", input_model=RegStepInput,
             handler=step_registration, writes=("qc",), persistent=True, reads=("qc",),
             tags=REG_TAGS),
        free(name="qc.registration_compute", tool_name="compute_registration_mismatch",
             purpose="Measure how far the comparison channel is displaced from the "
                     "reference, block by block over the tissue: the global shift, each "
                     "block's shift, and the % of evaluated tissue at or above the "
                     "threshold (`highlighted_pct`). Cached; a threshold change re-reads "
                     "nothing.",
             permission="read", input_model=RegComputeInput, handler=compute_registration,
             egress="aggregates", reads=("image", "qc"), tags=REG_TAGS),
        free(name="qc.segmentation_run", tool_name="run_segmentation_qc",
             purpose="Check the segmentation mask against the DNA stain: every cell is "
                     "scored for under-segmentation (one label, several nuclei) and "
                     "over-segmentation (one nucleus split across labels), judged against "
                     "its own neighbourhood so dense tissue is not over-called. A job "
                     "(job_wait); reused when the image, mask, DNA channel and parameters "
                     "are unchanged. The mask is never modified.",
             permission="read", input_model=SegRunInput, handler=run_segmentation,
             execution="job", egress="aggregates", reads=("image", "mask", "qc"),
             tags=SEG_TAGS),
        free(name="qc.segmentation_status", tool_name="get_segmentation_qc",
             purpose="Segmentation QC's summary: cells and area under- / over-segmented "
                     "(percentages kept apart), the DNA channel, whether the result is "
                     "stale, and a running job's progress.",
             permission="read", input_model=SegStatusInput, handler=get_segmentation,
             egress="aggregates", reads=("qc", "mask"), tags=SEG_TAGS),
        free(name="qc.segmentation_clear", tool_name="clear_segmentation_qc",
             purpose="Forget the current Segmentation QC result (the overlays go).",
             permission="reversible_write", input_model=SegStatusInput,
             handler=clear_segmentation, writes=("qc",), persistent=True, tags=SEG_TAGS),
        free(name="qc.blur_run", tool_name="run_blur_check",
             purpose="Find out-of-focus regions without a model: multi-scale gradient "
                     "energy (Tenengrad) per tile of the tissue at a fixed analysis "
                     "resolution, against the image's own sharpest tiles, as a 0-1 Blur "
                     "Score with an automatic threshold and a whole-image blur warning, for "
                     "each listed DNA channel (default the first three nuclear ones) or the "
                     "ones given. A job (job_wait); a channel whose image and parameters "
                     "are unchanged is reused. Nothing is written to the ROIs.",
             permission="read", input_model=BlurRunInput, handler=run_blur,
             execution="job", egress="aggregates", reads=("image", "qc"), tags=BLUR_TAGS),
        free(name="qc.blur_status", tool_name="get_blur_check",
             purpose="Blur QC's results, one per listed channel: its colour, threshold "
                     "(automatic or the user's), the % of evaluable tissue that is blurred, "
                     "the regions "
                     "(outlines on request), the global-blur warning, whether it is stale, "
                     "and a running job's progress. A threshold given here is a preview.",
             permission="read", input_model=BlurStatusInput, handler=get_blur,
             egress="aggregates", reads=("qc", "image"), tags=BLUR_TAGS),
        free(name="qc.blur_set", tool_name="set_blur_check",
             purpose="Set a Blur QC channel's threshold (\"auto\" puts the automatic one "
                     "back) or colour, which channels are listed, or the smallest region. "
                     "Recomputes only the mask, from the stored scores; the open panel "
                     "follows.",
             permission="reversible_write", input_model=BlurSetInput, handler=set_blur,
             writes=("qc",), persistent=True, reads=("qc",), tags=BLUR_TAGS),
        free(name="qc.blur_clear", tool_name="clear_blur_check",
             purpose="Forget one channel's Blur QC result, or every one (the overlays "
                     "go; ROIs it wrote stay).",
             permission="reversible_write", input_model=BlurClearInput, handler=clear_blur,
             writes=("qc",), persistent=True, tags=BLUR_TAGS),
        free(name="qc.blur_write_regions", tool_name="write_blur_regions",
             purpose="Write Blur QC's regions -- every listed channel's at its own "
                     "threshold, or one channel's -- as ROIs in \"QC: Out of focus\" "
                     "(action exclude), so the cells inside are flagged. That channel's "
                     "blur ROIs written before are replaced unless the user edited, "
                     "locked, renamed or moved them; those are kept and listed.",
             permission="reversible_write", input_model=BlurWriteInput,
             handler=write_blur_regions, writes=("qc", "rois"), persistent=True,
             reads=("qc", "rois", "table"), tags=BLUR_TAGS),
    ]
