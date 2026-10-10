"""Volumes as capabilities: what a z-stack is, and which of its planes to look at.

A z-stack is one object; a plane, a slab projection and a section are pictures
of it. These tools answer the questions an agent has to settle BEFORE it looks
at anything in 3-D -- is this a volume, is its z step known, which planes are
in focus -- with deterministic numbers, so the model's judgement is spent on
choosing what to look at and reading what it sees, not on guessing.

Every pixel read goes through `plexora.api` handles (`ImageHandle.volume`,
`SourceImage(view=)`), never the viewer's loaded project
(tests/test_agent_architecture.py).
"""

from __future__ import annotations

import math
from typing import Literal

import numpy as np
from pydantic import Field

from plexora.agent.errors import AgentError
from plexora.agent.registry import Capability
from plexora.agent.schemas import ProjectInput

TAGS = ("volume", "3d", "z-stack", "zstack", "z", "plane", "planes", "depth", "focus",
        "projection", "mip", "confocal", "section", "stack", "time", "frame")

#: The longest side a profile reads each plane at. A focus or intensity
#: profile compares planes with each other, so the coarse level that keeps
#: about this much detail is enough, and a 40-plane stack costs 40 small reads.
PROFILE_SIDE = 512

#: The most planes one profile reads.
MAX_PLANES_PER_PROFILE = 256


def _volume_of(call, project):
    """`(record, data, volume_dict)`; `invalid_input` for a planar image."""
    record = call.session.project(project)
    data = call.session.image_data(project)
    volume = data.image.volume()
    if volume is None:
        raise AgentError("invalid_input",
                         f"{project}'s image has a single plane: it is not a z-stack or a "
                         "time series, so there are no planes to choose between")
    return record, data, volume


def _channel_index(record, name):
    channels = list(record.image.real_channels)
    if name is None:
        return 0, channels[0].get("fullname") or channels[0].get("name")
    wanted = str(name).casefold()
    for index, channel in enumerate(channels):
        for candidate in (channel.get("fullname"), channel.get("name")):
            if candidate and str(candidate).casefold() == wanted:
                return index, channel.get("fullname") or channel.get("name")
    raise AgentError("invalid_input", f"no channel {name!r}",
                     detail={"channels": [c.get("fullname") or c.get("name")
                                          for c in channels]})


def _profile_level(volume):
    """The finest level whose longest side is at most PROFILE_SIDE."""
    for info in volume["levels"]:
        if max(info["height"], info["width"]) <= PROFILE_SIDE:
            return info["level"]
    return volume["levels"][-1]["level"]


def _focus(plane: np.ndarray) -> float:
    """Tenengrad: mean squared gradient magnitude -- high for a sharp plane.

    Scale-free comparisons only: the number means nothing on its own, and only
    its rank among one channel's planes is used.
    """
    values = plane.astype(np.float32)
    gy = values[2:, 1:-1] - values[:-2, 1:-1]
    gx = values[1:-1, 2:] - values[1:-1, :-2]
    return float(np.mean(gx * gx + gy * gy)) if gx.size else 0.0


def _plane_profile(call, project, volume, channel, t, box=None):
    """Per-plane focus, median, p99 and background for one channel."""
    from plexora.server.utils.source_image import SourceImage

    data = call.session.image_data(project)
    level = _profile_level(volume)
    depth = int(volume["depth"])
    if depth > MAX_PLANES_PER_PROFILE:
        raise AgentError("too_large", f"{depth} planes is more than one profile reads "
                         f"({MAX_PLANES_PER_PROFILE})")
    info = volume["levels"][level]
    scale = 2 ** level
    if box is None:
        lbox = (0, 0, info["width"], info["height"])
    else:
        lbox = (int(math.floor(box[0] / scale)), int(math.floor(box[1] / scale)),
                int(math.ceil(box[2] / scale)), int(math.ceil(box[3] / scale)))
    rows = []
    prefix = f"t{t}" if t else ""
    for z in range(depth):
        with SourceImage(data, view=f"{prefix}z{z}") as source:
            plane, _clipped = source.read(channel, level, lbox)
        plane = np.asarray(plane)
        finite = plane[np.isfinite(plane)] if plane.dtype.kind == "f" else plane.ravel()
        if finite.size == 0:
            rows.append({"z": z, "focus": 0.0, "median": 0.0, "p99": 0.0, "p05": 0.0})
            continue
        p05, median, p99 = (float(v) for v in np.percentile(finite, (5, 50, 99)))
        rows.append({"z": z, "focus": _focus(plane), "median": median, "p99": p99,
                     "p05": p05})
    return rows, level


