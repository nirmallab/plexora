"""Viewer control as capabilities: see and steer what an open tab shows.

Session-only by default. Changing the channels, the view or the layers of an
open tab changes what that tab shows and nothing on disk -- "the state a user
built by hand must survive" an agent showing them something. `persist: true`
on `viewer_set_channels` opts into the sidebar's own save, exactly as if the
user had made the change.

Every state-changing command is receipted and audited like any other write,
with `persistent_state: "none"` unless it persisted.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from plexora.agent.errors import AgentError
from plexora.agent.receipts import make_receipt
from plexora.agent.registry import Capability
from plexora.agent.render_spec import Bounds
from plexora.agent.schemas import AgentModel

TAGS = ("viewer", "view", "show", "display", "screen", "open", "tab", "browser")


class ViewInput(AgentModel):
    view_id: str | None = Field(None, description="Which open viewer (from list_viewers); "
                                "default the only one.")
    project: str | None = Field(None, description="Pick the viewer showing this project.")


def _control(call):
    from plexora.agent import viewer

    return viewer.require(call.link)


def _view(call, inp):
    from plexora.agent import viewer

    control = _control(call)
    return control, viewer.resolve_view(control, inp.view_id, getattr(inp, "project", None))


def _send(call, inp, type, arguments, *, timeout=None):
    control, view = _view(call, inp)
    ack = control.send(view["view_id"], type, arguments, timeout=timeout)
    return control, view, ack


def _receipted(call, view, ack, *, persisted=False, before=None):
    if call.project_name is None:
        call.project_name = view.get("project")
    receipt = make_receipt(
        call, changed=True, before=before,
        after={"command": ack.get("type"), "arguments": ack.get("arguments"),
               "result": ack.get("result")},
        revision_before=ack.get("expected_revision"),
        revision_after=ack.get("resulting_revision"),
        persistent_state="config" if persisted else "none", reversible=True)
    return {"view_id": view["view_id"], "ack": {k: ack.get(k) for k in
                                                ("command_id", "status", "result", "warning",
                                                 "resulting_revision")},
            "receipt": receipt.model_dump(mode="json")}


class ListViewersInput(AgentModel):
    project: str | None = None


def list_viewers(call, inp):
    from plexora.agent import viewer

    control = viewer.connect(call.link)
    if control is None:
        return {"attached": False, "viewers": [],
                "hint": "not attached to a running Plexora server; " + viewer.NOT_AVAILABLE_HINT}
    return {"attached": True, "transport": control.kind,
            "viewers": control.list_sessions(inp.project)}


def get_state(call, inp):
    _control_, view, ack = _send(call, inp, "get_state", {})
    return {"view_id": view["view_id"], "state": ack.get("result"),
            "revision": ack.get("resulting_revision")}


class OpenProjectInput(ViewInput):
    open: str = Field(description="The project to open in the viewer.")
    tool: str | None = Field(None, description="A tool to open with it, e.g. 'gating'.")


def open_project(call, inp):
    call.session.project(inp.open)   # unknown_project before anything is sent
    _c, view, ack = _send(call, inp, "open_project", {"project": inp.open, "tool": inp.tool})
    call.project_name = inp.open
    return _receipted(call, view, ack)


class ViewerChannel(AgentModel):
    name: str
    color: str | None = Field(None, description="#rrggbb")
    window: list[float] | Literal["auto"] | None = None
    enabled: bool = True


class SetChannelsInput(ViewInput):
    channels: list[ViewerChannel] = Field(max_length=12)
    mode: Literal["replace", "merge"] = Field(
        "replace", description="replace: show exactly these; merge: change only these.")
    persist: bool = Field(False, description="Also save to the project's channel list, as "
                          "if the user had made the change. Default: this tab only.")


def set_channels(call, inp):
    _c, view, ack = _send(call, inp, "set_channels", {
        "channels": [c.model_dump() for c in inp.channels], "mode": inp.mode,
        "persist": inp.persist})
    return _receipted(call, view, ack, persisted=inp.persist)


class NavigateInput(ViewInput):
    bounds: Bounds | None = Field(None, description="Fit this full-resolution pixel box.")
    center: list[float] | None = Field(None, min_length=2, max_length=2,
                                       description="[x, y] to centre on.")
    size_px: float | None = Field(None, gt=0, description="With center: field width in px.")
    size_um: float | None = Field(None, gt=0, description="With center: field width in µm.")
    cell_id: int | None = Field(None, description="Centre on this cell (by the table's id).")
    roi_id: str | None = Field(None, description="Fit this region.")


def navigate(call, inp):
    control, view = _view(call, inp)
    project = view.get("project")
    arguments = None
    command = None
    if inp.cell_id is not None:
        if not project:
            raise AgentError("invalid_input", "the viewer has no project open")
        data = call.session.data(project)
        frame = data.table.geometry()
        schema = data.schema
        column = schema.cell_id if schema and schema.cell_id in frame.columns else "id"
        import polars as pl

        match = frame.filter(pl.col(column) == inp.cell_id)
        if match.height == 0:
            raise AgentError("invalid_input", f"no cell {inp.cell_id} in {project!r}")
        x, y = float(match[schema.x][0]), float(match[schema.y][0])
        size = inp.size_px or 200.0
        command, arguments = "focus_cell", {"cell_id": inp.cell_id, "x": x, "y": y,
                                            "width": size}
    elif inp.roi_id is not None:
        from plexora.agent.render import _roi_box

        (x0, y0, x1, y1), _feature = _roi_box(project, inp.roi_id)
        pad = 0.1 * max(x1 - x0, y1 - y0, 1.0)
        command, arguments = "focus_roi", {"roi_id": inp.roi_id, "x": x0 - pad, "y": y0 - pad,
                                           "width": x1 - x0 + 2 * pad,
                                           "height": y1 - y0 + 2 * pad}
    elif inp.bounds is not None:
        command, arguments = "fit_region", inp.bounds.model_dump()
    elif inp.center is not None:
        size = inp.size_px
        if inp.size_um is not None:
            from plexora.server.utils import pixel_scale

            pixel = pixel_scale.pixel_size(call.session.project(project)) if project else None
            if not pixel:
                raise AgentError("precondition_missing", "size_um needs a calibrated image")
            size = inp.size_um / pixel["value"]
        if size:
            command, arguments = "fit_region", {"x": inp.center[0] - size / 2,
                                                "y": inp.center[1] - size / 2,
                                                "width": size, "height": size}
        else:
            command, arguments = "pan_to", {"x": inp.center[0], "y": inp.center[1]}
    else:
        raise AgentError("invalid_input", "give bounds, center, cell_id or roi_id")
    ack = control.send(view["view_id"], command, arguments)
    return _receipted(call, view, ack)


class LayerInput(ViewInput):
    layer_id: str = Field(description="A layer id from the viewer's state "
                          "(`__image__`, `__mask__`, `__centroids__`, or a registered layer).")
    visible: bool | None = None
    opacity: float | None = Field(None, ge=0, le=1)


def set_layer(call, inp):
    control, view = _view(call, inp)
    acks = []
    if inp.visible is not None:
        acks.append(control.send(view["view_id"], "set_layer_visibility",
                                 {"layer_id": inp.layer_id, "visible": inp.visible}))
    if inp.opacity is not None:
        acks.append(control.send(view["view_id"], "set_layer_opacity",
                                 {"layer_id": inp.layer_id, "opacity": inp.opacity}))
    if not acks:
        raise AgentError("invalid_input", "give visible and/or opacity")
    return _receipted(call, view, acks[-1])


class ToolInput(ViewInput):
    tool: str = Field(description="A tool name, e.g. 'gating', 'roi', 'cell_explorer'.")
    close: bool = False


def open_tool(call, inp):
    _c, view, ack = _send(call, inp, "close_tool" if inp.close else "open_tool",
                          {"tool": inp.tool})
    return _receipted(call, view, ack)


class CellModeInput(ViewInput):
    mode: str = Field(description="How cells are drawn: e.g. 'outlines', 'filled', "
                      "'centroids', 'off' (the viewer's own modes).")


def set_cell_mode(call, inp):
    _c, view, ack = _send(call, inp, "set_cell_render_mode", {"mode": inp.mode})
    return _receipted(call, view, ack)


class MarkerInput(ViewInput):
    marker: str


def set_active_marker(call, inp):
    _c, view, ack = _send(call, inp, "set_active_marker", {"marker": inp.marker})
    return _receipted(call, view, ack)


def capture(call, inp):
    from plexora.agent import artifacts
    from plexora.agent.core.visual import with_image

    control, view, ack = _send(call, inp, "capture_view", {}, timeout=30)
    artifact = (ack.get("result") or {}).get("artifact") or {}
    art_id = artifact.get("id")
    if not art_id:
        raise AgentError("viewer_not_responding", "the viewer did not return a capture",
                         detail=ack)
    try:
        png, sidecar = artifacts.get(art_id)
    except KeyError:
        status, png = control.link.request("GET", f"/agent/v1/captures/{art_id}", timeout=10)
        if status != 200:
            raise AgentError("viewer_not_responding", "the capture could not be fetched")
        sidecar = {"manifest": None}
    return with_image({"view_id": view["view_id"], "artifact": artifact,
                       "manifest": sidecar.get("manifest"),
                       "state": (ack.get("result") or {}).get("state")}, png)


class EvidenceInput(ViewInput):
    artifact_id: str = Field(description="A stored render (art_...) to show the user.")
    caption: str = ""


def show_evidence(call, inp):
    _c, view, ack = _send(call, inp, "show_evidence", {
        "artifact_id": inp.artifact_id, "caption": inp.caption,
        "url": f"agent/v1/captures/{inp.artifact_id}"})
    return {"view_id": view["view_id"], "ack": ack}


def capabilities():
    def cap(**kwargs):
        kwargs.setdefault("permission", "reversible_write")
        kwargs.setdefault("viewer_required", True)
        return Capability(owner="core", tags=TAGS, **kwargs)

    return [
        Capability(name="viewer.list", tool_name="list_viewers", owner="core",
                   purpose="The Plexora viewer tabs open right now (desktop, browser or "
                           "notebook), with the project each shows.",
                   permission="read", input_model=ListViewersInput, handler=list_viewers,
                   tags=TAGS),
        cap(name="viewer.get_state", tool_name="viewer_get_state",
            purpose="What an open viewer is showing: project, viewport, channels with "
                    "colours and windows, layers, cell-drawing mode, open tools.",
            permission="read", input_model=ViewInput, handler=get_state),
        cap(name="viewer.open_project", tool_name="viewer_open_project",
            purpose="Open a project in the viewer (optionally with a tool).",
            input_model=OpenProjectInput, handler=open_project),
        cap(name="viewer.set_channels", tool_name="viewer_set_channels",
            purpose="Choose which channels the viewer shows, with colours and windows. "
                    "This tab only unless persist=true.",
            input_model=SetChannelsInput, handler=set_channels),
        cap(name="viewer.navigate", tool_name="viewer_navigate",
            purpose="Move the viewer: fit a box, centre on a point (optionally at a field "
                    "size in px or µm), on a cell by id, or on a region.",
            input_model=NavigateInput, handler=navigate),
        cap(name="viewer.set_layer", tool_name="viewer_set_layer",
            purpose="Show, hide or fade a layer in the viewer.",
            input_model=LayerInput, handler=set_layer),
        cap(name="viewer.open_tool", tool_name="viewer_open_tool",
            purpose="Open (or close) a tool panel in the viewer, e.g. gating.",
            input_model=ToolInput, handler=open_tool),
        cap(name="viewer.set_cell_mode", tool_name="viewer_set_cell_mode",
            purpose="How the viewer draws cells: outlines, filled, centroids, off.",
            input_model=CellModeInput, handler=set_cell_mode),
        cap(name="viewer.set_active_marker", tool_name="viewer_set_active_marker",
            purpose="Make a marker the one the gating panel shows and colours by.",
            input_model=MarkerInput, handler=set_active_marker),
        cap(name="viewer.capture", tool_name="viewer_capture",
            purpose="A picture of exactly what the viewer shows now, stored as an artifact.",
            permission="read", input_model=ViewInput, handler=capture, visual_output=True,
            egress="rendered_pixels"),
        cap(name="viewer.show_evidence", tool_name="viewer_show_evidence",
            purpose="Show the user a stored render in the viewer, in a dialog over the image.",
            permission="read", input_model=EvidenceInput, handler=show_evidence),
    ]
