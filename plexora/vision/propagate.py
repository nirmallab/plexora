"""Magic select through a z-stack: one outline carried from plane to plane.

A region outlined on one plane (magic select, or drawn by hand) is followed up
and down the stack until the object ends. The answer is one outline per plane,
in full-resolution pixels -- what the ROI plugin stores as a region's
`solid: {kind: "contours"}`.

Two engines, one contract (`carry_frames`):

- **sam2** -- SAM 2.1's video predictor with planes as frames
  (plexora/vision/sam_video.py). It remembers the object across planes and its
  object-presence score says where the object ends. Needs the optional
  `volume3d` extra (torch).
- **mobile** -- MobileSAM plane by plane (plexora/vision/sam.py): each plane is
  prompted with the previous plane's mask and its box. Always available.

`auto` takes sam2 when it is set up. Measured on synthetic nuclei with known
shapes and on the real Imaris and light-sheet stacks before this was written
(docs/internal/3d/MAGIC_3D.md).

Whatever the engine, every plane must pass the same guards before it is kept,
walking outwards from the prompted plane; the first plane that fails ends that
direction, and says why (`stopped`):

- the object has ended -- an empty mask, or (sam2) a presence score below 0;
- `faded` -- it stands out from a ring around it by less than `FADE_MIN` of
  how much it stood out on the prompted plane. Past an object's pole a
  microscope's axial blur leaves a dimmer, wider ghost of it for several
  planes, and both models follow the ghost: MobileSAM for as long as the stack
  goes (its confidence stays above 0.85), SAM 2 for about three planes. Where
  the contrast falls to a third is within a plane of the true pole on
  synthetic nuclei with known extent;
- `lost` -- it overlaps the previous plane's outline by less than `IOU_MIN`;
- `grew` / `shrank` -- its area left `[AREA_MIN, AREA_MAX]` x the prompted
  plane's (a leak into the neighbours, or a fragment);
- `drifted` -- its centre moved more than `DRIFT_MAX` equivalent radii;
- `range_end` / `stack_end` / `limit` -- nowhere further to go.

Pixels are read exactly as magic select reads them on one plane
(`segment._embedding_for`): the view on screen, composited with the viewer's
channels and windows, at the finest level whose crop fits 1024 px -- every
plane at the same level and crop.
"""

from __future__ import annotations

import time

import numpy as np

from plexora.vision import sam, sam_weights
from plexora.vision.segment import (
    SegmentError, _rect, choose_crop, clamp_view, outline, parse_points, resolve_channels,
)

#: At most this many planes in one carry (both directions together). A
#: nucleus is 30-50 planes deep at a 0.28 um step. SAM 2 holds its whole
#: window as frames, so it gets at most `sam_video.MAX_FRAMES`; MobileSAM reads
#: a plane only when it gets there (`_Planes`).
MAX_PLANES = 128
IOU_MIN = 0.25
FADE_MIN = 0.35
#: Below this much contrast on the prompted plane (0-255 grey levels) the
#: object does not stand out enough for a ratio to mean anything.
FADE_FLOOR = 6.0
AREA_MIN = 0.12
AREA_MAX = 4.0
DRIFT_MAX = 0.75
ENGINES = ("auto", "sam2", "mobile")


# -- the engines: frames in, masks out --------------------------------------------


