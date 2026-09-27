"""A project's other layers, drawn into a picture of its reference image.

The agent's `render_region` draws the reference image and its mask; a scene
may hold more -- a second slide registered onto the first, a transcripts
layer, a second label mask. This draws those it can draw exactly and names
the rest, with the reason, rather than drawing something approximately right.

What it draws (a first version, deliberately narrow):

- **image** layers placed by translation and uniform scale (no rotation, shear
  or anisotropic scale): read through `SourceImage`, the reader every agent
  render and Figure Builder export already use, by handing it a stand-in
  project whose image IS the layer -- so local files, remote URLs, data nodes,
  OME-Zarr, DICOM and RGB slides all read the way they read everywhere else.
  Channel layers are added onto the picture (weighted by the layer's opacity);
  a colour layer is laid over it.
- **points** layers (transcripts) as dots, from the tile cache the viewer
  reads, capped at `MAX_LAYER_POINTS`.
- **labels** layers in the reference's own pixel grid, as outlines.

Never through `data_model`: nothing here loads a datasource, which is the
promise every agent render keeps.
"""

from __future__ import annotations

import math

import numpy as np

#: How far a transform may stray from translation + uniform scale and still be
#: drawn: the viewer's own tolerance (layerStack.js TRANSFORM_TOLERANCE).
TOLERANCE = 1e-4

#: Points drawn from one layer into one picture, at most.
MAX_LAYER_POINTS = 200_000

DEFAULT_POINT_COLOR = "#22e6e6"


def _rgb(hex_colour, fallback=(255, 255, 255)):
    try:
        value = str(hex_colour).lstrip("#")
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    except (TypeError, ValueError):
        return fallback


def placement(layer):
    """(scale, dx, dy) mapping layer pixels to reference pixels, or (None, reason)."""
    a, b, c, d, e, f = (float(v) for v in layer.affine)
    if not (math.isfinite(a) and a > 0):
        return None, "its transform is degenerate or mirrored"
    if abs(b) > TOLERANCE * a or abs(c) > TOLERANCE * a:
        return None, "its transform rotates or shears it, which agent renders do not draw yet"
    if abs(d / a - 1) > TOLERANCE:
        return None, "its transform scales x and y differently"
    return (a, e, f), None


def plan(record, layers="visible"):
    """(drawable, skipped): the layers this render will draw, and the ones it
    will not with the reason. `layers` is "visible", "none" or a list of ids."""
    if layers == "none":
        return [], []
    wanted = None if layers == "visible" else set(layers or ())
    drawable, skipped = [], []
    for layer in record.spatial_layers:
        if wanted is None and not layer.visible:
            continue
        if wanted is not None and layer.id not in wanted:
            continue

        def skip(reason):
            skipped.append({"layer": layer.id, "label": layer.label or layer.id,
                            "kind": layer.kind, "reason": reason})

        if layer.status != "ready":
            skip("it is still being built")
            continue
        if layer.kind == "shapes":
            skip("shapes layers are not drawn in agent renders yet")
            continue
        if layer.kind == "image" and not layer.src:
            skip("it has no image source to read")
            continue
        if layer.kind == "points" and (layer.render or {}).get("pointKind") == "bin":
            skip("binned (Visium HD) layers are not drawn in agent renders yet")
            continue
        where, reason = placement(layer)
        if where is None:
            skip(reason)
            continue
        drawable.append((layer, where))
    if wanted is not None:
        known = {layer.id for layer in record.spatial_layers}
        for missing in sorted(wanted - known):
            skipped.append({"layer": missing, "label": missing, "kind": None,
                            "reason": "this project has no layer by that id"})
    return drawable, skipped


# -- image layers ----------------------------------------------------------


def _stand_in(record, layer):
    """A project whose image is this layer, for `SourceImage` to read. Never saved."""
    import dataclasses

    from plexora.server.models.project import (IMAGE_TYPE_BRIGHTFIELD, ImageSpec,
                                               Project)

    rgb = bool((layer.render or {}).get("rgb"))
    image = ImageSpec(src=layer.src, kind=IMAGE_TYPE_BRIGHTFIELD if rgb else "ome_tiff",
                      channels=tuple(layer.channels), width=layer.width,
                      height=layer.height, max_level=layer.max_level,
                      tile_width=layer.tile_width, tile_height=layer.tile_height,
                      num_channels=len(layer.channels), pyramid=layer.pyramid,
                      pyramid_key=getattr(layer, "pyramid_key", None))
    resources = {}
    if layer.binding is not None and layer.binding.is_node:
        resources["image"] = dataclasses.replace(layer.binding, kind="image")
    return Project(name=f"{record.name}#layer:{layer.id}", image=image,
                   resources=resources)


