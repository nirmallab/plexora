"""Cell-level evidence: a gallery of single cells, and one cell explained."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from plexora.agent.cell_explain import MAX_NEIGHBOURS
from plexora.agent.cell_gallery import MAX_GALLERY_CELLS, SELECTIONS
from plexora.agent.core.visual import with_image
from plexora.agent.limits import MAX_IDS
from plexora.agent.registry import Capability
from plexora.agent.schemas import ProjectInput, QcInput
from plexora.api.plugin import Requires

# An RGB (brightfield) slide has no channels to gate, but its cells can still be
# looked at one by one.
_NEEDS_CELLS = Requires(table=True, roles=("x", "y"), excluded_image_kinds=())


class GalleryInput(ProjectInput, QcInput):
    cell_ids: list[int] | None = Field(None, max_length=MAX_IDS, description="These cells "
                                       "(the first n). Or give marker + select.")
    marker: str | None = Field(None, description="The marker whose value is under each "
                               "cell, and that select picks by.")
    select: Literal[SELECTIONS] | None = Field(
        None, description="positive / negative (by the gate, brightest first), borderline "
                          "(nearest the gate), brightest, dimmest.")
    low: float | None = Field(None, description="Gate; default the stored one.")
    high: float | None = None
    n: int = Field(24, ge=1, le=MAX_GALLERY_CELLS)
    crop_um: float | None = Field(None, gt=0, le=500, description="Crop side in µm "
                                  "(default 40; needs a calibrated image).")
    crop_px: float | None = Field(None, gt=0, le=2000, description="Crop side in "
                                  "full-resolution pixels (default 160 when uncalibrated).")
    tile_px: int = Field(160, ge=64, le=320, description="Each tile's side in the picture.")
    channels: list[str] | None = Field(None, max_length=3, description="Channels to draw "
                                       "(default the nuclear channel and the marker).")
    sort_by: Literal["value_desc", "value_asc", "id", "given"] | None = Field(
        None, description="For cell_ids: the order (default by marker value, brightest "
                          "first).")

    @model_validator(mode="after")
    def _one_way(self):
        if (self.cell_ids is None) == (self.select is None):
            raise ValueError("give either cell_ids, or marker with select")
        if self.select is not None and not self.marker:
            raise ValueError("select needs a marker")
        if self.crop_um is not None and self.crop_px is not None:
            raise ValueError("give at most one of crop_um and crop_px")
        return self


def cell_gallery(call, inp):
    from plexora.agent.cell_gallery import render_cell_gallery

    rendered = render_cell_gallery(
        call.session, call.data, cell_ids=inp.cell_ids, marker=inp.marker,
        select_how=inp.select, n=inp.n, low=inp.low, high=inp.high, crop_um=inp.crop_um,
        crop_px=inp.crop_px, tile_px=inp.tile_px, channels=inp.channels, sort=inp.sort_by)
    return with_image({"manifest": rendered["manifest"], "artifact": rendered["artifact"]},
                      rendered["png"])


class ExplainInput(ProjectInput):
    cell_id: int = Field(description="The cell's id, as the table's id column has it.")
    marker: str | None = Field(None, description="A marker to focus on: drawn in the crop "
                               "and reported for each neighbour.")
    k: int = Field(8, ge=0, le=MAX_NEIGHBOURS, description="Nearest neighbours to report.")
    within_um: float | None = Field(None, gt=0, description="Only neighbours this close "
                                    "(needs a calibrated image).")
    crop_um: float | None = Field(None, gt=0, le=500)
    crop_px: float | None = Field(None, gt=0, le=2000)
    include_crop: bool = True
    include_metadata: bool = Field(False, description="Every metadata column's value too "
                                   "(one read per column; slow on a remote table).")


def explain_cell(call, inp):
    from plexora.agent.cell_explain import explain_cell as explain

    result, png = explain(call.session, call.data, cell_id=inp.cell_id, marker=inp.marker,
                          k=inp.k, within_um=inp.within_um, crop_um=inp.crop_um,
                          crop_px=inp.crop_px, include_crop=inp.include_crop,
                          include_metadata=inp.include_metadata)
    return with_image(result, png) if png is not None else result


def capabilities():
    tags = ("cell", "cells", "single", "gallery", "crop", "evidence", "visual", "look",
            "inspect", "phenotype")
    return [
        Capability(
            name="cell.gallery", tool_name="render_cell_gallery", owner="core",
            purpose="A grid of single-cell crops -- each cell centred and outlined, its id "
                    "and marker value under it -- for given cells, or the positive, "
                    "negative, borderline (nearest the gate), brightest or dimmest cells "
                    "of a marker. The borderline gallery is the most useful gating view.",
            permission="read", input_model=GalleryInput, handler=cell_gallery,
            requires=_NEEDS_CELLS, visual_output=True, egress="rendered_pixels",
            reads=("image", "mask", "table", "gates"),
            tags=tags + ("borderline", "gate", "sorted", "expression")),
        Capability(
            name="cell.explain", tool_name="explain_cell", owner="core",
            purpose="One cell explained: every marker's value and percentile, its "
                    "strongest markers, the regions it lies in, its nearest neighbours "
                    "with distances, and a crop with it outlined.",
            permission="read", input_model=ExplainInput, handler=explain_cell,
            requires=_NEEDS_CELLS, visual_output=True, egress="rendered_pixels",
            reads=("image", "mask", "table", "rois"),
            tags=tags + ("explain", "neighbours", "neighbors", "percentile", "identity")),
    ]
