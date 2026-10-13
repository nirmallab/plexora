"""What a video IS, and which changes over time it is allowed to make.

One versioned JSON document per video holds the whole choreography: the viewer
state it starts from, the tracks that change that state over time, the output
settings, and the revision the conflict check turns on.

    {
      "schema_version": 1,
      "video_id": "vid_a1b2c3d4e5f6",
      "revision": 3,
      "title": "360° Rotation · 2026-10-11",
      "source":   {"datasource": "...", "fingerprint": {...}, "image_size": [w, h],
                   "depth": 190, "frames": 1, "spacing": {...}, "pixel_size": 0.65},
      "base":     {SceneState -- the viewer when the video starts},
      "duration_s": 8.0, "loop": true,
      "tracks":   [{Track}, ...],
      "viewpoints": {"name": {camera}}, "targets": {...}, "scenes": [...],
      "output":   {"width": 1920, "height": 1080, "fps": 30, "format": "mp4", ...},
      "provenance": {"created_by": "ui", "preset": {"name": "orbit", "params": {...}}}
    }

**A video references its image and never copies it**, exactly as a figure
does: `source` is a datasource name plus a fingerprint taken when the video
was made, so a re-imported image is noticed rather than rendered in the wrong
place.

**Acquired time and acquired planes are never interpolated.** A frame of a
video shows a plane that was imaged at a timepoint that was imaged. The track
kinds that move through acquired data (`acquired_z`, `acquired_t`, `slab`,
`view_mode`) take only integer, step-wise values, and `normalize_track` refuses
anything else -- a cross-fade between two timepoints is a picture of nothing
that was ever measured. What may change smoothly is how the data is SHOWN:
the camera, a clipping plane, a channel's presence (0..1), a display window.
The window and presence changes are display transitions, and the manifest of
every export lists them (`display_transitions`).

**Channel colour is held, never blended.** Blending red into green paints
yellow for a second, and yellow means co-expression to anyone reading a
multiplexed image. A colour keyframe takes effect at its time.

**The 3-D camera is the renderer's own.** `{q, dist, pan}` -- the orientation
quaternion, the distance and the pan of `volumeRenderer.js` -- rather than an
eye/target/up triple, so a stored pose is exactly what the viewer draws, and a
pose read back from the viewer needs no conversion that could drift.

**Frame times.** A video of N = round(duration x fps) frames presents frame k at
k / fps. Its CONTENT time is `content_time(k, ...)`: a loop samples
k x duration / N (so frame N, which would equal frame 0, is never rendered and
the loop has no seam); a non-loop samples k x duration / (N - 1), so its first
and last keyframes are both shown exactly.
"""

from __future__ import annotations

import copy
import json
import math
import re
import uuid

SCHEMA_VERSION = 1

#: SceneState is versioned separately from the project that holds it, as
#: Figure Builder versions its snapshot: a new output setting is not a new way
#: of describing a viewer.
SCENE_VERSION = 1

DEFAULT_TITLE = "Untitled video"
MAX_TITLE_LENGTH = 200
MAX_TRACKS = 200
MAX_KEYFRAMES = 2000
MAX_DURATION_S = 3600.0
MIN_DURATION_S = 0.1

VIDEO_ID_PATTERN = re.compile(r"^vid_[0-9a-f]{12}$")
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")

#: What the viewer can be showing a stack as (`volume_routes.MODES`).
VIEW_MODES = ("plane", "slab", "sections", "volume")
COMPOSITES = ("mip", "additive")
#: How the 3-D view may be cut (core's services/inspectGeometry.js MODES); a
#: cut is kept with a frame's view up to this many bytes of JSON.
INSPECT_MODES = ("full", "cutaway", "section", "slab", "crop")
INSPECT_MAX_BYTES = 4096
SLAB_OPS = ("max", "mean", "min", "std", "med", "pct")
CELL_MODES = ("none", "centroids", "outlines", "filled")

#: Output.
FORMATS = ("mp4", "webm", "mov", "gif", "png")
QUALITIES = ("draft", "good", "high", "max")
FPS_CHOICES = (24, 25, 30, 50, 60)
MAX_OUTPUT_SIDE = 7680
MIN_OUTPUT_SIDE = 64
OVERLAYS = ("scalebar", "label", "dataset", "legend", "axes", "sections", "attribution",
            "captions")

#: Easing names, mirrored by static/videoEasing.js. "step" holds the value of
#: the keyframe until the next one.
EASINGS = (
    "linear", "step",
    "quad_in", "quad_out", "quad_in_out",
    "cubic_in", "cubic_out", "cubic_in_out",
    "sine_in", "sine_out", "sine_in_out",
    "expo_in", "expo_out", "expo_in_out",
    "smootherstep", "cinematic",
)

#: Interpolation choices per aspect of a camera track.
ORIENTATION_INTERP = ("slerp", "squad")
PATH_INTERP = ("linear", "catmull")
SPEED_INTERP = ("time", "arc")

#: Rotation axes a procedural camera can turn about: the image's own axes
#: (which turn with the stack) or the screen's (a turntable as seen).
AXES = ("x", "y", "z", "screen_x", "screen_y")

