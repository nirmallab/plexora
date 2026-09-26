"""What an agent asks to see: a region, some channels, some overlays.

A spec is complete and literal on purpose. Everything the picture depends on
is in it -- after a preset has filled in what the caller left out, and after
"auto" windows have been resolved to numbers -- and the resolved spec is what
the manifest records, so the same spec always draws the same picture and a
manifest always says exactly how to draw it again.
"""

from __future__ import annotations

import re
from typing import Literal, Union

from pydantic import Field, field_validator, model_validator

from plexora.agent.limits import MAX_CHANNELS, MAX_IDS, MAX_OUTPUT_SIDE
from plexora.agent.schemas import AgentModel, ProjectInput

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _hex(value: str) -> str:
    if not isinstance(value, str) or not _HEX.match(value):
        raise ValueError(f"{value!r} is not a #rrggbb colour")
    return value.lower()


class Bounds(AgentModel):
    """A rectangle in full-resolution image pixels, top-left origin."""

    x: float = Field(description="Left edge, full-resolution pixels.")
    y: float = Field(description="Top edge, full-resolution pixels.")
    width: float = Field(gt=0)
    height: float = Field(gt=0)


class CenterBounds(AgentModel):
    """A square (or rectangle) around a point: in microns when the image is
    calibrated, else in pixels."""

    center_x: float
    center_y: float
    size_um: float | None = Field(None, gt=0, description="Side in microns (needs a "
                                  "calibrated image).")
    size_px: float | None = Field(None, gt=0, description="Side in full-resolution pixels.")
    aspect: float = Field(1.0, gt=0, le=10, description="width / height.")

    @model_validator(mode="after")
    def _one_size(self):
        if (self.size_um is None) == (self.size_px is None):
            raise ValueError("give exactly one of size_um and size_px")
        return self


class RoiBounds(AgentModel):
    roi_id: str = Field(description="A region's id from `list_rois`; the render frames "
                        "its bounding box.")
    padding: float = Field(0.1, ge=0, le=2, description="Margin, as a fraction of the "
                           "box's larger side.")


class ChannelSpec(AgentModel):
    name: str = Field(description="Channel name (or key), as `list_channels` gives it.")
    color: str = Field("#ffffff", description="#rrggbb.")
    window: list[float] | Literal["auto"] = Field(
        "auto", description="[low, high] in raw intensity units, or 'auto' (the "
                            "channel's 1st-99.9th percentile).")

    @field_validator("color")
    @classmethod
    def _color(cls, value):
        return _hex(value)

    @field_validator("window")
    @classmethod
    def _window(cls, value):
        if value == "auto":
            return value
        if len(value) != 2 or not value[0] < value[1]:
            raise ValueError("window must be [low, high] with low < high")
        return [float(value[0]), float(value[1])]


class MarkerHighlight(AgentModel):
    """Colour cells by a marker gate: positive cells in one colour, the rest in
    another (or undrawn)."""

    kind: Literal["marker"] = "marker"
    marker: str
    low: float | None = Field(None, description="Gate; default the stored one.")
    high: float | None = None


class IdHighlight(AgentModel):
    kind: Literal["ids"] = "ids"
    cell_ids: list[int] = Field(max_length=MAX_IDS)


class CellsSpec(AgentModel):
    highlight: Union[MarkerHighlight, IdHighlight, None] = Field(None, discriminator="kind")
    label_ids: bool = Field(False, description="Write cell ids next to cells in the field "
                            "(bounded by max_labels, nearest the centre first).")
    max_labels: int = Field(40, ge=0, le=200)
    #: Magenta, because the marker being judged is usually drawn yellow, and a
    #: highlight the channel's own colour cannot be seen.
    positive_color: str = "#ff3df2"
    negative_color: str = "#4f86c6"
    outline_color: str = "#e6e6e6"
    show_negative: bool = True

    @field_validator("positive_color", "negative_color", "outline_color")
    @classmethod
    def _colors(cls, value):
        return _hex(value)


class OutputSpec(AgentModel):
    width: int | None = Field(None, ge=16, le=MAX_OUTPUT_SIDE)
    height: int | None = Field(None, ge=16, le=MAX_OUTPUT_SIDE)


class RenderInput(ProjectInput):
    """Everything `render_region` accepts."""

    bounds: Bounds | None = Field(None, description="The region, in full-resolution "
                                  "pixels. Give one of bounds, center or roi; none means "
                                  "the whole image.")
    center: CenterBounds | None = None
    roi: RoiBounds | None = None
    marker: str | None = Field(None, description="Shortcut: show this marker's channel and "
                               "highlight cells its stored gate calls positive.")
    channels: list[ChannelSpec] | None = Field(None, max_length=MAX_CHANNELS,
                                               description="Channels to composite (default "
                                               "from the preset, else the first three).")
    segmentation: Literal["none", "outlines", "filled"] | None = Field(
        None, description="How to draw the cell mask (default: outlines when there is one).")
    cells: CellsSpec | None = None
    output: OutputSpec | None = None
    level: Literal["auto"] | int = Field("auto", description="Pyramid level, or 'auto' "
                                         "for the cheapest one with enough detail.")
    preset: Literal["marker_validation", "segmentation_qc", "roi_context",
                    "cell_identity", "spatial_context"] | None = None
    scale_bar: bool = Field(True, description="Draw a scale bar (only when calibrated).")

    @model_validator(mode="after")
    def _one_region(self):
        given = [name for name in ("bounds", "center", "roi") if getattr(self, name)]
        if len(given) > 1:
            raise ValueError(f"give at most one of bounds, center and roi (got {given})")
        if isinstance(self.level, int) and self.level < 0:
            raise ValueError("level must be >= 0")
        return self
