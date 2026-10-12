"""What an external agent may ask the ROI plugin to do.

Every write goes through `ROIRepository.apply` (via `server/service.py`), the
same revision-checked path the panel's autosave takes, so an agent's edit and
an open panel's edit conflict with each other instead of one erasing the other.
Deleting a region is the one thing here that cannot be undone from Plexora's
side, so it is classed destructive and needs the server's permission and the
call's `confirm: true`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from plexora.agent.errors import AgentError
from plexora.agent.limits import MAX_IDS, MAX_VERTICES
from plexora.agent.receipts import make_receipt
from plexora.agent.registry import Capability
from plexora.agent.schemas import AgentModel, ProjectInput
from plexora.api.plugin import Requires
from plexora.plugins.roi import PLUGIN

OWNER = "roi"

#: A region made 3-D -- a shape through the planes, carried, or exactly some
#: cells of a 3-D mask -- is Plexora 3D (plexora/licensing/manifest.py). Flat
#: regions, and flattening a 3-D one, stay Free.
SOLID_ENTITLEMENT = "volume:objects"


def _require_3d(name):
    """Refuse the 3-D half of a Free capability on a licence without it, as
    the registry refuses an entitled capability (over MCP: `mcp` too)."""
    from types import SimpleNamespace

    from plexora.agent.registry import CALL_ORIGIN
    from plexora.licensing import guards

    probe = SimpleNamespace(name=name, entitlement=SOLID_ENTITLEMENT)
    guards.check_capability(probe)
    guards.check_origin(probe, CALL_ORIGIN.get())
STATE = "plugin_store:roi"
TAGS = ("roi", "region", "regions", "annotation", "annotate", "polygon", "area")


def _service():
    from plexora.plugins.roi.server import service

    return service


def _conflict(exc):
    return AgentError("conflict", str(exc),
                      detail={"current_revision": exc.current_revision}, retryable=True)


def _guarded(fn, *args, **kwargs):
    from plexora.plugins.roi.server.repository import ConflictError, ImageMismatch

    try:
        return fn(*args, **kwargs)
    except ConflictError as exc:
        raise _conflict(exc) from exc
    except ImageMismatch as exc:
        raise AgentError("precondition_missing",
                         "the stored regions were drawn on an image of a different size",
                         detail={"stored": exc.stored, "current": exc.current}) from exc


def list_rois(call, inp):
    revision, rois = _service().list_rois(call.session.image_data(inp.project))
    return {"revision": revision, "rois": rois, "count": len(rois)}


class RoiInput(ProjectInput):
    roi_id: str = Field(description="The region's id, from `list_rois`.")


def get_roi(call, inp):
    revision, roi = _guarded(_service().get_roi, call.session.image_data(inp.project),
                             inp.roi_id, max_vertices=MAX_VERTICES)
    return {"revision": revision, "roi": roi}


Point = list[float]


class ExtentInput(AgentModel):
    """Which planes of a z-stack a region covers. Plane indices, inclusive."""

    kind: Literal["all", "plane", "slab"] = Field(
        "all", description="all: every plane (a flat image's only meaning); plane: one "
                           "plane `z`; slab: planes `z0` to `z1`.")
    z: int | None = Field(None, ge=0)
    z0: int | None = Field(None, ge=0)
    z1: int | None = Field(None, ge=0)
    t: int = Field(0, ge=0, description="Frame of a time series.")

    def to_extent(self) -> dict:
        return self.model_dump(exclude_none=True)


class SolidInput(AgentModel):
    """A region's shape through a z-stack (its geometry stays its footprint)."""

    kind: Literal["ellipsoid", "contours", "labels"] = Field(
        description="ellipsoid: the geometry is the outline on its middle plane `z` and it "
                    "tapers to nothing `rz` planes above and below; contours: one outline "
                    "per plane in `planes` (consecutive planes, no gaps) -- usually made by "
                    "carry_roi_through_planes rather than by hand.")
    z: float | None = Field(None, ge=0, description="ellipsoid: its middle plane.")
    rz: float | None = Field(None, gt=0, description="ellipsoid: half its depth, in planes.")
    planes: dict[str, dict[str, Any]] | None = Field(
        None, description="contours: {plane index: GeoJSON Polygon in image pixels}.")
    t: int = Field(0, ge=0, description="Frame of a time series.")

    def to_solid(self) -> dict:
        return self.model_dump(exclude_none=True)


