"""Which 2-D picture of a volume a plugin or an agent is working on, and what it may claim.

A volume is the image; a plane, a slab projection and an orthogonal section are
pictures OF it, and every 2-D tool Plexora has works on one of those pictures.
That is fine as long as two things are never lost:

1. **Which picture.** A result computed on plane 12 is a result about plane 12;
   one computed on a maximum projection of planes 8-20 is a result about that
   projection. The `view` travels with the result -- in provenance, in every
   render's manifest, in a region's extent.
2. **What a picture cannot say.** A projection flattens z: two markers that
   overlap in it may never touch, a bright spot may be two cells at two depths,
   and nothing in it has a z position. Those conclusions are refused here, by
   name, with a sentence -- not left for a reader to notice.

This is the public face of `plexora.server.volume.viewspec`. Agents and plugins
import from here, never from the server package: the token grammar (`z12`,
`z4-19max`, `xzy1200`) is one thing and this is its typed spelling, converted
by `to_spec` / `ViewSpec.token()`. There is no third form.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from plexora.server.volume.provenance import (PROJECTION_WARNING, SECTION_WARNING,
                                              Provenance, provenance_for)
from plexora.server.volume.viewspec import BadView, ViewSpec

__all__ = [
    "BadView", "FORBIDDEN_INFERENCES", "PROJECTION_WARNING", "PlaneView",
    "Provenance", "SECTION_WARNING", "SectionView", "SlabView", "ViewModel",
    "ViewSpec", "check_supported", "forbid", "kind_of", "provenance_for",
    "to_spec", "view_label",
]


class _View(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlaneView(_View):
    """One plane of the volume."""

    kind: Literal["plane"] = "plane"
    z: int | None = Field(None, ge=0, description="Plane index, 0 = first. None = the "
                          "image's default (middle) plane.")
    t: int = Field(0, ge=0, description="Frame of a time series (0 = first).")


class SlabView(_View):
    """A projection of planes z0..z1 (inclusive)."""

    kind: Literal["slab"] = "slab"
    z0: int = Field(0, ge=0, description="First plane, inclusive.")
    z1: int | None = Field(None, ge=0, description="Last plane, inclusive; None = the last.")
    op: Literal["max", "mean", "min", "std", "med", "pct", "sum"] = Field(
        "max", description="max = maximum-intensity projection (MIP); pct needs q. "
                           "sum is for exports and voxel tools only (never a picture).")
    q: float | None = Field(None, ge=0, le=100, description="Percentile for op='pct'.")
    t: int = Field(0, ge=0)


class SectionView(_View):
    """An orthogonal section: XZ at a y, or YZ at an x (full-resolution pixels)."""

    kind: Literal["section"] = "section"
    axis: Literal["xz", "yz"]
    index: int = Field(ge=0, description="y for an XZ section, x for a YZ one.")
    t: int = Field(0, ge=0)


#: The typed spelling an agent's input carries. A plain token string is also
#: accepted everywhere a view is (`to_spec`).
ViewModel = Annotated[Union[PlaneView, SlabView, SectionView], Field(discriminator="kind")]


def to_spec(view: Any, volume: Any = None) -> ViewSpec:
    """A token, a dict, a typed view or a `ViewSpec`, as one `ViewSpec`.

    Clipped to `volume` when one is given (anything with `depth`, `frames`,
    `width`, `height`, `default_view` -- a `Volume`). Raises `BadView` with a
    sentence for anything malformed or outside the image.
    """
    if isinstance(view, ViewSpec):
        return view.clipped(volume) if volume is not None and not view.default else view
    if isinstance(view, BaseModel):
        view = view.model_dump()
    if view is None or isinstance(view, str):
        return ViewSpec.parse(view, volume)
    if isinstance(view, dict):
        raw = dict(view)
        if raw.get("kind", "plane") == "plane" and raw.get("z") is None:
            raw["z"] = -1
        if raw.get("kind") == "slab" and raw.get("op") == "pct" and raw.get("q") is None:
            raise BadView("a percentile projection needs q (0-100)")
        return ViewSpec.from_dict(raw, volume)
    raise BadView(f"{view!r} is not a view")


def kind_of(view: Any) -> str:
    """`plane`, `slab` or `section`."""
    return to_spec(view).kind


def view_label(view: Any, volume: Any = None) -> str:
    """What a caption says about the picture ("MIP z 8–20")."""
    spec = to_spec(view, volume)
    return spec.label(getattr(volume, "depth", None), getattr(volume, "frames", None))


#: Conclusions a projection or a plane's labels cannot support, with the
#: sentence that says why. Raised as `inference_forbidden`.
FORBIDDEN_INFERENCES = {
    "voxel_z_from_projection": (
        "A projection has no z: every pixel in it may come from a different plane. "
        "Read the planes, or a section, to place anything in depth."),
    "colocalization_from_projection": (
        "Two markers overlapping in a projection may never touch in 3-D. Measure "
        "colocalization on voxels (colocalize_3d) or on one plane."),
    "object_from_plane_labels": (
        "Labels drawn plane by plane are not 3-D objects: one cell spans several "
        "planes and gets a label on each. Volume, surface and sphericity need a "
        "3-D label volume."),
}


def forbid(inference: str):
    """The `AgentError` for a forbidden inference (see FORBIDDEN_INFERENCES)."""
    from plexora.agent.errors import AgentError

    return AgentError("inference_forbidden", FORBIDDEN_INFERENCES[inference],
                      detail={"inference": inference})


def check_supported(view: Any, representations, *, capability: str = "this tool",
                    volume: Any = None) -> ViewSpec:
    """The view as a `ViewSpec`, or `representation_unsupported` when the
    capability's 2-D code does not accept that kind of picture.

    `representations` is a `Requires.representations` tuple. The refusal names
    what is accepted and, for the common case -- a projection handed to a
    plane-only tool -- the plane it would run on instead.
    """
    from plexora.agent.errors import AgentError

    try:
        spec = to_spec(view, volume)
    except BadView as exc:
        raise AgentError("invalid_input", str(exc)) from None
    accepted = tuple(representations or ("plane",))
    if spec.kind in accepted:
        return spec
    hint = None
    if spec.kind == "slab" and "plane" in accepted:
        middle = ((spec.z0 or 0) + (spec.z1 if spec.z1 is not None else (spec.z0 or 0))) // 2
        hint = (f"Run it on one plane instead -- e.g. view='z{middle}' -- and say which "
                "plane the result is about.")
    raise AgentError(
        "representation_unsupported",
        f"{capability} works on {' or '.join(_KIND_WORDS[k] for k in accepted)}, not on "
        f"{_KIND_WORDS[spec.kind]}: a result on {_KIND_WORDS[spec.kind]} would be "
        "mislabelled.",
        detail={"view": spec.token(), "kind": spec.kind, "accepted": list(accepted),
                "hint": hint})


_KIND_WORDS = {"plane": "a single plane", "slab": "a projection of planes",
               "section": "an orthogonal section"}
