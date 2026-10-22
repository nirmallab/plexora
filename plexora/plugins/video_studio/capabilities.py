"""Video Studio for an agent: storyboard a video, show it, export it.

The agent works on the same documents the Quick Capture bar and the timeline
do, through the same schema -- so an agent cannot make a track the UI would
refuse (an interpolated timepoint, a blended colour), and a user can open and
edit whatever an agent made.

    list_videos / get_video        read
    create_video                   a preset's storyboard, saved (undo: delete_video)
    add_video_keyframe /
    set_video_track / edit_video                     edits as operations (undo: edit_video with the inverse)
    preview_video                  plays or poses it in the open viewer
    export_video                   a job: the open viewer renders, the server encodes
    duplicate_video / delete_video

Rendering needs the live viewer, so `preview_video` and `export_video` need an
open tab; everything else works from the project record alone. Pro with the
plugin (`plugin:video_studio`, which the Pro default carries), so over MCP
these need external MCP access (`mcp`) too.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import Field

from plexora.agent.errors import AgentError
from plexora.agent.receipts import make_receipt
from plexora.agent.registry import Capability
from plexora.agent.schemas import AgentModel

OWNER = "video_studio"
TAGS = ("video", "animation", "movie", "orbit", "rotation", "storyboard", "3d", "z-stack",
        "time-lapse", "export", "mp4")
STATE = "plugin_document"

#: The presets an agent may name: server/presets.py PRESETS, spelled out so
#: the tool's schema can offer them as a choice (a test holds the two equal).
PRESET_NAMES = ("orbit", "rock", "tumble", "fly_through", "slice_reveal", "z_sweep", "time_lapse",
                "zoom", "xy_pan", "channel_reveal", "story_overview", "story_structure",
                "story_markers", "story_orthogonal", "story_timeseries")


class VideoRef(AgentModel):
    video_id: str = Field(description="The video, as list_videos or create_video returns it.")


class ListInput(AgentModel):
    project: str | None = Field(None, description="Only videos made on this project.")


class CreateInput(AgentModel):
    project: str = Field(description="Project name, as list_projects returns it.")
    preset: Literal[PRESET_NAMES] = Field(
        description="orbit (360° rotation), rock, tumble, fly_through, slice_reveal, z_sweep, "
                    "time_lapse, zoom, xy_pan, channel_reveal, or a storyboard: "
                    "story_overview, story_structure, story_markers, story_orthogonal, "
                    "story_timeseries.")
    params: dict[str, Any] = Field(default_factory=dict, description=(
        "The preset's parameters (get_video on any video, or the bar, shows them); omitted "
        "ones take defaults from the image. Channel keys for `order` as list_channels "
        "returns them."))
    title: str | None = None
    from_viewer: bool = Field(True, description="Start from what the open viewer shows "
                              "(camera, plane, channels, windows). False, or no viewer open: "
                              "from the image's defaults.")
    view_id: str | None = None


class KeyframeInput(VideoRef):
    track_id: str | None = Field(None, description="Add to this track; omit to add a new "
                                 "track of `kind`.")
    kind: str | None = Field(None, description="For a new track: camera3d, camera2d, "
                             "acquired_z, acquired_t, slab, view_mode, clip, channel, hud.")
    target: str | None = Field(None, description="A channel key for a channel track.")
    t: float = Field(ge=0, description="Seconds from the start.")
    value: Any = Field(description="The keyframe's value for the track's kind (a camera "
                       "{q, dist, pan}, a whole plane index, {presence: 0..1}, ...). Acquired "
                       "planes and timepoints are whole numbers and change in steps.")
    ease: str = "linear"
    base_revision: int | None = None


class TrackInput(VideoRef):
    track: dict[str, Any] = Field(description="A whole track. Replaces the track with the "
                                  "same track_id, or is added.")
    base_revision: int | None = None


class EditInput(VideoRef):
    operations: list[dict[str, Any]] = Field(description=(
        "Operations, applied all or nothing: set_title, set_duration, set_loop, set_output, "
        "add_track, remove_track, update_track, move_track, add_keyframe, update_keyframe, "
        "remove_keyframe, set_viewpoint, set_target, set_scenes."))
    base_revision: int | None = Field(None, description="Refuse if the video changed since.")


class PreviewInput(VideoRef):
    at_s: float | None = Field(None, ge=0, description="Show this moment and stop.")
    play: bool = Field(True, description="Play from `at_s` (or the start).")
    view_id: str | None = None


class ExportInput(VideoRef):
    output: dict[str, Any] = Field(default_factory=dict, description=(
        "width, height, fps (24/25/30/50/60), format (mp4, webm, mov, gif, png), quality "
        "(draft/good/high/max), overlays {scalebar, label, dataset, legend, axes, "
        "attribution, captions}."))
    view_id: str | None = None


class DeleteInput(VideoRef):
    confirm: bool = Field(False, description="Must be true: deleting a video removes its "
                          "exports with it.")


class DuplicateInput(VideoRef):
    title: str | None = None


def _repo():
    from plexora.plugins.video_studio.server import repository

    return repository


def _schema():
    from plexora.plugins.video_studio.server import schema

    return schema


def _load(video_id):
    repository = _repo()
    schema = _schema()
    try:
        schema.validate_video_id(video_id)
        return repository.load(video_id)
    except ValueError as exc:
        raise AgentError("invalid_input", str(exc)) from None
    except repository.UnknownVideo:
        raise AgentError("invalid_input", f"no video {video_id!r}",
                         detail={"hint": "list_videos"}) from None
    except schema.UnreadableVideo as exc:
        raise AgentError("resource_unavailable", str(exc)) from None


def _summary(document):
    schema = _schema()
    fps = document["output"]["fps"]
    return {"video_id": document["video_id"], "revision": document["revision"],
            "title": document["title"], "project": document["source"]["datasource"],
            "duration_s": document["duration_s"], "loop": document["loop"],
            "frames_at_output_fps": schema.frame_count(document["duration_s"], fps),
            "output": document["output"], "tracks": schema.manifest_tracks(document),
            "notes": document.get("notes") or [], "warnings": schema.validate(document),
            "preset": (document.get("provenance") or {}).get("preset")}


def _apply(call, video_id, ops, base_revision):
    repository = _repo()
    schema = _schema()
    document = _load(video_id)
    revision = document["revision"] if base_revision is None else base_revision
    try:
        result = repository.apply(video_id, revision, ops,
                                  origin=f"agent:{getattr(call, 'operation_id', '')}")
    except repository.ConflictError as exc:
        raise AgentError("conflict", "the video changed since base_revision",
                         detail={"revision": exc.current_revision}) from None
    except schema.IntegrityError as exc:
        raise AgentError("invalid_input", str(exc),
                         detail={"rule": "acquired timepoints and planes change only in whole "
                                         "steps; colours are held"}) from None
    except ValueError as exc:
        raise AgentError("invalid_input", str(exc)) from None
    receipt = make_receipt(
        call, changed=True, revision_before=revision, revision_after=result["revision"],
        after={"video_id": video_id, "revision": result["revision"]},
        persistent_state=STATE,
        undo_hint={"tool": "edit_video", "arguments": {
            "video_id": video_id, "operations": result["inverse"],
            "base_revision": result["revision"]}})
    return {"video": _summary(result["document"]), "receipt": receipt.model_dump(mode="json")}


# -- read ---------------------------------------------------------------------


def list_videos(call, inp):
    return {"videos": _repo().list_videos(inp.project)}


def get_video(call, inp):
    document = _load(inp.video_id)
    return {"video": _summary(document), "exports": _repo().list_exports(inp.video_id)}


# -- create and edit -----------------------------------------------------------


def _viewer_scene(call, project, view_id):
    """The open viewer's scene, contributed by Video Studio's own agent bridge
    once the tool is open (the 3-D camera lives only in the page)."""
    from plexora.agent import viewer

    control = viewer.connect(call.link)
    if control is None:
        return None, "No viewer is open; the video starts from the image's defaults."
    try:
        view = viewer.resolve_view(control, view_id, project)
        control.send(view["view_id"], "open_tool", {"tool": OWNER})
        ack = control.send(view["view_id"], "get_state", {})
    except AgentError as exc:
        return None, f"Could not read the viewer ({exc.code}); started from defaults."
    state = (ack or {}).get("result") or {}
    scene = ((state.get("plugins") or {}).get(OWNER) or {}).get("scene")
    if not isinstance(scene, dict):
        return None, "The viewer did not report a scene; started from the image's defaults."
    return scene, None


def create_video(call, inp):
    from plexora.plugins.video_studio.server import facts, presets

    try:
        image_facts = facts.dataset_facts(inp.project)
    except KeyError:
        raise AgentError("unknown_project", f"no project {inp.project!r}",
                         detail={"hint": "list_projects"}) from None
    scene, note = (None, None)
    if inp.from_viewer:
        scene, note = _viewer_scene(call, inp.project, inp.view_id)
    if inp.preset not in presets.PRESETS:
        raise AgentError("invalid_input", f"no preset {inp.preset!r}",
                         detail={"presets": sorted(presets.PRESETS)})
    try:
        document = presets.build(inp.preset, image_facts, scene, inp.params,
                                 created_by="agent")
    except ValueError as exc:
        raise AgentError("invalid_input", str(exc)) from None
    if note:
        document["notes"] = [*document.get("notes", []), note]
    video_id = _repo().create(document, title=inp.title)
    saved = _repo().load(video_id)
    call.project_name = inp.project
    receipt = make_receipt(call, changed=True, after={"video_id": video_id},
                           revision_after=saved["revision"], persistent_state=STATE,
                           undo_hint={"tool": "delete_video",
                                      "arguments": {"video_id": video_id, "confirm": True}})
    return {"video": _summary(saved), "receipt": receipt.model_dump(mode="json")}


def add_keyframe(call, inp):
    document = _load(inp.video_id)
    keyframe = {"t": inp.t, "value": inp.value, "ease": inp.ease}
    if inp.track_id:
        ops = [{"op": "add_keyframe", "track_id": inp.track_id, "keyframe": keyframe}]
    else:
        if not inp.kind:
            raise AgentError("invalid_input", "pass track_id, or kind for a new track")
        ops = [{"op": "add_track", "track": {"kind": inp.kind, "target": inp.target,
                                             "mode": "keyframes", "keyframes": [keyframe],
                                             "range": [0, document["duration_s"]]}}]
    return _apply(call, inp.video_id, ops, inp.base_revision)


def set_track(call, inp):
    document = _load(inp.video_id)
    track = dict(inp.track)
    existing = {t["track_id"] for t in document["tracks"]}
    if track.get("track_id") in existing:
        changes = {k: v for k, v in track.items() if k not in ("track_id", "kind", "class")}
        ops = [{"op": "update_track", "track_id": track["track_id"], "changes": changes}]
    else:
        ops = [{"op": "add_track", "track": track}]
    return _apply(call, inp.video_id, ops, inp.base_revision)


def edit_video(call, inp):
    return _apply(call, inp.video_id, inp.operations, inp.base_revision)


def duplicate_video(call, inp):
    _load(inp.video_id)
    new_id = _repo().duplicate(inp.video_id, title=inp.title)
    receipt = make_receipt(call, changed=True, after={"video_id": new_id},
                           persistent_state=STATE,
                           undo_hint={"tool": "delete_video",
                                      "arguments": {"video_id": new_id, "confirm": True}})
    return {"video": _summary(_repo().load(new_id)), "receipt": receipt.model_dump(mode="json")}


def delete_video(call, inp):
    if not inp.confirm:
        raise AgentError("permission_required", "pass confirm: true to delete this video "
                         "and its exports")
    document = _load(inp.video_id)
    _repo().delete(inp.video_id)
    receipt = make_receipt(call, changed=True, before={"video_id": inp.video_id,
                                                       "title": document["title"]},
                           persistent_state=STATE, reversible=False)
    return {"deleted": inp.video_id, "receipt": receipt.model_dump(mode="json")}


# -- the viewer: preview and export ---------------------------------------------


def _page_command(call, view_id, project, command, arguments, *, timeout=None):
    from plexora.agent import viewer

    control = viewer.require(call.link)
    view = viewer.resolve_view(control, view_id, project)
    control.send(view["view_id"], "open_tool", {"tool": OWNER})
    ack = control.send(view["view_id"], "plugin_command",
                       {"command": f"{OWNER}:{command}", "arguments": arguments},
                       timeout=timeout)
    if (ack or {}).get("status") not in ("done", None):
        raise AgentError("capability_unavailable", (ack or {}).get("error") or
                         f"the viewer could not {command} the video",
                         detail={"status": (ack or {}).get("status")})
    return view, ack


def preview_video(call, inp):
    document = _load(inp.video_id)
    view, ack = _page_command(call, inp.view_id, document["source"]["datasource"], "preview",
                              {"video_id": inp.video_id, "at_s": inp.at_s, "play": inp.play})
    return {"view_id": view["view_id"], "result": (ack or {}).get("result")}


def export_video(call, inp):
    """Asks the open viewer to render the video, then follows the export the
    page starts until its file is written. The page renders every frame (only
    the live viewer can); cancelling this job cancels the page's export."""
    from plexora.plugins.video_studio.server import encode_jobs

    document = _load(inp.video_id)
    started = time.time()
    view, ack = _page_command(call, inp.view_id, document["source"]["datasource"], "export",
                              {"video_id": inp.video_id, "output": inp.output,
                               "prefer": "server"}, timeout=60)
    export_id = ((ack or {}).get("result") or {}).get("export_id")
    total = _schema().frame_count(document["duration_s"],
                                  int(inp.output.get("fps") or document["output"]["fps"]))
    deadline = started + max(600.0, total * 5.0)
    while time.time() < deadline:
        if call.cancelled():
            _page_command(call, view["view_id"], None, "cancel_export",
                          {"video_id": inp.video_id})
            call.check_cancelled()
        job = encode_jobs.get(inp.video_id, export_id) if export_id else None
        if job is not None:
            call.progress(job["frames_written"], job["frames_expected"], "rendering")
            if job["status"] == "done":
                return {"export_id": export_id, "status": "done", "file": job["name"],
                        "bytes": job["bytes"], "frames": job["frames_written"],
                        "download": f"/plugins/{OWNER}/api/videos/{inp.video_id}/exports/"
                                    f"{export_id}/download",
                        "manifest": f"/plugins/{OWNER}/api/videos/{inp.video_id}/exports/"
                                    f"{export_id}/manifest"}
            if job["status"] in ("failed", "cancelled"):
                raise AgentError("resource_unavailable", job.get("error") or job["status"],
                                 detail={"status": job["status"], "tail": job.get("error_tail")})
        else:
            for row in _repo().list_exports(inp.video_id):
                if row["export_id"] == export_id or (not export_id and row["status"] == "done"
                                                     and row["created_at"] >= _iso(started)):
                    return {"export_id": row["export_id"], "status": row["status"],
                            "file": row["name"], "bytes": row["bytes"],
                            "note": "encoded in the browser; the file was saved there"}
        time.sleep(0.5)
    raise AgentError("viewer_not_responding", "the export did not finish in time",
                     detail={"export_id": export_id})


