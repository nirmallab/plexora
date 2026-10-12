"""Quick Capture presets: a few parameters in, a whole video project out.

A preset is a generator, not a template: given the image's facts (planes,
timepoints, channels, spacing), the viewer's current scene and a handful of
parameters, `build` returns the tracks of a complete, editable project. The
same function serves the Quick Capture bar (which posts the live viewer's
facts and scene) and an agent's `create_video` (which has only the project
record), so both get the same storyboard for the same request -- one
implementation, testable without a browser.

Every preset starts from the viewer as it is: the camera, plane, channels and
windows on screen are the video's first frame. Nothing here invents a view the
user did not choose, except where a preset needs a mode the image supports and
the viewer is not in (a 360° rotation of a z-stack shown as planes switches it
to the 3-D view, and says so in `notes`).

The parameter descriptions (`FIELDS`) are served to the browser, which draws
its controls from them: a parameter added here appears in the bar without a
client change.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from plexora.plugins.video_studio.server import schema

# The renderer's opening view of a stack (volumeCamera.js VIEWS.iso): the XY
# face turned 0.6 rad about y, then tipped 0.55 rad about x.


def _quat_axis_angle(axis, angle):
    s = math.sin(angle / 2)
    return [axis[0] * s, axis[1] * s, axis[2] * s, math.cos(angle / 2)]


def _quat_multiply(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return [aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz]


def _normalize(q):
    length = math.sqrt(sum(v * v for v in q)) or 1.0
    return [v / length for v in q]


ISO_Q = _normalize(_quat_multiply(_quat_axis_angle([1, 0, 0], 0.55),
                                  _quat_multiply(_quat_axis_angle([0, 1, 0], 0.6),
                                                 [1.0, 0.0, 0.0, 0.0])))
DEFAULT_CAMERA = {"q": ISO_Q, "dist": 2.2, "pan": [0.0, 0.0]}


@dataclass(frozen=True)
class Field:
    """One control in the Quick Capture strip."""

    name: str
    label: str
    kind: str                      # segmented | number | select | toggle | view | channels
    default: Any = None
    options: tuple = ()            # ((value, label), ...)
    unit: str = ""
    min: float | None = None
    max: float | None = None
    step: float | None = None
    hint: str = ""

    def describe(self, facts: dict | None = None) -> dict:
        """The control's description, with "depth"/"frames" bounds resolved
        against `facts` (the browser draws numbers, not names)."""
        facts = facts or {}
        out = {"name": self.name, "label": self.label, "kind": self.kind,
               "default": _bound(self.default, facts)}
        if self.options:
            out["options"] = [{"value": v, "label": label} for v, label in self.options]
        for key in ("unit", "min", "max", "step", "hint"):
            value = getattr(self, key)
            if value not in (None, ""):
                out[key] = _bound(value, facts) if key in ("min", "max") else value
        return out


@dataclass(frozen=True)
class Preset:
    name: str
    label: str
    group: str                     # 3d | stack | plane | channels | story
    icon: str
    summary: str
    fields: tuple
    build: Callable
    needs: tuple = ()              # "depth", "frames", "channels"
    story: bool = False

    def available(self, facts: dict) -> tuple[bool, str]:
        if "depth" in self.needs and int(facts.get("depth") or 1) < 2:
            return False, "Needs a z-stack."
        if "frames" in self.needs and int(facts.get("frames") or 1) < 2:
            return False, "Needs a time series."
        if "channels" in self.needs and len(facts.get("channels") or []) < 2:
            return False, "Needs two or more channels."
        return True, ""

    def describe(self, facts: dict | None = None) -> dict:
        ok, reason = self.available(facts or {})
        return {"name": self.name, "label": self.label, "group": self.group, "icon": self.icon,
                "summary": self.summary, "story": self.story, "available": ok,
                "reason": reason, "fields": [f.describe(facts) for f in self.fields]}

    def params(self, raw: dict | None, facts: dict) -> dict:
        """`raw` checked against the fields: unknown names dropped, numbers
        clamped, choices outside the options replaced by the default."""
        raw = raw if isinstance(raw, dict) else {}
        out = {}
        for f in self.fields:
            value = raw.get(f.name, _default(f, facts))
            if f.kind == "number":
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    value = float(_default(f, facts) or 0)
                if not math.isfinite(value):
                    value = float(_default(f, facts) or 0)
                if f.min is not None:
                    value = max(f.min, value)
                if f.max is not None:
                    value = min(_bound(f.max, facts), value)
            elif f.kind in ("segmented", "select"):
                values = [v for v, _ in f.options]
                if value not in values:
                    value = _default(f, facts)
            elif f.kind == "toggle":
                value = bool(value)
            elif f.kind == "channels":
                keys = [c["key"] for c in facts.get("channels") or []]
                value = [k for k in (value or []) if k in keys] if isinstance(value, list) else []
            elif f.kind == "view":
                value = value if isinstance(value, dict) else None
            out[f.name] = value
        # Keys not described by a field but passed by an agent are kept when
        # they are plain JSON scalars a builder reads (e.g. `pivot`).
        for key in ("pivot", "title"):
            if key in raw and key not in out:
                out[key] = raw[key]
        return out


def _bound(value, facts):
    if value == "depth":
        return max(0, int(facts.get("depth") or 1) - 1)
    if value == "frames":
        return max(0, int(facts.get("frames") or 1) - 1)
    return value


def _default(f: Field, facts: dict):
    return _bound(f.default, facts)


# -- shared pieces -------------------------------------------------------------


AXIS_FIELD = Field("axis", "Axis", "segmented", "y",
                   (("x", "X"), ("y", "Y"), ("z", "Z"), ("screen_y", "↔"), ("screen_x", "↕")),
                   hint="X, Y and Z are the stack's own axes; ↔ and ↕ turn it as seen.")
EASE_OPTIONS = (("linear", "Even"), ("sine_in_out", "Gentle"), ("cubic_in_out", "Smooth"),
                ("cinematic", "Cinematic"))


def _source(facts: dict) -> dict:
    return {
        "datasource": facts.get("datasource") or "",
        "fingerprint": facts.get("fingerprint") or {},
        "image_size": facts.get("image_size") or [0, 0],
        "depth": int(facts.get("depth") or 1),
        "frames": int(facts.get("frames") or 1),
        "spacing": facts.get("spacing") or {},
        "pixel_size": facts.get("pixel_size"),
        "channels": [{"key": c.get("key"), "name": c.get("name")}
                     for c in facts.get("channels") or [] if c.get("key")],
    }


def _base(facts: dict, scene: dict | None, *, mode: str | None = None) -> tuple[dict, list]:
    """The video's first frame: the viewer's scene, with the view mode a preset
    needs. Returns `(scene, notes)`."""
    source = _source(facts)
    base = schema.normalize_scene(copy.deepcopy(scene or {}), source=source)
    notes = []
    if base["viewport"] is None:
        width, height = source["image_size"]
        base["viewport"] = schema.normalize_camera2d(
            {"cx": width / 2, "cy": height / 2, "w": max(1.0, width)})
    if mode and base["view"]["mode"] != mode:
        if mode == "volume":
            notes.append("Switches the viewer to the 3-D view for the video.")
            # The whole stack, as the viewer's own 3-D button opens it: a slab
            # that followed the plane would otherwise become a 3-D clip.
            top = max(0, int(facts.get("depth") or 1) - 1)
            op = (base["view"]["slab"] or {}).get("op", "max")
            base["view"]["slab"] = {"z0": 0, "z1": top, "op": op}
        elif mode == "sections":
            notes.append("Shows the plane with its two sections for the video.")
        elif base["view"]["mode"] == "volume":
            notes.append("Shows the stack as planes for the video.")
        base["view"]["mode"] = mode
    if base["view"]["mode"] == "volume" and base["camera3d"] is None:
        base["camera3d"] = copy.deepcopy(DEFAULT_CAMERA)
    if base["view"]["mode"] == "volume":
        base["dims"] = "3d"
    return base, notes


def _project(name, facts, scene, params, *, base, duration, tracks, loop=False, notes=(),
             title=None) -> dict:
    preset = PRESETS[name]
    return {
        "title": title or params.get("title") or f"{preset.label} · {date.today().isoformat()}",
        "source": _source(facts),
        "base": base,
        "duration_s": round(float(duration), 4),
        "loop": bool(loop),
        "tracks": tracks,
        "notes": list(notes),
        "provenance": {"created_by": "ui", "preset": {"name": name, "params": params}},
    }


def _camera_track(generator, params, *, start=0.0, end, ease="linear", label=""):
    return {"kind": "camera3d", "mode": "procedural", "range": [start, end], "label": label,
            "procedural": {"generator": generator, "params": params, "ease": ease}}


def _flat_mode(scene: dict | None) -> str:
    mode = ((scene or {}).get("view") or {}).get("mode")
    return mode if mode in ("plane", "slab", "sections") else "plane"


# -- 3-D presets -----------------------------------------------------------------


def build_orbit(facts, scene, p):
    base, notes = _base(facts, scene, mode="volume")
    duration = p["duration_s"]
    loop = p["loop"]
    track = _camera_track("orbit", {"axis": p["axis"], "direction": p["direction"],
                                    "turns": p["turns"], "from": "$current",
                                    "pivot": p.get("pivot")},
                          end=duration, ease="linear" if loop else "sine_in_out",
                          label="Rotation")
    return _project("orbit", facts, scene, p, base=base, duration=duration, tracks=[track],
                    loop=loop, notes=notes)


def build_rock(facts, scene, p):
    base, notes = _base(facts, scene, mode="volume")
    duration = p["period_s"] * p["cycles"]
    track = _camera_track("rock", {"axis": p["axis"], "direction": 1, "from": "$current",
                                   "amplitude_deg": p["amplitude_deg"], "cycles": p["cycles"]},
                          end=duration, label="Rock")
    return _project("rock", facts, scene, p, base=base, duration=duration, tracks=[track],
                    loop=True, notes=notes)


def build_tumble(facts, scene, p):
    base, notes = _base(facts, scene, mode="volume")
    duration = p["duration_s"]
    track = _camera_track("tumble", {"axis": "screen_y", "direction": 1, "from": "$current",
                                     "turns": int(p["turns"]), "tilt_deg": p["tilt_deg"]},
                          end=duration, label="Tumble")
    return _project("tumble", facts, scene, p, base=base, duration=duration, tracks=[track],
                    loop=True, notes=notes)


def build_fly_through(facts, scene, p):
    base, notes = _base(facts, scene, mode="volume")
    duration = p["duration_s"]
    track = _camera_track("dolly", {"axis": "screen_y", "direction": 1, "from": "$current",
                                    "to_dist": p["to_dist"], "turn_deg": p["turn_deg"]},
                          end=duration, ease=p["ease"], label="Fly-through")
    return _project("fly_through", facts, scene, p, base=base, duration=duration,
                    tracks=[track], notes=notes)


#: Slice Reveal from a side: (clip axis, which end moves, from, to). Top and
#: bottom go plane by plane through the slab instead (acquired planes).
SIDES = {"left": ("x", "hi", 0.0, 1.0), "right": ("x", "lo", 1.0, 0.0),
         "front": ("y", "hi", 0.0, 1.0), "back": ("y", "lo", 1.0, 0.0)}


def build_slice_reveal(facts, scene, p):
    base, notes = _base(facts, scene, mode="volume")
    top = int(facts.get("depth") or 1) - 1
    sweep = p["duration_s"]
    hold = p["hold_end_s"]
    if p["direction"] in SIDES:
        axis, side, start, end = SIDES[p["direction"]]
        # Snapped to the image's own pixels as it moves: a cut through the
        # middle of a voxel shows half of something that was imaged whole.
        track = {"kind": "clip", "mode": "procedural", "range": [0.0, sweep], "label": "Reveal",
                 "procedural": {"generator": "reveal", "ease": "sine_in_out",
                                "params": {"axis": axis, "side": side, "from": start, "to": end,
                                           "snap": True}}}
    else:
        start, end = (0, top) if p["direction"] == "down" else (top, 0)
        track = {"kind": "slab", "mode": "procedural", "range": [0.0, sweep], "label": "Reveal",
                 "procedural": {"generator": "reveal",
                                "params": {"from": start, "to": end, "op": "max"}}}
    return _project("slice_reveal", facts, scene, p, base=base, duration=sweep + hold,
                    tracks=[track], notes=notes)


# -- stack and time presets --------------------------------------------------------


def _sweep_span(start, end, bounce):
    steps = abs(end - start) + 1
    return (2 * steps - 2 if bounce and steps > 1 else steps), steps


def build_z_sweep(facts, scene, p):
    base, notes = _base(facts, scene, mode=_flat_mode(scene))
    start, end = int(p["from"]), int(p["to"])
    if p["direction"] == "down":
        start, end = max(start, end), min(start, end)
    else:
        start, end = min(start, end), max(start, end)
    bounce = p["direction"] == "bounce"
    count, _ = _sweep_span(start, end, bounce)
    sweep = count * p["ms_per_plane"] / 1000.0
    hold = 0.0 if p["loop"] else p["hold_ends_s"]
    track = {"kind": "acquired_z", "mode": "procedural", "range": [hold, hold + sweep],
             "label": "Planes",
             "procedural": {"generator": "sweep",
                            "params": {"from": start, "to": end, "bounce": bounce}}}
    base["view"]["z"] = start
    return _project("z_sweep", facts, scene, p, base=base, duration=sweep + 2 * hold,
                    tracks=[track], loop=p["loop"], notes=notes)


def build_time_lapse(facts, scene, p):
    base, notes = _base(facts, scene)
    start, end = int(p["from"]), int(p["to"])
    bounce = bool(p["bounce"])
    count, _ = _sweep_span(start, end, bounce)
    sweep = count * p["ms_per_frame"] / 1000.0
    hold = 0.0 if p["loop"] else p["hold_ends_s"]
    track = {"kind": "acquired_t", "mode": "procedural", "range": [hold, hold + sweep],
             "label": "Timepoints",
             "procedural": {"generator": "sweep",
                            "params": {"from": start, "to": end, "bounce": bounce}}}
    base["view"]["t"] = start
    return _project("time_lapse", facts, scene, p, base=base, duration=sweep + 2 * hold,
                    tracks=[track], loop=p["loop"], notes=notes)


# -- plane presets -----------------------------------------------------------------


def _zoom_end(base, p, *, factor):
    end_view = p.get("end_view") or {}
    if base["view"]["mode"] == "volume":
        start = base["camera3d"]
        end = schema.normalize_camera3d(end_view.get("camera3d")) if end_view.get(
            "camera3d") else {**copy.deepcopy(start), "dist": max(0.15, start["dist"] / factor)}
        return "camera3d", start, end
    start = base["viewport"]
    end = schema.normalize_camera2d(end_view.get("viewport")) if end_view.get(
        "viewport") else {**copy.deepcopy(start), "w": start["w"] / factor}
    return "camera2d", start, end


def build_zoom(facts, scene, p):
    base, notes = _base(facts, scene)
    kind, start, end = _zoom_end(base, p, factor=float(p["factor"]))
    if p["direction"] == "out":
        start, end = end, start
        if kind == "camera3d":
            base["camera3d"] = copy.deepcopy(start)
        else:
            base["viewport"] = copy.deepcopy(start)
    move = p["duration_s"]
    hold = p["hold_end_s"]
    track = {"kind": kind, "mode": "keyframes", "label": "Zoom",
             "interp": {"orientation": "slerp", "path": "linear", "speed": "time"},
             "keyframes": [{"t": 0.0, "value": start, "ease": p["ease"]},
                           {"t": move, "value": end, "ease": "linear"}]}
    return _project("zoom", facts, scene, p, base=base, duration=move + hold, tracks=[track],
                    notes=notes)


PAN_VECTORS = {"left": (-1, 0), "right": (1, 0), "up": (0, -1), "down": (0, 1)}


def build_xy_pan(facts, scene, p):
    base, notes = _base(facts, scene)
    duration = p["duration_s"]
    dx, dy = PAN_VECTORS[p["direction"]]
    end_view = (p.get("end_view") or {})
    if base["view"]["mode"] == "volume":
        start = base["camera3d"]
        # The visible width at the camera's distance, in model units: the
        # renderer's vertical field of view is 0.7 rad.
        span = 2 * start["dist"] * math.tan(0.35) * (16 / 9)
        shift = p["speed"] * duration * span
        end = schema.normalize_camera3d(end_view.get("camera3d")) if end_view.get(
            "camera3d") else {**copy.deepcopy(start),
                              "pan": [start["pan"][0] - dx * shift, start["pan"][1] + dy * shift]}
        kind = "camera3d"
    else:
        start = base["viewport"]
        shift = p["speed"] * duration * start["w"]
        end = schema.normalize_camera2d(end_view.get("viewport")) if end_view.get(
            "viewport") else {**copy.deepcopy(start), "cx": start["cx"] + dx * shift,
                              "cy": start["cy"] + dy * shift}
        kind = "camera2d"
    track = {"kind": kind, "mode": "keyframes", "label": "Pan",
             "interp": {"orientation": "slerp", "path": "linear", "speed": "time"},
             "keyframes": [{"t": 0.0, "value": start, "ease": p["ease"]},
                           {"t": duration, "value": end, "ease": "linear"}]}
    return _project("xy_pan", facts, scene, p, base=base, duration=duration, tracks=[track],
                    notes=notes)


# -- channel presets -----------------------------------------------------------------


def _reveal_tracks(order, *, start, per, fade, mode, hold):
    """One channel track per key: presence 0 -> 1 as each channel's turn comes.

    `add` keeps every revealed channel; `solo` shows each alone, then all of
    them together for the last `hold` seconds."""
    tracks = []
    n = len(order)
    end = start + n * per
    for i, key in enumerate(order):
        t_on = start + i * per
        frames = []
        if mode == "add":
            if i == 0:
                frames = [{"t": 0.0, "value": {"visible": True, "presence": 1.0}, "ease": "linear"}]
            else:
                frames = [{"t": 0.0, "value": {"visible": True, "presence": 0.0}, "ease": "linear"},
                          {"t": t_on, "value": {"presence": 0.0}, "ease": "sine_in_out"},
                          {"t": t_on + fade, "value": {"presence": 1.0}, "ease": "linear"}]
        else:
            t_off = t_on + per
            first = 1.0 if i == 0 else 0.0
            frames = [{"t": 0.0, "value": {"visible": True, "presence": first}, "ease": "linear"}]
            if i > 0:
                frames += [{"t": max(0.0, t_on - fade / 2), "value": {"presence": 0.0},
                            "ease": "sine_in_out"},
                           {"t": t_on + fade / 2, "value": {"presence": 1.0}, "ease": "linear"}]
            if i < n - 1:
                frames += [{"t": t_off - fade / 2, "value": {"presence": 1.0},
                            "ease": "sine_in_out"},
                           {"t": t_off + fade / 2, "value": {"presence": 0.0}, "ease": "linear"}]
            if hold > 0 and i < n - 1:
                frames += [{"t": end, "value": {"presence": 0.0}, "ease": "sine_in_out"},
                           {"t": end + min(fade, hold), "value": {"presence": 1.0},
                            "ease": "linear"}]
        tracks.append({"kind": "channel", "target": key, "mode": "keyframes",
                       "label": key, "keyframes": frames})
    return tracks, end


def _reveal_order(facts, scene, p):
    keys = [c["key"] for c in facts.get("channels") or []]
    order = [k for k in p.get("order") or [] if k in keys]
    if not order:
        visible = [c["key"] for c in (scene or {}).get("channels") or []
                   if c.get("visible", True) and c.get("key") in keys]
        # A reveal of one channel reveals nothing: with fewer than two on
        # screen, the image's first channels (up to four) in sidebar order.
        order = visible if len(visible) >= 2 else keys[:4]
    return order


def build_channel_reveal(facts, scene, p):
    base, notes = _base(facts, scene)
    order = _reveal_order(facts, scene, p)
    tracks, end = _reveal_tracks(order, start=0.0, per=p["per_channel_s"],
                                 fade=min(p["fade_s"], p["per_channel_s"]), mode=p["mode"],
                                 hold=p["hold_s"])
    keyed = {c["key"] for c in base["channels"]}
    names = {c["key"]: c.get("name") or c["key"] for c in facts.get("channels") or []}
    for key in order:
        if key not in keyed:
            base["channels"].append(schema.normalize_channel(
                {"key": key, "name": names.get(key, key), "visible": True}))
    for channel in base["channels"]:
        if channel["key"] in order:
            channel["visible"] = True
    if p.get("captions"):
        tracks.append({"kind": "hud", "mode": "keyframes", "label": "Captions",
                       "keyframes": [{"t": i * p["per_channel_s"],
                                      "value": {"caption": names.get(k, k)}, "ease": "step"}
                                     for i, k in enumerate(order)]
                       + [{"t": end, "value": {"caption": ""}, "ease": "step"}]})
    return _project("channel_reveal", facts, scene, p, base=base, duration=end + p["hold_s"],
                    tracks=tracks, notes=notes)


# -- storyboards ----------------------------------------------------------------------


def build_story_structure(facts, scene, p):
    """3-D Structure Showcase: the stack built up plane by plane, a full turn,
    then a rock. The reveal comes first so the stack is never shown whole and
    then cut back to one plane."""
    base, notes = _base(facts, scene, mode="volume")
    turn, reveal, rock = p["turn_s"], p["reveal_s"], p["rock_s"]
    top = int(facts.get("depth") or 1) - 1
    tracks = [
        {"kind": "slab", "mode": "procedural", "range": [0.0, reveal], "label": "Reveal",
         "procedural": {"generator": "reveal", "params": {"from": top, "to": 0, "op": "max"}}},
        _camera_track("orbit", {"axis": "screen_y", "direction": 1, "turns": 1,
                                "from": "$current"}, start=reveal, end=reveal + turn,
                      ease="sine_in_out", label="Rotation"),
        _camera_track("rock", {"axis": "screen_y", "direction": 1, "from": "$current",
                               "amplitude_deg": 20, "cycles": 1},
                      start=turn + reveal, end=turn + reveal + rock, label="Rock"),
    ]
    return _project("story_structure", facts, scene, p, base=base,
                    duration=turn + reveal + rock, tracks=tracks, notes=notes)


def build_story_overview(facts, scene, p):
    """Tissue Overview: the whole field, a zoom to the region, the markers one
    by one, and back out."""
    base, notes = _base(facts, scene)
    width, height = (facts.get("image_size") or [0, 0])
    whole = schema.normalize_camera2d({"cx": width / 2, "cy": height / 2, "w": max(1, width)})
    region = p.get("end_view") or {}
    if base["view"]["mode"] == "volume":
        base["view"]["mode"] = _flat_mode(None)
        notes.append("Shows the stack as planes for the overview.")
    near = (schema.normalize_camera2d(region.get("viewport")) if region.get("viewport")
            else {**whole, "w": whole["w"] / float(p["factor"])})
    hold, zoom = 1.0, p["zoom_s"]
    base["viewport"] = copy.deepcopy(whole)
    order = _reveal_order(facts, scene, {})
    per = p["per_channel_s"]
    reveal_start = hold + zoom
    reveal, reveal_end = _reveal_tracks(order, start=reveal_start, per=per,
                                        fade=min(0.6, per), mode="add", hold=0.0)
    out_start = reveal_end + 1.0
    camera = {"kind": "camera2d", "mode": "keyframes", "label": "Overview",
              "interp": {"orientation": "slerp", "path": "linear", "speed": "time"},
              "keyframes": [
                  {"t": 0.0, "value": whole, "ease": "linear"},
                  {"t": hold, "value": whole, "ease": "cinematic"},
                  {"t": hold + zoom, "value": near, "ease": "linear"},
                  {"t": out_start, "value": near, "ease": "cinematic"},
                  {"t": out_start + zoom, "value": whole, "ease": "linear"}]}
    return _project("story_overview", facts, scene, p, base=base,
                    duration=out_start + zoom + hold, tracks=[camera] + reveal, notes=notes)


def build_story_markers(facts, scene, p):
    """Sequential Marker Reveal: each marker alone with its name, then all."""
    params = {"order": p.get("order"), "per_channel_s": p["per_channel_s"], "fade_s": 0.6,
              "mode": "solo", "hold_s": 2.0, "captions": True}
    doc = build_channel_reveal(facts, scene, params)
    doc["provenance"]["preset"] = {"name": "story_markers", "params": p}
    doc["title"] = f"{PRESETS['story_markers'].label} · {date.today().isoformat()}"
    return doc


def build_story_orthogonal(facts, scene, p):
    """Orthogonal Exploration: the plane and its two sections, swept through z."""
    base, notes = _base(facts, scene, mode="sections")
    top = int(facts.get("depth") or 1) - 1
    count = top + 1
    sweep = count * p["ms_per_plane"] / 1000.0
    track = {"kind": "acquired_z", "mode": "procedural", "range": [0.5, 0.5 + sweep],
             "label": "Planes",
             "procedural": {"generator": "sweep", "params": {"from": 0, "to": top}}}
    base["view"]["z"] = 0
    return _project("story_orthogonal", facts, scene, p, base=base, duration=sweep + 1.0,
                    tracks=[track], notes=notes)


def build_story_timeseries(facts, scene, p):
    """Time-Series Comparison: every timepoint, with the acquired time printed."""
    params = {"from": 0, "to": int(facts.get("frames") or 1) - 1, "bounce": False,
              "ms_per_frame": p["ms_per_frame"], "loop": False, "hold_ends_s": 1.0}
    doc = build_time_lapse(facts, scene, params)
    doc["provenance"]["preset"] = {"name": "story_timeseries", "params": p}
    doc["title"] = f"{PRESETS['story_timeseries'].label} · {date.today().isoformat()}"
    doc["output"] = {"overlays": {"label": True, "scalebar": True}}
    return doc


# -- the table -----------------------------------------------------------------------


def _duration(default, lo=0.5, hi=600.0):
    return Field("duration_s", "Length", "number", default, unit="s", min=lo, max=hi, step=0.5)


PRESETS: dict[str, Preset] = {}


def _register(*presets):
    for preset in presets:
        PRESETS[preset.name] = preset


_register(
    Preset("orbit", "360° Rotation", "3d", "rotate", "A full turn of the stack about one axis.",
           (AXIS_FIELD,
            Field("direction", "Direction", "segmented", 1, ((1, "↻"), (-1, "↺"))),
            _duration(8.0),
            Field("turns", "Turns", "number", 1, min=1, max=10, step=1),
            Field("loop", "Loop", "toggle", True,
                  hint="A loop never repeats its first frame, so it plays without a seam.")),
           build_orbit, needs=("depth",)),
    Preset("rock", "Rock", "3d", "arrows-left-right", "A back-and-forth turn about one axis.",
           (AXIS_FIELD,
            Field("amplitude_deg", "Swing", "segmented", 30.0,
                  ((15.0, "±15°"), (30.0, "±30°"), (45.0, "±45°"))),
            Field("period_s", "Period", "number", 4.0, unit="s", min=1, max=60, step=0.5),
            Field("cycles", "Cycles", "number", 2, min=1, max=20, step=1)),
           build_rock, needs=("depth",)),
    Preset("tumble", "3-D Tumble", "3d", "cube", "A turn with a gentle wobble, as a loop.",
           (_duration(10.0),
            Field("turns", "Turns", "number", 1, min=1, max=6, step=1),
            Field("tilt_deg", "Tilt", "number", 25.0, unit="°", min=0, max=60, step=5)),
           build_tumble, needs=("depth",)),
    Preset("fly_through", "Fly-through", "3d", "plane-departure",
           "The camera moves in toward the stack, turning as it goes.",
           (_duration(8.0),
            Field("to_dist", "End distance", "number", 0.6, min=0.15, max=4, step=0.05,
                  hint="The renderer's distance units: 2.2 is the opening view."),
            Field("turn_deg", "Turn", "number", 60.0, unit="°", min=-360, max=360, step=15),
            Field("ease", "Pace", "segmented", "cinematic", EASE_OPTIONS)),
           build_fly_through, needs=("depth",)),
    Preset("slice_reveal", "Slice Reveal", "3d", "layer-group",
           "Planes added to the 3-D view one at a time, then held.",
           (Field("direction", "From", "segmented", "down",
                  (("down", "Top"), ("up", "Bottom"), ("left", "Left"), ("right", "Right"),
                   ("front", "Front"), ("back", "Back")),
                  hint="Top and Bottom add whole planes; the sides cut the stack open."),
            _duration(6.0),
            Field("hold_end_s", "Hold end", "number", 1.0, unit="s", min=0, max=30, step=0.5)),
           build_slice_reveal, needs=("depth",)),
    Preset("z_sweep", "Z-Stack Sweep", "stack", "layer-group",
           "Every plane from one to another, at an even pace.",
           (Field("from", "From", "number", 0, min=0, max="depth", step=1),
            Field("to", "To", "number", "depth", min=0, max="depth", step=1),
            Field("direction", "Direction", "segmented", "up",
                  (("up", "Up"), ("down", "Down"), ("bounce", "Bounce"))),
            Field("ms_per_plane", "Per plane", "number", 100.0, unit="ms", min=10, max=5000,
                  step=10),
            Field("hold_ends_s", "Hold ends", "number", 0.5, unit="s", min=0, max=10, step=0.5),
            Field("loop", "Loop", "toggle", False)),
           build_z_sweep, needs=("depth",)),
    Preset("time_lapse", "Time-Lapse", "stack", "clock",
           "Every acquired timepoint in order. Timepoints are never blended.",
           (Field("from", "From", "number", 0, min=0, max="frames", step=1),
            Field("to", "To", "number", "frames", min=0, max="frames", step=1),
            Field("ms_per_frame", "Per timepoint", "number", 200.0, unit="ms", min=10,
                  max=10000, step=10),
            Field("bounce", "Bounce", "toggle", False),
            Field("hold_ends_s", "Hold ends", "number", 0.5, unit="s", min=0, max=10, step=0.5),
            Field("loop", "Loop", "toggle", False)),
           build_time_lapse, needs=("frames",)),
    Preset("zoom", "Zoom In/Out", "plane", "magnifying-glass-plus",
           "From the current view to a closer one, or back.",
           (Field("end_view", "End view", "view", None,
                  hint="Move the image to where the zoom should end, then Set end view."),
            Field("factor", "Zoom", "segmented", 4.0,
                  ((2.0, "2×"), (4.0, "4×"), (8.0, "8×"), (16.0, "16×"))),
            Field("direction", "Direction", "segmented", "in", (("in", "In"), ("out", "Out"))),
            Field("duration_s", "Length", "number", 4.0, unit="s", min=0.5, max=120, step=0.5),
            Field("ease", "Pace", "segmented", "sine_in_out", EASE_OPTIONS),
            Field("hold_end_s", "Hold end", "number", 1.0, unit="s", min=0, max=30, step=0.5)),
           build_zoom),
    Preset("xy_pan", "XY Pan", "plane", "arrows-up-down-left-right",
           "A steady move across the image.",
           (Field("direction", "Direction", "segmented", "right",
                  (("left", "←"), ("right", "→"), ("up", "↑"), ("down", "↓"))),
            Field("end_view", "End view", "view", None,
                  hint="Or move to where the pan should end and Set end view."),
            Field("speed", "Speed", "number", 0.15, unit="views/s", min=0.01, max=2, step=0.01),
            Field("duration_s", "Length", "number", 6.0, unit="s", min=0.5, max=120, step=0.5),
            Field("ease", "Pace", "segmented", "linear", EASE_OPTIONS)),
           build_xy_pan),
    Preset("channel_reveal", "Channel Reveal", "channels", "swatchbook",
           "Channels fade in one after another.",
           (Field("order", "Order", "channels", [],
                  hint="The visible channels, in sidebar order, unless chosen here."),
            Field("mode", "Style", "segmented", "add", (("add", "Add"), ("solo", "Solo"))),
            Field("per_channel_s", "Per channel", "number", 1.5, unit="s", min=0.2, max=30,
                  step=0.1),
            Field("fade_s", "Fade", "number", 0.5, unit="s", min=0, max=10, step=0.1),
            Field("hold_s", "Hold end", "number", 1.0, unit="s", min=0, max=30, step=0.5),
            Field("captions", "Names", "toggle", False, hint="Print each channel's name.")),
           build_channel_reveal, needs=("channels",)),
    Preset("story_overview", "Tissue Overview", "story", "map",
           "The whole field, a zoom to one region, the markers one by one, back out.",
           (Field("end_view", "Region", "view", None,
                  hint="Move to the region to zoom to, then Set region."),
            Field("factor", "Zoom", "segmented", 4.0, ((2.0, "2×"), (4.0, "4×"), (8.0, "8×"))),
            Field("zoom_s", "Zoom length", "number", 3.0, unit="s", min=0.5, max=30, step=0.5),
            Field("per_channel_s", "Per channel", "number", 1.2, unit="s", min=0.2, max=10,
                  step=0.1)),
           build_story_overview, story=True),
    Preset("story_structure", "3-D Structure Showcase", "story", "cubes",
           "The stack built up plane by plane, a full turn, then a rock.",
           (Field("turn_s", "Turn", "number", 8.0, unit="s", min=1, max=60, step=0.5),
            Field("reveal_s", "Reveal", "number", 5.0, unit="s", min=1, max=60, step=0.5),
            Field("rock_s", "Rock", "number", 4.0, unit="s", min=1, max=60, step=0.5)),
           build_story_structure, needs=("depth",), story=True),
    Preset("story_markers", "Sequential Marker Reveal", "story", "list-ol",
           "Each marker alone with its name, then all of them together.",
           (Field("order", "Order", "channels", []),
            Field("per_channel_s", "Per marker", "number", 2.0, unit="s", min=0.5, max=30,
                  step=0.5)),
           build_story_markers, needs=("channels",), story=True),
    Preset("story_orthogonal", "Orthogonal Exploration", "story", "table-cells",
           "The plane and its two sections, swept through every plane.",
           (Field("ms_per_plane", "Per plane", "number", 120.0, unit="ms", min=20, max=5000,
                  step=10),),
           build_story_orthogonal, needs=("depth",), story=True),
    Preset("story_timeseries", "Time-Series Comparison", "story", "chart-line",
           "Every timepoint with its acquired time printed.",
           (Field("ms_per_frame", "Per timepoint", "number", 300.0, unit="ms", min=20,
                  max=10000, step=10),),
           build_story_timeseries, needs=("frames",), story=True),
)

GROUPS = (("3d", "3-D"), ("stack", "Stack"), ("plane", "Plane"), ("channels", "Channels"),
          ("story", "Storyboards"))


def describe_all(facts: dict | None = None) -> list[dict]:
    return [preset.describe(facts) for preset in PRESETS.values()]


def default_preset(facts: dict, scene: dict | None = None) -> str:
    """The preset the bar opens on: a rotation in 3-D, a sweep on a stack shown
    as planes, a time-lapse on a series, a zoom on a flat image."""
    mode = ((scene or {}).get("view") or {}).get("mode")
    if int(facts.get("depth") or 1) > 1:
        return "orbit" if mode == "volume" else "z_sweep"
    if int(facts.get("frames") or 1) > 1:
        return "time_lapse"
    return "zoom"


def build(name: str, facts: dict, scene: dict | None = None, params: dict | None = None,
          *, created_by: str = "ui") -> dict:
    """A normalised project (no id yet) for preset `name`. Raises KeyError for
    an unknown preset and ValueError for one this image cannot support."""
    preset = PRESETS[name]
    ok, reason = preset.available(facts)
    if not ok:
        raise ValueError(f"{preset.label}: {reason}")
    params = preset.params(params, facts)
    raw = preset.build(facts, scene, params)
    raw["provenance"]["created_by"] = created_by
    project = schema.normalize_project(raw)
    project["provenance"]["preset"] = {"name": name, "params": params}
    return project
