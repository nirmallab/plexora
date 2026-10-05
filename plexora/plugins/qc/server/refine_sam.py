"""The QC tracer's model-backed method: magic select inside the envelope.

For the artifacts a person outlines at a glance -- debris, a fold, an air
bubble, torn or lifted tissue, a blurred patch -- the classical tracer
(`refine.py`) works from statistics of one or two channels, and a large
object with an uneven inside comes back as a scatter of parts or misses its
edge (a blurred patch's focus map is coarse at its border). The
segmentation model (`plexora.vision.sam`) sees the composited picture and
outlines the object as one thing.

It never replaces the classical trace; it competes with it under guards:

- the model's outline is cut to the envelope the agent judged (never outside
  what was asked about), and most of it must lie inside it (`inside_min`);
- it must not simply be the envelope back (`full_fraction`) -- an outline
  that fills its box says nothing the envelope did not -- nor a speck of it
  (`min_fill`, `min_object_um2`: one cell is not an artifact);
- it must cover the candidate's peak, when there is one;
- when the classical trace succeeded, the two must agree (`agree_min`): the
  model sharpens an outline the statistics already found -- a dark patch's
  edge, debris as one piece -- and never overrules them on a shape it reads
  badly (a thin diagonal fold inside a box is one: measured IoU with truth
  0.31 against the classical 0.72, so it stays classical). When the
  classical trace fell back, the model's outline stands on its own guards --
  except where `SAM_CLASS` says the class needs the classical trace (a
  blurred patch: soft edges, so the model never outlines one alone).

Pass, and the region is written with `method: "sam"` and the classical
result kept as the alternative in its record; fail, and the classical result
stands exactly as it would have without this module. Without the model
(weights missing, `PLEXORA_SEGMENT=0`, a test without a backend) every path
is the classical one.

Two phases, because the reader shelf's lock is exclusive per image: `prepare`
reads the crop inside the lock (with the classical trace), `run` infers after
it is released. `trace()` does both for a caller holding nothing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from plexora.plugins.qc.server import refine

#: The artifact classes the model traces: physical objects with an edge, and
#: a blurred patch (its edge is where the texture goes soft, which the model
#: sees; the agreement guard still holds it to the classical focus trace).
SAM_CLASSES = ("debris_or_foreign_object", "tissue_fold", "air_bubble_or_coverslip",
               "tissue_damage_or_detachment", "out_of_focus")

#: [cal] Per class, what differs from `SAM`. A blurred patch has a soft edge:
#: a box round part of one comes back as most of the box (bench `blur`, an
#: edge envelope: agreement 0.52 with the focus trace at twice its area,
#: where a whole patch agrees at 0.94), so the model only sharpens a focus
#: trace that succeeded, and must agree with it closely.
SAM_CLASS = {"out_of_focus": {"agree_min": 0.75, "needs_classical": True}}

#: [cal] the guards, and the margin read round the envelope.
SAM = {"inside_min": 0.6, "full_fraction": 0.90, "min_fill": 0.01, "min_object_um2": 100.0, "margin_frac": 0.10, "margin_um": 20.0,
       "min_area_um2": 25.0, "agree_min": 0.5}

#: Channel colours of the picture the model is shown, in reading order.
_COLOURS = ({"r": 255, "g": 255, "b": 255}, {"r": 0, "g": 255, "b": 0},
            {"r": 255, "g": 0, "b": 255}, {"r": 0, "g": 255, "b": 255})


def available() -> bool:
    try:
        from plexora.vision import sam
    except Exception:  # pragma: no cover - a core package
        return False
    return sam.available()


def eligible(klass) -> bool:
    return klass in SAM_CLASSES and available()


def model_version():
    """Part of every trace-cache key: new weights never serve an old outline."""
    if not available():
        return None
    from plexora.vision import sam_weights

    return sam_weights.MODEL_VERSION


@dataclass
class SamJob:
    rgb: np.ndarray
    origin: tuple[int, int]
    factor: tuple[float, float]
    image_size: tuple[int, int]
    envelope: dict
    point: tuple[float, float] | None   # crop pixels
    box: tuple[float, float, float, float]   # crop pixels
    pixel_um: float | None
    channels: list
    klass: str
    peak_full: tuple[float, float] | None
    read_s: float


def _windows(scan, name):
    summary = scan.channel(name).get("summary") or {}
    high = float(summary.get("p999_tissue") or 0.0)
    low = float(summary.get("p50_off_tissue") or 0.0)
    if not high > low:
        low, high = 0.0, max(1.0, high)
    return [low, high]


def prepare(candidate, envelope_mask, scan, source, *, pixel_um, envelope,
            image_size, any_class=False, hint=None) -> SamJob | None:
    """Read the model's crop: inside the reader lock. None when not eligible
    (`any_class` lifts the class rule -- `refine_qc_roi(method: sam)`)."""
    from shapely.geometry import Point, shape

    from plexora.server.utils import source_image
    from plexora.vision import segment

    about = refine.describe(candidate, scan)
    klass = about["class"]
    if not envelope or not (eligible(klass) or (any_class and available())):
        return None
    method = refine.method_for(klass)
    channels, _note = refine._channels(method, about, scan, np.asarray(envelope_mask, bool))
    if channels is None and any_class:
        usable = refine._usable(scan)
        channels = [refine._primary(about, usable)] if usable else []
    if not channels and not scan.meta.get("brightfield"):
        return None
    started = time.perf_counter()
    polygon = shape(envelope)
    x0, y0, x1, y1 = polygon.bounds
    um = float(pixel_um) if pixel_um else None
    grow = max(SAM["margin_frac"] * max(x1 - x0, y1 - y0),
               (SAM["margin_um"] / um) if um else 0.0)
    view = segment.clamp_view((x0 - grow, y0 - grow, x1 + grow, y1 + grow), image_size)
    peak = about.get("peak")
    peak_full = None
    if peak and polygon.buffer(1.0).contains(Point(float(peak[0]), float(peak[1]))):
        peak_full = (float(peak[0]), float(peak[1]))
    positives = np.array([peak_full]) if peak_full else np.zeros((0, 2))
    level, lbox, factor, _capped = segment.choose_crop(source, image_size, view, positives)
    specs = []
    if not source.is_brightfield:
        for i, name in enumerate(channels[:4]):
            key = scan.channel(name).get("key")
            if not key:
                continue
            specs.append({"key": key, "color": _COLOURS[i % len(_COLOURS)],
                          "window": _windows(scan, name), "visible": True})
        if not specs:
            return None
    rgb, _rendered, _bg = source_image.composite(source, specs, level, lbox)
    fx, fy = factor
    to_crop = lambda x, y: (x / fx - lbox[0], y / fy - lbox[1])  # noqa: E731
    point = to_crop(*peak_full) if peak_full else None
    bx0, by0 = to_crop(x0, y0)
    bx1, by1 = to_crop(x1, y1)
    if point is None and hint:
        # No peak, but the classical trace found the artifact: a point well
        # inside what it found is where the object is.
        inner = shape(hint).buffer(0)
        if not inner.is_empty:
            spot = inner.representative_point()
            point = to_crop(spot.x, spot.y)
    if point is None:
        # No peak and no trace (a region drawn by hand): the most unusual
        # place inside the envelope -- furthest from its typical brightness,
        # blurred at a fraction of its size so one bright cell cannot win.
        # A loose box alone comes back as the box.
        point = _salient(rgb, envelope, (lbox[0], lbox[1]), factor, scan=scan)
    return SamJob(rgb=np.ascontiguousarray(rgb), origin=(lbox[0], lbox[1]), factor=factor,
                  image_size=tuple(image_size), envelope=envelope, point=point,
                  box=(bx0, by0, bx1, by1), pixel_um=um, channels=list(channels[:4]),
                  klass=klass, peak_full=peak_full,
                  read_s=round(time.perf_counter() - started, 4))


def _judge(prediction, job, envelope_px, theirs):
    """(guards, first failed guard or None, mask cut to the envelope)."""
    mask = prediction.mask & envelope_px
    area = int(prediction.mask.sum()) or 1
    inside = float(mask.sum()) / area
    fill = float(mask.sum()) / (int(envelope_px.sum()) or 1)
    guards = {"inside_envelope": {"ok": inside >= SAM["inside_min"], "value": round(inside, 4)},
              "fills_envelope": {"ok": SAM["min_fill"] <= fill <= SAM["full_fraction"],
                                 "value": round(fill, 4)}}
    if job.pixel_um:
        # Not one cell: an artifact the model outlines is bigger than that.
        um2 = float(mask.sum()) * job.factor[0] * job.factor[1] * job.pixel_um ** 2
        guards["object_size"] = {"ok": um2 >= SAM["min_object_um2"], "value": round(um2, 1)}
    if job.point:
        r = int(np.clip(round(job.point[1]), 0, mask.shape[0] - 1))
        c = int(np.clip(round(job.point[0]), 0, mask.shape[1] - 1))
        guards["peak"] = {"ok": bool(mask[r, c])}
    if theirs is not None:
        both = int((mask | theirs).sum()) or 1
        agree = float((mask & theirs).sum()) / both
        bar = SAM_CLASS.get(job.klass, {}).get("agree_min", SAM["agree_min"])
        guards["agrees_with_classical"] = {"ok": agree >= bar, "value": round(agree, 4),
                                           "min": bar}
    failed = next((name for name, g in guards.items() if not g["ok"]), None)
    return guards, failed, mask


def _salient(rgb, envelope, origin, factor, scan=None):
    """(x, y) crop pixel of the strongest deviation inside the envelope --
    on tissue, when the scan says where tissue is (a loose region that runs
    onto glass would otherwise find the glass)."""
    import cv2
    from shapely.geometry import shape

    polygon = shape(envelope)
    bx0, by0, bx1, by1 = polygon.bounds
    side = min(bx1 - bx0, by1 - by0) / max(factor)
    lum = cv2.GaussianBlur(rgb.astype(np.float32).mean(axis=2), (0, 0),
                           max(2.0, 0.06 * side))
    inside = np.zeros(lum.shape, np.uint8)
    for part in (polygon.geoms if polygon.geom_type == "MultiPolygon" else [polygon]):
        ring = np.asarray(part.exterior.coords) / list(factor) - list(origin)
        cv2.fillPoly(inside, [np.round(ring - 0.5).astype(np.int32)], 1)
    where = inside.astype(bool)
    if scan is not None:
        try:
            from scipy import ndimage

            # One map cell in from the tissue's edge: the map is coarse, and
            # its edge cells hold glass, which is the strongest "deviation".
            tissue = ndimage.binary_erosion(np.asarray(scan.tissue(), bool), iterations=1)
            cell = float(scan.grid["cell_full_px"])
            ny, nx = tissue.shape
            rows = (((np.arange(lum.shape[0]) + origin[1] + 0.5) * factor[1]) // cell)
            cols = (((np.arange(lum.shape[1]) + origin[0] + 0.5) * factor[0]) // cell)
            on = tissue[np.ix_(rows.astype(int).clip(0, ny - 1), cols.astype(int).clip(0, nx - 1))]
            if (where & on).any():
                where &= on
        except Exception:  # noqa: BLE001 -- no tissue map: the whole envelope
            pass
    if not where.any():
        return None
    deviation = np.abs(lum - float(np.median(lum[where])))
    deviation[~where] = -1.0
    row, col = np.unravel_index(int(np.argmax(deviation)), deviation.shape)
    return (float(col), float(row))


def _rasterise(geometry, job: SamJob, shape_hw):
    import cv2
    from shapely.geometry import shape

    out = np.zeros(shape_hw, np.uint8)
    polygon = shape(geometry)
    fx, fy = job.factor
    ox, oy = job.origin
    parts = polygon.geoms if polygon.geom_type == "MultiPolygon" else [polygon]
    for part in parts:
        ring = np.asarray(part.exterior.coords) / [fx, fy] - [ox, oy]
        cv2.fillPoly(out, [np.round(ring - 0.5).astype(np.int32)], 1)
        for hole in part.interiors:
            inner = np.asarray(hole.coords) / [fx, fy] - [ox, oy]
            cv2.fillPoly(out, [np.round(inner - 0.5).astype(np.int32)], 0)
    return out.astype(bool)


def run(job: SamJob | None, classical: refine.RefinementResult) -> refine.RefinementResult:
    """Infer (outside the lock); the model's outline when it passes its
    guards, else `classical` unchanged. Never raises."""
    if job is None:
        return classical
    from plexora.plugins.qc.server import polygons
    from plexora.vision import sam, sam_weights
    from plexora.vision import segment as segmenter

    started = time.perf_counter()
    alternative = {"method": classical.method, "status": classical.status,
                   "reason": classical.reason, "kept_fraction": classical.kept_fraction}
    try:
        embedding = sam.embed(job.rgb)
        # Box with the point, and (when there is a point) the point alone: one
        # decoder call. A box much looser than the object comes back as the
        # box; a point on a compact object comes back as the object.
        prompts = [([job.point] if job.point else np.zeros((0, 2)),
                    [1] if job.point else [], job.box)]
        if job.point:
            prompts.append(([job.point], [1], None))
        predictions = sam.predict_batch(embedding, prompts, max_fraction=0.9)
    except Exception as exc:  # noqa: BLE001 -- the classical trace is always there
        classical.params["sam"] = {"status": "failed", "reason": str(exc)[:200]}
        return classical
    envelope_px = _rasterise(job.envelope, job, predictions[0].mask.shape)
    theirs = _rasterise(classical.geometry, job, envelope_px.shape) \
        if classical.refined and classical.geometry else None
    judged = [(_judge(p, job, envelope_px, theirs), p, kind)
              for p, kind in zip(predictions, ("box+point" if job.point else "box", "point"))]
    passing = [j for j in judged if j[0][1] is None]
    if not passing:
        guards, failed, _mask = judged[0][0]
        classical.params["sam"] = {"status": "rejected", "guard": failed, "guards": guards,
                                   "tried": [j[2] for j in judged]}
        return classical
    # The surest outline; on a tie, the tighter one.
    (guards, _failed, mask), prediction, kind = max(
        passing, key=lambda j: (round(j[1].iou, 3), -int(j[0][2].sum())))
    params = {"model": sam_weights.MODEL_NAME, "model_version": sam_weights.MODEL_VERSION,
              "prompt": {"point": bool(job.point), "box": kind != "point", "kind": kind},
              "iou": round(prediction.iou, 4), "alternative": alternative,
              "channels": job.channels}
    um = job.pixel_um
    min_area_full = SAM["min_area_um2"] / (um * um) if um else 0.0
    image_border = (job.origin[0] <= 0, job.origin[1] <= 0, True, True)
    geometry, flags, _cleaned = segmenter.outline(
        mask, origin=job.origin, factor=job.factor, image_size=job.image_size,
        multi=True, min_area_px=min_area_full, image_border=image_border,
        simplify_px=refine.REFINE["simplify_px"] * max(job.factor))
    if geometry is None:
        classical.params["sam"] = {"status": "rejected", "guard": "empty", "guards": guards}
        return classical
    try:
        from plexora.plugins.roi.server import geometry as roi_geometry

        geometry = polygons.clip_to(roi_geometry.validate_geometry(geometry), job.envelope)
    except Exception as exc:  # noqa: BLE001
        classical.params["sam"] = {"status": "rejected", "guard": f"invalid outline: {exc}"}
        return classical
    if geometry is None:
        classical.params["sam"] = {"status": "rejected", "guard": "empty inside the envelope"}
        return classical
    area = polygons.area_of(geometry)
    envelope_area = classical.envelope_area_px2 or polygons.area_of(job.envelope) or 1.0
    px_area = (um * um) if um else None
    result = refine.RefinementResult(
        status="refined", reason="outlined by the segmentation model inside the envelope",
        method="sam", geometry=geometry, level=None, factor=float(max(job.factor)),
        refine_um=(um * max(job.factor)) if um else None,
        area_px2=area, area_um2=area * px_area if px_area else None,
        envelope_area_px2=envelope_area,
        envelope_area_um2=envelope_area * px_area if px_area else None,
        kept_fraction=area / envelope_area if envelope_area else 1.0,
        parts=1 if geometry.get("type") == "Polygon" else len(geometry.get("coordinates") or []),
        channels=job.channels, params={**classical.params, **params, "method": "sam"},
        guards=guards,
        timing={"read_s": job.read_s, "infer_s": round(time.perf_counter() - started, 4)})
    return result


#: [cal] An Artifact Detector object is read inside its own outline grown by
#: this share of its radius (at least `SAM["margin_um"]`): room for the model
#: to find an edge the detector stopped short of, and to stay under the
#: `full_fraction` guard -- an envelope the object fills is refused as "the
#: whole box".
OBJECT_PAD = 0.35


def refine_object(candidate, envelope_mask, scan, image_data, *, outline, pixel_um,
                  image_size=None) -> refine.RefinementResult:
    """An Artifact Detector region's outline, sharpened by the model. The
    detector's own outline stands as the classical trace: the model's is kept
    only when it passes the same guards, agreement with that outline among
    them. Without the model, or for a class it does not trace, the
    detector's outline comes back unchanged (status `refined`, method
    `artifact_detector`)."""
    import math

    from shapely.geometry import mapping, shape

    from plexora.plugins.qc.server import polygons
    from plexora.server.utils import source_image

    area = polygons.area_of(outline) or 1.0
    classical = refine.RefinementResult(
        status="refined", reason="the detector's own traced outline is the region",
        method="artifact_detector", geometry=outline, area_px2=area, envelope_area_px2=area,
        kept_fraction=1.0)
    klass = refine.describe(candidate, scan)["class"]
    if not eligible(klass):
        return classical
    pad = math.sqrt(area / math.pi) * OBJECT_PAD
    if pixel_um:
        pad = max(pad, SAM["margin_um"] / float(pixel_um))
    envelope = mapping(shape(outline).buffer(pad))
    with source_image.SHELF.reader(image_data) as source:
        size = image_size
        if size is None:
            full_h, full_w = source.level_shape(0)
            size = (int(full_w), int(full_h))
        try:
            job = prepare(candidate, envelope_mask, scan, source, pixel_um=pixel_um,
                          envelope=envelope, image_size=size, hint=outline)
        except Exception as exc:  # noqa: BLE001 -- the detector's outline stands
            classical.params["sam"] = {"status": "failed", "reason": str(exc)[:200]}
            return classical
    return run(job, classical)


def redraw(candidate, envelope_mask, scan, image_data, *, pixel_um, envelope, options=None,
           image_size=None) -> refine.RefinementResult:
    """The model's outline because the agent asked for it (an outline it
    judged badly drawn): any class, held to every guard except agreement with
    the classical trace it just rejected. The classical trace comes back,
    with why in `params["sam"]`, when the model is absent or its outline
    fails a guard."""
    import dataclasses

    from plexora.server.utils import source_image

    with source_image.SHELF.reader(image_data) as source:
        classical = refine.refine(candidate, envelope_mask, scan, source, pixel_um=pixel_um,
                                  envelope=envelope, options=options)
        size = image_size
        if size is None:
            full_h, full_w = source.level_shape(0)
            size = (int(full_w), int(full_h))
        try:
            job = prepare(candidate, envelope_mask, scan, source, pixel_um=pixel_um,
                          envelope=envelope, image_size=size, any_class=True)
        except Exception as exc:  # noqa: BLE001 -- the classical trace stands
            classical.params["sam"] = {"status": "failed", "reason": str(exc)[:200]}
            return classical
    if job is None:
        classical.params["sam"] = {"status": "not_applicable",
                                   "reason": "the segmentation model is not installed here"}
        return classical
    # Unbound from the trace the agent rejected: no agreement guard against it.
    result = run(job, dataclasses.replace(classical, status="fallback"))
    if result.method != "sam":
        return dataclasses.replace(classical, params=result.params)
    result.params["requested_by"] = "agent"
    return result


def trace(candidate, envelope_mask, scan, image_data, *, pixel_um, envelope, options=None,
          image_size=None) -> refine.RefinementResult:
    """The classical trace and, for an eligible class, the model's -- read
    inside the reader lock, inferred outside it. What `refine.refine` would
    return when the model is absent or its outline fails a guard."""
    from plexora.server.utils import source_image

    with source_image.SHELF.reader(image_data) as source:
        classical = refine.refine(candidate, envelope_mask, scan, source, pixel_um=pixel_um,
                                  envelope=envelope, options=options)
        job = None
        klass = refine.describe(candidate, scan)["class"]
        wanted = ("refined",) if SAM_CLASS.get(klass, {}).get("needs_classical") \
            else ("refined", "fallback")
        if classical.status in wanted and eligible(klass):
            size = image_size
            if size is None:
                full_h, full_w = source.level_shape(0)
                size = (int(full_w), int(full_h))
            try:
                job = prepare(candidate, envelope_mask, scan, source, pixel_um=pixel_um,
                              envelope=envelope, image_size=size,
                              hint=classical.geometry if classical.refined else None)
            except Exception as exc:  # noqa: BLE001 -- the classical trace stands
                classical.params["sam"] = {"status": "failed", "reason": str(exc)[:200]}
                job = None
    return run(job, classical)
