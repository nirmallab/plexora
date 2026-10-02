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
                                   one, or `adjust`: a step tighter or looser than
                                   it) and colour, the channels listed, the
                                   smallest region
    clear_blur_check               forget one channel's result, or every one
    write_blur_regions             the blurred regions as "QC: Blur / focus issue" ROIs
    write_registration_regions     the misregistered regions as "QC: Registration
                                   issue" ROIs, at the mismatch map's own grain
    write_segmentation_flags       Segmentation QC's calls as cell reasons
                                   (seg_under, seg_over, seg_small, seg_large,
                                   seg_irregular), and its clusters as regions
    run_artifact_check             a job: folds, tears, debris and saturation across
                                   every channel, as snug scored objects
    get_artifact_check             per category its threshold, what it keeps and the
                                   objects (outlines on request)
    set_artifact_check             a category's threshold or colour, the channels the
                                   panel lists
    clear_artifact_check           forget the result
    write_artifact_regions         the retained objects as "QC: Tissue / acquisition
                                   artifact" ROIs

A threshold an agent moves is moved in steps (`adjust: tighter | looser`,
one step the larger of a MAD of the scores and the check's floor), never
typed: it is stored as steps from the automatic threshold, its source
`user_relative`, and at most `adjust_max_steps` either way.

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
ART_TAGS = ("qc", "artifacts", "fold", "tear", "debris", "saturation", "foreign-object",
            "fiber", "clipping")
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
    include_regions: bool = Field(False, description="Also the misregistered regions at the "
                                  "bar in force (GeoJSON, full-resolution pixels): what "
                                  "write_registration_regions would write.")
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


def _field_summary(call, project, check, *, steps=0, source="auto", regions=False,
                   **where):
    """{threshold {value, auto, source, offset_steps, step}, distribution,
    at_threshold, regions?} of a check's last result, or None when it has not
    run (read from its cache: no pixel is read)."""
    from plexora.plugins.qc.server import score_fields, score_review

    try:
        field = score_review.field_for(call.session, project, check, **where)
    except AgentError:
        return None
    bar = score_fields.bar(field, steps)
    found = score_fields.regions(field, bar["value"], geometry=regions)
    out = {"threshold": {"value": bar["value"], "auto": bar["auto"], "source": source,
                         "offset_steps": bar["offset_steps"], "step": bar["step"]},
           "distribution": score_fields.distribution(field),
           "at_threshold": {"flagged_pct": found["flagged_pct"],
                            "denominator": found["denominator"],
                            "n_regions": found["n_regions"]},
           "global": score_fields.global_possible(field)}
    if regions:
        out["regions"] = [{k: r[k] for k in ("id", "cells", "area_um2", "mean", "max",
                                             "bbox", "peak", "geometry")}
                          for r in found["regions"]]
    return out


def get_registration(call, inp):
    reg = _registration()
    record = call.session.project(inp.project)
    out = {"project": inp.project, "registration": reg.public_state(inp.project, record)}
    comparison = out["registration"].get("comparison")
    steps = int((out["registration"].get("offsets") or {}).get(comparison) or 0)
    scores = _field_summary(call, inp.project, "registration", steps=steps,
                            source="user_relative" if steps else "auto",
                            regions=inp.include_regions, comparison=comparison) \
        if comparison else None
    if scores is not None:
        out["mismatch_map"] = scores
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
    include_regions: bool = Field(False, description="Also where the flagged cells cluster "
                                  "(GeoJSON, full-resolution pixels), at the cluster map's "
                                  "automatic bar.")


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
    from plexora.plugins.qc.server import results, schemas
    from plexora.plugins.qc.server.cells import modules as cell_modules

    seg = _segqc()
    out = {"project": inp.project, "segmentation_qc": seg.public_status(call.session,
                                                                        inp.project)}
    if out["segmentation_qc"].get("summary"):
        clusters = _field_summary(call, inp.project, "segmentation",
                                  regions=inp.include_regions)
        if clusters is not None:
            out["clusters"] = clusters
        active = results.active(results.load(inp.project)) or {}
        modules = (active.get("cells") or {}).get("modules") or {}
        written = {name: {"cutoffs": entry.get("cutoffs"),
                          "threshold_source": (entry.get("decision") or {}).get(
                              "threshold_source"),
                          "offset_steps": {side: ((entry.get("decision") or {}).get(side)
                                                  or {}).get("offset_steps")
                                           for side in cell_modules.sides_of(name)}}
                   for name, entry in modules.items() if name in cell_modules.SEG_MODULES}
        if written:
            cells = active.get("cells") or {}
            out["cell_calls"] = {
                "modules": written, "denominator": int(cells.get("n") or 0),
                "excluded": {r: int((cells.get("by_reason") or {}).get(r) or 0)
                             for r in schemas.SEG_REASONS},
                "warned": {r: int((cells.get("warn_by_reason") or {}).get(r) or 0)
                           for r in schemas.SEG_REASONS}}
    return out


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
    min_region_tiles: int | None = Field(None, ge=1, le=1000, description="Evaluate with "
                                         "this smallest region instead (nothing is stored).")


ADJUST_DESCRIPTION = ("Move the threshold one step instead of typing one -- tighter flags "
                      "more (artifacts were passing), looser flags less (normal tissue was "
                      "flagged); stored as steps from the automatic threshold "
                      "(threshold_source user_relative), refused past the allowed steps, "
                      "never together with `threshold`.")


class BlurSetInput(ProjectInput):
    channel: str | None = _CHANNEL
    threshold: Annotated[float, Field(ge=0, le=1)] | Literal["auto"] | None = Field(
        None, description="The channel's threshold: a tile is blurred at or above this Blur "
        "Score (0..1); \"auto\" goes back to its automatic threshold.")
    adjust: Literal["tighter", "looser"] | None = Field(None, description=ADJUST_DESCRIPTION
                                                        + " Needs `channel`.")
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
    adjust: Literal["tighter", "looser"] | None = Field(None, description=ADJUST_DESCRIPTION
                                                        + " Needs `channel`; the step is "
                                                          "stored, as set_blur_check stores "
                                                          "it.")
    min_region_tiles: int | None = Field(None, ge=1, le=1000, description="A region needs "
                                         "at least this many flagged grid cells.")


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
    from plexora.plugins.qc.server import score_fields

    blur = _blur()
    status = blur.public_status(call.session, inp.project, channel=inp.channel,
                                include_regions=inp.include_regions)
    for entry in status["results"]:
        summary = entry.get("summary")
        if not summary or summary.get("status") != "ok":
            continue
        arrays = blur.load_arrays(inp.project, summary["fingerprint"])
        if arrays is not None:
            dist = score_fields.distribution(score_fields.from_blur(summary, arrays))
            entry["distribution"] = dist
            entry["summary"] = {k: v for k, v in summary.items() if k != "histogram"}
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
    if "threshold" in fields or "color" in fields or "adjust" in fields:
        arguments["channel"] = label
    if "threshold" in fields or "adjust" in fields:
        if mine.get("offset_steps") and mine.get("threshold") is None:
            # Back to where it was: the same steps, taken the other way.
            back = {"tighter": "looser", "looser": "tighter"}.get(fields.get("adjust"))
            if back and "threshold" not in fields:
                arguments["adjust"] = back
            else:
                arguments["threshold"] = "auto"
        elif "adjust" in fields and mine.get("threshold") is None:
            arguments["threshold"] = "auto"
        else:
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
    if "adjust" in fields and "threshold" in fields:
        raise AgentError("invalid_input", "give `threshold` or `adjust`, not both: a step is "
                         "taken from the automatic threshold")
    if "adjust" in fields and inp.channel is None:
        raise AgentError("invalid_input", "`adjust` moves one channel's threshold: give "
                         "`channel` with it")
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
            own["offset_steps"] = None
        if "adjust" in fields:
            if blur.current(inp.project, label) is None:
                raise AgentError("precondition_missing", f"Blur QC has not run on {label}: "
                                 "run_blur_check first")
            steps = _moved(mine.get("offset_steps"), fields["adjust"])
            own["offset_steps"] = steps or None
            own["threshold"] = None
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


def _moved(current, adjust):
    """`current` steps moved one way; refused past `adjust_max_steps`."""
    from plexora.plugins.qc.server import schemas

    bound = int(schemas.ENGINE["adjust_max_steps"])
    steps = int(current or 0) + schemas.ADJUST[adjust]
    if abs(steps) > bound:
        raise AgentError("invalid_input", f"the threshold is already {bound} steps "
                         f"{'tighter' if steps > 0 else 'looser'} than the automatic one: "
                         "look at the regions before moving it further",
                         detail={"offset_steps": int(current or 0), "max_steps": bound})
    return steps


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


def _check_candidate(detector, version, klass, key, region, *, channels, scope,
                     threshold, action="exclude", cycles=(), extra_metrics=None,
                     score_key="max", trace="method", cell_um=None):
    """A check's region as a result candidate the user asked to write: its
    action pinned as an approval pins one, so a strictness change never
    renames it."""
    import hashlib

    from plexora.plugins.qc.server import refine, results

    # A map region's outline is the check's score map, and says so; a
    # detector's object is its own traced outline.
    refinement = {"status": "map", "method": refine.MAP_METHODS.get(klass, "score_map"),
                  "kept_fraction": 1.0, "refine_um": cell_um,
                  "reason": "the check's score map is the outline"} if trace == "map" else \
        dict(OBJECT_REFINEMENT) if trace == "object" else None
    return {"id": "cand_" + hashlib.sha1(f"{detector}:{key}".encode()).hexdigest()[:10],
            "trace": trace, "cell_um": cell_um, "refinement": refinement,
            "detector": detector, "detector_version": version, "class": klass,
            "class_alternatives": [], "scope": scope, "channels": list(channels),
            "cycles": [int(c) for c in cycles], "geometry": region["geometry"],
            "envelope_geometry": region["geometry"],
            # A check's score is not a severity: the region row would print it as one.
            "severity": None, "score": region.get(score_key),
            "metrics": {**threshold, **(extra_metrics or {})},
            "measurement": {}, "ai_decision": None, "evidence_artifacts": [],
            "origin": "check", "fine_grid": True,
            "created_by": detector, "state": "confirmed", "action": action,
            "created_at": results.now_iso(),
            "user_state": {"approved": True, "approved_action": action}}


def _blur_candidate(fp, k, region, summary, evaluation, bar=None):
    blur = _blur()
    channel = summary.get("channel")
    bar = bar or {}
    return _check_candidate(
        "blur", blur.VERSION, "out_of_focus", f"{fp}:{k}",
        {**region, "max": region["max_blur"]}, channels=[channel] if channel else [],
        scope="all_channels",
        threshold={"threshold": evaluation["threshold"],
                   "threshold_source": bar.get("source") or "auto",
                   "auto_threshold": bar.get("auto"),
                   "offset_steps": bar.get("offset_steps")},
        extra_metrics={"tiles": region["tiles"], "area_um2": region["area_um2"],
                       "mean_blur": region["mean_blur"], "max_blur": region["max_blur"],
                       "fingerprint": fp, "channel": blur.label_of(channel)})


#: The refinement record of a region that is a detector's own traced object.
OBJECT_REFINEMENT = {"status": "detector", "method": "artifact_detector", "kept_fraction": 1.0,
                     "reason": "the detector's own traced outline is the region"}


def _replaceable(row, feature, klass, action=None):
    """Whether a region a check wrote before may be replaced: nobody edited,
    locked or moved it, and -- for a check that pins one action -- nobody
    renamed it to another (renaming is choosing the action). `klass` is the
    class the check writes, or the set of them."""
    classes = {klass} if isinstance(klass, str) else set(klass)
    if row.get("user_edited") or row.get("locked") or feature.get("locked") \
            or row.get("removed_from_qc") or row.get("class") not in classes:
        return False
    return action is None or row.get("approved_action") == action


def _write_regions(call, project, *, detector, klass, covered, work, row_keys,
                   pinned="exclude"):
    """Write a check's regions as ROIs, replacing the ones it wrote before for
    the same keys (channels, comparisons) unless the user made them theirs.

    `work` is [(key, [candidate record])]; `row_keys(row)` the keys an
    earlier row covers. Returns (written, kept, removed, receipts, per_key,
    revisions)."""
    import dataclasses

    from plexora.plugins.qc.server import results, roi_link, strictness
    from plexora.plugins.roi.server.repository import ConflictError, ROIRepository

    ds = call.session.image_data(project)
    written, kept, removed, receipts = [], [], [], []
    per_key = {}
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
            if row.get("detector") != detector or row.get("deleted") or feature is None:
                continue
            if not covered.intersection(row_keys(row)):
                continue
            if not _replaceable(row, feature, klass, pinned):
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
        for key, records in work:
            mine = per_key.setdefault(key, {"written": []})
            for record in records:
                k += 1
                action = record.get("action") or "exclude"
                try:
                    before, after, feature = roi_link.create(ds, record, action=action)
                except ConflictError:
                    before, after, feature = roi_link.create(ds, record, action=action)
                child = dataclasses.replace(call, operation_id=f"{call.operation_id}.{k:03d}",
                                            receipted=False, notify=None,
                                            extras=dict(call.extras))
                receipt = make_receipt(
                    child, changed=True, before=None,
                    after={"roi_id": feature["id"], "name": feature["name"], "key": key},
                    revision_before=before, revision_after=after,
                    persistent_state="plugin_store:roi",
                    undo_hint={"tool": "delete_roi", "arguments": {
                        "project": project, "roi_id": feature["id"], "confirm": True}},
                    extra={"parent_operation_id": call.operation_id,
                           "artifact_class": record["class"], "action": action})
                receipts.append(receipt.operation_id)
                record["roi_id"] = feature["id"]
                record["action_by_strictness"] = strictness.actions_by_preset(record)
                result.setdefault("candidates", {})[record["id"]] = record
                rows.append({**roi_link.meta_row(record, feature, result=result,
                                                 session_id=None, action=action,
                                                 strictness=None, agent=None,
                                                 operation_id=receipt.operation_id,
                                                 created_by=detector),
                             "approved": True, "approved_action": action})
                written.append(feature["id"])
                mine["written"].append(feature["id"])
        if rows:
            results.upsert_roi_meta(project, rows)
        results.put_result(document, result)
        results.save(project, document)
        revision_after = repo.load()["revision"]
    if written or removed:
        roi_link.tell_roi_panel(call, project, "create")
        if call.session.project(project).has_table:
            from plexora.plugins.qc.server.cells import calls

            calls.write_for_active(call, project)
    return written, kept, removed, receipts, per_key, (revision_before, revision_after)


def _write_receipt(call, written, kept, removed, receipts, revisions):
    changed = bool(written or removed)
    receipt = make_receipt(
        call, changed=changed, before={"removed": [r["roi_id"] for r in removed]},
        after={"written": written, "kept": kept}, revision_before=revisions[0],
        revision_after=revisions[1], persistent_state="plugin_store:roi",
        reversible=True, undo_hint={"note": "each region has its own receipt (children): "
                                    "undo_operation on one deletes that region",
                                    "children": receipts},
        extra={"children": receipts})
    return receipt.model_dump(mode="json")


def write_blur_regions(call, inp):
    """Each listed channel's regions (or one channel's) as ROIs in
    `qc_blur_focus`: the blur ROIs of those channels that nobody has edited,
    locked, renamed or moved are replaced; the rest are kept."""
    blur = _blur()
    project = inp.project
    if inp.threshold is not None and inp.channel is None:
        raise AgentError("invalid_input", "a threshold applies to one channel: give "
                         "`channel` with it")
    if inp.adjust is not None and inp.channel is None:
        raise AgentError("invalid_input", "`adjust` moves one channel's threshold: give "
                         "`channel` with it")
    if inp.adjust is not None and inp.threshold is not None:
        raise AgentError("invalid_input", "give `threshold` or `adjust`, not both")
    labels = _blur_labels(call, project, inp.channel)
    if inp.adjust is not None:
        set_blur(call, BlurSetInput(project=project, channel=labels[0], adjust=inp.adjust))
    work, per_bar = [], {}
    for label in labels:
        summary = blur.current(project, label)
        evaluation = blur.evaluation(project, label, threshold=inp.threshold,
                                     min_region_tiles=inp.min_region_tiles) \
            if summary is not None else None
        if evaluation is None:
            continue
        bar = blur.threshold_of(project, label, summary) if inp.threshold is None else \
            {"value": inp.threshold, "auto": summary.get("auto_threshold"), "source": "user",
             "offset_steps": None}
        per_bar[label] = (evaluation, bar)
        records = [_blur_candidate(summary["fingerprint"], n, region, summary, evaluation,
                                   bar)
                   for n, region in enumerate(evaluation["regions"], start=1)
                   if region.get("geometry")]
        work.append((label, records))
    if not work:
        raise AgentError("precondition_missing", "run Blur QC first (run_blur_check): no "
                         "listed channel has a result with evaluable tissue")
    written, kept, removed, receipts, per_key, revisions = _write_regions(
        call, project, detector="blur", klass="out_of_focus",
        covered={label for label, _r in work}, work=work,
        row_keys=lambda row: list(row.get("channels") or []) or [blur.BRIGHTFIELD])
    per_channel = {}
    for label, (evaluation, bar) in per_bar.items():
        per_channel[label] = {"written": per_key.get(label, {}).get("written", []),
                              "threshold": evaluation["threshold"],
                              "threshold_source": bar.get("source"),
                              "offset_steps": bar.get("offset_steps"),
                              "blurred_pct": evaluation["blurred_pct"],
                              "denominator": evaluation["denominator"],
                              "residual": evaluation["residual"]}
    return {"receipt": _write_receipt(call, written, kept, removed, receipts, revisions),
            "written": written, "kept": kept, "removed": [r["roi_id"] for r in removed],
            "channels": per_channel}


# -- Artifact Detector ------------------------------------------------------------------


_CATEGORY = Literal["fold", "tear", "debris", "saturation"]


class ArtifactParams(AgentModel):
    pixel_um: float | None = Field(None, ge=0.01, le=100, description="Microns per pixel at "
                                   "full resolution, when the image does not say (every "
                                   "size the detector uses is in microns).")
    categories: list[_CATEGORY] | None = Field(None, max_length=4, description="The "
                                               "categories to look for (default all four).")
    pan_channels: int | None = Field(None, ge=2, le=12, description="Channels averaged into "
                                     "the pan image the objects are traced on (default 8: "
                                     "one nuclear per cycle, then markers evenly spaced).")
    feather_um: float | None = Field(None, ge=0, le=1000, description="How far past the "
                                     "tissue edge the search reaches (default 100).")
    tissue_min_width_um: float | None = Field(None, ge=0, le=2000, description="Tissue "
                                              "narrower than this (a peeled ribbon) is left "
                                              "out of the analysis region (default 200).")
    grow_um: float | None = Field(None, ge=10, le=1000, description="How far the fine pass "
                                  "may grow an object past its coarse seed (default 150).")
    fold_z: float | None = Field(None, ge=1, le=20, description="A fold seed's least "
                                 "brightness over the tissue, robust SDs (default 2.5).")
    tear_level: float | None = Field(None, ge=0, le=1, description="A tear holds at most "
                                     "this share of the tissue's signal over the glass "
                                     "(default 0.35), or is darker in most channels.")
    debris_z: float | None = Field(None, ge=1, le=50, description="Compact debris' least "
                                   "brightness over the glass, robust SDs (default 3).")


class ArtifactRunInput(ProjectInput):
    force: bool = Field(False, description="Run again even if a result for these inputs is "
                        "stored.")
    params: ArtifactParams | None = Field(None, description="Detection parameters "
                                          "(default: the detector's own; each is in "
                                          "microns or robust SDs).")


class ArtifactStatusInput(ProjectInput):
    include_regions: bool = Field(False, description="Also the retained objects with their "
                                  "outlines (GeoJSON, full-resolution pixels), strongest "
                                  "first.")
    thresholds: dict[_CATEGORY, Annotated[float, Field(ge=0, le=1)]] | None = Field(
        None, description="Evaluate at these per-category thresholds instead of the stored "
        "ones (a preview: nothing is stored).")
    categories: list[_CATEGORY] | None = Field(None, max_length=4, description="Only these "
                                               "categories' objects.")
    channel: str | None = Field(None, max_length=200, description="Only objects seen in "
                                "this channel.")
    max_objects: int = Field(200, ge=1, le=2000, description="At most this many objects.")


class ArtifactSetInput(ProjectInput):
    category: _CATEGORY | None = Field(None, description="The category whose threshold or "
                                       "colour this sets.")
    threshold: Annotated[float, Field(ge=0, le=1)] | Literal["auto"] | None = Field(
        None, description="The category's threshold: an object is kept at or above this "
        "score (0..1); \"auto\" goes back to the automatic one. Needs `category`.")
    adjust: Literal["tighter", "looser"] | None = Field(None, description=ADJUST_DESCRIPTION
                                                        + " Needs `category`.")
    color: Annotated[str, Field(pattern=_HEX)] | Literal["auto"] | None = Field(
        None, description="The colour the category's objects are drawn in (#rrggbb); "
        "\"auto\" goes back to the default. Needs `category`.")
    channels: list[Annotated[str, Field(max_length=200)]] | Literal["default"] | None = Field(
        None, description="The channels the panel lists, in order (at most 12): objects are "
        "shown where any of their channels is listed; \"default\" lists none (every "
        "channel). Never changes a threshold.")


class ArtifactClearInput(ProjectInput):
    pass


class ArtifactWriteInput(ProjectInput):
    categories: list[_CATEGORY] | None = Field(None, max_length=4, description="Write these "
                                               "categories' retained objects (default all).")
    channel: str | None = Field(None, max_length=200, description="Only objects seen in this "
                                "channel.")
    action: Literal["exclude", "warn"] = Field("exclude", description="What the regions do to "
                                               "the cells inside.")


def _artifacts():
    from plexora.plugins.qc.server import artifacts

    return artifacts


def _art_notify(call, project, payload):
    if call.notify is None:
        return False
    try:
        return bool(call.notify(project, "qc", "qc.artifacts", payload))
    except Exception:
        return False


def _art_channel(call, project, channel):
    if channel is None:
        return None
    names = _artifacts().channel_names(call.session.project(project))
    if channel not in names:
        raise AgentError("invalid_input", f"{channel!r} is not a channel of this image",
                         detail={"channels": names[:100]})
    return channel


def run_artifacts(call, inp):
    art = _artifacts()
    params = inp.params.model_dump(exclude_none=True) if inp.params else None
    if call.job is not None:
        art.note_running(inp.project, call.job["job_id"])
    try:
        summary, reused = art.load_or_run(call.session, inp.project, params=params,
                                          force=inp.force, progress=call.progress,
                                          check_cancelled=call.check_cancelled)
    finally:
        if call.job is not None:
            art.note_running(inp.project, None)
    _art_notify(call, inp.project, {"event": "done"})
    return {"project": inp.project, "reused": reused, "fingerprint": summary["fingerprint"],
            "summary": art.public_summary(summary),
            "evaluation": art.public_evaluation(art.evaluation(inp.project)),
            "next": "get_artifact_check for the objects each threshold keeps; "
                    "write_artifact_regions turns them into QC ROIs"}


def get_artifacts(call, inp):
    from plexora.plugins.qc.server import score_fields

    art = _artifacts()
    channel = _art_channel(call, inp.project, inp.channel)
    status = art.public_status(call.session, inp.project, include_regions=inp.include_regions,
                               thresholds=inp.thresholds, categories=inp.categories,
                               channel=channel, max_objects=inp.max_objects)
    summary = art.current(inp.project)
    arrays = art.load_arrays(inp.project, summary["fingerprint"]) if summary else None
    if arrays is not None:
        for entry in status["categories"]:
            field = art.score_field(summary, arrays, entry["key"])
            entry["distribution"] = score_fields.distribution(field)
    return {"project": inp.project, "artifacts_qc": status}


def _art_undo(project, category, before, fields):
    """set_artifact_check's arguments that put back what `fields` changed."""
    mine = (before.get("per") or {}).get(category) or {}
    arguments = {"project": project}
    if category is not None and ({"threshold", "adjust", "color"} & set(fields)):
        arguments["category"] = category
    if "threshold" in fields or "adjust" in fields:
        if mine.get("threshold") is not None:
            arguments["threshold"] = mine["threshold"]
        elif mine.get("offset_steps") and "adjust" in fields:
            arguments["adjust"] = {"tighter": "looser", "looser": "tighter"}[fields["adjust"]]
        else:
            arguments["threshold"] = "auto"
    if "color" in fields:
        arguments["color"] = mine.get("color") or "auto"
    if "channels" in fields:
        arguments["channels"] = before.get("channels") or "default"
    return arguments


def set_artifacts(call, inp):
    from plexora.plugins.qc.server import results

    art = _artifacts()
    fields = inp.model_dump(exclude={"project", "category"}, exclude_none=True)
    if not fields:
        raise AgentError("invalid_input", "give at least one field to change")
    if "adjust" in fields and "threshold" in fields:
        raise AgentError("invalid_input", "give `threshold` or `adjust`, not both: a step is "
                         "taken from the automatic threshold")
    if ({"threshold", "adjust", "color"} & set(fields)) and inp.category is None:
        raise AgentError("invalid_input", "a threshold or colour belongs to one category: "
                         "give `category` with it",
                         detail={"categories": list(art.CATEGORIES)})
    if isinstance(fields.get("channels"), list):
        names = art.channel_names(call.session.project(inp.project))
        wrong = [c for c in fields["channels"] if c not in names]
        if wrong:
            raise AgentError("invalid_input", f"{wrong[0]!r} is not a channel of this image",
                             detail={"channels": names[:100]})
        fields["channels"] = list(dict.fromkeys(fields["channels"]))[:12]
    category = inp.category
    with results.lock(inp.project):
        before = art.settings(inp.project)
        mine = (before.get("per") or {}).get(category) or {} if category else {}
        own = {}
        if "threshold" in fields:
            own["threshold"] = None if fields["threshold"] == "auto" \
                else float(fields["threshold"])
            own["offset_steps"] = None
        if "adjust" in fields:
            if art.current(inp.project) is None:
                raise AgentError("precondition_missing", "the Artifact Detector has not run: "
                                 "run_artifact_check first")
            steps = _moved(mine.get("offset_steps"), fields["adjust"])
            own["offset_steps"] = steps or None
            own["threshold"] = None
        if "color" in fields:
            own["color"] = None if fields["color"] == "auto" else fields["color"].lower()
        top = {}
        if "channels" in fields:
            top["channels"] = None if fields["channels"] == "default" else fields["channels"]
        changed = any(mine.get(k) != v for k, v in own.items()) \
            or any(before.get(k) != v for k, v in top.items())
        if changed:
            if own:
                art.save_category_settings(inp.project, category, **own)
            if top:
                art.save_settings(inp.project, **top)
    public = {"category": category, "shown": art.shown(call.session, inp.project)}
    if category:
        public.update(threshold=art.threshold_of(inp.project, category),
                      color=art.color_of(inp.project, category))
    undo = _art_undo(inp.project, category, before, fields)
    receipt = make_receipt(call, changed=changed, before=undo, after=public,
                           persistent_state=STATE, reversible=True,
                           undo_hint={"tool": "set_artifact_check", "arguments": undo})
    if changed:
        _art_notify(call, inp.project, {"event": "set", "category": category})
    return {"receipt": receipt.model_dump(mode="json"), **public,
            "evaluation": art.public_evaluation(art.evaluation(inp.project))}


def clear_artifacts(call, inp):
    art = _artifacts()
    before = art.current_fingerprint(inp.project)
    changed = art.clear(inp.project)
    receipt = make_receipt(call, changed=changed, before={"fingerprint": before},
                           after={"fingerprint": None}, persistent_state=STATE,
                           reversible=True,
                           undo_hint={"tool": "run_artifact_check",
                                      "arguments": {"project": inp.project}})
    _art_notify(call, inp.project, {"event": "cleared"})
    return {"receipt": receipt.model_dump(mode="json"), "cleared": changed}


def _artifact_candidate(fp, obj, bar, action):
    art = _artifacts()
    saturation = obj["category"] == "saturation"
    metrics = obj.get("metrics") or {}
    return _check_candidate(
        "artifacts", art.VERSION, obj["class"], f"{fp}:{obj['id']}",
        {**obj, "max": obj["score"]}, channels=obj.get("channels") or [],
        scope="channel" if saturation else "all_channels",
        threshold={"threshold": bar["value"], "threshold_source": bar.get("source") or "auto",
                   "auto_threshold": bar.get("auto"), "offset_steps": bar.get("offset_steps")},
        action=action, trace="object",
        extra_metrics={"category": obj["category"], "object": obj["id"],
                       "score": obj["score"], "strength": obj["strength"],
                       "area_um2": obj["area_um2"], "fingerprint": fp,
                       "source_channel": obj.get("source_channel"),
                       "shape": metrics.get("shape"), "agreement": metrics.get("agreement"),
                       "refined": obj.get("refined")})


def write_artifact_regions(call, inp):
    """The retained objects of the categories asked (default all) as ROIs in
    `qc_tissue_acquisition`: the artifact ROIs of those categories nobody has
    edited, locked, renamed or moved are replaced; the rest are kept."""
    art = _artifacts()
    project = inp.project
    summary = art.current(project)
    if summary is None:
        raise AgentError("precondition_missing", "run the Artifact Detector first "
                         "(run_artifact_check)")
    channel = _art_channel(call, project, inp.channel)
    categories = list(inp.categories or art.CATEGORIES)
    fp = summary["fingerprint"]
    work, per_bar = [], {}
    for category in categories:
        bar = art.threshold_of(project, category, summary)
        kept = art.objects_at(summary, {category: bar["value"]}, categories=[category],
                              channels=[channel] if channel else None, geometry=True)
        per_bar[category] = (bar, kept)
        work.append((category, [_artifact_candidate(fp, o, bar, inp.action) for o in kept]))
    classes = {art.CATEGORY_CLASS[c] for c in categories}
    written, kept, removed, receipts, per_key, revisions = _write_regions(
        call, project, detector="artifacts", klass=classes, covered=set(categories),
        work=work, row_keys=lambda row: [art.CLASS_CATEGORY.get(row.get("class"))],
        pinned=inp.action)
    per_category = {}
    for category, (bar, objs) in per_bar.items():
        per_category[category] = {"written": per_key.get(category, {}).get("written", []),
                                  "threshold": bar["value"],
                                  "threshold_source": bar.get("source"),
                                  "offset_steps": bar.get("offset_steps"),
                                  "n_objects": len(objs)}
    return {"receipt": _write_receipt(call, written, kept, removed, receipts, revisions),
            "written": written, "kept": kept, "removed": [r["roi_id"] for r in removed],
            "categories": per_category}


# -- Registration regions ---------------------------------------------------------------


class RegWriteInput(ProjectInput):
    comparison: str | None = Field(
        None, max_length=200, description="The channel compared to the reference; default "
                                          "the current comparison. \"all\": every nuclear "
                                          "channel measured against the reference.")
    adjust: Literal["tighter", "looser"] | None = Field(None, description=ADJUST_DESCRIPTION
                                                        + " Needs one `comparison`.")
    include_widespread: bool = Field(
        False, description="When most of the tissue is displaced (a whole cycle shifted), "
                           "write one region over the evaluated tissue instead of its "
                           "pieces.")
    min_region_um2: float | None = Field(None, ge=0, le=1e8, description="Drop regions "
                                         "smaller than this (square microns).")


def _reg_offsets(project):
    return dict(_registration().load_state(project).get("offsets") or {})


def _save_reg_offset(project, comparison, steps):
    from plexora.plugins.qc.server import results

    reg = _registration()
    with results.lock(project):
        state = reg.load_state(project)
        offsets = dict(state.get("offsets") or {})
        if steps:
            offsets[comparison] = int(steps)
        else:
            offsets.pop(comparison, None)
        reg.save_state(project, {**state, "offsets": offsets})


def _cycle_channels(call, project, channel):
    """(cycle, channels imaged with `channel`), from the channel names."""
    from plexora.plugins.qc.server import cycles

    names = _registration().channel_names(call.session.project(project))
    try:
        found = cycles.infer(names)
    except Exception:
        return None, [channel]
    for cycle in found.get("cycles") or []:
        if channel in (cycle.get("channels") or []):
            return cycle.get("index"), list(cycle.get("channels"))
    return None, [channel]


def write_registration_regions(call, inp):
    """The comparison's misregistered regions -- the mismatch map's cells at
    or above its bar (`registration.DENSE_FRACTION` moved by the steps asked
    for), 8-connected -- as ROIs in `qc_registration`, one class
    (cross_cycle_registration_error), scoped to the comparison's cycle."""
    from plexora.plugins.qc.server import registration, schemas, score_fields, score_review

    project = inp.project
    state = registration.resolve(registration.load_state(project),
                                 registration.channel_names(call.session.project(project)))
    if inp.comparison == "all":
        names = registration.channel_names(call.session.project(project))
        candidates, _method = registration.candidates_for(names, state["rule"])
        comparisons = [c for c in candidates if c != state.get("reference")]
    else:
        comparisons = [inp.comparison or state.get("comparison")]
    comparisons = [c for c in comparisons if c]
    if not comparisons:
        raise AgentError("precondition_missing", "no comparison channel: pick one "
                         "(set_registration_check comparison=...)")
    if inp.adjust is not None and (inp.comparison == "all" or len(comparisons) != 1):
        raise AgentError("invalid_input", "`adjust` moves one comparison's bar: give one "
                         "`comparison`")
    offsets = _reg_offsets(project)
    if inp.adjust is not None:
        steps = _moved(offsets.get(comparisons[0]), inp.adjust)
        _save_reg_offset(project, comparisons[0], steps)
        offsets[comparisons[0]] = steps
    work, per = [], {}
    for comparison in comparisons:
        try:
            field = score_review.field_for(call.session, project, "registration",
                                           comparison=comparison)
        except AgentError:
            if inp.comparison == "all":
                continue
            raise
        steps = int(offsets.get(comparison) or 0)
        bar = score_fields.bar(field, steps)
        source = "user_relative" if steps else "auto"
        min_cells = None
        if inp.min_region_um2 and field.cell_um:
            import math

            min_cells = max(1, int(math.ceil(inp.min_region_um2 / field.cell_um ** 2)))
        found = score_fields.regions(field, bar["value"], min_cells=min_cells)
        cycle, members = _cycle_channels(call, project, comparison)
        threshold = {"threshold": bar["value"], "threshold_source": source,
                     "auto_threshold": bar["auto"], "offset_steps": bar["offset_steps"],
                     "step": bar["step"]}
        records = []
        whole = score_fields.global_possible(field)
        if inp.include_widespread and whole["possible"]:
            import numpy as np

            ys, xs = np.nonzero(field.valid)
            region = {"geometry": score_fields.cell_geometry(ys, xs, field.grid),
                      "max": None, "cells": int(ys.size)}
            records.append(_check_candidate(
                "registration", registration.VERSION, "cross_cycle_registration_error",
                f"{field.fingerprint}:{comparison}:whole", region, channels=members,
                scope="cycle", threshold=threshold, cycles=[cycle] if cycle else [],
                trace="map", cell_um=field.cell_um,
                extra_metrics={"whole_tissue": True, "reference": field.reference,
                               "comparison": comparison, "fingerprint": field.fingerprint,
                               "why": whole["why"]}))
        else:
            for n, region in enumerate(found["regions"], start=1):
                records.append(_check_candidate(
                    "registration", registration.VERSION, "cross_cycle_registration_error",
                    f"{field.fingerprint}:{comparison}:{bar['value']}:{n}", region,
                    channels=members, scope="cycle", threshold=threshold,
                    cycles=[cycle] if cycle else [], trace="map", cell_um=field.cell_um,
                    extra_metrics={"cells": region["cells"], "mean": region["mean"],
                                   "max": region["max"], "area_um2": region["area_um2"],
                                   "reference": field.reference, "comparison": comparison,
                                   "fingerprint": field.fingerprint}))
        work.append((comparison, records))
        per[comparison] = {"threshold": {"value": bar["value"], "auto": bar["auto"],
                                         "source": source, "offset_steps": bar["offset_steps"],
                                         "step": bar["step"]},
                           "highlighted_pct": found["flagged_pct"],
                           "denominator": found["denominator"],
                           "n_regions": found["n_regions"], "residual": found["residual"],
                           "cycle": cycle, "global": whole}
    if not work:
        raise AgentError("precondition_missing", "no comparison has been measured: "
                         "compute_registration_mismatch first")
    written, kept, removed, receipts, per_key, revisions = _write_regions(
        call, project, detector="registration", klass="cross_cycle_registration_error",
        covered={c for c, _r in work}, work=work,
        row_keys=lambda row: [c for c in comparisons
                              if c in (row.get("channels") or [])])
    for comparison in per:
        per[comparison]["written"] = per_key.get(comparison, {}).get("written", [])
    return {"receipt": _write_receipt(call, written, kept, removed, receipts, revisions),
            "written": written, "kept": kept, "removed": [r["roi_id"] for r in removed],
            "comparisons": per, "category": schemas.category_of_class(
                "cross_cycle_registration_error")}


# -- Segmentation flags -----------------------------------------------------------------

SEG_ADJUST_SIDES = {"under": ("seg_under", "high"), "over": ("seg_over", "high"),
                    "large": ("seg_size", "high"), "small": ("seg_size", "low"),
                    "irregular": ("seg_shape", "low")}
SEG_REASON_MODULES = {"seg_under": "seg_under", "seg_over": "seg_over",
                      "seg_large": "seg_size", "seg_small": "seg_size",
                      "seg_irregular": "seg_shape"}


class SegWriteInput(ProjectInput):
    reasons: list[Literal["seg_under", "seg_over", "seg_small", "seg_large",
                          "seg_irregular"]] | None = Field(
        None, max_length=5, description="The Segmentation QC reasons to write as cell calls "
        "(default all five). seg_under / seg_over exclude the cell; seg_large excludes "
        "only where the DNA also says two nuclei, seg_small only under a preset that "
        "excludes on size alone; seg_irregular only warns.")
    adjust: dict[Literal["under", "over", "large", "small", "irregular"],
                 Literal["tighter", "looser"]] | None = Field(
        None, description="Move a reason's bar one step instead of typing one -- tighter "
                          "flags more, looser fewer; stored as steps from Segmentation "
                          "QC's own (threshold_source user_relative), refused past the "
                          "allowed steps.")
    clusters: bool = Field(False, description="Also write where the flagged cells cluster "
                           "(the density map at its automatic bar) as regions in "
                           "\"QC: Segmentation issue\".")
    cluster_action: Literal["warn", "exclude"] = Field(
        "warn", description="What a cluster region does to the cells in it: warn (default: "
                            "the cells' own calls exclude them) or exclude them all.")
    clear: bool = Field(False, description="Remove the Segmentation QC reasons written "
                        "before (the undo of this tool).")
    offsets: dict[Literal["under", "over", "large", "small", "irregular"], int] | None = \
        Field(None, description="Steps to set outright -- what an undo puts back; to move a "
                                "bar, use `adjust`.")


def write_segmentation_flags(call, inp):
    """Segmentation QC's calls into the active result's cells, as reasons of
    their own (`seg_*`, the category Segmentation issue); optionally its
    clusters as regions."""
    from plexora.plugins.qc.server import results, schemas
    from plexora.plugins.qc.server.cells import calls
    from plexora.plugins.qc.server.cells import modules as cell_modules

    project = inp.project
    record = call.session.project(project)
    if not record.has_table:
        raise AgentError("precondition_missing", "cell calls need the project's cell table")
    ds = call.session.data(project)
    ok, why = cell_modules.SegUnder().available(ds, {})
    if not ok and not inp.clear:
        raise AgentError("precondition_missing", f"{why}: run_segmentation_qc first",
                         detail={"hint": "run_segmentation_qc"})
    bound = int(schemas.ENGINE["adjust_max_steps"])
    with results.lock(project):
        document = results.load(project)
        result = results.ensure_active(document, project)
        modules = result.setdefault("cells", {}).setdefault("modules", {})
        before = {name: modules.get(name) for name in cell_modules.SEG_MODULES}
        steps_before = {word: int((((before.get(module) or {}).get("decision") or {})
                                   .get(side) or {}).get("offset_steps") or 0)
                        for word, (module, side) in SEG_ADJUST_SIDES.items()}
        if inp.clear:
            for name in cell_modules.SEG_MODULES:
                modules.pop(name, None)
        else:
            steps = dict(steps_before)
            for word, value in (inp.offsets or {}).items():
                if abs(int(value)) > bound:
                    raise AgentError("invalid_input", f"at most {bound} steps either way",
                                     detail={"offsets": inp.offsets})
                steps[word] = int(value)
            for word, how in (inp.adjust or {}).items():
                steps[word] = _moved(steps[word], how)
            reasons = list(inp.reasons or schemas.SEG_REASONS)
            wanted = {SEG_REASON_MODULES[r] for r in reasons}
            fp = ((modules.get("seg_under") or {}).get("fingerprint")) or None
            from plexora.plugins.qc.server.segqc import run as segqc

            summary = segqc.current(project) or {}
            for name in cell_modules.SEG_MODULES:
                if name not in wanted:
                    modules.pop(name, None)
                    continue
                decision = {}
                for side in cell_modules.sides_of(name):
                    word = next(w for w, (m, s_) in SEG_ADJUST_SIDES.items()
                                if m == name and s_ == side)
                    reason = {"under": "seg_under", "over": "seg_over", "large": "seg_large",
                              "small": "seg_small", "irregular": "seg_irregular"}[word]
                    decision[side] = {"verdict": "accept" if reason in reasons
                                      else "not_shown", "offset_steps": steps[word],
                                      "veto": False}
                moved = any(v["offset_steps"] for v in decision.values())
                decision["threshold_source"] = "user_relative" if moved else "user"
                decision["by"] = "tool"
                modules[name] = {"available": True, "state": "decided",
                                 "reason": "written on request (write_segmentation_flags)",
                                 "decision": decision, "fingerprint": summary.get(
                                     "fingerprint") or fp,
                                 "version": cell_modules.VERSION}
        results.put_result(document, result)
        results.save(project, document)
    summary = calls.write_for_active(call, project) or {}
    by_reason = {r: int((summary.get("by_reason") or {}).get(r) or 0)
                 for r in schemas.SEG_REASONS}
    warned = {r: int((summary.get("warn_by_reason") or {}).get(r) or 0)
              for r in schemas.SEG_REASONS}
    clusters = None
    if inp.clusters and not inp.clear:
        clusters = _write_seg_clusters(call, project, inp.cluster_action)
    undo = {"project": project, "clear": True} if not any(before.values()) else {
        "project": project, "reasons": [r for r in schemas.SEG_REASONS
                                        if before.get(SEG_REASON_MODULES[r])],
        "offsets": steps_before}
    receipt = make_receipt(call, changed=True, before={k: bool(v) for k, v in before.items()},
                           after={"excluded": by_reason, "warned": warned},
                           persistent_state=STATE, reversible=True,
                           undo_hint={"tool": "write_segmentation_flags", "arguments": undo})
    from plexora.plugins.qc.server.cells import modules as cell_modules_after

    thresholds = {}
    document = results.load(project)
    active = results.active(document) or {}
    for name, entry in ((active.get("cells") or {}).get("modules") or {}).items():
        if name in cell_modules_after.SEG_MODULES:
            thresholds[name] = {"cutoffs": entry.get("cutoffs"),
                                "threshold_source": (entry.get("decision") or {}).get(
                                    "threshold_source"),
                                "offset_steps": {s: (entry.get("decision") or {}).get(
                                    s, {}).get("offset_steps")
                                    for s in cell_modules_after.sides_of(name)}}
    return {"receipt": receipt.model_dump(mode="json"), "excluded": by_reason,
            "warned": warned, "denominator": int(summary.get("n") or 0),
            "thresholds": thresholds, "clusters": clusters}


def _write_seg_clusters(call, project, action):
    from plexora.plugins.qc.server import score_fields, score_review
    from plexora.plugins.qc.server.segqc import run as segqc

    field = score_review.field_for(call.session, project, "segmentation")
    bar = score_fields.bar(field)
    found = score_fields.regions(field, bar["value"])
    threshold = {"threshold": bar["value"], "threshold_source": "auto",
                 "auto_threshold": bar["auto"], "offset_steps": 0, "step": bar["step"]}
    records = [_check_candidate("segmentation", segqc.VERSION, "segmentation_error",
                                f"{field.fingerprint}:{n}", region, channels=[],
                                scope="all_channels", threshold=threshold, action=action,
                                trace="map", cell_um=field.cell_um,
                                extra_metrics={"cells": region["cells"],
                                               "mean": region["mean"], "max": region["max"],
                                               "area_um2": region["area_um2"],
                                               "fingerprint": field.fingerprint})
               for n, region in enumerate(found["regions"], start=1)]
    written, kept, removed, receipts, _per, revisions = _write_regions(
        call, project, detector="segmentation", klass="segmentation_error",
        covered={"mask"}, work=[("mask", records)], row_keys=lambda row: ["mask"],
        pinned=None)
    return {"written": written, "kept": kept, "removed": [r["roi_id"] for r in removed],
            "threshold": {"value": bar["value"], "auto": bar["auto"], "source": "auto"},
            "flagged_pct": found["flagged_pct"], "denominator": found["denominator"],
            "children": receipts}


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
                     "back; `adjust` moves it a step tighter or looser instead of typing a "
                     "number) or colour, which channels are listed, or the smallest region. "
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
                     "threshold, or one channel's -- as ROIs in \"QC: Blur / focus "
                     "issue\" (action exclude), so the cells inside are flagged. That "
                     "channel's blur ROIs written before are replaced unless the user "
                     "edited, locked, renamed or moved them; those are kept and listed. "
                     "Each region records its threshold and threshold_source.",
             permission="reversible_write", input_model=BlurWriteInput,
             handler=write_blur_regions, writes=("qc", "rois"), persistent=True,
             reads=("qc", "rois", "table"), tags=BLUR_TAGS),
        free(name="qc.registration_write_regions", tool_name="write_registration_regions",
             purpose="Write the Registration Check's misregistered regions -- the "
                     "mismatch map's cells (about 6.5 microns) at or above its bar, "
                     "joined -- as ROIs in \"QC: Registration issue\", scoped to the "
                     "comparison's cycle, so its cells are flagged. A whole cycle shifted "
                     "can be written as one region (`include_widespread`). The bar moves "
                     "only in steps (`adjust`). Earlier ones are replaced unless the user "
                     "made them theirs.",
             permission="reversible_write", input_model=RegWriteInput,
             handler=write_registration_regions, writes=("qc", "rois"), persistent=True,
             reads=("qc", "rois", "table", "image"), tags=REG_TAGS),
        free(name="qc.segmentation_write_flags", tool_name="write_segmentation_flags",
             purpose="Write Segmentation QC's calls into the cells' QC: merged "
                     "(seg_under) and split (seg_over) cells excluded, size and shape "
                     "outliers (seg_large, seg_small, seg_irregular) excluded only where "
                     "the evidence allows and otherwise warned, all in the category "
                     "Segmentation issue; optionally where they cluster as regions. Bars "
                     "move only in steps (`adjust`); receipted, undone with `clear`.",
             permission="reversible_write", input_model=SegWriteInput,
             handler=write_segmentation_flags, writes=("qc", "rois"), persistent=True,
             reads=("qc", "mask", "table"), tags=SEG_TAGS),
        free(name="qc.artifacts_run", tool_name="run_artifact_check",
             purpose="Find physical artifacts without a model, across every channel: tissue "
                     "folds, tears and detached tissue, debris and fibers on the glass round "
                     "the section, and saturated (clipped) pixels per channel -- coarse to "
                     "fine, each a snug outline with a 0-1 score, the channels it shows in "
                     "and the one it shows most. A job (job_wait); reused when the image and "
                     "parameters are unchanged. Needs a pixel size. Nothing is written to "
                     "the ROIs.",
             permission="read", input_model=ArtifactRunInput, handler=run_artifacts,
             execution="job", egress="aggregates", reads=("image", "qc"), tags=ART_TAGS),
        free(name="qc.artifacts_status", tool_name="get_artifact_check",
             purpose="The Artifact Detector's result: per category (fold, tear, debris, "
                     "saturation) its threshold (automatic or the user's), the objects it "
                     "keeps out of all found and their area, the score distribution, and "
                     "the objects themselves on request; whether it is stale and a running "
                     "job's progress. Thresholds given here are a preview.",
             permission="read", input_model=ArtifactStatusInput, handler=get_artifacts,
             egress="aggregates", reads=("qc", "image"), tags=ART_TAGS),
        free(name="qc.artifacts_set", tool_name="set_artifact_check",
             purpose="Set an artifact category's threshold (\"auto\" puts the automatic one "
                     "back; `adjust` moves it a step tighter or looser) or colour, or the "
                     "channels the panel lists. A threshold filters the stored objects "
                     "(nothing is re-detected); the open panel follows.",
             permission="reversible_write", input_model=ArtifactSetInput, handler=set_artifacts,
             writes=("qc",), persistent=True, reads=("qc",), tags=ART_TAGS),
        free(name="qc.artifacts_clear", tool_name="clear_artifact_check",
             purpose="Forget the Artifact Detector's result (the overlay goes; ROIs it "
                     "wrote stay).",
             permission="reversible_write", input_model=ArtifactClearInput,
             handler=clear_artifacts, writes=("qc",), persistent=True, tags=ART_TAGS),
        free(name="qc.artifacts_write_regions", tool_name="write_artifact_regions",
             purpose="Write the Artifact Detector's retained objects -- every category's at "
                     "its own threshold, or the ones asked -- as ROIs in \"QC: Tissue / "
                     "acquisition artifact\" with their class (tissue_fold, "
                     "tissue_damage_or_detachment, debris_or_foreign_object, "
                     "saturation_or_clipping), so the cells inside are flagged; a saturated "
                     "patch is scoped to its own channel. Those categories' regions written "
                     "before are replaced unless the user edited, locked, renamed or moved "
                     "them.",
             permission="reversible_write", input_model=ArtifactWriteInput,
             handler=write_artifact_regions, writes=("qc", "rois"), persistent=True,
             reads=("qc", "rois", "table"), tags=ART_TAGS),
    ]