class CreateInput(ProjectInput):
    category: str = Field(description="Category label; created if the project has none "
                          "by that name (matched case-insensitively).")
    points: list[Point] | None = Field(None, description="One ring of [x, y] vertices in "
                                       "full-resolution image pixels.")
    geometry: dict[str, Any] | None = Field(None, description="GeoJSON Polygon or "
                                            "MultiPolygon, in image pixels, instead of points.")
    name: str = ""
    notes: str = ""
    color: str | None = Field(None, description="#rrggbb, for a new category.")
    base_revision: int | None = Field(None, description="The revision last read; refused "
                                      "if the regions changed since.")
    method: Literal["freehand", "polygon", "rectangle", "ellipse", "sam"] | None = Field(
        None, description="Which tool drew the shape, recorded as provenance.")
    extent: ExtentInput | None = Field(
        None, description="On a z-stack: the planes the region covers (default: all).")
    solid: SolidInput | None = Field(
        None, description="On a z-stack: its shape through the planes (sets the planes "
                          "it covers too).")
    cells: list[int] | None = Field(
        None, description="On a z-stack with a 3-D mask: make the region exactly these cells "
                          "(label ids, which are the table's cell ids), outlined plane by plane "
                          "from the mask -- instead of geometry or points.")
    t: int = Field(0, ge=0, description="With `cells`: the frame of a time series.")


def create_roi(call, inp):
    if inp.solid or inp.cells:
        _require_3d("roi.create")
    ds = call.session.image_data(inp.project)
    solid = inp.solid.to_solid() if inp.solid else None
    geometry = inp.geometry
    if inp.cells:
        try:
            solid = _service().labels_solid(ds, inp.cells, t=inp.t)
        except LookupError as exc:
            raise AgentError("precondition_missing", str(exc)) from None
        geometry = _service().footprint_of(solid["planes"])
    before_rev, after_rev, roi = _guarded(
        _service().create_roi, ds, category=inp.category, geometry=geometry,
        points=inp.points, name=inp.name, notes=inp.notes, color=inp.color,
        base_revision=inp.base_revision, method=inp.method,
        extent=inp.extent.to_extent() if inp.extent else None,
        solid=solid)
    receipt = make_receipt(call, changed=True, before=None, after=roi,
                           revision_before=before_rev, revision_after=after_rev,
                           persistent_state=STATE,
                           undo_hint={"tool": "delete_roi", "arguments": {
                               "project": inp.project, "roi_id": roi["id"],
                               "confirm": True, "base_revision": after_rev}})
    return {"receipt": receipt.model_dump(mode="json"), "roi": roi}


class UpdateInput(RoiInput):
    name: str | None = None
    notes: str | None = None
    category: str | None = None
    points: list[Point] | None = None
    geometry: dict[str, Any] | None = None
    visible: bool | None = None
    locked: bool | None = None
    base_revision: int | None = None
    method: Literal["freehand", "polygon", "rectangle", "ellipse", "sam"] | None = Field(
        None, description="With a new outline: which tool drew it.")
    extent: ExtentInput | None = Field(
        None, description="On a z-stack: the planes the region covers; kind 'all' for every plane.")
    solid: SolidInput | None = Field(
        None, description="On a z-stack: its shape through the planes.")
    flatten: bool = Field(False, description="Drop its shape through the planes (keep its "
                                             "footprint, on the planes in `extent`).")


