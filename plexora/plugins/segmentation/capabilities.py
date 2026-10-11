"""The agent's 3-D segmentation: `segment_3d`, a job."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from plexora.agent.errors import AgentError
from plexora.agent.receipts import make_receipt
from plexora.agent.registry import Capability
from plexora.agent.schemas import ProjectInput


class Segment3DInput(ProjectInput):
    nuclear: str = Field(description="The nuclear channel (e.g. DAPI, Hoechst, DNA).")
    cytoplasm: str | None = Field(None, description="A membrane or cytoplasm channel, for "
                                  "whole-cell outlines (optional).")
    mode: Literal["3d", "stitch"] = Field(
        "3d", description="3d: segment the stack in 3-D (needs the z step); stitch: plane by "
                          "plane, the planes' cells joined by overlap (no z step needed; "
                          "under-segments cells stacked along z).")
    diameter_um: float | None = Field(None, gt=0, description="Typical cell diameter in µm, "
                                      "if known; Cellpose-SAM usually needs none.")
    stitch_threshold: float = Field(0.4, ge=0, le=1, description="stitch: the overlap (IoU) "
                                    "that joins cells in neighbouring planes.")
    cellprob_threshold: float = Field(0.0, description="Lower finds fainter cells.")
    min_size_um3: float | None = Field(None, gt=0, description="Drop objects smaller than "
                                       "this (µm³; needs the z step).")
    level: int | Literal["auto"] = Field("auto", description="The XY level to segment at; "
                                         "auto: the finest that fits (labels are drawn back "
                                         "up to full resolution).")
    t: int = Field(0, ge=0, description="Frame of a time series.")
    device: Literal["auto", "cpu"] = "auto"
    replace_mask: bool = Field(False, description="Replace the project's mask if it has one "
                               "(its file is left where it is).")
    measure: bool = Field(True, description="Measure every cell into the object table after.")


def segment_3d(call, inp):
    """Cellpose on the project's z-stack; the label volume becomes its mask."""
    from plexora.plugins.segmentation.server import cellpose3d, run

    record = call.session.project(inp.project)
    try:
        result = run.segment_project(
            record, nuclear=inp.nuclear, cytoplasm=inp.cytoplasm, mode=inp.mode,
            diameter_um=inp.diameter_um, stitch_threshold=inp.stitch_threshold,
            cellprob_threshold=inp.cellprob_threshold, min_size_um3=inp.min_size_um3,
            level=inp.level, t=inp.t, device=inp.device, replace_mask=inp.replace_mask,
            measure=inp.measure,
            progress=lambda done, total: call.progress(done, total, "segmenting"),
            cancelled=call.cancelled)
    except cellpose3d.CellposeMissing as exc:
        raise AgentError("precondition_missing", str(exc), detail={
            "missing": [{"key": f"extra:{cellpose3d.EXTRA}", "kind": "install",
                         "label": "Cellpose (3-D segmentation)"}],
            "install": cellpose3d.INSTALL}) from None
    except run.SegmentationRefused as exc:
        message = str(exc)
        if "distance between planes" in message:
            raise AgentError("precondition_missing", message, detail={"missing": [
                {"key": "spacing:z", "kind": "spacing", "label": "Distance between planes"}],
                "hint": "set_z_spacing, or mode='stitch'"}) from None
        raise AgentError("invalid_input", message) from None
    call.session.invalidate(inp.project)
    from plexora.api import features

    features.reload_if_loaded(inp.project)
    receipt = make_receipt(call, changed=True, before=None,
                           after={"cells": result["cells"], "labels": result["labels"]},
                           persistent_state="project_config")
    return {**result, "receipt": receipt.model_dump(mode="json")}


def capabilities():
    return [
        Capability(
            name="segmentation.segment_3d", tool_name="segment_3d", owner="segmentation",
            purpose="Segment a z-stack's cells in 3-D with Cellpose (Cellpose-SAM): one id per "
                    "cell through every plane, made the project's mask and measured into the "
                    "object table. Needs the segment3d extra; 3-D mode needs the z step.",
            permission="reversible_write", input_model=Segment3DInput, handler=segment_3d,
            execution="job", writes=("project_config",), reads=("image",),
            egress="aggregates",
            tags=("segmentation", "segment", "cellpose", "3d", "volume", "z-stack", "cells",
                  "mask", "nuclei")),
    ]
