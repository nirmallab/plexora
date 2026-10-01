"""Rendering a region of a project the way the viewer would draw it.

The agent's eyes. `render_region` reads the pyramid directly (core's
`source_image`, the same reader Figure Builder exports with), composites the
channels with the viewer's own arithmetic, draws the segmentation mask and a
gate's positive cells over it with the viewer's own outline rule
(`label_overlay`), and hands back a PNG plus a **manifest**: every number the
picture depends on, resolved -- the region in full-resolution pixels, the
level read, each channel's window and where the window came from, what was
drawn and what could not be. Same spec, same bytes, same manifest; there is no
randomness and no clock in either.

Nothing here touches `data_model` or the viewer's state. An agent can look at
a project nobody has open, at a scale nobody is looking at.
"""

from __future__ import annotations

import math
import threading

import numpy as np

from plexora.agent import gate_rule
from plexora.agent.errors import AgentError
from plexora.agent.limits import (DEFAULT_OUTPUT_SIDE, MAX_OUTPUT_PIXELS, MAX_OUTPUT_SIDE,
                                  MAX_LAYER_POINTS, MAX_SOURCE_PIXELS_AGENT)
from plexora.agent.render_spec import IdHighlight, MarkerHighlight
from plexora.agent.schemas import SCHEMA_VERSION

MANIFEST_KIND = "plexora.render_region"


# -- the mask ------------------------------------------------------------


class _MaskShelf:
    """A few label-mask providers, keyed by project and checked by where the
    mask is -- so a local mask stays open between renders, and a node's is one
    object that knows its address."""

    def __init__(self, limit=4):
        self.limit = limit
        self._held = {}
        self._order = []
        self._lock = threading.Lock()

    def get(self, name, where, make):
        with self._lock:
            held = self._held.get(name)
            if held is not None and held[0] == where:
                return held[1]
        provider = make()
        with self._lock:
            self._held[name] = (where, provider)
            if name in self._order:
                self._order.remove(name)
            self._order.append(name)
            while len(self._order) > self.limit:
                self._held.pop(self._order.pop(0), None)
        return provider

    def close(self):
        with self._lock:
            self._held.clear()
            self._order.clear()


_MASKS = _MaskShelf()


def close_masks():
    _MASKS.close()


def _mask_for(record):
    """(provider or None, status, reason, locator).

    The provider answers `read_region(level, box, max_pixels)` whether the
    mask is a local file or on a data node. A local one is opened here, so a
    mask that cannot be read says so now; a node's is asked once whether it
    can be reached, and one that cannot falls back to centroid marks.
    """
    from plexora import api

    seg = record.segmentation
    if not seg.available:
        reason = "the mask is still being prepared" if seg.pending else "no segmentation mask"
        return None, "none", reason, None
    handle = api.SegHandle(record)
    locator = handle.locator
    binding = record.resources.get("segmentation")
    where = (("node", binding.node, binding.resource_id) if binding is not None
             else ("local", seg.derived))
    try:
        provider = _MASKS.get(record.name, where, handle.provider)
        # One pixel: cheap, and a mask that cannot be read says so here, not
        # halfway through a render.
        provider.read_region(0, (0, 0, 1, 1))
    except Exception as exc:
        _MASKS.close()
        where_text = "on its data node" if binding is not None else "on this machine"
        return None, "unavailable", (f"the mask {where_text} could not be read ({exc}); "
                                     "cells are marked at their centroids instead"), locator
    return provider, "rendered", None, locator


# -- resolving the spec --------------------------------------------------


def resolve_channel(name, channels):
    """(index, record) for a channel named by fullname, name or key; an
    `invalid_input` naming the real channels otherwise. Never a substitute."""
    from plexora.server.utils.source_image import channel_key

    for field in ("fullname", "name"):
        for index, channel in enumerate(channels):
            if channel.get(field) == name:
                return index, channel
    for index, channel in enumerate(channels):
        if channel_key(channel) == name:
            return index, channel
    folded = str(name).casefold()
    for index, channel in enumerate(channels):
        if str(channel.get("fullname") or channel.get("name") or "").casefold() == folded:
            return index, channel
    raise AgentError("invalid_input", f"{name!r} is not a channel of this image",
                     detail={"channels": [c.get("fullname") or c.get("name")
                                          for c in channels]})


