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
from plexora.agent.schemas import ProjectInput
from plexora.api.plugin import Requires
from plexora.plugins.roi import PLUGIN

OWNER = "roi"
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


def create_roi(call, inp):
    ds = call.session.image_data(inp.project)
    before_rev, after_rev, roi = _guarded(
        _service().create_roi, ds, category=inp.category, geometry=inp.geometry,
        points=inp.points, name=inp.name, notes=inp.notes, color=inp.color,
        base_revision=inp.base_revision)
    receipt = make_receipt(call, changed=True, before=None, after=roi,
                           revision_before=before_rev, revision_after=after_rev,
                           persistent_state=STATE,
                           undo_hint={"tool": "delete_roi", "arguments": {
                               "project": inp.project, "roi_id": roi["id"],
                               "confirm": True}})
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


def update_roi(call, inp):
    ds = call.session.image_data(inp.project)
    before_rev, after_rev, before, after = _guarded(
        _service().update_roi, ds, inp.roi_id, name=inp.name, notes=inp.notes,
        category=inp.category, geometry=inp.geometry, points=inp.points,
        visible=inp.visible, locked=inp.locked, base_revision=inp.base_revision)
    receipt = make_receipt(call, changed=before != after, before=before, after=after,
                           revision_before=before_rev, revision_after=after_rev,
                           persistent_state=STATE,
                           undo_hint={"tool": "update_roi", "arguments": {
                               "project": inp.project, "roi_id": inp.roi_id,
                               "name": before["name"], "category": before["category"]}})
    return {"receipt": receipt.model_dump(mode="json"), "roi": after}


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
                               "name": deleted["name"]}})
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