def _iso(epoch):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def capabilities():
    def cap(**kwargs):
        kwargs.setdefault("owner", OWNER)
        kwargs.setdefault("tags", TAGS)
        return Capability(**kwargs)

    return [
        cap(name="video.list", tool_name="list_videos", permission="read",
            purpose="List Video Studio videos on this machine (or on one project).",
            input_model=ListInput, handler=list_videos, reads=("plugin_document",)),
        cap(name="video.get", tool_name="get_video", permission="read",
            purpose="One video: its tracks (kind, range, generator or keyframe count), "
                    "duration, output settings, warnings and exports.",
            input_model=VideoRef, handler=get_video, reads=("plugin_document",)),
        cap(name="video.create", tool_name="create_video", permission="reversible_write",
            purpose="Create a video from a preset or storyboard (360° rotation, rock, z-sweep, "
                    "time-lapse, zoom, pan, channel reveal, ...) starting from what the viewer "
                    "shows. Show the user get_video's summary and preview_video before "
                    "exporting.",
            input_model=CreateInput, handler=create_video, persistent=True,
            writes=("plugin_document",), reads=("image", "viewer")),
        cap(name="video.add_keyframe", tool_name="add_video_keyframe", permission="reversible_write",
            purpose="Add a keyframe to a video's track, or a new track with one keyframe.",
            input_model=KeyframeInput, handler=add_keyframe, persistent=True,
            writes=("plugin_document",)),
        cap(name="video.set_track", tool_name="set_video_track", permission="reversible_write",
            purpose="Replace or add one whole track of a video.",
            input_model=TrackInput, handler=set_track, persistent=True,
            writes=("plugin_document",)),
        cap(name="video.edit", tool_name="edit_video", permission="reversible_write",
            purpose="Edit a video with operations, all or nothing. The receipt's undo_hint "
                    "undoes it.",
            input_model=EditInput, handler=edit_video, persistent=True,
            writes=("plugin_document",)),
        cap(name="video.preview", tool_name="preview_video", permission="read",
            purpose="Play a video in the open viewer, or show one moment of it.",
            input_model=PreviewInput, handler=preview_video, viewer_required=True,
            visual_output=True, reads=("plugin_document", "viewer")),
        cap(name="video.export", tool_name="export_video", permission="reversible_write",
            purpose="Render and encode a video in the open viewer (MP4 by default) with a "
                    "manifest of what every frame shows. A job: job_wait follows it.",
            input_model=ExportInput, handler=export_video, viewer_required=True,
            execution="job", streams_progress=True, writes=("plugin_export",),
            reads=("plugin_document", "viewer")),
        cap(name="video.duplicate", tool_name="duplicate_video", permission="reversible_write",
            purpose="Copy a video (not its exports) to try something different.",
            input_model=DuplicateInput, handler=duplicate_video, persistent=True,
            writes=("plugin_document",)),
        cap(name="video.delete", tool_name="delete_video", permission="destructive",
            purpose="Delete a video and its exports. Needs confirm: true.",
            input_model=DeleteInput, handler=delete_video, reversible=False, persistent=True,
            writes=("plugin_document",)),
    ]