# -- inspect_volume ----------------------------------------------------------


def inspect_volume(call, inp):
    record = call.session.project(inp.project)
    data = call.session.image_data(inp.project)
    volume = data.image.volume()
    if volume is None:
        out = {"project": record.name, "volumetric": False, "depth": 1, "frames": 1}
        if record.image.z_semantics == "cycle":
            out["note"] = ("The file stores planes on Z but they were read as imaging cycles "
                           "(no z step stated), so they are channels here.")
        return out
    spacing = volume["spacing"]
    warnings = []
    z = spacing.get("z") or {}
    if volume["depth"] > 1 and z.get("source") not in ("metadata", "manual"):
        warnings.append("No z step is known: sections are drawn one pixel per plane and "
                        "no physical 3-D measurement can be made. Ask the user for it "
                        "(set_z_spacing) before any distance, volume or depth in µm.")
    xy = spacing.get("x") or {}
    if xy.get("source") not in ("metadata", "manual"):
        warnings.append("No pixel size is known; distances are in pixels.")
    if volume.get("z_semantics") == "cycle":
        warnings.append("Planes on Z were read as imaging cycles.")
    depth = int(volume["depth"])
    suggested = [{"view": volume["default_view"], "why": "the plane the viewer opens on"}]
    if depth > 1:
        suggested.append({"view": f"z0-{depth - 1}max",
                          "why": "maximum projection of every plane: an overview of "
                                 "what is anywhere in the stack (not a plane)"})
    segmentation = {"available": bool(record.segmentation.derived),
                    "kind": "labels" if record.segmentation.derived else None,
                    "note": ("one 2-D mask, drawn on every plane" if record.segmentation.derived
                             else None)}
    return {
        "project": record.name, "volumetric": True,
        "depth": depth, "frames": int(volume["frames"]),
        "channels": int(volume["channels"]),
        "shape": volume["shape"], "axes": volume["axes"],
        "spacing": spacing,
        "anisotropy": volume.get("anisotropy"),
        "anisotropy_source": volume.get("anisotropy_source"),
        "volumetric_ok": volume.get("volumetric_ok"),
        "z_semantics": volume.get("z_semantics"),
        "default_view": volume["default_view"],
        "levels": volume["levels"],
        "segmentation": segmentation,
        "suggested_views": suggested,
        "warnings": warnings,
        "view_grammar": "z12 = plane 12; z4-19max = maximum projection of planes 4..19 "
                        "(inclusive); zmean, zmin, zstd, zmed, z2-8p95; t3z12 = frame 3",
    }


# -- set_z_spacing -------------------------------------------------------------


class ZSpacingInput(ProjectInput):
    microns_per_plane: float | None = Field(
        description="The distance between planes in microns, as the user states it. "
                    "None (or 0) clears a typed value.", ge=0)


def set_z_spacing(call, inp):
    """Record the z step of a stack whose file states none -- the z twin of
    set_pixel_size, and refused for the same reason when the file states one."""
    from plexora import datasource
    from plexora.agent.receipts import make_receipt
    from plexora.api import features

    record, data, volume = _volume_of(call, inp.project)
    before = (volume.get("spacing") or {}).get("z") or {}
    if before.get("source") == "metadata":
        raise AgentError("conflict", f"{inp.project!r}'s image file states its z step "
                         f"({before.get('value'):.4g} {before.get('unit')}); it is not "
                         "overridden here", detail={"z": before})
    value = inp.microns_per_plane or None
    try:
        datasource.set_pixel_size(inp.project, value, axis="z")
    except ValueError as exc:
        raise AgentError("invalid_input", str(exc)) from None
    call.session.invalidate(inp.project)
    after = (call.session.image_data(inp.project).image.volume() or {}).get(
        "spacing", {}).get("z", {})
    before_value = before.get("value") if before.get("source") == "manual" else None
    changed = before_value != (after or {}).get("value") or before.get("source") != \
        (after or {}).get("source")
    if changed:
        features.reload_if_loaded(inp.project)
    receipt = make_receipt(
        call, changed=changed, before={"microns_per_plane": before_value},
        after={"microns_per_plane": value}, persistent_state="project_config",
        undo_hint={"tool": "set_z_spacing", "arguments": {
            "project": inp.project, "microns_per_plane": before_value}})
    if changed and call.notify is not None:
        try:
            call.notify(inp.project, "core", "reload", {"datasource_changed": True})
        except Exception:
            pass
    return {"receipt": receipt.model_dump(mode="json"), "before": before, "after": after,
            "changed": changed}