def roi_features(project_name) -> list:
    """Every stored region of a project as (feature, category label), read from
    the ROI plugin's store directly (JSON) so core never imports the plugin."""
    import json

    from plexora import api

    blob = api.store(project_name, "roi").get_state()
    if not blob:
        return []
    state = json.loads(blob.decode("utf-8"))
    labels = {c.get("id"): c.get("label") or "" for c in state.get("categories") or []}
    out = []
    for entry in (state.get("images") or {}).values():
        for feature in entry.get("features") or []:
            if feature.get("geometry"):
                out.append((feature, labels.get(feature.get("category_id"), "")))
    return out


def rois_containing(project_name, x, y) -> list:
    """[{roi_id, name, category}] of the regions whose polygon contains (x, y)
    in full-resolution pixels -- the polygon, holes outside, not its box. The
    ROI service's own predicate (shapely `contains_xy`)."""
    import shapely
    from shapely.geometry import shape

    found = []
    for feature, category in roi_features(project_name):
        try:
            polygon = shape(feature["geometry"])
        except Exception:
            continue
        if bool(shapely.contains_xy(polygon, float(x), float(y))):
            found.append({"roi_id": feature.get("id"), "name": feature.get("name") or "",
                          "category": category})
    return found


def _roi_box(project_name, roi_id):
    """The bounding box of a stored region, and the region."""
    features = roi_features(project_name)
    if not features:
        raise AgentError("invalid_input", f"{project_name!r} has no regions")
    for feature, _category in features:
        if feature.get("id") != roi_id:
            continue
        xs, ys = [], []
        coords = (feature.get("geometry") or {}).get("coordinates") or []
        rings = ([ring for polygon in coords for ring in polygon]
                 if feature["geometry"].get("type") == "MultiPolygon" else coords)
        for ring in rings:
            for x, y in ring:
                xs.append(float(x))
                ys.append(float(y))
        if xs:
            return (min(xs), min(ys), max(xs), max(ys)), feature
    raise AgentError("invalid_input", f"no region {roi_id!r} in {project_name!r}",
                     detail={"hint": "call list_rois"})


def resolve_bounds(spec, record, pixel):
    """(x0, y0, x1, y1) in full-resolution pixels, and how it was chosen."""
    width, height = record.image.width or 0, record.image.height or 0
    if spec.bounds is not None:
        b = spec.bounds
        return (b.x, b.y, b.x + b.width, b.y + b.height), {"from": "bounds"}
    if spec.center is not None:
        c = spec.center
        if c.size_um is not None:
            if not pixel:
                raise AgentError(
                    "precondition_missing",
                    "size_um needs a calibrated image, and this one states no pixel size",
                    detail={"missing": [{"key": "pixel_size", "label": "Pixel size"}],
                            "hint": "use size_px, or set the pixel size in Plexora"})
            side = c.size_um / pixel["value"]
        else:
            side = c.size_px
        w = side * math.sqrt(c.aspect)
        h = side / math.sqrt(c.aspect)
        return ((c.center_x - w / 2, c.center_y - h / 2, c.center_x + w / 2,
                 c.center_y + h / 2), {"from": "center", "size_um": c.size_um,
                                        "size_px": side})
    if spec.roi is not None:
        (x0, y0, x1, y1), feature = _roi_box(record.name, spec.roi.roi_id)
        pad = spec.roi.padding * max(x1 - x0, y1 - y0, 1.0)
        return ((x0 - pad, y0 - pad, x1 + pad, y1 + pad),
                {"from": "roi", "roi_id": spec.roi.roi_id, "roi_name": feature.get("name")})
    return (0.0, 0.0, float(width), float(height)), {"from": "whole_image"}