def _low_res_of(mask, embedding):
    """A crop-frame mask as MobileSAM's 256x256 mask prompt (logits)."""
    import cv2

    h, w = embedding.crop_hw
    nh, nw = embedding.input_hw
    canvas = np.zeros((sam.LOW_RES, sam.LOW_RES), np.float32)
    small = cv2.resize(mask.astype(np.uint8), (max(1, nw // 4), max(1, nh // 4)),
                       interpolation=cv2.INTER_AREA)
    canvas[:small.shape[0], :small.shape[1]] = small
    return np.where(canvas > 0, 10.0, -10.0).astype(np.float32)[None]


def _box_of(mask, pad=0.1):
    ys, xs = np.nonzero(mask)
    span = max(np.ptp(xs), np.ptp(ys), 4)
    return np.array([xs.min() - pad * span, ys.min() - pad * span,
                     xs.max() + pad * span, ys.max() + pad * span], np.float32)


def contrast(rgb, mask) -> float:
    """How much brighter the masked object is than a ring around it (a third
    of its radius wide), in grey levels of the composited picture."""
    from scipy import ndimage

    if rgb is None or not mask.any():
        return 0.0
    grey = np.asarray(rgb, np.float32).mean(axis=2)
    width = max(2, int(np.sqrt(mask.sum() / np.pi) / 3))
    ring = ndimage.binary_dilation(mask, iterations=width) & ~mask
    return float(grey[mask].mean() - grey[ring].mean()) if ring.any() else 0.0


class _Guard:
    """The guards of the module docstring, measured against the start plane."""

    def __init__(self, start):
        self.start = start
        self.start_area = 1
        self.start_contrast = 0.0
        self.stopped = {}

    def begin(self, mask, frame=None):
        self.start_area = max(1, int(mask.sum()))
        self.start_contrast = contrast(frame, mask)

    def __call__(self, z, got, score, previous, frame=None) -> bool:
        direction = "increasing" if z > self.start else "decreasing"
        reason = None
        area = int(got.sum())
        if area == 0 or (score is not None and score <= 0):
            reason = "object_ended"
        elif (frame is not None and abs(self.start_contrast) >= FADE_FLOOR
              and contrast(frame, got) / self.start_contrast < FADE_MIN):
            reason = "faded"
        elif (np.logical_and(got, previous).sum()
              / max(1, np.logical_or(got, previous).sum())) < IOU_MIN:
            reason = "lost"
        elif area > AREA_MAX * self.start_area:
            reason = "grew"
        elif area < AREA_MIN * self.start_area:
            reason = "shrank"
        else:
            here, there = np.argwhere(got).mean(0), np.argwhere(previous).mean(0)
            radius = max(1.0, np.sqrt(previous.sum() / np.pi))
            if np.hypot(*(here - there)) > DRIFT_MAX * radius:
                reason = "drifted"
        if reason:
            self.stopped[direction] = reason
            return False
        return True


def _mobile(frames, start, box, points, labels, mask, order, guard):
    """MobileSAM, plane by plane, each plane prompted by the one before."""
    if mask is None:
        embedding = sam.embed(frames[start])
        prediction = sam.predict(
            embedding, points if points is not None else np.zeros((0, 2), np.float32),
            labels if labels is not None else np.zeros(0), box=box)
        mask = prediction.mask
    guard.begin(mask, frames[start])
    out = {start: (mask, None)}
    for step in order:
        previous = mask
        z = start + step
        while 0 <= z < len(frames) and previous.any():
            embedding = sam.embed(frames[z])
            prediction = sam.predict(embedding, np.zeros((0, 2), np.float32), np.zeros(0),
                                     box=_box_of(previous),
                                     mask_input=_low_res_of(previous, embedding))
            if not guard(z, prediction.mask, None, previous, frames[z]):
                break
            out[z] = (prediction.mask, None)
            previous = prediction.mask
            z += step
    return out


def _sam2(frames, start, box, points, labels, mask, order, guard):
    """SAM 2's video predictor, stopped at the first plane a guard rejects."""
    from plexora.vision import sam_video

    kept = {}
    previous = {}

    def until(index, got, score):
        if index == start:
            # Each direction yields the prompted plane first: where the
            # guards measure from (a given outline is kept exactly).
            if start not in kept:
                first = mask if mask is not None else got
                guard.begin(first, frames[start])
                kept[start] = (first, score)
            return True
        step = 1 if index > start else -1
        if not guard(index, got, score, previous.get(step, kept[start][0]), frames[index]):
            return False
        kept[index] = (got, score)
        previous[step] = got
        return True

    sam_video.carry(frames, start, box=box, points=points, labels=labels, mask=mask,
                    forward=1 in order, backward=-1 in order, until=until)
    return kept


def carry_frames(frames, start, *, box=None, points=None, labels=None, mask=None,
                 engine="auto", order=(1, -1)):
    """Masks for the object prompted on `frames[start]`, carried each way in
    `order` (+1 is increasing z) until a guard stops it.

    `frames` are H x W x 3 uint8 pictures of consecutive planes. The prompt is
    a `box` (x0, y0, x1, y1), `points` + `labels`, or a `mask` (H x W bool, an
    existing outline) -- in the frames' pixels. Returns `(kept, stopped,
    engine)`: `kept` maps frame index to `(mask, presence score or None)`,
    `stopped` maps each direction ("increasing", "decreasing") to why it ended.
    """
    engine = choose_engine(engine)
    guard = _Guard(start)
    run = _sam2 if engine == "sam2" else _mobile
    kept = run(frames, start, box, points, labels, mask, order, guard)
    stopped = dict(guard.stopped)
    for step in order:
        direction = "increasing" if step > 0 else "decreasing"
        if direction not in stopped:
            edge = max(kept) if step > 0 else min(kept)
            stopped[direction] = "stack_end" if edge in (0, len(frames) - 1) else "limit"
    return kept, stopped, engine


def choose_engine(engine="auto") -> str:
    from plexora.vision import sam_video

    if engine not in ENGINES:
        raise SegmentError("invalid_input", f"engine is one of {', '.join(ENGINES)}")
    if engine == "auto":
        return "sam2" if sam_video.available() else "mobile"
    if engine == "sam2" and not sam_video.available():
        raise SegmentError("precondition_missing", sam_video.status().message,
                           install=sam_video.INSTALL)
    return engine


class _Planes:
    """The planes of a carry's window as frames, composited when first asked
    for -- MobileSAM stops where the object does and never reads the rest."""

    def __init__(self, source, specs, level, lbox, t, first, last):
        self._source, self._specs = source, specs
        self._level, self._lbox, self._t = level, lbox, int(t)
        self._first, self._count = first, last - first + 1
        self._cache = {}
        self.read_seconds = 0.0

    def __len__(self):
        return self._count

    def __getitem__(self, index):
        from plexora.server.utils import source_image

        if not 0 <= index < self._count:
            raise IndexError(index)
        frame = self._cache.get(index)
        if frame is None:
            started = time.perf_counter()
            reader = self._source.at_plane(self._t, self._first + index)
            rgb, _rendered, _bg = source_image.composite(reader, self._specs, self._level,
                                                         self._lbox)
            frame = self._cache[index] = np.ascontiguousarray(rgb)
            self.read_seconds += time.perf_counter() - started
        return frame

    def __iter__(self):
        return (self[i] for i in range(self._count))


def footprint(planes):
    """The outline covering every plane's outline (GeoJSON, image pixels):
    what a region carried through the planes covers seen from above -- the
    2-D geometry it is stored with, so everything reading one outline per
    region keeps working."""
    import shapely
    from shapely.geometry import mapping, shape

    from plexora.server.utils import mask_polygon

    union = shapely.unary_union([shape(g) for g in planes.values()])
    geometry = mask_polygon.to_geojson(mask_polygon.polygonal(union), simplify_px=0.5,
                                       max_vertices=mask_polygon.MAX_VERTICES)
    return geometry or mapping(union)


# -- a project's planes ----------------------------------------------------------------


def _mask_in_crop(geometry, origin, factor, shape):
    """A full-resolution GeoJSON outline rasterised into the crop's pixels."""
    import cv2
    from shapely.geometry import shape as shapely_shape

    polygon = shapely_shape(geometry)
    x0, y0 = origin
    fx, fy = factor
    canvas = np.zeros(shape, np.uint8)
    parts = polygon.geoms if polygon.geom_type == "MultiPolygon" else [polygon]

    def ring(coords):
        xy = (np.asarray(coords, np.float64) / [fx, fy] - [x0, y0]) * 16
        return np.round(xy).astype(np.int32).reshape(-1, 1, 2)

    for part in parts:
        cv2.fillPoly(canvas, [ring(part.exterior.coords)], 1, shift=4)
        for hole in part.interiors:
            cv2.fillPoly(canvas, [ring(hole.coords)], 0, shift=4)
    return canvas.astype(bool)


def carry(session, project, *, z, view, geometry=None, points=None, box=None, channels=None,
          z_range=None, t=0, engine="auto", options=None) -> dict:
    """Carry an outline on plane `z` up and down a project's stack.

    The prompt is `geometry` (an existing outline, GeoJSON in full-resolution
    pixels, kept exactly on its own plane), or `box` / `points` as magic select
    takes them. `view` is the rectangle on screen. `z_range` (first, last)
    bounds the planes; at most MAX_PLANES are visited. See the module
    docstring for the guards and the answer.
    """
    from shapely.geometry import shape as shapely_shape

    from plexora.server.utils import source_image
    from plexora.server.volume.viewspec import BadView

    options = dict(options or {})
    started = time.perf_counter()
    timing = {}
    xy, labels = parse_points(points or [])
    if box is not None:
        box = _rect(box, "box")
    if geometry is None and box is None and not (labels == 1).any():
        raise SegmentError("invalid_input", "an outline, a box or an include point is needed")
    record = session.project(project)
    width, height = int(record.image.width or 0), int(record.image.height or 0)
    if not width or not height:
        raise SegmentError("unsupported_modality", "this project has no image to outline")
    image_size = (width, height)
    view = clamp_view(_rect(view, "view"), image_size)
    if geometry is not None:
        try:
            bounds = shapely_shape(geometry).bounds
        except Exception:  # noqa: BLE001
            raise SegmentError("invalid_input", "the outline is not a polygon") from None
        # The whole outline has to be in the picture the model sees.
        view = clamp_view((min(view[0], bounds[0]), min(view[1], bounds[1]),
                           max(view[2], bounds[2]), max(view[3], bounds[3])), image_size)
    positives = xy[labels == 1]
    engine = choose_engine(engine)

    image_data = session.image_data(project)
    token = None
    t0 = time.perf_counter()
    try:
        with source_image.SHELF.reader(image_data, view=f"z{int(z)}" if not t
                                       else f"t{int(t)}z{int(z)}") as source:
            volume = source.volume
            if volume is None or volume.depth < 2:
                raise SegmentError("unsupported_modality",
                                   "this image has one plane: there is nothing to carry it through")
            if not 0 <= int(z) < volume.depth:
                raise SegmentError("invalid_input", f"plane {z} is outside the stack's "
                                   f"{volume.depth} planes")
            depth = volume.depth
            first, last = 0, volume.depth - 1
            asked = None
            if z_range:
                first, last = sorted(int(v) for v in z_range)
                first, last = max(0, first), min(volume.depth - 1, last)
                asked = (first, last)
                if not first <= int(z) <= last:
                    raise SegmentError("invalid_input", "the plane prompted is outside z_range")
            window = MAX_PLANES
            if engine == "sam2":
                from plexora.vision import sam_video

                window = min(MAX_PLANES, sam_video.MAX_FRAMES)
            first = max(first, int(z) - window // 2)
            last = min(last, first + window - 1)
            level, lbox, factor, capped = choose_crop(source, image_size, view, positives)
            specs = (resolve_channels(source, record, channels)
                     if not source.is_brightfield else [])
            frames = _Planes(source, specs, level, lbox, t, first, last)
            token = source.view

            lx0, ly0, lx1, ly1 = lbox
            fx, fy = factor
            start = int(z) - first
            crop_shape = frames[start].shape[:2]
            mask = crop_box = crop_points = crop_labels = None
            if geometry is not None:
                mask = _mask_in_crop(geometry, (lx0, ly0), factor, crop_shape)
                if not mask.any():
                    raise SegmentError("invalid_input",
                                       "the outline is too small to carry at this zoom")
            else:
                if box is not None:
                    crop_box = np.array([box[0] / fx - lx0, box[1] / fy - ly0,
                                         box[2] / fx - lx0, box[3] / fy - ly0], np.float32)
                if len(xy):
                    crop_points = (xy / [fx, fy] - [lx0, ly0]).astype(np.float32)
                    crop_labels = labels
            # Run while the reader is held: the planes are read as the
            # engine reaches them.
            kept, stopped, engine = carry_frames(frames, start, box=crop_box,
                                                 points=crop_points, labels=crop_labels,
                                                 mask=mask, engine=engine)
    except (BadView, source_image.RenderError) as exc:
        raise SegmentError("invalid_input", str(exc)) from None
    timing["read_ms"] = round(frames.read_seconds * 1000, 1)
    timing["model_ms"] = round((time.perf_counter() - t0) * 1000 - timing["read_ms"], 1)
    # Where a direction ran out of planes: the stack's end, the range asked
    # for, or MAX_PLANES.
    for direction, step in (("increasing", 1), ("decreasing", -1)):
        if stopped.get(direction) in ("stack_end", "limit"):
            edge = first + (max(kept) if step > 0 else min(kept))
            stopped[direction] = ("stack_end" if edge in (0, depth - 1) else
                                  "range_end" if asked and edge in asked else "limit")

    border = (lx0 <= 0, ly0 <= 0, lx1 * fx >= width - fx, ly1 * fy >= height - fy)
    planes, rows = {}, []
    simplify = options.get("simplify_px")
    previous_area = None
    for index in sorted(kept):
        plane = first + index
        got, score = kept[index]
        if index == start and geometry is not None:
            planes[str(plane)] = geometry
            area = float(shapely_shape(geometry).area)
        else:
            centre = np.argwhere(got).mean(0) if got.any() else None
            geometry_z, _flags, _cleaned = outline(
                got, origin=(lx0, ly0), factor=factor, image_size=image_size,
                positives_rc=() if centre is None else np.round(centre[None]).astype(np.int64),
                simplify_px=simplify, image_border=border)
            if geometry_z is None:
                continue
            planes[str(plane)] = geometry_z
            area = float(shapely_shape(geometry_z).area)
        rows.append({"z": plane, "area_px2": round(area, 1),
                     **({"presence": round(score, 3)} if score is not None else {}),
                     **({"area_vs_previous": round(area / previous_area, 3)}
                        if previous_area else {})})
        previous_area = area
    timing["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
    from plexora.vision import sam_video

    model = ({"name": sam_video.MODEL_NAME, "version": sam_video.MODEL_VERSION,
              "license": sam_video.MODEL_LICENSE, "device": sam_video.used_device()}
             if engine == "sam2" else
             {"name": sam_weights.MODEL_NAME, "version": sam_weights.MODEL_VERSION,
              "license": sam_weights.MODEL_LICENSE})
    zs = sorted(int(k) for k in planes)
    return {
        "planes": planes,
        "start": int(z),
        "range": [zs[0], zs[-1]] if zs else None,
        "per_plane": rows,
        "stopped": {"increasing_z": stopped.get("increasing"),
                    "decreasing_z": stopped.get("decreasing")},
        "engine": engine,
        "level": level,
        "capped_view": capped,
        "crop": {"x": lx0 * fx, "y": ly0 * fy, "width": (lx1 - lx0) * fx,
                 "height": (ly1 - ly0) * fy},
        "timing": timing,
        "provenance": {"method": "sam", "engine": engine, "model": model, "view": token,
                       "t": int(t), "planes_visited": [first, last],
                       "guards": {"iou_min": IOU_MIN, "area": [AREA_MIN, AREA_MAX],
                                  "drift_radii": DRIFT_MAX, "fade_min": FADE_MIN}},
        "channels": [{"name": c["name"], "range": c["window"]} for c in specs],
    }