# -- sample_z_planes / axis_profile ------------------------------------------


class SampleZInput(ProjectInput):
    channel: str | None = Field(None, description="Which channel to judge planes by "
                                "(default: the first; a nuclear stain is usually best).")
    strategy: Literal["focus", "intensity", "uniform", "edges"] = Field(
        "focus", description="focus: the sharpest planes, spread out; intensity: the "
                             "brightest; uniform: evenly spaced; edges: first, middle, last.")
    n: int = Field(3, ge=1, le=24, description="How many planes to return.")
    t: int = Field(0, ge=0, description="Frame of a time series.")


def _spread_pick(scores, n, min_gap):
    """The highest-scoring indices, at least `min_gap` apart where possible."""
    order = sorted(range(len(scores)), key=lambda i: -scores[i])
    picked = []
    for index in order:
        if all(abs(index - other) >= min_gap for other in picked):
            picked.append(index)
        if len(picked) == n:
            break
    for index in order:
        if len(picked) == n:
            break
        if index not in picked:
            picked.append(index)
    return sorted(picked)


def sample_z_planes(call, inp):
    record, _data, volume = _volume_of(call, inp.project)
    depth = int(volume["depth"])
    if depth < 2:
        raise AgentError("invalid_input", f"{inp.project} has one plane per frame; there "
                         "is nothing to choose between")
    if inp.t >= int(volume["frames"]):
        raise AgentError("invalid_input", f"t {inp.t} is past the last frame")
    index, name = _channel_index(record, inp.channel)
    n = min(inp.n, depth)
    rows, level = _plane_profile(call, inp.project, volume, index, inp.t)
    if inp.strategy == "focus":
        top = max(row["focus"] for row in rows) or 1.0
        scores = [row["focus"] / top for row in rows]
        picked = _spread_pick(scores, n, max(1, depth // (2 * n)))
        why = "relative sharpness {:.2f} (Tenengrad, 1 = sharpest plane)"
    elif inp.strategy == "intensity":
        top = max(row["p99"] for row in rows) or 1.0
        scores = [row["p99"] / top for row in rows]
        picked = _spread_pick(scores, n, max(1, depth // (2 * n)))
        why = "relative 99th-percentile intensity {:.2f}"
    elif inp.strategy == "uniform":
        picked = sorted({round(i * (depth - 1) / max(1, n - 1)) for i in range(n)}) \
            if n > 1 else [depth // 2]
        scores = [1.0] * depth
        why = "evenly spaced"
    else:
        picked = sorted({0, depth // 2, depth - 1})[:n]
        scores = [1.0] * depth
        why = "the first, middle and last plane"
    prefix = f"t{inp.t}" if inp.t else ""
    planes = [{"z": z, "view": f"{prefix}z{z}", "score": round(scores[z], 4),
               "why": why.format(scores[z]) if "{" in why else why} for z in picked]
    return {"project": record.name, "channel": name, "strategy": inp.strategy,
            "t": inp.t, "depth": depth, "planes": planes, "profile_level": level,
            "profile": [{k: round(v, 4) if isinstance(v, float) else v
                         for k, v in row.items()} for row in rows]}


class AxisProfileInput(ProjectInput):
    channel: str | None = Field(None, description="Which channel (default: the first).")
    t: int = Field(0, ge=0)
    bounds: list[float] | None = Field(None, min_length=4, max_length=4,
                                       description="Optional region [x0, y0, x1, y1] in "
                                                   "full-resolution pixels.")


def axis_profile(call, inp):
    """Per-plane focus and intensity, the sharpest plane, and how fast signal
    fades with depth -- the numbers behind 'is this stack usable all the way
    down'."""
    record, _data, volume = _volume_of(call, inp.project)
    if inp.t >= int(volume["frames"]):
        raise AgentError("invalid_input", f"t {inp.t} is past the last frame")
    index, name = _channel_index(record, inp.channel)
    rows, level = _plane_profile(call, inp.project, volume, index, inp.t,
                                 box=tuple(inp.bounds) if inp.bounds else None)
    focus = [row["focus"] for row in rows]
    best = int(np.argmax(focus)) if focus else 0
    attenuation = _attenuation(rows, (volume.get("spacing") or {}).get("z") or {})
    return {"project": record.name, "channel": name, "t": inp.t, "level": level,
            "z": [row["z"] for row in rows],
            "focus": [round(row["focus"], 4) for row in rows],
            "median": [round(row["median"], 3) for row in rows],
            "p99": [round(row["p99"], 3) for row in rows],
            "background": [round(row["p05"], 3) for row in rows],
            "best_z": best, "attenuation": attenuation}


def _attenuation(rows, z_spacing):
    """An exponential fit of the 99th percentile over background against depth.

    Reported, never applied: a fade with depth is something to know about the
    stack, and correcting it would change the user's data.
    """
    signal = np.array([max(0.0, row["p99"] - row["p05"]) for row in rows], dtype=float)
    if len(signal) < 6 or not np.all(np.isfinite(signal)) or signal.max() <= 0:
        return {"fitted": False, "reason": "fewer than six planes with signal"}
    z = np.arange(len(signal), dtype=float)
    peak = int(np.argmax(signal))
    tail = signal[peak:]
    if len(tail) < 4 or np.any(tail <= 0):
        return {"fitted": False, "reason": "signal does not fall below its peak"}
    slope, _intercept = np.polyfit(z[peak:], np.log(tail), 1)
    if slope >= 0:
        return {"fitted": True, "decay_per_plane": 0.0,
                "note": "no fall-off with depth after the brightest plane"}
    planes = -1.0 / slope
    out = {"fitted": True, "from_z": peak, "decay_length_planes": round(planes, 2),
           "signal_last_over_peak": round(float(signal[-1] / signal[peak]), 3)}
    value = z_spacing.get("value")
    if value and z_spacing.get("source") in ("metadata", "manual"):
        out["decay_length_um"] = round(planes * float(value), 2)
    return out


# -- render_projection ---------------------------------------------------------


def render_projection(call, inp):
    """render_region with a projection as the view, said in so many words."""
    from plexora.agent.core import visual
    from plexora.api.representation import BadView, to_spec

    try:
        kind = to_spec(inp.view).kind if inp.view else None
    except BadView as exc:
        raise AgentError("invalid_input", str(exc)) from None
    if kind != "slab":
        raise AgentError("invalid_input", "render_projection draws a projection: give view "
                         "like 'z4-19max' or 'zmean' (render_region draws single planes)")
    return visual.render_region(call, inp)


def capabilities():
    from plexora.agent.render_spec import RenderInput

    return [
        Capability(
            name="volume.inspect", tool_name="inspect_volume", owner="core",
            purpose="Whether a project's image is a z-stack or time series, and what it is: "
                    "planes, frames, the z step with where it came from, anisotropy, levels, "
                    "the default plane, suggested views and warnings (a missing z step "
                    "blocks physical 3-D measurement). Call before anything 3-D.",
            permission="read", input_model=ProjectInput, handler=inspect_volume,
            egress="metadata", reads=("image",), tags=TAGS),
        Capability(
            name="volume.set_z_spacing", tool_name="set_z_spacing", owner="core",
            purpose="Record the distance between planes of a z-stack whose file states "
                    "none, in microns, as the user states it (the z twin of set_pixel_size).",
            permission="reversible_write", input_model=ZSpacingInput, handler=set_z_spacing,
            writes=("project_config",), tags=TAGS + ("calibration", "spacing")),
        Capability(
            name="volume.sample_z_planes", tool_name="sample_z_planes", owner="core",
            purpose="Choose which planes of a z-stack to look at: the sharpest, the "
                    "brightest, evenly spaced, or the ends -- with a per-plane focus and "
                    "intensity profile behind the choice.",
            permission="read", input_model=SampleZInput, handler=sample_z_planes,
            egress="aggregates", reads=("image",), tags=TAGS),
        Capability(
            name="volume.axis_profile", tool_name="axis_profile", owner="core",
            purpose="Focus and intensity plane by plane through a z-stack (optionally in a "
                    "region): the sharpest plane and how fast signal fades with depth.",
            permission="read", input_model=AxisProfileInput, handler=axis_profile,
            egress="aggregates", reads=("image",), tags=TAGS),
        Capability(
            name="volume.render_projection", tool_name="render_projection", owner="core",
            purpose="Draw a projection of a z-stack's planes (max = MIP, mean, min, std, "
                    "median, percentile) as render_region does a plane. Labelled as a "
                    "projection in the manifest: overlap in it is not colocalization and a "
                    "spot in it may be two cells at different depths.",
            permission="read", input_model=RenderInput, handler=render_projection,
            visual_output=True, egress="rendered_pixels", reads=("image",), tags=TAGS),
    ]
