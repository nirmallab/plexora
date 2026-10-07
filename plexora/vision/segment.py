"""Magic select on a project: the view a person (or agent) sees -> a polygon.

`segment()` is what `POST /segment/v1/segment` and QC's agent tool call. It
reads the view's pixels composited exactly as the viewer's shader draws them
(`source_image.composite`), so the model sees what the person saw: the same
channels, colours and contrast windows. Prompts and the answer are in
full-resolution image pixels.

Speed comes from three places:

- **One embedding per view.** The encoder is the only expensive call (~0.1 s
  on a GPU, ~0.5 s on CPU). Its output is kept in a small LRU keyed by what
  the pixels were (image identity, level, crop, channels and windows), so a
  second click -- grow, carve, refine -- costs one decoder call (~20-50 ms).
  The `token` the caller gets back names that entry; even without it the key
  is recomputed and hits.
- **Reads inside, inference outside.** The reader shelf's lock is exclusive
  per image, so the read happens in it and the model runs after it is
  released; a tile request never waits for a click.
- **Bounded crops.** The finest pyramid level whose crop of the view fits in
  1024 px is read, never more: the model works at 1024 px whatever the zoom.

Nothing here writes anything; the caller commits the polygon through its own
store (the ROI panel's undoable operations, or QC's region writer).
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

import numpy as np

from plexora.vision import sam, sam_weights

#: Longest side of the crop handed to the model.
CROP_SIDE = sam.INPUT_SIDE
#: A view smaller than this many full-resolution pixels is grown to it: the
#: model needs context around an object to find its edge.
MIN_VIEW = 64
#: A mask covering more than this fraction of the crop is "the whole field",
#: never an artifact.
TOO_LARGE = 0.90
#: Masks kept per embedding for `use_prev_mask` (the newest few).
PREV_MASKS_KEPT = 4
CACHE_ENTRIES = 8
DEFAULT_COLOURS = ("#ffffff", "#00ff00", "#ff00ff", "#00ffff", "#ffff00", "#ff0000",
                   "#0000ff", "#ff8800")


class SegmentError(Exception):
    """A request that cannot be answered, with an HTTP-ish code."""

    def __init__(self, code, message, **detail):
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass
class _Entry:
    token: str
    key: tuple
    identity: str | None
    level: int
    lbox: tuple[int, int, int, int]
    factor: tuple[float, float]
    image_size: tuple[int, int]
    embedding: sam.Embedding
    channels: list
    capped: bool
    pixel_um: float | None
    low_res: dict = field(default_factory=dict)


_CACHE: "OrderedDict[str, _Entry]" = OrderedDict()
_BY_KEY: dict[tuple, str] = {}
_CACHE_LOCK = threading.Lock()


def forget() -> None:
    """Drop every cached embedding (tests, `plexora ai segment remove`)."""
    with _CACHE_LOCK:
        _CACHE.clear()
        _BY_KEY.clear()


def _remember(entry: _Entry) -> None:
    with _CACHE_LOCK:
        _CACHE[entry.token] = entry
        _BY_KEY[entry.key] = entry.token
        _CACHE.move_to_end(entry.token)
        while len(_CACHE) > CACHE_ENTRIES:
            _, old = _CACHE.popitem(last=False)
            _BY_KEY.pop(old.key, None)


def _lookup(token=None, key=None) -> _Entry | None:
    with _CACHE_LOCK:
        if token is None and key is not None:
            token = _BY_KEY.get(key)
        entry = _CACHE.get(token) if token else None
        if entry is not None:
            _CACHE.move_to_end(entry.token)
        return entry


# -- request parsing ----------------------------------------------------------


def _number(value, what):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise SegmentError("invalid_input", f"{what} must be a number") from None
    if not math.isfinite(number):
        raise SegmentError("invalid_input", f"{what} must be finite")
    return number


def _rect(value, what):
    if not isinstance(value, dict):
        raise SegmentError("invalid_input", f"{what} must be {{x, y, width, height}}")
    x, y = _number(value.get("x"), f"{what}.x"), _number(value.get("y"), f"{what}.y")
    w = _number(value.get("width"), f"{what}.width")
    h = _number(value.get("height"), f"{what}.height")
    if w <= 0 or h <= 0:
        raise SegmentError("invalid_input", f"{what} must have a positive size")
    return x, y, x + w, y + h


def parse_points(points):
    if not isinstance(points, list):
        raise SegmentError("invalid_input", "points must be a list of {x, y, label}")
    if len(points) > sam.MAX_POINTS:
        raise SegmentError("invalid_input", f"at most {sam.MAX_POINTS} points")
    xy = np.zeros((len(points), 2), np.float64)
    labels = np.zeros(len(points), np.int64)
    for i, point in enumerate(points):
        if not isinstance(point, dict):
            raise SegmentError("invalid_input", "each point is {x, y, label}")
        xy[i] = (_number(point.get("x"), "point.x"), _number(point.get("y"), "point.y"))
        label = point.get("label", 1)
        if label not in (0, 1, True, False):
            raise SegmentError("invalid_input", "a point's label is 1 (include) or 0 (exclude)")
        labels[i] = int(label)
    return xy, labels


def _hex(colour):
    if isinstance(colour, dict):
        return {k: int(max(0, min(255, round(float(colour.get(k, 0)))))) for k in "rgb"}
    text = str(colour or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    try:
        value = int(text[:6], 16)
    except ValueError:
        return {"r": 255, "g": 255, "b": 255}
    return {"r": (value >> 16) & 255, "g": (value >> 8) & 255, "b": value & 255}


def resolve_channels(source, record, channels):
    """[{name, color, range, visible}] -> composite specs, windows filled in.

    A missing range is the channel's p01-p999 auto window (as the viewer's
    first paint), a missing colour cycles the viewer's defaults. No channels
    at all means the first real channel in white.
    """
    from plexora.agent.errors import AgentError
    from plexora.agent.render import resolve_channel
    from plexora.server.utils import source_image

    records = list(record.image.real_channels)
    wanted = [c for c in (channels or []) if isinstance(c, dict) and c.get("visible", True)]
    if not wanted and records:
        wanted = [{"name": records[0].get("fullname") or records[0].get("name")}]
    specs = []
    for i, channel in enumerate(wanted[:12]):
        try:
            _index, found = resolve_channel(channel.get("name"), records)
        except AgentError as exc:
            raise SegmentError("invalid_input", str(exc), **(exc.detail or {})) from None
        key = source_image.channel_key(found)
        window = channel.get("range") or channel.get("window")
        if (not isinstance(window, (list, tuple)) or len(window) != 2
                or not float(window[0]) < float(window[1])):
            stats = source_image.channel_stats(source, key)
            window = [stats["p01"], stats["p999"]]
            if not window[0] < window[1]:
                window = [stats["min"], max(stats["min"] + 1.0, stats["max"])]
        specs.append({"name": found.get("fullname") or found.get("name"), "key": key,
                      "color": _hex(channel.get("color") or DEFAULT_COLOURS[i % 8]),
                      "window": [float(window[0]), float(window[1])], "visible": True})
    return specs


def _signature(specs) -> str:
    body = [(s["key"], s["color"]["r"], s["color"]["g"], s["color"]["b"],
             round(s["window"][0], 3), round(s["window"][1], 3)) for s in specs]
    return hashlib.sha1(json.dumps(body).encode()).hexdigest()


# -- crop geometry --------------------------------------------------------------


def choose_crop(source, image_size, view, positives):
    """(level, level box, (fx, fy), capped) for a full-resolution view.

    The finest level whose crop of the view fits in CROP_SIDE, from the real
    level shapes (a pyramid's levels are not always exact halves). When even
    the coarsest level is too big, a CROP_SIDE window centred on the positive
    prompts is taken instead and `capped` says so.
    """
    width, height = image_size
    x0, y0, x1, y1 = view
    levels = max(1, int(source.levels))
    chosen = None
    for level in range(levels):
        lh, lw = source.level_shape(level)
        fx, fy = width / max(1, lw), height / max(1, lh)
        if max((x1 - x0) / fx, (y1 - y0) / fy) <= CROP_SIDE:
            chosen = (level, fx, fy)
            break
    capped = chosen is None
    if capped:
        level = levels - 1
        lh, lw = source.level_shape(level)
        fx, fy = width / max(1, lw), height / max(1, lh)
        cx, cy = (np.mean(positives, axis=0) if len(positives)
                  else ((x0 + x1) / 2, (y0 + y1) / 2))
        half_w, half_h = CROP_SIDE * fx / 2, CROP_SIDE * fy / 2
        x0, x1 = max(x0, cx - half_w), min(x1, cx + half_w)
        y0, y1 = max(y0, cy - half_h), min(y1, cy + half_h)
    else:
        level, fx, fy = chosen
    lbox = (int(math.floor(x0 / fx)), int(math.floor(y0 / fy)),
            int(math.ceil(x1 / fx)), int(math.ceil(y1 / fy)))
    lbox = (lbox[0], lbox[1], max(lbox[0] + 1, lbox[2]), max(lbox[1] + 1, lbox[3]))
    return level, lbox, (fx, fy), capped


def clamp_view(view, image_size, positives=()):
    width, height = image_size
    x0, y0, x1, y1 = view
    if x1 - x0 < MIN_VIEW or y1 - y0 < MIN_VIEW:
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        half = max(MIN_VIEW, x1 - x0, y1 - y0) / 2
        x0, x1, y0, y1 = cx - half, cx + half, cy - half, cy + half
    x0, y0 = max(0.0, x0), max(0.0, y0)
    x1, y1 = min(float(width), x1), min(float(height), y1)
    if x1 - x0 < 1 or y1 - y0 < 1:
        raise SegmentError("invalid_input", "the view is outside the image")
    return x0, y0, x1, y1


# -- mask -> outline ------------------------------------------------------------


def outline(mask, *, origin, factor, image_size, positives_rc=(), multi=False,
            min_area_px=0.0, simplify_px=None, margin_px=0.0, image_border=(True,) * 4):
    """Clean a crop mask and turn it into a full-resolution polygon + guards.

    Shared by `segment()` and QC's tracer, so a clicked outline and a traced
    one get the same tidy-up. `image_border` says which crop edges (left, top,
    right, bottom) are the image's own edge, where touching is fine.
    """
    import shapely
    from shapely.geometry import box as shapely_box

    from plexora.server.utils import mask_polygon

    fx, fy = factor
    area_scale = fx * fy
    h, w = mask.shape
    raw = int(mask.sum())
    flags = {"empty": raw == 0, "touches_edge": False, "too_large": False}
    if raw == 0:
        return None, flags, mask
    if raw > TOO_LARGE * h * w:
        flags["too_large"] = True
        return None, flags, mask
    cleaned = mask_polygon.clean_mask(
        mask, positives_rc=positives_rc,
        min_area_px=float(min_area_px) / area_scale if min_area_px else 0,
        keep_largest=not multi)
    if not cleaned.any():
        flags["empty"] = True
        return None, flags, cleaned
    rows, cols = cleaned.any(axis=1), cleaned.any(axis=0)
    left, top, right, bottom = image_border
    flags["touches_edge"] = bool((cols[0] and not left) or (rows[0] and not top)
                                 or (cols[-1] and not right) or (rows[-1] and not bottom))
    simplify = 0.7 * max(fx, fy) if simplify_px is None else max(0.0, float(simplify_px))
    shape = mask_polygon.mask_to_shape(cleaned, origin, 1.0)
    if shape is None:
        flags["empty"] = True
        return None, flags, cleaned
    if fx != 1.0 or fy != 1.0:
        shape = shapely.transform(shape, lambda xy: xy * [fx, fy])
    if margin_px:
        shape = shape.buffer(float(margin_px), join_style="mitre", mitre_limit=2.0)
    shape = mask_polygon.polygonal(shape.intersection(shapely_box(0, 0, *image_size)))
    geometry = mask_polygon.to_geojson(shape, simplify_px=simplify,
                                       max_vertices=mask_polygon.MAX_VERTICES)
    if geometry is None:
        flags["empty"] = True
    return geometry, flags, cleaned


# -- the call ---------------------------------------------------------------------


def segment(session, project, *, view, points, channels=None, box=None, token=None,
            options=None) -> dict:
    """One magic-select request. See the module docstring and the route."""
    options = dict(options or {})
    started = time.perf_counter()
    timing = {}
    xy, labels = parse_points(points)
    if box is not None:
        box = _rect(box, "box")
    if not (labels == 1).any() and box is None:
        raise SegmentError("invalid_input", "at least one include point or a box is needed")
    view = _rect(view, "view")

    record = session.project(project)
    width, height = int(record.image.width or 0), int(record.image.height or 0)
    if not width or not height:
        raise SegmentError("unsupported_modality", "this project has no image to outline")
    image_size = (width, height)
    view = clamp_view(view, image_size)
    # The model sees the view and nothing else: an include point off it is a
    # click it cannot answer, an exclude point off it says nothing, and a box
    # (a selected region's, say) is cut to what is on screen.
    vx0, vy0, vx1, vy1 = view
    inside = (xy[:, 0] >= vx0) & (xy[:, 0] <= vx1) & (xy[:, 1] >= vy0) & (xy[:, 1] <= vy1)
    if (~inside & (labels == 1)).any():
        raise SegmentError("invalid_input", "click inside the view")
    xy, labels = xy[inside], labels[inside]
    if box is not None:
        box = (max(box[0], vx0), max(box[1], vy0), min(box[2], vx1), min(box[3], vy1))
        if box[2] - box[0] < 1 or box[3] - box[1] < 1:
            box = None
    if not (labels == 1).any() and box is None:
        raise SegmentError("invalid_input", "at least one include point or a box is needed")
    positives = xy[labels == 1]

    entry = _lookup(token=token) if token else None
    if entry is not None and not _covers(entry, xy, box):
        entry = None
    if entry is None:
        entry = _embedding_for(session, project, record, view, positives, channels, timing,
                               image_size)
    timing["read_ms"] = timing.get("read_ms", 0.0)

    level_x0, level_y0, level_x1, level_y1 = entry.lbox
    fx, fy = entry.factor
    crop_h, crop_w = entry.embedding.crop_hw
    to_crop = np.array([fx, fy])
    crop_xy = xy / to_crop - np.array([level_x0, level_y0])
    crop_box = None
    if box is not None:
        crop_box = ((box[0] / fx) - level_x0, (box[1] / fy) - level_y0,
                    (box[2] / fx) - level_x0, (box[3] / fy) - level_y0)
    mask_input, used_prev = None, False
    if options.get("use_prev_mask") and options.get("prev_key") in entry.low_res:
        mask_input, used_prev = entry.low_res[options["prev_key"]], True
    elif options.get("mask_geometry"):
        # An existing region to start from (a refine): its own outline.
        mask_input = mask_prompt(entry, options["mask_geometry"])

    t = time.perf_counter()
    prediction = sam.predict(entry.embedding, crop_xy, labels, box=crop_box,
                             mask_input=mask_input, max_fraction=TOO_LARGE)
    timing["decode_ms"] = round((time.perf_counter() - t) * 1000, 1)

    positives_rc = np.round(crop_xy[labels == 1][:, ::-1]).astype(np.int64)
    positives_rc[:, 0] = np.clip(positives_rc[:, 0], 0, crop_h - 1)
    positives_rc[:, 1] = np.clip(positives_rc[:, 1], 0, crop_w - 1)
    border = (level_x0 <= 0, level_y0 <= 0,
              level_x1 * fx >= width - fx, level_y1 * fy >= height - fy)
    t = time.perf_counter()
    geometry, flags, cleaned = outline(
        prediction.mask, origin=(level_x0, level_y0), factor=(fx, fy), image_size=image_size,
        positives_rc=positives_rc, multi=bool(options.get("multi")),
        min_area_px=float(options.get("min_area_px") or 0.0),
        simplify_px=options.get("simplify_px"), margin_px=float(options.get("margin_px") or 0.0),
        image_border=border)
    timing["polygon_ms"] = round((time.perf_counter() - t) * 1000, 1)
    flags["capped_view"] = entry.capped

    # The prompts that made this mask -- the box too, or every box-only
    # prompt would share one key (the hash of nothing) and a retry naming an
    # older preview would decode from whichever came last.
    prompt_bytes = np.ascontiguousarray(xy).tobytes() + labels.tobytes() + \
        (np.asarray(box, dtype=np.float64).tobytes() if box is not None else b"")
    prev_key = hashlib.sha1(prompt_bytes).hexdigest()[:12]
    # The last few masks, so a retry may name an earlier preview, not only
    # the latest.
    entry.low_res.pop(prev_key, None)
    entry.low_res[prev_key] = prediction.low_res
    while len(entry.low_res) > PREV_MASKS_KEPT:
        entry.low_res.pop(next(iter(entry.low_res)))

    area_px2 = 0.0
    bbox = None
    if geometry is not None:
        from shapely.geometry import shape as shapely_shape

        polygon = shapely_shape(geometry)
        area_px2 = float(polygon.area)
        bx0, by0, bx1, by1 = polygon.bounds
        bbox = {"x": bx0, "y": by0, "width": bx1 - bx0, "height": by1 - by0}
    timing["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return {
        "geometry": geometry,
        "bbox": bbox,
        "iou": round(prediction.iou, 4),
        "area_px2": round(area_px2, 1),
        "area_um2": (round(area_px2 * entry.pixel_um ** 2, 2)
                     if entry.pixel_um and geometry is not None else None),
        "token": entry.token,
        "prev_key": prev_key,
        # Whether a `use_prev_mask` round found the mask it named (it starts
        # afresh, and says so, when the mask was evicted or never made here).
        "used_prev_mask": used_prev,
        "level": entry.level,
        "factor": round(float(fx), 4),
        "crop": {"x": level_x0 * fx, "y": level_y0 * fy,
                 "width": (level_x1 - level_x0) * fx, "height": (level_y1 - level_y0) * fy},
        "flags": flags,
        "timing": timing,
        "provenance": {"method": "sam", "model": sam_weights.MODEL_NAME,
                       "model_version": sam_weights.MODEL_VERSION,
                       "prompts": {"points": [{"x": round(float(p[0]), 1),
                                               "y": round(float(p[1]), 1),
                                               "label": int(lab)}
                                              for p, lab in zip(xy, labels)],
                                   "box": (None if box is None else
                                           {"x": box[0], "y": box[1], "width": box[2] - box[0],
                                            "height": box[3] - box[1]})}},
        "channels": [{"name": c["name"], "range": c["window"],
                      "color": "#%02x%02x%02x" % (c["color"]["r"], c["color"]["g"],
                                                  c["color"]["b"])}
                     for c in entry.channels],
        "device": dict(getattr(sam.session(), "device", {}) or {}),
    }


def mask_prompt(entry: _Entry, geometry) -> np.ndarray | None:
    """An existing outline (full-resolution GeoJSON) as the model's mask
    prompt: 256x256 logits in its low-resolution frame, positive inside.

    How an existing region becomes the starting point of a refinement -- the
    model redraws *that* region rather than guessing anew from a click."""
    import cv2
    from shapely.geometry import shape

    if not geometry:
        return None
    try:
        polygon = shape(geometry)
    except Exception:  # noqa: BLE001 -- a bad outline is no prompt, not an error
        return None
    fx, fy = entry.factor
    x0, y0 = entry.lbox[0], entry.lbox[1]
    h, w = entry.embedding.crop_hw
    nh, nw = entry.embedding.input_hw
    sx, sy = nw / w / 4.0, nh / h / 4.0
    canvas = np.zeros((sam.LOW_RES, sam.LOW_RES), np.uint8)
    parts = polygon.geoms if polygon.geom_type == "MultiPolygon" else [polygon]

    def ring(coords):
        xy = np.asarray(coords, np.float64)
        xy = (xy / [fx, fy] - [x0, y0]) * [sx, sy]
        return np.round(xy * 16).astype(np.int32).reshape(-1, 1, 2)

    for part in parts:
        cv2.fillPoly(canvas, [ring(part.exterior.coords)], 1, shift=4)
        for hole in part.interiors:
            cv2.fillPoly(canvas, [ring(hole.coords)], 0, shift=4)
    if not canvas.any():
        return None
    return np.where(canvas > 0, 10.0, -10.0).astype(np.float32)[None]


def _covers(entry: _Entry, xy, box) -> bool:
    """Every prompt falls inside the cached crop (else a new crop is needed)."""
    fx, fy = entry.factor
    x0, y0, x1, y1 = entry.lbox
    left, top, right, bottom = x0 * fx, y0 * fy, x1 * fx, y1 * fy
    inside = ((xy[:, 0] >= left) & (xy[:, 0] <= right)
              & (xy[:, 1] >= top) & (xy[:, 1] <= bottom))
    if box is not None:
        inside = np.append(inside, box[0] >= left and box[2] <= right
                           and box[1] >= top and box[3] <= bottom)
    return bool(inside.all())


def _embedding_for(session, project, record, view, positives, channels, timing, image_size):
    """Read + composite inside the shelf lock, embed outside it, cache."""
    from plexora.server.utils import pixel_scale, source_image

    image_data = session.image_data(project)
    identity = source_image.SHELF.identity_of(image_data)
    stamp = _file_stamp(record)
    t = time.perf_counter()
    with source_image.SHELF.reader(image_data) as source:
        level, lbox, factor, capped = choose_crop(source, image_size, view, positives)
        specs = resolve_channels(source, record, channels) if not source.is_brightfield else []
        key = (identity, stamp, project, level, lbox, _signature(specs))
        hit = _lookup(key=key)
        if hit is not None:
            return hit
        rgb, _rendered, _bg = source_image.composite(source, specs, level, lbox)
    timing["read_ms"] = round((time.perf_counter() - t) * 1000, 1)
    t = time.perf_counter()
    embedding = sam.embed(np.ascontiguousarray(rgb))
    timing["embed_ms"] = round((time.perf_counter() - t) * 1000, 1)
    size = pixel_scale.pixel_size(record)
    token = "sam_" + hashlib.sha1(repr(key).encode()).hexdigest()[:16]
    entry = _Entry(token=token, key=key, identity=identity, level=level, lbox=lbox,
                   factor=factor, image_size=image_size, embedding=embedding,
                   channels=specs, capped=capped,
                   pixel_um=float(size["value"]) if size else None)
    _remember(entry)
    return entry


def _file_stamp(record):
    import os

    src = getattr(record.image, "src", None)
    if not src:
        return None
    try:
        info = os.stat(str(src))
        return (info.st_size, info.st_mtime_ns)
    except OSError:
        return None