def update_roi(call, inp):
    if inp.solid is not None:
        _require_3d("roi.update")
    ds = call.session.image_data(inp.project)
    before_rev, after_rev, before, after = _guarded(
        _service().update_roi, ds, inp.roi_id, name=inp.name, notes=inp.notes,
        category=inp.category, geometry=inp.geometry, points=inp.points,
        visible=inp.visible, locked=inp.locked, base_revision=inp.base_revision,
        method=inp.method, extent=inp.extent.to_extent() if inp.extent else None,
        solid=inp.solid.to_solid() if inp.solid else None, flatten=inp.flatten)
    undo = {"project": inp.project, "roi_id": inp.roi_id, "name": before["name"],
            "notes": before["notes"], "category": before["category"],
            "visible": before["visible"], "locked": before["locked"],
            "base_revision": after_rev}
    if inp.extent is not None:
        undo["extent"] = before.get("extent") or {"kind": "all"}
    reshaped = inp.geometry is not None or bool(inp.points)
    if inp.solid is not None or inp.flatten:
        # Back to how it was: flat (its old footprint and planes), or a solid
        # whose outlines a summary does not carry -- then the hint is partial.
        reshaped = True
        if before.get("solid"):
            undo["geometry"] = None
            before = dict(before, geometry=None)
        else:
            undo.update(flatten=True, extent=before.get("extent") or {"kind": "all"})
    # The summary carries the old outline only up to a vertex budget; past it
    # the hint cannot put the shape back, and says so by not claiming to.
    partial = reshaped and before.get("geometry") is None
    if reshaped and not partial:
        undo["geometry"] = before["geometry"]
    hint = {"tool": "update_roi", "arguments": undo}
    if partial:
        hint["partial"] = True
    receipt = make_receipt(call, changed=before != after, before=before, after=after,
                           revision_before=before_rev, revision_after=after_rev,
                           persistent_state=STATE, reversible=not partial,
                           undo_hint=hint)
    return {"receipt": receipt.model_dump(mode="json"), "roi": after}


class CarryInput(RoiInput):
    z: int | None = Field(None, ge=0, description="The plane whose outline is carried "
                          "(default: the middle of the planes the region covers).")
    z_first: int | None = Field(None, ge=0, description="Go no further down than this plane.")
    z_last: int | None = Field(None, ge=0, description="Go no further up than this plane.")
    engine: Literal["auto", "sam2", "mobile"] = Field(
        "auto", description="auto: SAM 2 through the planes when the 3-D extra is installed, "
                            "else MobileSAM plane by plane (which tends to run past the "
                            "object's poles in blurred stacks).")
    channels: list[str] | None = Field(None, description="Channel names to outline from "
                                       "(default: the first channel).")
    base_revision: int | None = None


def carry_roi(call, inp):
    from plexora.vision.segment import SegmentError

    ds = call.session.image_data(inp.project)
    z_range = None
    if inp.z_first is not None or inp.z_last is not None:
        z_range = (inp.z_first if inp.z_first is not None else 0,
                   inp.z_last if inp.z_last is not None else 10**6)
    try:
        before_rev, after_rev, before, after, result = _guarded(
            _service().carry_through_planes, ds, call.session, inp.roi_id, z=inp.z,
            z_range=z_range, engine=inp.engine,
            channels=[{"name": name} for name in inp.channels] if inp.channels else None,
            base_revision=inp.base_revision)
    except SegmentError as exc:
        raise AgentError("precondition_missing" if exc.code == "precondition_missing"
                         else "invalid_input", str(exc), detail=exc.detail) from exc
    except ValueError as exc:
        raise AgentError("invalid_input", str(exc)) from exc
    undo = {"project": inp.project, "roi_id": inp.roi_id, "base_revision": after_rev}
    partial = bool(before.get("solid")) or before.get("geometry") is None
    if not partial:
        undo.update(flatten=True, geometry=before["geometry"],
                    extent=before.get("extent") or {"kind": "all"})
    hint = {"tool": "update_roi", "arguments": undo, **({"partial": True} if partial else {})}
    receipt = make_receipt(call, changed=True, before=before, after=after,
                           revision_before=before_rev, revision_after=after_rev,
                           persistent_state=STATE, reversible=not partial, undo_hint=hint)
    return {"receipt": receipt.model_dump(mode="json"), "roi": after,
            "carried": {key: result[key] for key in
                        ("start", "range", "stopped", "engine", "per_plane", "timing",
                         "provenance") if key in result}}