def _layer_channels(layer, source):
    """[(key, colour, window or None)] the layer's own panel would draw."""
    from plexora.server.utils import source_image

    render = dict(layer.render or {})
    listed = list(layer.channels)
    rows = render.get("channels") if isinstance(render.get("channels"), list) else None
    if not rows:
        rows = [{"index": render.get("channelIndex", layer.channel_index or 0),
                 "color": render.get("color") or "#ffffff", "range": render.get("range")}]
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        index = row.get("index")
        found = listed[index] if isinstance(index, int) and 0 <= index < len(listed) else None
        if found is None and row.get("name"):
            found = next((c for c in listed if c.get("name") == row["name"]), None)
        if found is None:
            continue
        window = row.get("range")
        if not (isinstance(window, (list, tuple)) and len(window) == 2
                and float(window[0]) < float(window[1])):
            window = None
        out.append((source_image.channel_key(found), row.get("color") or "#ffffff", window))
    return out


def _draw_image_layer(record, layer, where, rgb, box, out_size):
    from PIL import Image

    from plexora.api.dataset import _project_data_for
    from plexora.server.utils import source_image

    scale, dx, dy = where
    out_w, out_h = out_size
    # The reference box in the layer's own pixels.
    lx0, ly0 = (box[0] - dx) / scale, (box[1] - dy) / scale
    lx1, ly1 = (box[2] - dx) / scale, (box[3] - dy) / scale
    lw, lh = layer.width or 0, layer.height or 0
    # Where the layer covers the box, in layer pixels, clipped to the layer.
    cx0, cy0 = max(0.0, lx0), max(0.0, ly0)
    cx1, cy1 = min(float(lw), lx1), min(float(lh), ly1)
    if lw and lh and (cx1 <= cx0 or cy1 <= cy0):
        return None, "it does not reach this region"
    if not lw or not lh:
        cx0, cy0, cx1, cy1 = lx0, ly0, lx1, ly1
    source = source_image.SourceImage(_project_data_for(_stand_in(record, layer)))
    # The output pixels the covered part lands on.
    px0 = int(math.floor((cx0 - lx0) / (lx1 - lx0) * out_w))
    py0 = int(math.floor((cy0 - ly0) / (ly1 - ly0) * out_h))
    px1 = int(math.ceil((cx1 - lx0) / (lx1 - lx0) * out_w))
    py1 = int(math.ceil((cy1 - ly0) / (ly1 - ly0) * out_h))
    tw, th = max(1, px1 - px0), max(1, py1 - py0)
    level = source_image.choose_level(source, cx1 - cx0, tw)
    level = min(level, max(0, int(source.levels) - 1))
    div = 2 ** level
    lbox = (int(math.floor(cx0 / div)), int(math.floor(cy0 / div)),
            max(int(math.floor(cx0 / div)) + 1, int(math.ceil(cx1 / div))),
            max(int(math.floor(cy0 / div)) + 1, int(math.ceil(cy1 / div))))
    opacity = (layer.render or {}).get("opacity")
    opacity = 1.0 if opacity is None else max(0.0, min(1.0, float(opacity)))
    channels, windows = [], []
    if not source.is_brightfield:
        for key, colour, window in _layer_channels(layer, source):
            if window is None:
                stats = source_image.channel_stats(source, key)
                window = [stats["p01"], stats["p999"]]
                if not window[0] < window[1]:
                    window = [stats["min"], max(stats["min"] + 1.0, stats["max"])]
                how = f"auto:p01-p999@level{stats['level']}"
            else:
                how = "layer"
            channels.append({"key": key, "window": [float(window[0]), float(window[1])],
                             "visible": True, "color": dict(zip("rgb", _rgb(colour)))})
            windows.append({"key": key, "color": colour,
                            "window": [float(window[0]), float(window[1])],
                            "window_source": how})
        if not channels:
            return None, "none of its stored channels exist in its image"
    pixels, _rendered, _background = source_image.composite(source, channels, level, lbox)
    patch = Image.fromarray(np.ascontiguousarray(pixels), "RGB").resize((tw, th),
                                                                        Image.LANCZOS)
    patch = np.asarray(patch).astype(np.float32)
    # Paste into the output, clipped to it.
    ox0, oy0 = max(0, px0), max(0, py0)
    ox1, oy1 = min(out_w, px0 + tw), min(out_h, py0 + th)
    if ox1 <= ox0 or oy1 <= oy0:
        return None, "it does not reach this region"
    piece = patch[oy0 - py0:oy1 - py0, ox0 - px0:ox1 - px0]
    region = rgb[oy0:oy1, ox0:ox1].astype(np.float32)
    if source.is_brightfield:
        blended = region * (1 - opacity) + piece * opacity
        blend = "over"
    else:
        blended = region + piece * opacity
        blend = "add"
    rgb[oy0:oy1, ox0:ox1] = np.clip(blended, 0, 255).astype(np.uint8)
    return {"layer": layer.id, "label": layer.label or layer.id, "kind": "image",
            "transform": list(layer.affine), "level": level, "opacity": opacity,
            "blend": blend, "channels": windows,
            "covered_output_px": [ox0, oy0, ox1 - ox0, oy1 - oy0]}, None


# -- points layers ---------------------------------------------------------