def output_size(box, output):
    bw, bh = max(1e-6, box[2] - box[0]), max(1e-6, box[3] - box[1])
    aspect = bw / bh
    width = output.width if output else None
    height = output.height if output else None
    if width is None and height is None:
        if aspect >= 1:
            width = DEFAULT_OUTPUT_SIDE
        else:
            height = DEFAULT_OUTPUT_SIDE
    if width is None:
        width = height * aspect
    if height is None:
        height = width / aspect
    width, height = int(round(width)), int(round(height))
    scale = min(1.0, MAX_OUTPUT_SIDE / max(width, height),
                math.sqrt(MAX_OUTPUT_PIXELS / max(1, width * height)))
    return max(16, int(round(width * scale))), max(16, int(round(height * scale)))


# -- drawing -------------------------------------------------------------


def _font(size=11):
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - older Pillow
        return ImageFont.load_default()


def nice_length(target_um):
    if target_um <= 0:
        return None
    exponent = math.floor(math.log10(target_um))
    for step in (5, 2, 1):
        value = step * 10 ** exponent
        if value <= target_um:
            return value
    return 10 ** exponent


def draw_scale_bar(image, um_per_px, *, approximate=False):
    """A bar about a fifth of the image wide, bottom-left, labelled in µm --
    "~" before the number when the scale is an estimate, not a calibration."""
    from PIL import ImageDraw

    width, height = image.size
    length_um = nice_length(um_per_px * width / 5)
    if not length_um:
        return None
    length_px = int(round(length_um / um_per_px))
    if length_px < 4:
        return None
    draw = ImageDraw.Draw(image)
    x0, y1 = 16, height - 16
    y0 = y1 - max(3, height // 160)
    draw.rectangle((x0 - 1, y0 - 1, x0 + length_px + 1, y1 + 1), fill=(0, 0, 0))
    draw.rectangle((x0, y0, x0 + length_px, y1), fill=(255, 255, 255))
    mark = "~" if approximate else ""
    label = f"{'≈' if approximate else ''}{length_um:g} µm"
    # Drawn as "um" (and "~" for "≈"): the font Pillow bundles has neither, and
    # a box where the unit should be is worse than the ASCII spelling. The
    # manifest keeps the proper one.
    draw.text((x0, y0 - 18), f"{mark}{length_um:g} um", fill=(255, 255, 255), font=_font(14),
              stroke_width=2, stroke_fill=(0, 0, 0))
    return {"length_um": float(length_um), "length_px": length_px, "label": label,
            "approximate": bool(approximate)}


def _rgb(hex_colour):
    value = hex_colour.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


# -- the table side --------------------------------------------------------


def _highlight_sets(data, highlight):
    """(positive ids, known ids, how) for a highlight, from the session's table."""
    from plexora.server.utils.label_overlay import cell_ids

    frame = data.table.geometry()
    ids, keep = cell_ids(frame, data.schema.cell_id if data.schema else None)
    known = set(ids.tolist())
    if isinstance(highlight, IdHighlight):
        return set(int(i) for i in highlight.cell_ids) & known, known, {
            "kind": "ids", "requested": len(highlight.cell_ids)}
    marker = highlight.marker
    if marker not in data.table.markers:
        raise AgentError("invalid_input", f"{marker!r} is not a marker of {data.name!r}",
                         detail={"markers": data.table.markers[:200]})
    low, high = highlight.low, highlight.high
    source = "explicit"
    if low is None or high is None:
        stored = _stored_gate(data, marker)
        if low is None:
            low = stored["low"]
            source = stored["source"]
        if high is None:
            high = stored["high"]
    values = np.asarray(data.table.columns([marker])[marker], dtype=np.float64)[keep]
    positive = ids[gate_rule.passes(values, low, high)]
    return set(positive.tolist()), known, {"kind": "marker", "marker": marker,
                                            "low": float(low), "high": float(high),
                                            "gate_source": source}


def _qc_left_out(data, cells) -> set:
    """Ids QC left out of estimation for a marker highlight's marker (drawn
    muted, never as positive); empty for an id highlight or `as_others`."""
    from plexora.agent import cell_exclusions

    highlight = cells.highlight
    if cells.qc_failures != "muted" or not isinstance(highlight, MarkerHighlight):
        return set()
    record = cell_exclusions.current(data)
    if record is None or record.empty:
        return set()
    return set(record.ids_for(highlight.marker).tolist())


def _stored_gate(data, marker):
    """The gate gating has stored for a marker, read from its store without
    importing the plugin: the pickled rows the sidebar writes."""
    import pickle

    from plexora import api

    desc = data.table.describe().get(marker) or {}
    low, high = desc.get("min"), desc.get("max")
    source = "default_range"
    blob = api.store(data.name, "gating").get_state()
    if blob:
        try:
            for row in pickle.loads(blob) or []:
                if isinstance(row, dict) and row.get("channel") == marker:
                    if row.get("gate_start") is not None:
                        low, high = row["gate_start"], row["gate_end"]
                        source = "stored_gate"
                    break
        except Exception:
            pass
    if low is None or high is None:
        raise AgentError("invalid_input", f"{marker!r} has no values to gate")
    return {"low": float(low), "high": float(high), "source": source}


def _centroids_in(data, box):
    """(ids, xs, ys) of table cells whose centroid is inside `box`."""
    from plexora.server.utils.label_overlay import cell_ids

    schema = data.schema
    frame = data.table.geometry()
    if frame is None or not schema or schema.x not in frame.columns \
            or schema.y not in frame.columns:
        return np.empty(0, np.uint32), np.empty(0), np.empty(0)
    ids, keep = cell_ids(frame, schema.cell_id)
    xs = frame[schema.x].to_numpy().astype(np.float64)[keep]
    ys = frame[schema.y].to_numpy().astype(np.float64)[keep]
    inside = (xs >= box[0]) & (xs < box[2]) & (ys >= box[1]) & (ys < box[3])
    return ids[inside], xs[inside], ys[inside]


# -- the render ------------------------------------------------------------


def _unsupported_image(record):
    """Why this sample cannot be rendered, or None. Only a blank reference
    frame is refused: every image format the viewer opens, `SourceImage`
    opens too."""
    if record.image.is_blank:
        return "this sample has no image to render (a blank reference frame)"
    return None


def _shape_rings(shape):
    """[(exterior, [holes])] of a ShapeSpec, as lists of (x, y) full-res points."""
    if shape.bounds is not None:
        b = shape.bounds
        ring = [(b.x, b.y), (b.x + b.width, b.y), (b.x + b.width, b.y + b.height),
                (b.x, b.y + b.height), (b.x, b.y)]
        return [(ring, [])]
    geometry = shape.geometry
    polygons = geometry["coordinates"] if geometry["type"] == "MultiPolygon" \
        else [geometry["coordinates"]]
    out = []
    for polygon in polygons:
        if not polygon:
            continue
        rings = [[(float(p[0]), float(p[1])) for p in ring] for ring in polygon]
        out.append((rings[0], rings[1:]))
    return out


def _dashed(points, dash=8.0, gap=5.0):
    """A closed polyline split into dashes of `dash` px with `gap` px gaps."""
    segments, current, drawn, on = [], [points[0]], 0.0, True
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        length = math.hypot(x1 - x0, y1 - y0)
        at = 0.0
        while at < length - 1e-9:
            left = (dash if on else gap) - drawn
            step = min(left, length - at)
            at += step
            drawn += step
            t = at / max(length, 1e-9)
            point = (x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)
            if on:
                current.append(point)
            if drawn >= (dash if on else gap) - 1e-9:
                if on and len(current) > 1:
                    segments.append(current)
                on, drawn, current = not on, 0.0, [point]
    if on and len(current) > 1:
        segments.append(current)
    return segments


def _draw_shapes(image, shapes, fullres, out_size):
    """Outline (and optionally fill) `shapes` over `image` in place; returns
    the manifest's `shapes` entries. A pure function of the spec, so the
    render stays deterministic."""
    from PIL import Image, ImageDraw

    out_w, out_h = out_size
    sx = out_w / max(1e-9, fullres[2] - fullres[0])
    sy = out_h / max(1e-9, fullres[3] - fullres[1])

    def to_px(ring):
        return [((x - fullres[0]) * sx, (y - fullres[1]) * sy) for x, y in ring]

    manifest = []
    for shape in shapes:
        colour = _rgb(shape.color)
        parts = _shape_rings(shape)
        vertices = sum(len(ext) + sum(len(h) for h in holes) for ext, holes in parts)
        clipped = False
        if shape.fill_alpha > 0:
            fill = Image.new("L", (out_w, out_h), 0)
            fdraw = ImageDraw.Draw(fill)
            for exterior, holes in parts:
                if len(exterior) >= 3:
                    fdraw.polygon(to_px(exterior), fill=255)
                for hole in holes:
                    if len(hole) >= 3:
                        fdraw.polygon(to_px(hole), fill=0)
            alpha = int(round(255 * shape.fill_alpha))
            mask = fill.point(lambda v, a=alpha: a if v else 0)
            image.paste(Image.new("RGB", (out_w, out_h), colour), (0, 0), mask)
        draw = ImageDraw.Draw(image)
        top = None
        for exterior, holes in parts:
            for ring in [exterior, *holes]:
                if len(ring) < 2:
                    continue
                points = to_px(ring)
                if points[0] != points[-1]:
                    points.append(points[0])
                for x, y in points:
                    if x < 0 or y < 0 or x > out_w or y > out_h:
                        clipped = True
                    if top is None or y < top[1]:
                        top = (x, y)
                lines = _dashed(points, 4.0 * shape.width, 2.5 * shape.width) \
                    if shape.dash else [points]
                for line in lines:
                    draw.line(line, fill=colour, width=shape.width, joint="curve")
        if shape.label and top is not None:
            font = _font(12)
            x = min(max(2, top[0]), max(2, out_w - 8 * len(shape.label)))
            y = min(max(2, top[1] - 16), max(2, out_h - 14))
            draw.text((x, y), shape.label, fill=colour, font=font, stroke_width=2,
                      stroke_fill=(0, 0, 0))
        manifest.append({"id": shape.id, "type": "bounds" if shape.bounds is not None
                         else shape.geometry["type"], "vertices": vertices,
                         "color": shape.color, "width": shape.width,
                         "fill_alpha": shape.fill_alpha, "dash": shape.dash,
                         "label": shape.label, "clipped": clipped})
    return manifest


def render_region(session, spec, *, store=True, pixel_size=None):
    """Render `spec` (a `RenderInput`). Returns `{png, manifest, artifact}`.

    `pixel_size` (`{value, source}`, microns per pixel) stands in for the
    image's own calibration -- a gating session's estimate for an image that
    states none. Its scale bar is then marked approximate."""
    from PIL import Image

    from plexora import api
    from plexora.agent import artifacts, presets
    from plexora.server.utils import fast_png, pixel_scale, source_image
    from plexora.server.utils.label_overlay import paint_labels, resize_labels_nearest

    record = session.project(spec.project)
    reason = _unsupported_image(record)
    if reason:
        raise AgentError("unsupported_modality", reason,
                         detail={"image_kind": record.image.kind})
    image_data = session.image_data(spec.project)
    channel_records = list(record.image.real_channels)
    channel_names = [c.get("fullname") or c.get("name") for c in channel_records]
    mask, mask_status, mask_reason, mask_locator = _mask_for(record)
    # A mask that exists but cannot be read still counts: the preset asks for
    # outlines, and the manifest then says why they are not there.
    spec, preset_filled = presets.apply(spec, channel_names, has_mask=mask is not None
                                        or mask_status == "unavailable")
    pixel = pixel_size if pixel_size is not None else pixel_scale.pixel_size(record)
    box, bounds_how = resolve_bounds(spec, record, pixel)
    width, height = record.image.width or 0, record.image.height or 0
    out_w, out_h = output_size(box, spec.output)

    needs_table = bool(spec.cells and (spec.cells.highlight is not None
                                       or spec.cells.label_ids))
    data = session.data(spec.project) if needs_table and record.has_table else None
    if needs_table and data is None:
        raise AgentError("precondition_missing", "highlighting cells needs a cell table",
                         detail={"missing": [{"key": "table", "label": "Cell table"}]})

    with source_image.SHELF.reader(image_data) as source:
        levels = max(1, int(source.levels))
        if spec.level == "auto":
            level = source_image.choose_level(source, box[2] - box[0], out_w)
            level_source = "auto"
        else:
            level = min(int(spec.level), levels - 1)
            level_source = "explicit" if level == spec.level else "clamped"
        level = min(level, levels - 1)
        while True:
            div = 2 ** level
            lbox = (int(math.floor(box[0] / div)), int(math.floor(box[1] / div)),
                    int(math.ceil(box[2] / div)), int(math.ceil(box[3] / div)))
            lbox = (lbox[0], lbox[1], max(lbox[0] + 1, lbox[2]), max(lbox[1] + 1, lbox[3]))
            area = (lbox[2] - lbox[0]) * (lbox[3] - lbox[1])
            if area <= MAX_SOURCE_PIXELS_AGENT:
                break
            if level >= levels - 1:
                raise AgentError("too_large", "this region is too large to read at any "
                                 "level of this image's pyramid",
                                 detail={"source_pixels": area,
                                         "max": MAX_SOURCE_PIXELS_AGENT})
            level += 1
            level_source = "coarsened_for_budget"

        from plexora.agent.evidence import calibration as display

        resolved_channels = []
        # The project's display calibration, when one is stored, is what
        # "auto" means -- the same windows an open viewer is given when it
        # mirrors an agent's work (plexora/agent/evidence/calibration.py).
        calibration = None
        if not source.is_brightfield and any(c.window == "auto" for c in spec.channels or []):
            calibration = display.load(record.name)
        if not source.is_brightfield:
            for channel in spec.channels or []:
                index, found = resolve_channel(channel.name, channel_records)
                key = source_image.channel_key(found)
                calibrated = (None, None)
                if channel.window == "auto" and calibration is not None:
                    calibrated = display.window_for(
                        calibration, found.get("fullname") or found.get("name"))
                if calibrated[0] is not None:
                    window, window_source = calibrated
                elif channel.window in ("auto", "percentiles"):
                    stats = source_image.channel_stats(source, key)
                    window = [stats["p01"], stats["p999"]]
                    if not window[0] < window[1]:
                        window = [stats["min"], max(stats["min"] + 1.0, stats["max"])]
                    window_source = f"auto:p01-p999@level{stats['level']}"
                else:
                    window, window_source = list(channel.window), "explicit"
                resolved_channels.append({
                    "name": found.get("fullname") or found.get("name"), "key": key,
                    "index": index, "color": channel.color,
                    "window": [float(window[0]), float(window[1])],
                    "window_source": window_source})
        scene_channels = [{"key": c["key"], "window": c["window"], "visible": True,
                           "color": dict(zip("rgb", _rgb(c["color"])))}
                          for c in resolved_channels]
        pixels, rendered, _background = source_image.composite(source, scene_channels,
                                                               level, lbox)
        brightfield = bool(source.is_brightfield)

    image = Image.fromarray(np.ascontiguousarray(pixels), "RGB")
    if image.size != (out_w, out_h):
        image = image.resize((out_w, out_h), Image.LANCZOS)
    rgb = np.array(image)

    div = 2 ** level
    fullres = (lbox[0] * div, lbox[1] * div, lbox[2] * div, lbox[3] * div)

    # The scene's other layers, over the reference image and under the cells.
    from plexora.server.utils import layer_composite

    layers_rendered, not_rendered = layer_composite.composite(
        record, rgb, fullres, level, (out_w, out_h), layers=spec.layers,
        max_points=MAX_LAYER_POINTS)
    clipped = (max(0, fullres[0]), max(0, fullres[1]), min(width, fullres[2]),
               min(height, fullres[3]))

    # Cells.
    cells_manifest = {"visible_cells": None, "positive_cells": None,
                      "highlight_rendering": "none", "label_ids_drawn": 0}
    positive, known, highlight_info = set(), set(), None
    qc_left = set()
    if spec.cells and spec.cells.highlight is not None:
        positive, known, highlight_info = _highlight_sets(data, spec.cells.highlight)
        qc_left = _qc_left_out(data, spec.cells)
        positive -= qc_left
    segmentation_mode = spec.segmentation or "none"
    mask_level = None
    labels = None
    if mask is not None and (segmentation_mode != "none" or highlight_info):
        mask_level = level + record.segmentation.extra_levels
        labels = mask.read_region(mask_level, lbox, max_pixels=MAX_SOURCE_PIXELS_AGENT)
        labels = resize_labels_nearest(labels, out_w, out_h)
        cells = spec.cells
        outline = cells.outline_color if cells else "#e6e6e6"

        def colour_for(label):
            if highlight_info is not None and label in known:
                if label in qc_left:
                    return cells.qc_color
                if label in positive:
                    return cells.positive_color
                return cells.negative_color if cells.show_negative else None
            return outline if segmentation_mode != "none" else None

        mode = segmentation_mode if segmentation_mode != "none" else "outlines"
        # One screen pixel of outline in the viewer; a picture meant to be read
        # at its own size gets a width that survives being looked at.
        thickness = 1 if max(out_w, out_h) < 600 else 2
        paint_labels(rgb, labels, mode=mode, colour_for=colour_for, alpha=1.0,
                     thickness=thickness)
        visible = set(np.unique(labels).tolist()) - {0}
        cells_manifest.update(
            visible_cells=len(visible),
            positive_cells=len(visible & positive) if highlight_info else None,
            highlight_rendering="mask" if highlight_info else "none")
        if qc_left:
            cells_manifest.update(qc_left_out_cells=len(visible & qc_left),
                                  qc_color=cells.qc_color)
    image = Image.fromarray(rgb, "RGB")

    # Centroid marks (no local mask) and id labels.
    sx, sy = out_w / max(1e-9, fullres[2] - fullres[0]), out_h / max(1e-9, fullres[3] - fullres[1])
    if data is not None and (labels is None or (spec.cells and spec.cells.label_ids)):
        from PIL import ImageDraw

        ids, xs, ys = _centroids_in(data, fullres)
        draw = ImageDraw.Draw(image)
        if labels is None and highlight_info is not None:
            radius = max(3, int(round(min(out_w, out_h) / 128)))
            for cid, x, y in zip(ids.tolist(), xs, ys):
                is_pos = cid in positive
                if cid in qc_left:
                    colour = _rgb(spec.cells.qc_color)
                elif not is_pos and not spec.cells.show_negative:
                    continue
                else:
                    colour = _rgb(spec.cells.positive_color if is_pos
                                  else spec.cells.negative_color)
                px, py = (x - fullres[0]) * sx, (y - fullres[1]) * sy
                draw.ellipse((px - radius, py - radius, px + radius, py + radius),
                             outline=colour, width=2)
            cells_manifest.update(visible_cells=int(len(ids)),
                                  positive_cells=int(sum(1 for c in ids.tolist()
                                                         if c in positive)),
                                  highlight_rendering="centroids")
            if qc_left:
                cells_manifest.update(
                    qc_left_out_cells=int(sum(1 for c in ids.tolist() if c in qc_left)),
                    qc_color=spec.cells.qc_color)
        if spec.cells and spec.cells.label_ids and len(ids):
            cx, cy = (fullres[0] + fullres[2]) / 2, (fullres[1] + fullres[3]) / 2
            order = np.lexsort((ids, (xs - cx) ** 2 + (ys - cy) ** 2))
            font = _font(12 if max(out_w, out_h) < 900 else 14)
            drawn = 0
            for i in order[:spec.cells.max_labels]:
                px, py = (xs[i] - fullres[0]) * sx, (ys[i] - fullres[1]) * sy
                draw.text((px + 3, py - 6), str(int(ids[i])), fill=(255, 255, 255),
                          font=font, stroke_width=2, stroke_fill=(0, 0, 0))
                drawn += 1
            cells_manifest["label_ids_drawn"] = drawn

    shapes_manifest = _draw_shapes(image, spec.shapes, fullres, (out_w, out_h)) \
        if spec.shapes else []

    scale_bar = None
    if spec.scale_bar and pixel:
        scale_bar = draw_scale_bar(image, pixel["value"] * (fullres[2] - fullres[0]) / out_w,
                                   approximate=pixel.get("source") == "estimated")

    png = fast_png.encode_rgb8_png(np.asarray(image))

    if mask_status in ("unsupported", "unavailable") and segmentation_mode != "none":
        not_rendered.append({"layer": "__mask__", "reason": mask_reason})

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "kind": MANIFEST_KIND,
        "project": record.name,
        "preset": spec.preset,
        "preset_filled": preset_filled,
        "spec": spec.model_dump(mode="json"),
        "bounds_requested": {"x": box[0], "y": box[1], "width": box[2] - box[0],
                             "height": box[3] - box[1], **bounds_how},
        "bounds_fullres": {"x": fullres[0], "y": fullres[1],
                           "width": fullres[2] - fullres[0],
                           "height": fullres[3] - fullres[1]},
        "bounds_clipped": {"x": clipped[0], "y": clipped[1],
                           "width": max(0, clipped[2] - clipped[0]),
                           "height": max(0, clipped[3] - clipped[1])},
        "padded": clipped != fullres,
        "image_size": [width, height],
        "physical": pixel is not None,
        "pixel_size_um": pixel["value"] if pixel else None,
        "pixel_size_source": pixel.get("source") if pixel else None,
        "physical_size_um": ({"width": (fullres[2] - fullres[0]) * pixel["value"],
                              "height": (fullres[3] - fullres[1]) * pixel["value"]}
                             if pixel else None),
        "um_per_output_px": (pixel["value"] * (fullres[2] - fullres[0]) / out_w
                             if pixel else None),
        "level": level,
        "level_source": level_source,
        "levels": levels,
        "output_size": [out_w, out_h],
        "brightfield": brightfield,
        "channels": resolved_channels,
        "channels_rendered": rendered,
        "segmentation": {"requested": segmentation_mode, "status": mask_status,
                         "reason": mask_reason, "mask_level": mask_level,
                         "mask_scale": record.segmentation.scale},
        "highlight": highlight_info,
        "cells": cells_manifest,
        "overlays": [o for o in (
            "segmentation" if labels is not None and segmentation_mode != "none" else None,
            "gate_highlight" if highlight_info else None,
            "cell_ids" if cells_manifest["label_ids_drawn"] else None,
            "layers" if layers_rendered else None,
            "shapes" if shapes_manifest else None,
            "scale_bar" if scale_bar else None) if o],
        "shapes": shapes_manifest,
        "layers_rendered": layers_rendered,
        "not_rendered": not_rendered,
        "scale_bar": scale_bar,
        "egress": "rendered_pixels",
        "provenance": {
            "image": api.ImageHandle(record).locator.to_dict(),
            "mask_path": (record.segmentation.derived
                          if mask is not None and mask_locator.is_local else None),
            "mask": mask_locator.to_dict() if mask is not None else None,
            "table": (data.table.locator.to_dict() if data is not None else None),
            "renderer": "plexora.agent.render/1",
        },
    }
    artifact = artifacts.put(record.name, png, manifest) if store else None
    return {"png": png, "manifest": manifest, "artifact": artifact}
