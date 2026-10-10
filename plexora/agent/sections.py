"""Orthogonal sections as one picture: XY, and XZ and YZ through the same point.

The picture a person uses to judge an object in depth -- is it one cell or two
stacked, is it cut off by the top of the stack, does a stain sit on top of a
nucleus or inside it -- and the one a projection cannot answer. Three panes
through one point `(x, y, z)`:

    +--------+----+
    |   XY   | YZ |      XY: plane z, the field around (x, y)
    |  (z)   |    |      YZ: the column x, every plane; y runs down beside XY
    +--------+----+      XZ: the row y, every plane; x runs across under XY
    |   XZ   |
    +--------+

drawn at ONE scale and with ONE window per channel, so a bright spot is
equally bright in every pane. A section's rows are planes drawn at the stack's
z step (an assumed step when the file states none, which the manifest says).
Dashed lines mark where each pane cuts the others.

Reads through `SourceImage(view=)`, never the viewer's loaded project.
"""

from __future__ import annotations

import math

import numpy as np

from plexora.agent.errors import AgentError

GAP = 4
LABEL_BG = (5, 7, 10)


def _resample(rgb: np.ndarray, width: int, height: int) -> np.ndarray:
    from PIL import Image

    width, height = max(1, int(round(width))), max(1, int(round(height)))
    if rgb.shape[1] == width and rgb.shape[0] == height:
        return rgb
    return np.asarray(Image.fromarray(np.ascontiguousarray(rgb), "RGB")
                      .resize((width, height), Image.NEAREST))


def _label_crop(labels, level, z0, z1, rows, cols):
    """`(Z, h, w)` of a label volume at `level`, zero outside it.

    `rows`/`cols` are (start, stop) in that level's pixels and may run past
    the volume's edge, as a field centred near the edge does.
    """
    wanted = max(0, int(level))
    level = min(wanted, labels.levels - 1)
    # A mask with fewer levels than the image: read the coarsest it has, over
    # the same ground (the caller resamples to the pane anyway).
    k = 2 ** (wanted - level)
    rows, cols = (rows[0] * k, rows[1] * k), (cols[0] * k, cols[1] * k)
    height, width = labels.level_shapes()[level]
    out = np.zeros((z1 - z0 + 1, rows[1] - rows[0], cols[1] - cols[0]), dtype=np.uint32)
    r0, r1 = max(0, rows[0]), min(height, rows[1])
    c0, c1 = max(0, cols[0]), min(width, cols[1])
    if r1 > r0 and c1 > c0:
        out[:, r0 - rows[0]:r1 - rows[0], c0 - cols[0]:c1 - cols[0]] = labels.read(
            level, z0, z1, slice(r0, r1), slice(c0, c1))
    return out


def _nearest(array, height, width):
    """`array` (2-D) resampled to (height, width) by nearest neighbour."""
    height, width = max(1, int(round(height))), max(1, int(round(width)))
    rows = np.minimum((np.arange(height) * array.shape[0] / height).astype(int),
                      array.shape[0] - 1)
    cols = np.minimum((np.arange(width) * array.shape[1] / width).astype(int),
                      array.shape[1] - 1)
    return array[rows][:, cols]


#: Outline colours: every cell, and the one asked about.
OUTLINE = (235, 238, 245)
HIGHLIGHT = (255, 214, 10)


def _outline(rgb, labels, highlight=None):
    """`rgb` with the boundaries of `labels` (same shape) drawn on it."""
    from skimage.segmentation import find_boundaries

    edges = find_boundaries(labels, mode="inner") & (labels > 0)
    out = rgb.copy()
    out[edges] = (0.45 * out[edges] + 0.55 * np.asarray(OUTLINE)).astype(np.uint8)
    if highlight is not None:
        mine = edges & (labels == int(highlight))
        out[mine] = HIGHLIGHT
    return out