#: Track kind -> (class, how its values may change, procedural generators).
#: `step` kinds change only at keyframe times, by whole values: see the module
#: docstring on acquired data.
TRACK_KINDS: dict[str, dict] = {
    "camera3d":   {"class": "camera", "interp": "smooth",
                   "generators": ("orbit", "rock", "tumble", "dolly")},
    "camera2d":   {"class": "camera", "interp": "smooth", "generators": ("pan",)},
    "acquired_t": {"class": "acquired_time", "interp": "step", "generators": ("sweep",)},
    "acquired_z": {"class": "slice", "interp": "step", "generators": ("sweep",)},
    "slab":       {"class": "slice", "interp": "step", "generators": ("reveal",)},
    "view_mode":  {"class": "slice", "interp": "step", "generators": ()},
    "clip":       {"class": "slice", "interp": "smooth", "generators": ("reveal",)},
    "channel":    {"class": "visualization", "interp": "smooth", "generators": ()},
    "cell_layer": {"class": "visualization", "interp": "smooth", "generators": ()},
    "layer":      {"class": "visualization", "interp": "smooth", "generators": ()},
    "emphasis":   {"class": "visualization", "interp": "smooth", "generators": ()},
    "hud":        {"class": "annotation", "interp": "smooth", "generators": ()},
    "transition": {"class": "display_transition", "interp": "smooth", "generators": ()},
}

#: Kinds that need a `target` (which channel, which layer).
TARGETED_KINDS = ("channel", "cell_layer", "layer", "emphasis")

#: Fields a channel keyframe may set, and how each changes between keyframes.
CHANNEL_FIELDS = {"visible": "step", "presence": "lerp", "opacity": "lerp",
                  "window": "lerp", "color": "step"}


class UnreadableVideo(Exception):
    """This video cannot be read by this build (damaged, or newer).

    Loud rather than shrugged off, for Figure Builder's reason: reading a newer
    document with today's rules drops what the newer schema added, and the next
    save would write that loss back.
    """


class IntegrityError(ValueError):
    """A track asks for a change that would show data that was never acquired
    -- an interpolated timepoint or plane, a blended colour."""


# -- small cleaners ---------------------------------------------------------


def validate_video_id(value):
    """A video id safe to use as a directory name, or a ValueError."""
    if not isinstance(value, str) or not VIDEO_ID_PATTERN.match(value):
        raise ValueError(f"invalid video id: {value!r}")
    return value


def new_video_id() -> str:
    return "vid_" + uuid.uuid4().hex[:12]


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def clean_text(value, limit=MAX_TITLE_LENGTH) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return _CONTROL_CHARS.sub("", value).strip()[:limit]


def clean_id(value, prefix: str) -> str:
    if isinstance(value, str) and ID_PATTERN.match(value):
        return value
    return new_id(prefix)


def _number(value, fallback=0.0, *, lo=None, hi=None) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(out):
        return fallback
    if lo is not None:
        out = max(lo, out)
    if hi is not None:
        out = min(hi, out)
    return out


def _int(value, fallback=0, *, lo=None, hi=None) -> int:
    """A whole number, or the fallback. A float that is not whole is refused
    by the callers that care (acquired indices), not rounded here."""
    if isinstance(value, bool):
        return fallback
    try:
        out = int(value)
    except (TypeError, ValueError):
        return fallback
    if lo is not None:
        out = max(lo, out)
    if hi is not None:
        out = min(hi, out)
    return out


