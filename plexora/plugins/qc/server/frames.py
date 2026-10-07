"""Where a picture's pixels lie on the image: one frame, or one per tile.

An agent points at what it was shown (`{artifact_id, px}`), never at
coordinates it worked out. A `render_region` picture has one frame -- its
manifest's `bounds_fullres` and `output_size`; a sheet of several tiles (the
visual overview, a channel contact sheet, a two-panel preview) has one per
tile, listed in its manifest's `frames`: where the tile sits on the sheet
(`origin`, `size`, sheet pixels) and what it shows (`bounds_fullres`). A
pixel is mapped through the tile under it; one in a gutter, a caption or the
title bar is refused in words, naming the nearest tile.
"""

from __future__ import annotations

from plexora.agent.errors import AgentError


def _load(project, artifact_id):
    from plexora.agent import artifacts

    try:
        _png, sidecar = artifacts.get(artifact_id)
    except KeyError:
        raise AgentError("invalid_input", f"no artifact {artifact_id!r}") from None
    manifest = sidecar.get("manifest") or {}
    owner = sidecar.get("project")
    if owner not in (None, project) or manifest.get("project") not in (None, project):
        raise AgentError("invalid_input", f"{artifact_id} is a picture of another project")
    return manifest


def frames_of(manifest) -> list:
    """Every frame of a manifest: `frames` as listed, else the one frame of a
    single render ({origin: [0, 0], size: output_size, bounds_fullres}), else
    an empty list (a picture that does not say where its pixels are)."""
    listed = manifest.get("frames")
    if isinstance(listed, list) and listed:
        return [f for f in listed if f.get("bounds_fullres") and f.get("size")]
    bounds = manifest.get("bounds_fullres")
    size = manifest.get("output_size")
    if bounds and size:
        return [{"slot": 0, "origin": [0, 0], "size": [float(size[0]), float(size[1])],
                 "bounds_fullres": bounds}]
    return []


def _inside(frame, px):
    ox, oy = frame.get("origin") or (0, 0)
    w, h = frame["size"]
    return ox <= px[0] <= ox + w and oy <= px[1] <= oy + h


def _distance(frame, px):
    ox, oy = frame.get("origin") or (0, 0)
    w, h = frame["size"]
    dx = max(ox - px[0], 0.0, px[0] - (ox + w))
    dy = max(oy - px[1], 0.0, px[1] - (oy + h))
    return (dx * dx + dy * dy) ** 0.5


def frame_at(manifest, px, *, artifact_id="the picture") -> dict:
    """The frame under `px` (sheet pixels): {origin, size, bounds_fullres,
    slot, panel?}. `invalid_input` when the picture has no frame, or the
    pixel is between tiles."""
    frames = frames_of(manifest)
    if not frames:
        raise AgentError("invalid_input", f"{artifact_id} does not say where its pixels are "
                         "on the image; point on a render_region picture, the visual "
                         "overview, a channel sheet or a preview, or give image pixels",
                         detail={"kind": manifest.get("kind")})
    for frame in frames:
        if _inside(frame, px):
            return frame
    nearest = min(frames, key=lambda f: _distance(f, px))
    label = nearest.get("panel") or nearest.get("caption") or f"tile {nearest.get('slot')}"
    raise AgentError("invalid_input", f"[{px[0]:.0f}, {px[1]:.0f}] is not on a tile of "
                     f"{artifact_id} (a gutter or caption); the nearest tile is {label}",
                     detail={"nearest": {k: nearest.get(k) for k in ("slot", "panel", "origin",
                                                                      "size")}})


def to_image(frame, px) -> tuple:
    """A sheet pixel inside `frame` as full-resolution image pixels (x, y)."""
    ox, oy = frame.get("origin") or (0, 0)
    w, h = frame["size"]
    b = frame["bounds_fullres"]
    sx = float(b["width"]) / max(float(w), 1e-9)
    sy = float(b["height"]) / max(float(h), 1e-9)
    return float(b["x"]) + (px[0] - ox) * sx, float(b["y"]) + (px[1] - oy) * sy


def scale_of(frame) -> float:
    """Full-resolution image pixels per sheet pixel on `frame`."""
    w, h = frame["size"]
    b = frame["bounds_fullres"]
    return max(float(b["width"]) / max(float(w), 1e-9),
               float(b["height"]) / max(float(h), 1e-9))


def locate_point(project, artifact_id, px) -> tuple:
    """(x, y) image pixels of a point on a picture the agent was shown, and
    the manifest it was read from."""
    point, manifest, _scale = locate_point_scaled(project, artifact_id, px)
    return point, manifest


def locate_point_scaled(project, artifact_id, px) -> tuple:
    """`locate_point`, and the scale of the tile it fell on (image pixels per
    sheet pixel): a click on a whole-slide picture means a larger place than
    the same click on a close crop."""
    manifest = _load(project, artifact_id)
    frame = frame_at(manifest, px, artifact_id=artifact_id)
    return to_image(frame, px), manifest, scale_of(frame)


def locate_box(project, artifact_id, px4) -> tuple:
    """{x, y, width, height} image pixels of a box drawn on one tile of a
    picture (both corners on the same tile), and the manifest."""
    manifest = _load(project, artifact_id)
    x0, y0, x1, y1 = px4
    first = frame_at(manifest, (x0, y0), artifact_id=artifact_id)
    second = frame_at(manifest, (x1, y1), artifact_id=artifact_id)
    if first is not second:
        raise AgentError("invalid_input", f"the box's corners fall on two tiles of "
                         f"{artifact_id}; draw it inside one tile",
                         detail={"corners": [first.get("panel") or first.get("slot"),
                                             second.get("panel") or second.get("slot")]})
    ax, ay = to_image(first, (min(x0, x1), min(y0, y1)))
    bx, by = to_image(first, (max(x0, x1), max(y0, y1)))
    return {"x": ax, "y": ay, "width": max(bx - ax, 1e-6), "height": max(by - ay, 1e-6)}, manifest


def picture_channels(manifest) -> list | None:
    """The channels a single render drew, as segment specs, or None (a sheet
    composes its own panels and names none)."""
    listed = manifest.get("channels")
    if not isinstance(listed, list):
        return None
    channels = [{"name": c.get("name"), "color": c.get("color"), "range": c.get("window")}
                for c in listed if isinstance(c, dict) and c.get("name")]
    return channels or None