class DeleteInput(RoiInput):
    confirm: Literal[True] = Field(description="Must be true, on the user's explicit request.")
    base_revision: int | None = None


def delete_roi(call, inp):
    ds = call.session.image_data(inp.project)
    before_rev, after_rev, deleted = _guarded(
        _service().delete_roi, ds, inp.roi_id, base_revision=inp.base_revision)
    receipt = make_receipt(call, changed=True, before=deleted, after=None,
                           revision_before=before_rev, revision_after=after_rev,
                           persistent_state=STATE, reversible=False,
                           undo_hint={"tool": "create_roi", "arguments": {
                               "project": inp.project, "category": deleted["category"],
                               "geometry": deleted.get("geometry"),
                               "name": deleted["name"], "notes": deleted["notes"],
                               "base_revision": after_rev}})
    return {"receipt": receipt.model_dump(mode="json")}


class CellsInput(RoiInput):
    max_ids: int = Field(200, ge=0, le=MAX_IDS)


def cells_in_roi(call, inp):
    try:
        return _guarded(_service().cells_in_roi, call.data, inp.roi_id,
                        max_ids=inp.max_ids)
    except LookupError as exc:
        if isinstance(exc, KeyError):
            raise
        raise AgentError("precondition_missing", str(exc),
                         detail={"missing": [{"key": "table", "label": "Cell table"}]}) from exc


def capabilities():
    image_only = PLUGIN.requires

    def cap(**kwargs):
        kwargs.setdefault("requires", image_only)
        kwargs.setdefault("reads", ("rois",))
        return Capability(owner=OWNER, tags=TAGS, **kwargs)

    return [
        cap(name="roi.list", tool_name="list_rois",
            purpose="Every region drawn on a project, with category, bounds and size.",
            permission="read", input_model=ProjectInput, handler=list_rois),
        cap(name="roi.get", tool_name="get_roi",
            purpose="One region, with its geometry (vertices capped).",
            permission="read", input_model=RoiInput, handler=get_roi),
        cap(name="roi.create", tool_name="create_roi",
            purpose="Draw a region (polygon in image pixels) in a category. Reversible; "
                    "the panel sees it on its next load.",
            permission="reversible_write", input_model=CreateInput, handler=create_roi,
            writes=("rois",), persistent=True),
        cap(name="roi.update", tool_name="update_roi",
            purpose="Rename, recategorize, reshape, hide or lock a region.",
            permission="reversible_write", input_model=UpdateInput, handler=update_roi,
            writes=("rois",), persistent=True),
        cap(name="roi.carry", tool_name="carry_roi_through_planes",
            entitlement=SOLID_ENTITLEMENT,
            purpose="On a z-stack: follow a region's outline up and down the planes until "
                    "the object ends, and keep one outline per plane as the region's 3-D "
                    "shape. Says why each direction stopped. Reversible.",
            permission="reversible_write", input_model=CarryInput, handler=carry_roi,
            writes=("rois",), persistent=True),
        cap(name="roi.delete", tool_name="delete_roi",
            purpose="Delete a region. Destructive: needs --allow-destructive and "
                    "confirm: true.",
            permission="destructive", input_model=DeleteInput, handler=delete_roi,
            writes=("rois",), reversible=False, persistent=True),
        cap(name="roi.cells", tool_name="count_cells_in_roi",
            purpose="How many cells (by centroid) fall inside a region, and which -- "
                    "counted only; nothing is written onto the cells.",
            permission="read", input_model=CellsInput, handler=cells_in_roi,
            requires=Requires(table=True, roles=("x", "y")), egress="aggregates",
            reads=("rois", "table")),
    ]