def _whole(value, what: str) -> int:
    """An acquired index: an integer, or IntegrityError -- never rounded, since
    a 3.5 is a request for a plane between two planes."""
    if isinstance(value, bool):
        raise IntegrityError(f"{what} must be a whole acquired index, not {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise IntegrityError(f"{what} must be a whole acquired index, not {value!r}")


def color(value, fallback="#ffffff") -> str:
    if isinstance(value, str) and _HEX_COLOR.match(value):
        return value.lower()
    if isinstance(value, dict):
        try:
            r, g, b = (max(0, min(255, int(value.get(k, 0)))) for k in ("r", "g", "b"))
            return f"#{r:02x}{g:02x}{b:02x}"
        except (TypeError, ValueError):
            return fallback
    return fallback


def _choice(value, choices, fallback):
    return value if value in choices else fallback


def _quat(value, fallback=(1.0, 0.0, 0.0, 0.0)) -> list:
    """A unit quaternion [x, y, z, w]; the XY view when unusable."""
    try:
        q = [float(v) for v in value]
    except (TypeError, ValueError):
        return list(fallback)
    if len(q) != 4 or not all(math.isfinite(v) for v in q):
        return list(fallback)
    length = math.sqrt(sum(v * v for v in q))
    if length < 1e-9:
        return list(fallback)
    return [v / length for v in q]


def _pair(value, fallback=(0.0, 1.0), *, lo=None, hi=None) -> list:
    try:
        a, b = (float(v) for v in value)
    except (TypeError, ValueError):
        return list(fallback)
    if not (math.isfinite(a) and math.isfinite(b)):
        return list(fallback)
    if lo is not None:
        a, b = max(lo, a), max(lo, b)
    if hi is not None:
        a, b = min(hi, a), min(hi, b)
    return [a, b]


# -- the scene: one resolved frame ---------------------------------------------


def normalize_camera3d(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    pan = raw.get("pan")
    if not isinstance(pan, (list, tuple)):
        pan = [raw.get("panX", 0.0), raw.get("panY", 0.0)]
    out = {
        "q": _quat(raw.get("q")),
        # The renderer clamps a dragged distance to [0.15, 8]; a stored pose
        # may sit anywhere a preset put it, within the same sane bounds.
        "dist": _number(raw.get("dist"), 2.2, lo=0.05, hi=20.0),
        "pan": _pair(pan, (0.0, 0.0), lo=-10.0, hi=10.0),
    }
    fov = raw.get("fov")
    if fov is not None:
        out["fov"] = _number(fov, 0.7, lo=0.1, hi=2.5)
    return out


def normalize_camera2d(raw) -> dict | None:
    """A 2-D view: centre and width in full-resolution image pixels, plus the
    view transform. The height follows from the output's aspect ratio."""
    if not isinstance(raw, dict):
        return None
    if "cx" not in raw and "x" in raw:
        x, y = _number(raw.get("x")), _number(raw.get("y"))
        w, h = _number(raw.get("w"), 1.0, lo=1e-3), _number(raw.get("h"), 1.0, lo=1e-3)
        raw = {**raw, "cx": x + w / 2, "cy": y + h / 2, "w": w}
    return {
        "cx": _number(raw.get("cx")),
        "cy": _number(raw.get("cy")),
        "w": _number(raw.get("w"), 1.0, lo=1e-3),
        "degrees": _number(raw.get("degrees"), 0.0) % 360.0,
        "flip_h": bool(raw.get("flip_h")),
        "flip_v": bool(raw.get("flip_v")),
    }


def normalize_channel(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    key = clean_text(raw.get("key"), 128)
    if not key:
        return None
    window = raw.get("window")
    return {
        "key": key,
        "name": clean_text(raw.get("name"), 128) or key,
        "color": color(raw.get("color")),
        "window": _pair(window, (0.0, 1.0)) if window is not None else None,
        "visible": bool(raw.get("visible", True)),
        "opacity": _number(raw.get("opacity"), 1.0, lo=0.0, hi=1.0),
        "presence": _number(raw.get("presence"), 1.0, lo=0.0, hi=1.0),
    }


def normalize_view(raw, *, depth=1, frames=1) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    top = max(0, int(depth) - 1)
    last = max(0, int(frames) - 1)
    view = {
        "t": _int(raw.get("t"), 0, lo=0, hi=last),
        "z": _int(raw.get("z"), 0, lo=0, hi=top),
        "mode": _choice(raw.get("mode"), VIEW_MODES, "plane"),
        "composite": _choice(raw.get("composite"), COMPOSITES, "mip"),
        "slab": None,
        "clip": {"x": [0.0, 1.0], "y": [0.0, 1.0], "z": [0.0, 1.0]},
    }
    slab = raw.get("slab")
    if isinstance(slab, dict):
        z0 = _int(slab.get("z0"), 0, lo=0, hi=top)
        z1 = _int(slab.get("z1"), top, lo=0, hi=top)
        view["slab"] = {"z0": min(z0, z1), "z1": max(z0, z1),
                        "op": _choice(slab.get("op"), SLAB_OPS, "max")}
    clip = raw.get("clip")
    if isinstance(clip, dict):
        for axis in ("x", "y", "z"):
            if axis in clip:
                a, b = _pair(clip[axis], (0.0, 1.0), lo=0.0, hi=1.0)
                view["clip"][axis] = [min(a, b), max(a, b)]
    # How the 3-D view was cut when the video was made (core's inspect, image
    # units): kept as the viewer wrote it, bounded; the browser repairs it
    # against the image when a frame is applied (inspectGeometry.clean).
    inspect = raw.get("inspect")
    if isinstance(inspect, dict) and inspect.get("mode") in INSPECT_MODES:
        try:
            text = json.dumps(inspect)
        except (TypeError, ValueError):
            text = ""
        if text and len(text) <= INSPECT_MAX_BYTES:
            view["inspect"] = json.loads(text)
    section = raw.get("section")
    if isinstance(section, dict):
        view["section"] = {axis: _number(section.get(axis), 0.0, lo=0.0) for axis in ("x", "y")}
    return view


def normalize_scene(raw, *, source=None) -> dict:
    """A SceneState: one resolved frame of the viewer.

    A superset of Figure Builder's scene snapshot -- it adds the plane, the
    timepoint, the view mode, the 3-D camera and channel presence -- so a frame
    can be sent to Figure Builder by dropping what that scene has no field for.
    """
    raw = raw if isinstance(raw, dict) else {}
    source = source if isinstance(source, dict) else {}
    depth = _int(source.get("depth"), 1, lo=1)
    frames = _int(source.get("frames"), 1, lo=1)
    channels = [c for c in (normalize_channel(c) for c in raw.get("channels") or []) if c]
    seen = set()
    channels = [c for c in channels if not (c["key"] in seen or seen.add(c["key"]))]
    box = raw.get("box")
    if isinstance(box, dict):
        box = {k: _number(box.get(k), 0.0) for k in ("x", "y", "width", "height")}
        if box["width"] <= 0 or box["height"] <= 0:
            box = None
    else:
        box = None
    plugins = raw.get("plugins") if isinstance(raw.get("plugins"), dict) else {}
    return {
        "scene_version": SCENE_VERSION,
        "dims": _choice(raw.get("dims"), ("2d", "3d"), "3d" if depth > 1 else "2d"),
        "view": normalize_view(raw.get("view"), depth=depth, frames=frames),
        "viewport": normalize_camera2d(raw.get("viewport")),
        "camera3d": normalize_camera3d(raw.get("camera3d")),
        "box": box,
        "channels": channels,
        "hd_mode": raw.get("hd_mode") if isinstance(raw.get("hd_mode"), bool) else None,
        "cell_layers": [
            {"name": clean_text(layer.get("name"), 128),
             "mode": _choice(layer.get("mode"), CELL_MODES, "none"),
             "opacity": _number(layer.get("opacity"), 1.0, lo=0.0, hi=1.0),
             "visible": bool(layer.get("visible", True))}
            for layer in raw.get("cell_layers") or [] if isinstance(layer, dict)
            and clean_text(layer.get("name"), 128)],
        "layers": [
            {"id": clean_text(layer.get("id"), 128),
             "visible": bool(layer.get("visible", True)),
             "opacity": _number(layer.get("opacity"), 1.0, lo=0.0, hi=1.0)}
            for layer in raw.get("layers") or [] if isinstance(layer, dict)
            and clean_text(layer.get("id"), 128)],
        # Opaque, like Figure Builder's: each plugin restores its own state.
        "plugins": {str(k)[:64]: v for k, v in plugins.items() if isinstance(v, dict)},
    }


# -- tracks ------------------------------------------------------------------


def _ease(value, kind: str) -> str:
    if TRACK_KINDS[kind]["interp"] == "step":
        if value not in (None, "step", "hold"):
            raise IntegrityError(
                f"A {kind} track moves through acquired data and changes only in whole "
                f"steps; it cannot use the easing {value!r}.")
        return "step"
    return _choice(value, EASINGS, "linear")


def _keyframe_value(kind: str, value, source: dict):
    """One keyframe's value for a track of `kind`, normalised -- or
    IntegrityError for a value a step kind may not hold."""
    if value == "$current":
        return "$current"
    depth = _int(source.get("depth"), 1, lo=1)
    frames = _int(source.get("frames"), 1, lo=1)
    if kind == "camera3d":
        if isinstance(value, dict) and value.get("look_at"):
            return {"look_at": clean_text(value.get("look_at"), 64),
                    "fit": _number(value.get("fit"), 1.0, lo=0.05, hi=10.0)}
        return normalize_camera3d(value) or normalize_camera3d({})
    if kind == "camera2d":
        return normalize_camera2d(value) or normalize_camera2d({})
    if kind == "acquired_z":
        return max(0, min(depth - 1, _whole(value, "acquired_z")))
    if kind == "acquired_t":
        return max(0, min(frames - 1, _whole(value, "acquired_t")))
    if kind == "slab":
        if value is None:
            return None
        if not isinstance(value, dict):
            raise IntegrityError("a slab keyframe is {z0, z1, op} or null")
        z0 = max(0, min(depth - 1, _whole(value.get("z0"), "slab.z0")))
        z1 = max(0, min(depth - 1, _whole(value.get("z1"), "slab.z1")))
        return {"z0": min(z0, z1), "z1": max(z0, z1),
                "op": _choice(value.get("op"), SLAB_OPS, "max")}
    if kind == "view_mode":
        if value not in VIEW_MODES:
            raise IntegrityError(f"view_mode must be one of {', '.join(VIEW_MODES)}")
        return value
    if kind == "clip":
        value = value if isinstance(value, dict) else {}
        out = {}
        for axis in ("x", "y", "z"):
            a, b = _pair(value.get(axis, (0.0, 1.0)), (0.0, 1.0), lo=0.0, hi=1.0)
            out[axis] = [min(a, b), max(a, b)]
        return out
    if kind == "channel":
        value = value if isinstance(value, dict) else {}
        out = {}
        if "visible" in value:
            out["visible"] = bool(value["visible"])
        for field in ("presence", "opacity"):
            if field in value:
                out[field] = _number(value[field], 1.0, lo=0.0, hi=1.0)
        if value.get("window") is not None:
            out["window"] = _pair(value["window"])
        if "color" in value:
            out["color"] = color(value["color"])
        return out
    if kind in ("cell_layer", "layer"):
        value = value if isinstance(value, dict) else {}
        out = {}
        if "visible" in value:
            out["visible"] = bool(value["visible"])
        if "opacity" in value:
            out["opacity"] = _number(value["opacity"], 1.0, lo=0.0, hi=1.0)
        if kind == "cell_layer" and "mode" in value:
            out["mode"] = _choice(value["mode"], CELL_MODES, "none")
        return out
    if kind == "emphasis":
        # A spotlight on a target region (`targets[track.target]`): everything
        # outside it dimmed by `strength`, or the region outlined. Drawn over
        # 2-D frames; a display effect, listed as a transition.
        value = value if isinstance(value, dict) else {}
        return {"mode": _choice(value.get("mode"), ("dim_others", "outline", "none"), "dim_others"),
                "strength": _number(value.get("strength"), 0.6, lo=0.0, hi=1.0)}
    if kind == "hud":
        value = value if isinstance(value, dict) else {}
        out = {}
        if "caption" in value:
            out["caption"] = clean_text(value["caption"], 300)
        if "opacity" in value:
            out["opacity"] = _number(value["opacity"], 1.0, lo=0.0, hi=1.0)
        for flag in OVERLAYS:
            if flag in value and flag != "captions":
                out[flag] = bool(value[flag])
        return out
    if kind == "transition":
        # Through black, or a cut. A cross-fade between two scenes would need
        # two pictures of each frame; it is not offered rather than ignored.
        value = value if isinstance(value, dict) else {}
        return {"kind": _choice(value.get("kind"), ("fade_black", "cut"), "fade_black"),
                "alpha": _number(value.get("alpha"), 0.0, lo=0.0, hi=1.0)}
    return value


def _generator_params(kind: str, generator: str, raw, source: dict) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    depth = _int(source.get("depth"), 1, lo=1)
    frames = _int(source.get("frames"), 1, lo=1)
    if kind == "camera3d":
        params = {
            "axis": _choice(raw.get("axis"), AXES, "y"),
            "direction": -1 if _number(raw.get("direction"), 1) < 0 else 1,
            "from": normalize_camera3d(raw.get("from")) if raw.get("from") not in (None, "$current")
            else "$current",
        }
        if generator == "orbit":
            params["turns"] = _number(raw.get("turns"), 1.0, lo=-20.0, hi=20.0)
            pivot = raw.get("pivot")
            # In the renderer's model space (the box centred on the origin,
            # its longest side 1); None turns about the box's centre.
            params["pivot"] = ([_number(v, 0.0, lo=-5.0, hi=5.0) for v in pivot]
                               if isinstance(pivot, (list, tuple)) and len(pivot) == 3 else None)
        elif generator == "rock":
            params["amplitude_deg"] = _number(raw.get("amplitude_deg"), 30.0, lo=1.0, hi=180.0)
            params["cycles"] = _int(raw.get("cycles"), 2, lo=1, hi=100)
        elif generator == "tumble":
            params["turns"] = _int(raw.get("turns"), 1, lo=1, hi=20)
            params["tilt_deg"] = _number(raw.get("tilt_deg"), 25.0, lo=0.0, hi=80.0)
        elif generator == "dolly":
            params["to_dist"] = _number(raw.get("to_dist"), 0.6, lo=0.05, hi=20.0)
            params["turn_deg"] = _number(raw.get("turn_deg"), 0.0, lo=-720.0, hi=720.0)
        return params
    if kind == "camera2d":
        return {"from": normalize_camera2d(raw.get("from")) if isinstance(raw.get("from"), dict)
                else "$current",
                "dx": _number(raw.get("dx"), 0.0), "dy": _number(raw.get("dy"), 0.0)}
    if kind in ("acquired_z", "acquired_t"):
        top = (depth if kind == "acquired_z" else frames) - 1
        start = max(0, min(top, _whole(raw.get("from", 0), f"{kind}.from")))
        end = max(0, min(top, _whole(raw.get("to", top), f"{kind}.to")))
        return {"from": start, "to": end, "bounce": bool(raw.get("bounce")),
                "repeat": _int(raw.get("repeat"), 1, lo=1, hi=1000)}
    if kind == "slab":
        top = depth - 1
        return {"from": max(0, min(top, _whole(raw.get("from", 0), "slab.from"))),
                "to": max(0, min(top, _whole(raw.get("to", top), "slab.to"))),
                "op": _choice(raw.get("op"), SLAB_OPS, "max"),
                "anchor": _choice(raw.get("anchor"), ("top", "bottom"), "top")}
    if kind == "clip":
        return {"axis": _choice(raw.get("axis"), ("x", "y", "z"), "z"),
                "side": _choice(raw.get("side"), ("lo", "hi"), "hi"),
                "from": _number(raw.get("from"), 0.0, lo=0.0, hi=1.0),
                "to": _number(raw.get("to"), 1.0, lo=0.0, hi=1.0),
                "snap": bool(raw.get("snap", True))}
    return {}


def normalize_track(raw, *, source=None, duration=None) -> dict:
    """One track, normalised. Raises IntegrityError for a change a track of its
    kind may not make, ValueError for one that is merely malformed."""
    if not isinstance(raw, dict):
        raise ValueError("a track is an object")
    source = source if isinstance(source, dict) else {}
    kind = raw.get("kind")
    if kind not in TRACK_KINDS:
        raise ValueError(f"unknown track kind {kind!r}")
    spec = TRACK_KINDS[kind]
    duration = _number(duration, MAX_DURATION_S, lo=MIN_DURATION_S, hi=MAX_DURATION_S)
    mode = _choice(raw.get("mode"), ("procedural", "keyframes"), "keyframes")
    target = clean_text(raw.get("target"), 128) or None
    if kind in TARGETED_KINDS and not target:
        raise ValueError(f"a {kind} track needs a target")
    start, end = _pair(raw.get("range"), (0.0, duration), lo=0.0, hi=duration)
    track = {
        "track_id": clean_id(raw.get("track_id"), "trk"),
        "kind": kind,
        "class": spec["class"],
        "target": target,
        "label": clean_text(raw.get("label"), 120),
        "range": [min(start, end), max(start, end)],
        "muted": bool(raw.get("muted")),
        "mode": mode,
        "procedural": None,
        "keyframes": [],
    }
    if kind in ("camera3d", "camera2d"):
        interp = raw.get("interp") if isinstance(raw.get("interp"), dict) else {}
        track["interp"] = {
            "orientation": _choice(interp.get("orientation"), ORIENTATION_INTERP, "squad"),
            "path": _choice(interp.get("path"), PATH_INTERP, "catmull"),
            "speed": _choice(interp.get("speed"), SPEED_INTERP, "time"),
        }
    if mode == "procedural":
        proc = raw.get("procedural") if isinstance(raw.get("procedural"), dict) else {}
        generator = proc.get("generator")
        if generator not in spec["generators"]:
            raise ValueError(f"a {kind} track has no generator {generator!r}")
        ease = proc.get("ease")
        if spec["interp"] == "step" and ease not in (None, "linear", "step"):
            raise IntegrityError(
                f"A {kind} sweep steps through acquired data at an even pace; it cannot "
                f"use the easing {ease!r}.")
        track["procedural"] = {
            "generator": generator,
            "params": _generator_params(kind, generator, proc.get("params"), source),
            "ease": "linear" if spec["interp"] == "step" else _choice(ease, EASINGS, "linear"),
            "loop": bool(proc.get("loop")),
        }
        return track
    keyframes = []
    for item in (raw.get("keyframes") or [])[:MAX_KEYFRAMES]:
        if not isinstance(item, dict):
            continue
        keyframes.append({
            "kf_id": clean_id(item.get("kf_id"), "kf"),
            "t": _number(item.get("t"), 0.0, lo=0.0, hi=duration),
            "value": _keyframe_value(kind, item.get("value"), source),
            "ease": _ease(item.get("ease"), kind),
            "hold": _number(item.get("hold"), 0.0, lo=0.0, hi=duration),
            **({"label": clean_text(item["label"], 80)} if item.get("label") else {}),
        })
    keyframes.sort(key=lambda kf: kf["t"])
    track["keyframes"] = keyframes
    return track


# -- the project -------------------------------------------------------------


def normalize_output(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    width = _int(raw.get("width"), 1920, lo=MIN_OUTPUT_SIDE, hi=MAX_OUTPUT_SIDE)
    height = _int(raw.get("height"), 1080, lo=MIN_OUTPUT_SIDE, hi=MAX_OUTPUT_SIDE)
    # Even sides: H.264 4:2:0 refuses odd ones, and a size that works for one
    # format and not another is a setting that silently changes on export.
    width -= width % 2
    height -= height % 2
    overlays = raw.get("overlays") if isinstance(raw.get("overlays"), dict) else {}
    return {
        "width": width,
        "height": height,
        "fps": _int(raw.get("fps"), 30, lo=1, hi=120),
        "format": _choice(raw.get("format"), FORMATS, "mp4"),
        "quality": _choice(raw.get("quality"), QUALITIES, "good"),
        "bitrate_mbps": _number(raw.get("bitrate_mbps"), 0.0, lo=0.0, hi=400.0) or None,
        "transparent": bool(raw.get("transparent")),
        "overlays": {name: bool(overlays.get(name, name in ("scalebar", "label")))
                     for name in OVERLAYS},
        "attribution": clean_text(raw.get("attribution"), 200),
    }


def normalize_source(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    size = raw.get("image_size")
    try:
        width, height = (int(v) for v in size)
    except (TypeError, ValueError):
        width = height = 0
    spacing = raw.get("spacing") if isinstance(raw.get("spacing"), dict) else {}
    return {
        "datasource": clean_text(raw.get("datasource"), 200),
        "fingerprint": raw.get("fingerprint") if isinstance(raw.get("fingerprint"), dict) else {},
        "image_size": [max(0, width), max(0, height)],
        "depth": _int(raw.get("depth"), 1, lo=1),
        "frames": _int(raw.get("frames"), 1, lo=1),
        "spacing": {axis: spacing[axis] for axis in ("x", "y", "z", "t")
                    if isinstance(spacing.get(axis), dict)},
        "pixel_size": _number(raw.get("pixel_size"), 0.0, lo=0.0) or None,
        "channels": [{"key": clean_text(c.get("key"), 128), "name": clean_text(c.get("name"), 128)}
                     for c in raw.get("channels") or [] if isinstance(c, dict) and c.get("key")],
    }


def new_document(video_id, *, title=None, source=None, base=None, created_at="") -> dict:
    return normalize_project({
        "video_id": video_id, "title": title or DEFAULT_TITLE, "source": source or {},
        "base": base or {}, "duration_s": 8.0, "tracks": [], "created_at": created_at,
        "updated_at": created_at})


def normalize_project(raw, *, video_id=None) -> dict:
    """The whole document, normalised and integrity-checked.

    Raises UnreadableVideo for a document from a newer build, IntegrityError for
    a track that would show what was never acquired, ValueError for anything
    else malformed.
    """
    if not isinstance(raw, dict):
        raise ValueError("a video is an object")
    version = _int(raw.get("schema_version"), SCHEMA_VERSION)
    if version > SCHEMA_VERSION:
        raise UnreadableVideo(
            f"this video was saved by a newer Plexora (schema {version}); this build reads "
            f"schema {SCHEMA_VERSION}")
    source = normalize_source(raw.get("source"))
    duration = _number(raw.get("duration_s"), 8.0, lo=MIN_DURATION_S, hi=MAX_DURATION_S)
    tracks_raw = raw.get("tracks") or []
    if not isinstance(tracks_raw, list):
        raise ValueError("tracks must be a list")
    if len(tracks_raw) > MAX_TRACKS:
        raise ValueError(f"a video holds at most {MAX_TRACKS} tracks")
    tracks = [normalize_track(t, source=source, duration=duration) for t in tracks_raw]
    ids = set()
    for track in tracks:
        while track["track_id"] in ids:
            track["track_id"] = new_id("trk")
        ids.add(track["track_id"])
    viewpoints = {}
    for name, value in (raw.get("viewpoints") or {}).items() if isinstance(
            raw.get("viewpoints"), dict) else ():
        name = clean_text(name, 64)
        if not name or not isinstance(value, dict):
            continue
        camera = (normalize_camera3d(value.get("camera3d")) if value.get("camera3d")
                  else None)
        viewport = normalize_camera2d(value.get("viewport")) if value.get("viewport") else None
        if camera or viewport:
            viewpoints[name] = {"camera3d": camera, "viewport": viewport,
                                "z": _int(value.get("z"), 0, lo=0),
                                "t": _int(value.get("t"), 0, lo=0)}
    scenes = []
    for item in raw.get("scenes") or []:
        if not isinstance(item, dict):
            continue
        scenes.append({"scene_id": clean_id(item.get("scene_id"), "scn"),
                       "label": clean_text(item.get("label"), 120),
                       "start_s": _number(item.get("start_s"), 0.0, lo=0.0, hi=duration),
                       "duration_s": _number(item.get("duration_s"), 0.0, lo=0.0, hi=duration)})
    targets = {}
    for key, value in (raw.get("targets") or {}).items() if isinstance(
            raw.get("targets"), dict) else ():
        key = clean_text(key, 64)
        if key and isinstance(value, dict):
            targets[key] = _normalize_target(value)
    provenance = raw.get("provenance") if isinstance(raw.get("provenance"), dict) else {}
    preset = provenance.get("preset") if isinstance(provenance.get("preset"), dict) else None
    notes = [clean_text(n, 300) for n in raw.get("notes") or [] if isinstance(n, str)][:20]
    recording = raw.get("recording") if isinstance(raw.get("recording"), dict) else None
    return {
        "schema_version": SCHEMA_VERSION,
        "video_id": video_id or raw.get("video_id") or "",
        "revision": _int(raw.get("revision"), 0, lo=0),
        "title": clean_text(raw.get("title")) or DEFAULT_TITLE,
        "created_at": clean_text(raw.get("created_at"), 40),
        "updated_at": clean_text(raw.get("updated_at"), 40),
        "source": source,
        "base": normalize_scene(raw.get("base"), source=source),
        "duration_s": duration,
        "loop": bool(raw.get("loop")),
        "tracks": tracks,
        "viewpoints": viewpoints,
        "targets": targets,
        "scenes": scenes,
        "output": normalize_output(raw.get("output")),
        "notes": notes,
        # A recorded exploration's raw samples (static/videoRecorder.js), kept
        # so its cleanup passes can be run again differently. Opaque here.
        "recording": recording,
        "provenance": {
            "created_by": _choice(provenance.get("created_by"), ("ui", "agent", "import",
                                                                 "recording"), "ui"),
            "preset": ({"name": clean_text(preset.get("name"), 64),
                        "params": preset.get("params") if isinstance(preset.get("params"), dict)
                        else {}} if preset else None),
        },
    }


def _normalize_target(raw) -> dict:
    kind = _choice(raw.get("kind"), ("cell", "roi", "selection", "bbox", "point"), "bbox")
    resolved = raw.get("resolved") if isinstance(raw.get("resolved"), dict) else {}
    bbox = resolved.get("bbox")
    out = {"kind": kind, "id": clean_text(raw.get("id"), 128) or None,
           "status": _choice(raw.get("status"), ("ok", "missing", "unresolved"), "unresolved"),
           "resolved": {"x": _number(resolved.get("x")), "y": _number(resolved.get("y")),
                        "z": _number(resolved.get("z"))}}
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        out["resolved"]["bbox"] = [_number(v) for v in bbox]
    return out


# -- frame times and the acquired data each frame shows -----------------------


def frame_count(duration_s: float, fps: int) -> int:
    """round(duration x fps), halves up -- as the browser's Math.round does.
    Python's round() takes halves to even (82.5 -> 82), and a server and a
    browser that disagree by a frame refuse each other's export."""
    return max(1, int(math.floor(float(duration_s) * int(fps) + 0.5)))


def content_time(k: int, n: int, duration_s: float, loop: bool) -> float:
    """The animation time frame k of n shows (see the module docstring)."""
    if n <= 1:
        return 0.0
    return k * duration_s / (n if loop else n - 1)


def _step_value(track: dict, t: float, fallback):
    """The value a step track holds at time t: its last keyframe at or before
    t, its first before any. `$current` resolves to the fallback."""
    value = fallback
    frames = track.get("keyframes") or []
    if not frames:
        return fallback
    value = frames[0]["value"]
    for kf in frames:
        if kf["t"] <= t + 1e-9:
            value = kf["value"]
        else:
            break
    return fallback if value == "$current" else value


def sweep_index(params: dict, u: float) -> int:
    """The acquired index a sweep shows at progress u in [0, 1].

    Every index from `from` to `to` gets an equal share of the time (and with
    `bounce` the way back is the same indices in reverse, without the turning
    index shown twice). Integer by construction: a sweep never asks for a plane
    between planes.
    """
    start, end = int(params["from"]), int(params["to"])
    step = 1 if end >= start else -1
    run = list(range(start, end + step, step))
    if params.get("bounce") and len(run) > 1:
        run = run + run[-2:0:-1]
    run = run * max(1, int(params.get("repeat") or 1))
    u = min(1.0, max(0.0, u))
    # The epsilon absorbs float error in u (frame 9 of 12 is 8.999999999999998
    # planes in): a sweep at one frame per plane must show every plane.
    return run[min(len(run) - 1, int(u * len(run) + 1e-9))]


def _progress(track: dict, t: float) -> float | None:
    start, end = track["range"]
    if end <= start:
        return 1.0 if t >= start else 0.0
    return (t - start) / (end - start)


def acquired_at(project: dict, t: float) -> dict:
    """Which acquired timepoint, plane, slab and view mode the video shows at
    content time t -- the part of a frame that must be exact, evaluated here so
    the manifest and the agent's summary do not depend on the browser.

    Mirrors static/videoAnimation.js; a probe holds the two together."""
    base = project["base"]["view"]
    out = {"t": base["t"], "z": base["z"], "mode": base["mode"],
           "slab": copy.deepcopy(base["slab"])}
    for track in project["tracks"]:
        if track["muted"]:
            continue
        kind = track["kind"]
        if kind not in ("acquired_t", "acquired_z", "slab", "view_mode"):
            continue
        key = {"acquired_t": "t", "acquired_z": "z", "slab": "slab", "view_mode": "mode"}[kind]
        if track["mode"] == "procedural":
            # A procedural track does nothing before its range starts
            # (static/videoAnimation.js composes the same way).
            if t < track["range"][0] - 1e-9:
                continue
            u = _progress(track, t)
            params = track["procedural"]["params"]
            if kind in ("acquired_t", "acquired_z"):
                out[key] = sweep_index(params, u)
            elif kind == "slab":
                edge = sweep_index({"from": params["from"], "to": params["to"]}, u)
                fixed = params["from"]
                out["slab"] = {"z0": min(fixed, edge), "z1": max(fixed, edge), "op": params["op"]}
        else:
            out[key] = _step_value(track, t, out[key])
    return out


def acquired_plan(project: dict, fps: int | None = None) -> list[dict]:
    """`acquired_at` for every frame of the video at `fps`."""
    fps = int(fps or project["output"]["fps"])
    n = frame_count(project["duration_s"], fps)
    return [{"k": k, **acquired_at(project, content_time(k, n, project["duration_s"],
                                                          project["loop"]))}
            for k in range(n)]


def display_transitions(project: dict) -> list[dict]:
    """Every change a video makes to how data is SHOWN rather than which data:
    presence fades, window changes, cross-fades. Listed in each export's
    manifest so a reader can tell a display effect from a measurement."""
    out = []
    for track in project["tracks"]:
        if track["muted"]:
            continue
        if track["kind"] == "channel":
            frames = track["keyframes"]
            for before, after in zip(frames, frames[1:]):
                changed = [f for f in ("presence", "opacity", "window")
                           if before["value"].get(f) != after["value"].get(f)
                           and f in after["value"]]
                if changed:
                    out.append({"track_id": track["track_id"], "kind": "channel",
                                "target": track["target"], "fields": changed,
                                "from_s": before["t"], "to_s": after["t"]})
        elif track["kind"] in ("transition", "emphasis", "cell_layer", "layer"):
            frames = track["keyframes"]
            if len(frames) > 1:
                out.append({"track_id": track["track_id"], "kind": track["kind"],
                            "target": track["target"], "from_s": frames[0]["t"],
                            "to_s": frames[-1]["t"]})
    return out


def validate(project: dict) -> list[dict]:
    """Warnings about a valid project: things that will render, but perhaps not
    as the author expects. Never raises -- refusal is `normalize_project`'s job.
    """
    warnings = []
    source = project["source"]
    for track in project["tracks"]:
        kind = track["kind"]
        if kind == "camera3d" and track["mode"] == "keyframes":
            frames = [kf for kf in track["keyframes"] if isinstance(kf["value"], dict)
                      and "q" in kf["value"]]
            for a, b in zip(frames, frames[1:]):
                dot = abs(sum(x * y for x, y in zip(a["value"]["q"], b["value"]["q"])))
                angle = math.degrees(2 * math.acos(min(1.0, dot)))
                if angle > 150:
                    warnings.append({"code": "large_turn", "track_id": track["track_id"],
                                     "message": f"Two camera positions {angle:.0f}° apart: the "
                                                "turn between them may go either way. Add a "
                                                "position in between."})
        if kind in ("acquired_z", "slab", "clip") and source["depth"] <= 1:
            warnings.append({"code": "no_depth", "track_id": track["track_id"],
                             "message": "This image has one plane; the track changes nothing."})
        if kind == "acquired_t" and source["frames"] <= 1:
            warnings.append({"code": "no_time", "track_id": track["track_id"],
                             "message": "This image has one timepoint; the track changes "
                                        "nothing."})
        if kind == "camera3d" and source["depth"] <= 1:
            warnings.append({"code": "flat_image", "track_id": track["track_id"],
                             "message": "A 3-D camera needs a z-stack; this image is flat."})
    for track in project["tracks"]:
        if track["kind"] == "emphasis" and track["target"] not in project["targets"]:
            warnings.append({"code": "unknown_target", "track_id": track["track_id"],
                             "message": f"No target {track['target']!r}: add it with set_target "
                                        "(a region's bounding box) for the spotlight to show."})
    keys = {c["key"] for c in project["base"]["channels"]}
    for track in project["tracks"]:
        if track["kind"] == "channel" and keys and track["target"] not in keys:
            warnings.append({"code": "unknown_channel", "track_id": track["track_id"],
                             "message": f"No channel {track['target']!r} in this image."})
    return warnings


def manifest_tracks(project: dict) -> list[dict]:
    """The compact per-track table the agent and the manifest show."""
    rows = []
    for track in project["tracks"]:
        row = {"track_id": track["track_id"], "kind": track["kind"], "class": track["class"],
               "target": track["target"], "range": track["range"], "mode": track["mode"],
               "muted": track["muted"]}
        if track["mode"] == "procedural":
            row["generator"] = track["procedural"]["generator"]
            row["params"] = track["procedural"]["params"]
        else:
            row["keyframes"] = len(track["keyframes"])
        rows.append(row)
    return rows