def render_sections(session, inp, *, store=True):
    from PIL import Image, ImageDraw

    from plexora.agent import artifacts
    from plexora.agent.render import _font, _rgb, resolve_channel
    from plexora.server.utils import source_image

    record = session.project(inp.project)
    data = session.image_data(inp.project)
    volume = data.image.volume()
    if volume is None or int(volume["depth"]) < 2:
        raise AgentError("invalid_input", f"{inp.project}'s image has a single plane per "
                         "frame, so it has no sections")
    depth, width, height = int(volume["depth"]), int(volume["width"]), int(volume["height"])
    frames = int(volume["frames"])
    t = int(inp.t or 0)
    if t >= frames:
        raise AgentError("invalid_input", f"t {t} is past the last frame")
    cx = int(inp.x if inp.x is not None else width // 2)
    cy = int(inp.y if inp.y is not None else height // 2)
    cz = int(inp.z if inp.z is not None else depth // 2)
    if not (0 <= cx < width and 0 <= cy < height and 0 <= cz < depth):
        raise AgentError("invalid_input", "the point is outside the image",
                         detail={"width": width, "height": height, "depth": depth})
    size = int(inp.size_px)
    out = int(inp.output_px)
    level = max(0, min(len(volume["levels"]) - 1, int(math.floor(math.log2(max(1, size / out))))))
    scale = 2 ** level
    half = size // 2
    x0, y0 = cx - half, cy - half
    lbox = (x0 // scale, y0 // scale, (x0 + size) // scale, (y0 + size) // scale)
    prefix = f"t{t}" if t else ""
    channel_records = list(record.image.real_channels)
    specs = inp.channels or [_default_channel(channel_records, i) for i in
                             range(min(3, len(channel_records)))]

    with source_image.SourceImage(data, view=f"{prefix}z{cz}") as xy_source:
        resolved = []
        for spec in specs:
            index, found = resolve_channel(spec.name, channel_records)
            key = source_image.channel_key(found)
            if isinstance(spec.window, list):
                window, how = [float(spec.window[0]), float(spec.window[1])], "explicit"
            else:
                stats = source_image.channel_stats(xy_source, key)
                window = [stats["p01"], stats["p999"]]
                if not window[0] < window[1]:
                    window = [stats["min"], max(stats["min"] + 1.0, stats["max"])]
                how = f"auto:p01-p999@level{stats['level']}"
            resolved.append({"name": found.get("fullname") or found.get("name"), "key": key,
                             "color": spec.color, "window": window, "window_source": how})
        scene = [{"key": c["key"], "window": c["window"], "visible": True,
                  "color": dict(zip("rgb", _rgb(c["color"])))} for c in resolved]
        xy, _rendered, _bg = source_image.composite(xy_source, scene, level, lbox)

    rows_l = None
    with source_image.SourceImage(data, view=f"{prefix}xzy{cy}") as xz_source:
        rows_l = xz_source.level_shape(level)[0]
        xz, _r, _b = source_image.composite(
            xz_source, scene, level, (lbox[0], 0, lbox[2], rows_l))
    with source_image.SourceImage(data, view=f"{prefix}yzx{cx}") as yz_source:
        yz_rows = yz_source.level_shape(level)[0]
        yz, _r, _b = source_image.composite(
            yz_source, scene, level, (lbox[1], 0, lbox[3], yz_rows))
    yz = np.ascontiguousarray(np.transpose(yz, (1, 0, 2)))  # y down, z across

    factor = out / max(1, lbox[2] - lbox[0])                  # output px per level px
    xy_img = _resample(xy, out, out)
    xz_img = _resample(xz, out, rows_l * factor)
    yz_img = _resample(yz, yz_rows * factor, out)

    # A 3-D mask's outlines in every pane, cut at the same point: the way to
    # see whether a cell is one cell in depth, and where it ends.
    outlined = None
    highlight = getattr(inp, "highlight", None)
    if getattr(inp, "segmentation", "none") == "outlines":
        from plexora.server.volume import labels as volume_labels

        seg = record.segmentation
        if not (seg.available and seg.derived and volume_labels.is_label_volume(seg.derived)):
            raise AgentError("precondition_missing", "outlines in sections need a 3-D label "
                             "volume; this project's mask is flat or absent")
        labels = volume_labels.open_label_volume(seg.derived)
        rows_y = (lbox[1], lbox[3])
        cols_x = (lbox[0], lbox[2])
        plane = _label_crop(labels, level, cz, cz, rows_y, cols_x)[0]
        xz_labels = _label_crop(labels, level, 0, depth - 1,
                                (cy // scale, cy // scale + 1), cols_x)[:, 0, :]
        yz_labels = _label_crop(labels, level, 0, depth - 1, rows_y,
                                (cx // scale, cx // scale + 1))[:, :, 0]
        xy_img = _outline(xy_img, _nearest(plane, *xy_img.shape[:2]), highlight)
        xz_img = _outline(xz_img, _nearest(xz_labels, *xz_img.shape[:2]), highlight)
        yz_img = _outline(yz_img, _nearest(yz_labels.T, *yz_img.shape[:2]), highlight)
        outlined = {"labels": str(seg.derived), "highlight": highlight}
    canvas_w = out + GAP + yz_img.shape[1]
    canvas_h = out + GAP + xz_img.shape[0]
    canvas = np.zeros((canvas_h, canvas_w, 3), dtype=np.uint8)
    canvas[:out, :out] = xy_img
    canvas[:out, out + GAP:out + GAP + yz_img.shape[1]] = yz_img
    canvas[out + GAP:out + GAP + xz_img.shape[0], :out] = xz_img

    image = Image.fromarray(canvas, "RGB")
    draw = ImageDraw.Draw(image)
    rows_per_plane = rows_l / depth
    z_px = (cz + 0.5) * rows_per_plane * factor
    cross_x = (cx // scale - lbox[0]) * factor
    cross_y = (cy // scale - lbox[1]) * factor
    if inp.crosshair:
        dash = (255, 255, 255)
        _dashed(draw, (cross_x, 0), (cross_x, out), dash)                       # XY |
        _dashed(draw, (0, cross_y), (out, cross_y), dash)                       # XY -
        _dashed(draw, (cross_x, out + GAP), (cross_x, canvas_h), dash)          # XZ |
        _dashed(draw, (0, out + GAP + z_px), (out, out + GAP + z_px), dash)     # XZ z
        _dashed(draw, (out + GAP, cross_y), (canvas_w, cross_y), dash)          # YZ -
        _dashed(draw, (out + GAP + z_px, 0), (out + GAP + z_px, out), dash)     # YZ z
    font = _font(12)
    spacing = volume.get("spacing") or {}
    z_known = (spacing.get("z") or {}).get("source") in ("metadata", "manual")
    labels = [((4, 4), f"XY · z {cz}/{depth}"),
              ((4, out + GAP + 2), f"XZ · y {cy:,}"),
              ((out + GAP + 2, 4), f"YZ · x {cx:,}")]
    for (lx, ly), text in labels:
        box = draw.textbbox((lx, ly), text, font=font)
        draw.rectangle((box[0] - 2, box[1] - 1, box[2] + 2, box[3] + 1), fill=LABEL_BG)
        draw.text((lx, ly), text, fill=(232, 236, 242), font=font)

    from plexora.server.utils import fast_png

    png = fast_png.encode_rgb8_png(np.asarray(image))
    warnings = []
    if not z_known:
        warnings.append("This image states no z step: sections are drawn one pixel per "
                        "plane (assumed), so depths in them are not to scale.")
    manifest = {
        "kind": "plexora.sections",
        "project": record.name,
        "center": {"x": cx, "y": cy, "z": cz, "t": t},
        "level": level,
        "output_px_per_level_px": factor,
        "panes": {
            "xy": {"view": f"{prefix}z{cz}", "bounds_fullres": [x0, y0, x0 + size, y0 + size],
                   "pixels": [0, 0, out, out]},
            "xz": {"view": f"{prefix}xzy{cy}", "x_fullres": [x0, x0 + size], "z": [0, depth - 1],
                   "pixels": [0, out + GAP, out, out + GAP + xz_img.shape[0]],
                   "rows_per_plane": rows_per_plane * factor},
            "yz": {"view": f"{prefix}yzx{cx}", "y_fullres": [y0, y0 + size], "z": [0, depth - 1],
                   "pixels": [out + GAP, 0, out + GAP + yz_img.shape[1], out],
                   "columns_per_plane": rows_per_plane * factor},
        },
        "channels": resolved,
        **({"outlines": outlined} if outlined else {}),
        "spacing": spacing,
        "anisotropy": volume.get("anisotropy"),
        "z_label": f"sections through x {cx}, y {cy}, z {cz}",
        "warnings": warnings,
        "egress": "rendered_pixels",
    }
    artifact = artifacts.put(record.name, png, manifest) if store else None
    return {"png": png, "manifest": manifest, "artifact": artifact}


def _default_channel(channels, index):
    from plexora.agent.render_spec import ChannelSpec

    colours = ("#2388ff", "#ffd60a", "#ec4899")
    channel = channels[index]
    return ChannelSpec(name=channel.get("fullname") or channel.get("name"),
                       color=colours[index % len(colours)], window="auto")


def _dashed(draw, start, end, colour, dash=5, gap=4):
    (x0, y0), (x1, y1) = start, end
    length = math.hypot(x1 - x0, y1 - y0)
    if length <= 0:
        return
    dx, dy = (x1 - x0) / length, (y1 - y0) / length
    position = 0.0
    while position < length:
        stop = min(length, position + dash)
        draw.line((x0 + dx * position, y0 + dy * position, x0 + dx * stop, y0 + dy * stop),
                  fill=colour, width=1)
        position = stop + gap