def _draw_points_layer(record, layer, where, rgb, box, out_size, max_points):
    from PIL import Image, ImageDraw

    from plexora.server.models import transcript_tiles

    scale, dx, dy = where
    manifest = transcript_tiles.read_manifest(record.name, layer.id)
    if not manifest:
        return None, "its point tiles have not been built yet"
    render = dict(layer.render or {})
    genes = render.get("genes") if isinstance(render.get("genes"), list) else None
    colours = render.get("colors") if isinstance(render.get("colors"), list) else None
    bounds = {"minX": (box[0] - dx) / scale, "minY": (box[1] - dy) / scale,
              "maxX": (box[2] - dx) / scale, "maxY": (box[3] - dy) / scale}
    points = transcript_tiles.read_region(record.name, layer.id, bounds, genes=genes,
                                          max_points=None, min_q=render.get("minq"))
    xs = points["x"].astype(np.float64) * scale + dx
    ys = points["y"].astype(np.float64) * scale + dy
    inside = (xs >= box[0]) & (xs < box[2]) & (ys >= box[1]) & (ys < box[3])
    points, xs, ys = points[inside], xs[inside], ys[inside]
    total = int(len(points))
    truncated = total > max_points
    if truncated:
        step = int(math.ceil(total / max_points))
        points, xs, ys = points[::step][:max_points], xs[::step][:max_points], \
            ys[::step][:max_points]
    out_w, out_h = out_size
    sx, sy = out_w / (box[2] - box[0]), out_h / (box[3] - box[1])
    colour_of = {}
    if genes and colours:
        names = manifest.get("genes") or []
        for name, colour in zip(genes, colours):
            if name in names:
                colour_of[names.index(name)] = _rgb(colour)
    default = _rgb(render.get("color") or DEFAULT_POINT_COLOR)
    image = Image.fromarray(rgb, "RGB")
    draw = ImageDraw.Draw(image)
    radius = 1 if len(points) > 20_000 else 2
    for gene, x, y in zip(points["gene"].tolist(), xs, ys):
        px, py = (x - box[0]) * sx, (y - box[1]) * sy
        draw.ellipse((px - radius, py - radius, px + radius, py + radius),
                     fill=colour_of.get(gene, default))
    rgb[...] = np.asarray(image)
    return {"layer": layer.id, "label": layer.label or layer.id, "kind": "points",
            "transform": list(layer.affine), "points_in_region": total,
            "points_drawn": int(len(points)), "truncated": truncated,
            "genes": genes, "min_q": render.get("minq")}, None


# -- label layers ----------------------------------------------------------


def _draw_labels_layer(record, layer, where, rgb, box, level, out_size):
    from plexora.server.providers.local import LocalSegmentationProvider
    from plexora.server.utils.label_overlay import paint_labels, resize_labels_nearest

    scale, dx, dy = where
    if abs(scale - 1) > TOLERANCE or abs(dx) > TOLERANCE or abs(dy) > TOLERANCE:
        return None, "a label layer is drawn only in the reference's own pixel grid"
    if (layer.width, layer.height) != (record.image.width, record.image.height):
        return None, "its size is not the reference image's"
    if layer.binding is not None and layer.binding.is_node:
        from plexora.server.providers.node import NodeSegmentationProvider

        provider = NodeSegmentationProvider(layer.binding)
    else:
        provider = LocalSegmentationProvider(layer.src)
    div = 2 ** level
    lbox = (int(math.floor(box[0] / div)), int(math.floor(box[1] / div)),
            int(math.ceil(box[2] / div)), int(math.ceil(box[3] / div)))
    labels = provider.read_region(level, lbox)
    labels = resize_labels_nearest(labels, *out_size)
    colour = (layer.render or {}).get("color") or "#ffd60a"
    paint_labels(rgb, labels, mode="outlines", colour_for=lambda _label: colour, alpha=1.0,
                 thickness=1 if max(out_size) < 600 else 2)
    return {"layer": layer.id, "label": layer.label or layer.id, "kind": "labels",
            "transform": list(layer.affine), "level": level, "mode": "outlines",
            "color": colour}, None


def composite(record, rgb, box, level, out_size, *, layers="visible",
              max_points=MAX_LAYER_POINTS):
    """Draw `record`'s layers into `rgb` (H, W, 3 uint8, in place).

    `box` is the reference full-resolution rectangle the picture shows, and
    `level` the reference pyramid level it was read at. Returns
    (layers_rendered, not_rendered), in stacking order.
    """
    drawable, skipped = plan(record, layers)
    rendered = []
    for layer, where in drawable:
        try:
            if layer.kind == "image":
                entry, reason = _draw_image_layer(record, layer, where, rgb, box, out_size)
            elif layer.kind == "points":
                entry, reason = _draw_points_layer(record, layer, where, rgb, box, out_size,
                                                   max_points)
            elif layer.kind == "labels":
                entry, reason = _draw_labels_layer(record, layer, where, rgb, box, level,
                                                   out_size)
            else:
                entry, reason = None, f"{layer.kind} layers are not drawn in agent renders"
        except Exception as exc:
            entry, reason = None, f"it could not be read ({exc})"
        if entry is None:
            skipped.append({"layer": layer.id, "label": layer.label or layer.id,
                            "kind": layer.kind, "reason": reason})
        else:
            rendered.append(entry)
    return rendered, skipped
