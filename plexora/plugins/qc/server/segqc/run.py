"""Running Segmentation QC: sizing, the tile loop, the cache, the stored result.

The inputs are fingerprinted -- the image (path or node resource, size and
mtime), the mask (the same, and its scale), the DNA channel, the parameters
and `VERSION` -- and a result with that fingerprint is reused, from memory
or from `<QC store>/segqc/<fp>.parquet|.json`. Changing what is visible never
reaches here. The result is a per-cell table and a summary; the mask is
never written.

The level is chosen from the nuclei: a sample of level-0 blocks gives the
nuclear scale by scale selection on the DNA (and the median label area,
which still bounds a tile's halo), and the analysis runs at the coarsest
level where a nucleus is still at least `MIN_DIAMETER_PX` across (and, with
a pixel size, no coarser than 1 µm per pixel) -- nuclei stay nuclei, and a
1M-cell slide is read once at a fraction of its full-resolution pixels. A
sample with too few DNA peaks falls back to the labels' size, and says so
(`scale_method`).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from time import perf_counter

import numpy as np

from plexora.agent.errors import AgentError
from plexora.plugins.qc.server.segqc import analysis, kernels

VERSION = "2"
TILE_PX = 2048
SAMPLE_PX = 512
#: The smallest a nucleus may be at the run level (px); a label, when the
#: scale falls back to the labels' size.
MIN_DIAMETER_PX = 5.0
MIN_LABEL_DIAMETER_PX = 8.0
#: The nuclear scale is measured at the coarsest level where a label is
#: still this wide (px).
SIZING_DIAMETER_PX = 16.0
#: Under this share of the DNA peaks on a label, the mask is likely not of
#: nuclei or cells round them (a ring or cytoplasm mask): the summary says so.
PEAKS_ON_LABELS_MIN_PCT = 50.0
MAX_UM_PER_PX = 1.0
PARAMS_DEFAULT = {"flag": 0.6, "unsure": 0.4, "robust_ratio": 0.25}
PHASES = ("Detecting DNA peaks", "Mapping peaks to cells", "Scoring neighborhoods",
          "Finalizing overlays")
#: The overlay colours of the two findings.
COLORS = {"under": "#f97316", "over": "#a855f7"}
#: A slide whose label arrays would pass this is refused (too_large).
MAX_LABELS = 60_000_000

_GUARD = threading.Lock()
_MEMORY: dict = {}


# -- where it lives --------------------------------------------------------------------


def _folder(project):
    from plexora import api

    path = api.store(project, "qc").directory() / "segqc"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_write(path, data: bytes):
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def settings(project) -> dict:
    return _read_json(_folder(project) / "settings.json") or {}


def save_settings(project, **values):
    merged = {**settings(project), **values}
    _atomic_write(_folder(project) / "settings.json",
                  json.dumps(merged, sort_keys=True).encode("utf-8"))
    return merged


# -- inputs and their fingerprint --------------------------------------------------------


def _stamp(path):
    if not path:
        return None
    try:
        stat = os.stat(path)
        return f"{stat.st_size}-{stat.st_mtime_ns}"
    except OSError:
        return None


def mask_identity(record) -> dict:
    seg = record.segmentation
    binding = record.resources.get("segmentation")
    if binding is not None:
        where = f"node://{binding.node}/{binding.resource_id}"
        stamp = None
    else:
        where = str(seg.derived) if seg.derived else None
        stamp = _stamp(seg.derived)
    return {"where": where, "stamp": stamp, "scale": int(getattr(seg, "scale", 1) or 1),
            "mode": getattr(seg, "mode", None)}


def channel_names(record) -> list:
    return [c.get("fullname") or c.get("name") for c in record.image.real_channels]


def detected_dna(names):
    from plexora.agent import presets

    found = presets.nuclear_channels(names)
    return found[0] if found else None


def plan(session, project, *, dna_channel=None, params=None) -> tuple:
    """(fingerprint, context) of a run, without reading a pixel."""
    from plexora.agent.render import resolve_channel
    from plexora.server.utils import pixel_scale, source_image

    record = session.project(project)
    if record.image.is_blank:
        raise AgentError("unsupported_modality", "this sample has no image to check")
    seg = record.segmentation
    if not seg.available:
        raise AgentError("precondition_missing",
                         "the segmentation mask is still being prepared" if seg.pending
                         else "Segmentation QC needs a segmentation mask: add one first",
                         detail={"requires": ["segmentation"]})
    names = channel_names(record)
    dna = dna_channel or settings(project).get("dna_channel")
    if dna and dna not in names:
        dna = None
    dna = dna or detected_dna(names)
    if not dna:
        raise AgentError("precondition_missing",
                         "no DAPI / DNA / Hoechst channel was found: name the DNA channel "
                         "(dna_channel)", detail={"channels": names[:100]})
    _index, found = resolve_channel(dna, list(record.image.real_channels))
    key = source_image.channel_key(found)
    image_data = session.image_data(project)
    identity = source_image.ReaderShelf.identity_of(image_data)
    image_stamp = None if str(identity or "").startswith("node://") else _stamp(identity)
    mask = mask_identity(record)
    params = {**PARAMS_DEFAULT, **(params or {})}
    pixel = pixel_scale.pixel_size(record)
    pixel_um = float(pixel["value"]) if pixel else None
    blob = json.dumps({"v": VERSION, "image": identity, "image_stamp": image_stamp,
                       "mask": mask, "dna": key, "params": params, "pixel_um": pixel_um},
                      sort_keys=True)
    fp = hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]
    return fp, {"record": record, "image_data": image_data, "identity": identity,
                "image_stamp": image_stamp, "mask": mask, "dna": dna, "key": key,
                "params": params, "pixel_um": pixel_um, "names": names}


# -- reading ---------------------------------------------------------------------------------


def _labels(provider, level, box):
    labels = np.asarray(provider.read_region(level, box, max_pixels=0))
    if labels.ndim > 2:
        labels = labels.reshape(labels.shape[-2:])
    return labels.astype(np.uint32, copy=False)


def _sample_boxes(height, width, size=SAMPLE_PX, rows=3, cols=4):
    """A deterministic 4 x 3 grid of level-0 boxes over the image."""
    boxes = []
    for r in range(rows):
        for c in range(cols):
            cy = int((r + 0.5) * height / rows)
            cx = int((c + 0.5) * width / cols)
            x0 = max(0, min(width - size, cx - size // 2))
            y0 = max(0, min(height - size, cy - size // 2))
            boxes.append((x0, y0, min(width, x0 + size), min(height, y0 + size)))
    return boxes


def size_labels(provider, extra, height, width):
    """Median level-0 area of the labels that lie wholly inside the sample
    blocks, and the blocks that had labels in them."""
    areas, used = [], []
    for box in _sample_boxes(height, width):
        labels = _labels(provider, extra, box)
        if not labels.size or (labels > 0).mean() < 0.05:
            continue
        edge = np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]))
        present, counts = np.unique(labels[labels > 0], return_counts=True)
        inside = ~np.isin(present, edge)
        if inside.any():
            areas.append(counts[inside])
            used.append(box)
    if not areas:
        return None, []
    return float(np.median(np.concatenate(areas))), used


def size_nuclei(source, index, provider, extra, boxes, level):
    """(sigma, n labels voting, histogram) of the nuclei, in level-0 pixels,
    by scale selection on the DNA of the sample blocks read at `level`
    (`analysis.nuclear_sigma`)."""
    factor = 2 ** level
    blocks = []
    for box in boxes:
        lbox = tuple(int(v // factor) for v in box)
        labels = _labels(provider, level + extra, lbox)
        plane, _clip = source.read(index, level, lbox)
        plane = np.asarray(plane)[:labels.shape[0], :labels.shape[1]]
        blocks.append((plane, labels[:plane.shape[0], :plane.shape[1]]))
    sigma, voters, histogram = analysis.nuclear_sigma(blocks)
    return (sigma * factor if sigma is not None else None), voters, histogram


def choose_level(source, d_full, pixel_um, min_px=MIN_DIAMETER_PX):
    level = 0
    while level + 1 < max(1, source.levels):
        nxt = level + 1
        if d_full / (2 ** nxt) < min_px:
            break
        if pixel_um and pixel_um * (2 ** nxt) > MAX_UM_PER_PX:
            break
        level = nxt
    return level


def scale_of(d_nuc, d_label):
    """(sigma, radius, fine, halo) at the run level, from the nucleus and the
    label diameters there: the nuclear DoG scale, the peak suppression radius
    (half a nucleus, which merges chromatin texture into one peak), the fine
    plane's scale, and a halo that holds a label's peaks and every filter's
    reach (the widest being the coarse blob detector and its inner test)."""
    sigma = float(np.clip(0.3 * d_nuc, 1.5, 6.0))
    radius = max(2.0, 0.5 * d_nuc)
    fine = max(1.0, 0.5 * sigma)
    widest = max(analysis.COARSE) * sigma
    reach = 3 * 1.6 * widest + 1.28 * np.sqrt(2.0) * widest
    halo = int(np.ceil(max(16.0, 2.5 * d_label, reach)))
    return sigma, radius, fine, halo


def tiles(height, width, tile=TILE_PX):
    for y0 in range(0, height, tile):
        for x0 in range(0, width, tile):
            yield (x0, y0, min(width, x0 + tile), min(height, y0 + tile))


class _Grow:
    """Per-label arrays that grow with the largest label seen."""

    FLOAT = ("count", "sum_dna", "sum_y", "sum_x", "perimeter", "max_dna", "excess", "p1",
             "p2", "y1", "x1", "sep2", "depth2")

    def __init__(self, capacity=1024):
        self.capacity = 0
        self.arrays = {}
        self.n_peaks = np.zeros(0, dtype=np.int32)
        self.ensure(capacity)

    def ensure(self, size):
        if size <= self.capacity:
            return
        if size > MAX_LABELS:
            raise AgentError("too_large", f"the mask has labels up to {size - 1:,}; Segmentation "
                             f"QC handles up to {MAX_LABELS:,}")
        new = max(size, int(self.capacity * 1.5) + 1)
        for name in self.FLOAT:
            grown = np.zeros(new, dtype=np.float64)
            if name in self.arrays:
                grown[:self.capacity] = self.arrays[name]
            self.arrays[name] = grown
        grown = np.zeros(new, dtype=np.int32)
        grown[:self.capacity] = self.n_peaks
        self.n_peaks = grown
        self.capacity = new


# -- the run ---------------------------------------------------------------------------------


def run(session, project, fp, context, *, progress=None, check_cancelled=None) -> dict:
    from plexora.agent import render
    from plexora.server.utils import source_image

    def say(done, total, message):
        if progress is not None:
            progress(done=done, total=total, message=message)

    def stop_if_asked():
        if check_cancelled is not None:
            check_cancelled()

    record = context["record"]
    timing = {}
    started = perf_counter()
    provider, status, reason, _locator = render._mask_for(record)
    if provider is None:
        raise AgentError("resource_unavailable", reason or f"the mask is {status}")
    extra = int(getattr(record.segmentation, "extra_levels", 0) or 0)
    params = context["params"]
    with source_image.SHELF.reader(context["image_data"]) as source:
        if source.is_brightfield:
            raise AgentError("unsupported_modality",
                             "a brightfield image has no DNA channel to check a mask against")
        index = source.channel_index(context["key"])
        if index is None:
            raise AgentError("invalid_input", f"{context['dna']!r} is not a channel of this image")
        height0, width0 = source.level_shape(0)
        try:
            area0, sample = size_labels(provider, extra, height0, width0)
        except Exception as exc:
            raise AgentError("resource_unavailable", f"the mask could not be read ({exc})") \
                from exc
        if area0 is None:
            raise AgentError("precondition_missing",
                             "the mask has no complete labels in the sampled blocks: is it "
                             "empty, or not a label mask?")
        d_full = 2.0 * np.sqrt(area0 / np.pi)
        # The scale is chosen where a label is still SIZING_DIAMETER_PX
        # across: finer, chromatin texture is most of what a DoG sees.
        sizing_level = choose_level(source, d_full, context["pixel_um"], SIZING_DIAMETER_PX)
        sigma0, scale_peaks, scale_histogram = size_nuclei(source, index, provider, extra,
                                                           sample, sizing_level)
        if sigma0 is not None:
            scale_method = "dna"
            d_nuc_full = sigma0 / 0.3
            level = choose_level(source, d_nuc_full, context["pixel_um"])
        else:
            scale_method = "labels"
            d_nuc_full = d_full
            level = choose_level(source, d_full, context["pixel_um"], MIN_LABEL_DIAMETER_PX)
        factor = 2 ** level
        d_level = d_full / factor
        d_nuc = d_nuc_full / factor
        sigma, radius, fine, halo = scale_of(d_nuc, d_level)
        strengths = []
        for box in sample:
            lbox = tuple(int(v // factor) for v in box)
            plane, _clip = source.read(index, level, lbox)
            sample_labels = _labels(provider, level + extra, lbox)
            plane = np.asarray(plane)[:sample_labels.shape[0], :sample_labels.shape[1]]
            dog = analysis.smooth_planes(plane, sigma)[1]
            sample_labels = sample_labels[:dog.shape[0], :dog.shape[1]]
            strengths.append(analysis.tile_peaks(dog, sample_labels, 1e-9, radius)[2])
        tau = analysis.noise_floor(strengths)
        height, width = source.level_shape(level)
        boxes = list(tiles(height, width))
        total = len(boxes) + 3
        timing["sizing_s"] = round(perf_counter() - started, 3)
        grow = _Grow()
        edges = analysis.EdgeAccumulator()
        n_peaks_total = n_peaks_all = n_peaks_on = 0
        blobs = ([], [], [])
        t0 = perf_counter()
        for done, (x0, y0, x1, y1) in enumerate(boxes):
            stop_if_asked()
            say(done, total, PHASES[0])
            hx0, hy0 = max(0, x0 - halo), max(0, y0 - halo)
            hx1, hy1 = min(width, x1 + halo), min(height, y1 + halo)
            box = (hx0, hy0, hx1, hy1)
            try:
                labels = _labels(provider, level + extra, box)
            except Exception as exc:
                raise AgentError("resource_unavailable",
                                 f"the mask could not be read ({exc})") from exc
            top = int(labels.max()) if labels.size else 0
            if top == 0:
                continue
            dna, _clip = source.read(index, level, box)
            dna = np.asarray(dna)
            if dna.shape != labels.shape:
                fixed = np.zeros(labels.shape, dtype=dna.dtype)
                fixed[:dna.shape[0], :dna.shape[1]] = dna[:labels.shape[0], :labels.shape[1]]
                dna = fixed
            grow.ensure(top + 1)
            s, dog, coarse = analysis.tile_planes(dna, sigma, fine)
            del dna
            window = (y0 - hy0, y1 - hy0, x0 - hx0, x1 - hx0)
            a = grow.arrays
            kernels.label_stats(labels, s, hy0, hx0, *window,
                                analysis.tile_background(s, window), a["count"], a["sum_dna"],
                                a["sum_y"], a["sum_x"], a["perimeter"], a["max_dna"],
                                a["excess"])
            edges.add(*kernels.pairs(labels, s, hy0, hx0, window))
            by, bx, br = analysis.blob_candidates(coarse, dog, labels, tau, window)
            blobs[0].append(by + hy0)
            blobs[1].append(bx + hx0)
            blobs[2].append(br)
            del coarse
            ys, xs, strength = analysis.tile_peaks(dog, None, tau, radius)
            owned = (ys >= window[0]) & (ys < window[1]) & (xs >= window[2]) & (xs < window[3])
            on = labels[ys, xs] > 0
            n_peaks_all += int(owned.sum())
            n_peaks_on += int((owned & on).sum())
            ys, xs, strength = ys[on], xs[on], strength[on]
            peaks = analysis.label_peaks(ys, xs, strength, labels, s, params["robust_ratio"])
            if peaks["label"].size:
                py, px = peaks["y1"], peaks["x1"]
                owned = (py >= window[0]) & (py < window[1]) & (px >= window[2]) & \
                    (px < window[3])
                ids = peaks["label"][owned]
                grow.n_peaks[ids] = peaks["n"][owned]
                a["p1"][ids] = peaks["p1"][owned]
                a["p2"][ids] = peaks["p2"][owned]
                a["y1"][ids] = py[owned] + hy0
                a["x1"][ids] = px[owned] + hx0
                a["sep2"][ids] = peaks["sep2"][owned]
                a["depth2"][ids] = peaks["depth2"][owned]
                n_peaks_total += int(peaks["n"][owned].sum())
        timing["tiles_s"] = round(perf_counter() - t0, 3)
    stop_if_asked()
    say(len(boxes), total, PHASES[1])
    t0 = perf_counter()
    a = grow.arrays
    ids = np.flatnonzero(a["count"] > 0)
    count = a["count"][ids]
    cells = {"label": ids.astype(np.int64), "area": count,
             "mean_dna": a["sum_dna"][ids] / count, "cy": a["sum_y"][ids] / count,
             "cx": a["sum_x"][ids] / count, "perimeter": a["perimeter"][ids],
             "max_dna": a["max_dna"][ids], "excess": a["excess"][ids],
             "n_peaks": grow.n_peaks[ids], "p1": a["p1"][ids], "p2": a["p2"][ids],
             "sep2": a["sep2"][ids], "depth2": a["depth2"][ids]}
    no_peak = cells["p1"] <= 0
    cells["y1"] = np.where(no_peak, cells["cy"], a["y1"][ids])
    cells["x1"] = np.where(no_peak, cells["cx"], a["x1"][ids])
    del grow
    graph = edges.result()
    position = np.full(int(ids.max()) + 1 if ids.size else 1, -1, dtype=np.int64)
    position[ids] = np.arange(ids.size)

    def index_of(labels):
        labels = np.asarray(labels, dtype=np.int64)
        out = np.full(labels.shape, -1, dtype=np.int64)
        ok = labels < position.size
        out[ok] = position[labels[ok]]
        return out

    timing["graph_s"] = round(perf_counter() - t0, 3)
    stop_if_asked()
    say(len(boxes) + 1, total, PHASES[2])
    t0 = perf_counter()
    hood = analysis.context(cells["cy"], cells["cx"], cells["area"], cells["n_peaks"],
                            cells["p1"], cells["excess"])
    under = analysis.under_scores(cells, hood)
    over, partner = analysis.over_scores(cells, hood, graph, index_of, d_nuc)
    big, home, big_nuclei = analysis.big_scores(cells, hood, tuple(np.concatenate(b) if b else
                                                             np.zeros(0) for b in blobs))
    from_blob = big > over
    over = np.where(from_blob, big, over)
    partner = np.where(from_blob, home, partner)
    status = analysis.status_of(under, over, params["flag"], params["unsure"])
    timing["score_s"] = round(perf_counter() - t0, 3)
    stop_if_asked()
    say(len(boxes) + 2, total, PHASES[3])
    # The inputs must still be the ones measured: an image or mask swapped
    # mid-run is refused rather than stored under the old fingerprint.
    if mask_identity(record) != context["mask"] or (
            context["image_stamp"] is not None
            and _stamp(context["identity"]) != context["image_stamp"]):
        raise AgentError("conflict", "the image or the mask changed while Segmentation QC ran; "
                         "run it again", retryable=True)
    import polars as pl

    reason = np.full(status.shape, "", dtype=object)
    reason[status == analysis.UNDER] = "multiple_nuclei"
    reason[status == analysis.OVER] = "split_nucleus"
    reason[status == analysis.AMBIGUOUS] = "weak_evidence"
    area_full = cells["area"] * float(factor * factor)
    frame = pl.DataFrame({
        "cell_id": cells["label"], "under_score": under.astype(np.float32),
        "over_score": over.astype(np.float32), "status": status,
        "status_word": np.asarray(analysis.STATUS_WORDS, dtype=object)[status].tolist(),
        "reason": reason.tolist(), "n_peaks": cells["n_peaks"].astype(np.int16),
        "partner_id": partner, "area_px": area_full,
        # Where each cell is (full-resolution pixels) and how round, for the
        # density map; 4*pi*area / perimeter^2 at the level measured, 1 for a
        # disc and falling as the outline grows ragged or elongated.
        "cy": (cells["cy"] * float(factor)).astype(np.float32),
        "cx": (cells["cx"] * float(factor)).astype(np.float32),
        "circularity": np.clip(4.0 * np.pi * cells["area"]
                               / np.maximum(cells["perimeter"].astype(np.float64), 1.0) ** 2,
                               0.0, 1.5).astype(np.float32),
    })
    summary = summarize(frame, status, cells["area"])
    pixel_um = context["pixel_um"]
    on_labels = n_peaks_on
    peaks_seen = n_peaks_all
    peaks_on_labels_pct = round(100.0 * on_labels / peaks_seen, 1) if peaks_seen else None
    summary.update({
        "version": VERSION, "fingerprint": fp, "project": project,
        "dna_channel": context["dna"], "mask": context["mask"], "params": params,
        "level": int(level), "d_full_px": round(float(d_full), 2),
        "d_level_px": round(float(d_level), 2), "d_nucleus_px": round(float(d_nuc_full), 2),
        "d_nucleus_um": round(float(d_nuc_full) * pixel_um, 2) if pixel_um else None,
        "scale_method": scale_method, "scale_peaks": int(scale_peaks),
        "scale_histogram": scale_histogram, "sigma_px": round(sigma, 3),
        "fine_sigma_px": round(fine, 3), "tau": round(float(tau), 5), "tiles": len(boxes),
        "peaks": n_peaks_total, "peaks_on_labels_pct": peaks_on_labels_pct,
        "big_nuclei": int(big_nuclei), "edges": int(graph["a"].size), "pixel_um": pixel_um,
        "colors": dict(COLORS),
    })
    if peaks_on_labels_pct is not None and peaks_on_labels_pct < PEAKS_ON_LABELS_MIN_PCT:
        summary["notice"] = (f"Only {peaks_on_labels_pct:.0f}% of the DNA peaks fall on a "
                             "label: is this a cell-ring or cytoplasm mask? The calls "
                             "assume labels that hold their nuclei.")
    timing["total_s"] = round(perf_counter() - started, 3)
    summary["timing"] = timing
    return {"summary": summary, "frame": frame}


def summarize(frame, status, area) -> dict:
    n = int(status.size)
    total_area = float(area.sum()) or 1.0
    counts = {word: int((status == code).sum()) for code, word in enumerate(analysis.STATUS_WORDS)}

    def pct(mask):
        return round(100.0 * float(mask.sum()) / n, 2) if n else 0.0

    def pct_area(mask):
        return round(100.0 * float(area[mask].sum()) / total_area, 2)

    under, over, unsure = (status == analysis.UNDER), (status == analysis.OVER), \
        (status == analysis.AMBIGUOUS)
    return {"n_cells": n, "counts": counts,
            "pct_cells": {"under": pct(under), "over": pct(over), "ambiguous": pct(unsure)},
            "pct_area": {"under": pct_area(under), "over": pct_area(over),
                         "ambiguous": pct_area(unsure)},
            "denominators": {"cells": "labels in the mask", "area": "segmented (label) area"}}


# -- cache and store -------------------------------------------------------------------------


def _save(project, fp, result):
    import io

    from plexora.plugins.qc.server.results import now_iso

    folder = _folder(project)
    summary = {**result["summary"], "computed_at": now_iso()}
    buffer = io.BytesIO()
    result["frame"].write_parquet(buffer)
    _atomic_write(folder / f"{fp}.parquet", buffer.getvalue())
    _atomic_write(folder / f"{fp}.json", json.dumps(summary, default=str).encode("utf-8"))
    _atomic_write(folder / "current.json", json.dumps({"fingerprint": fp}).encode("utf-8"))
    _sweep(folder, keep=3)
    with _GUARD:
        _MEMORY[(project, fp)] = summary
        while len(_MEMORY) > 4:
            _MEMORY.pop(next(iter(_MEMORY)))
    return summary


def _sweep(folder, keep):
    metas = sorted((p for p in folder.glob("*.json")
                    if p.name not in ("current.json", "settings.json", "running.json")),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in metas[keep:]:
        for path in (stale, stale.with_suffix(".parquet")):
            try:
                path.unlink()
            except OSError:
                pass


def load_summary(project, fp):
    with _GUARD:
        held = _MEMORY.get((project, fp))
    if held is not None:
        return held
    folder = _folder(project)
    summary = _read_json(folder / f"{fp}.json")
    if not summary or summary.get("version") != VERSION \
            or not (folder / f"{fp}.parquet").is_file():
        return None
    with _GUARD:
        _MEMORY[(project, fp)] = summary
    return summary


def current_fingerprint(project):
    pointer = _read_json(_folder(project) / "current.json") or {}
    return pointer.get("fingerprint")


def current(project):
    """The summary of the last result stored, or None."""
    fp = current_fingerprint(project)
    return load_summary(project, fp) if fp else None


def frame(project, fp=None):
    """The per-cell table of a result (default the current one), or None."""
    import polars as pl

    fp = fp or current_fingerprint(project)
    if not fp:
        return None
    path = _folder(project) / f"{fp}.parquet"
    try:
        return pl.read_parquet(path)
    except Exception:
        return None


def clear(project) -> bool:
    folder = _folder(project)
    had = (folder / "current.json").is_file()
    try:
        (folder / "current.json").unlink()
    except OSError:
        pass
    return had


def load_or_run(session, project, *, dna_channel=None, params=None, force=False,
                progress=None, check_cancelled=None) -> tuple:
    """(summary, reused)."""
    fp, context = plan(session, project, dna_channel=dna_channel, params=params)
    if dna_channel:
        save_settings(project, dna_channel=context["dna"])
    if not force:
        summary = load_summary(project, fp)
        if summary is not None:
            _atomic_write(_folder(project) / "current.json",
                          json.dumps({"fingerprint": fp}).encode("utf-8"))
            return summary, True
    result = run(session, project, fp, context, progress=progress,
                 check_cancelled=check_cancelled)
    return _save(project, fp, result), False


def public_status(session, project) -> dict:
    """What the panel and an agent read: whether there is a mask, the DNA
    channel (chosen or detected), the current result and whether it is still
    the one these inputs would give (`stale`), and a running job."""
    record = session.project(project)
    seg = record.segmentation
    names = channel_names(record)
    chosen = settings(project).get("dna_channel")
    detected = detected_dna(names)
    out = {"available": bool(seg.available), "pending": bool(getattr(seg, "pending", False)),
           "dna_channel": chosen if chosen in names else detected, "detected": detected,
           "candidates": _candidates(names), "summary": None, "stale": False, "job": None}
    summary = current(project)
    if summary is not None:
        out["summary"] = summary
        if seg.available:
            try:
                fp, _context = plan(session, project, params=summary.get("params"))
                out["stale"] = fp != summary.get("fingerprint")
            except AgentError:
                out["stale"] = True
    running = _read_json(_folder(project) / "running.json") or {}
    if running.get("job_id"):
        from plexora.agent import jobs

        record_job = jobs.store().get(running["job_id"])
        if record_job and record_job.get("status") in ("queued", "running"):
            out["job"] = {k: record_job.get(k) for k in ("job_id", "status", "progress")}
    return out


def _candidates(names):
    from plexora.agent import presets

    return presets.nuclear_channels(names)


def note_running(project, job_id):
    path = _folder(project) / "running.json"
    if job_id:
        _atomic_write(path, json.dumps({"job_id": job_id}).encode("utf-8"))
    else:
        try:
            path.unlink()
        except OSError:
            pass


#: The range the panel's threshold sliders may ask for.
FLAG_RANGE = (0.3, 0.99)


def viewer_groups(project, *, include_ambiguous=False, flag_under=None,
                  flag_over=None) -> dict:
    """The flagged cells for the QC cell layer: `seg:under` and `seg:over`
    (and `seg:ambiguous` on request), in the shape `/plugins/qc/cells` sends.

    `flag_under` / `flag_over` re-threshold the stored scores for viewing
    (the panel's sliders), each side on its own: the ambiguous band keeps its
    width below each, and the counts and shares at those thresholds come back
    beside the groups. The stored calls, the export and a source write keep
    the run's own threshold."""
    summary = current(project)
    table = frame(project)
    if summary is None or table is None:
        return {"available": False, "groups": []}
    params = {**PARAMS_DEFAULT, **(summary.get("params") or {})}
    stored = float(params["flag"])
    ids = table["cell_id"]
    out = {"available": True, "fingerprint": summary.get("fingerprint"),
           "stored_flag": stored, "flag_under": stored, "flag_over": stored,
           # The largest label, so the panel can colour every cell of the mask
           # (its row's swatch) with one dense table.
           "max_id": int(ids.max()) if ids.len() else 0}
    if flag_under is not None or flag_over is not None:
        band = stored - float(params["unsure"])
        fu = stored if flag_under is None else float(np.clip(float(flag_under), *FLAG_RANGE))
        fo = stored if flag_over is None else float(np.clip(float(flag_over), *FLAG_RANGE))
        status = analysis.status_of(table["under_score"].to_numpy().astype(np.float64),
                                    table["over_score"].to_numpy().astype(np.float64),
                                    fu, max(0.0, fu - band), fo, max(0.0, fo - band))
        import polars as pl

        table = table.with_columns(pl.Series("status", status))
        area = table["area_px"].to_numpy().astype(np.float64)
        out.update(summarize(table, status, area))
        out.update(flag_under=fu, flag_over=fo)
    groups = []
    for key, code, color, word in (("seg:under", analysis.UNDER, COLORS["under"], "Under"),
                                   ("seg:over", analysis.OVER, COLORS["over"], "Over")):
        rows = table.filter(table["status"] == code)
        group = {"key": key, "reason": analysis.STATUS_WORDS[code], "status": "fail",
                 "level": "segqc", "label": word, "color": color,
                 "ids": rows["cell_id"].to_list(), "n": rows.height}
        if code == analysis.OVER:
            group["partner_ids"] = rows["partner_id"].to_list()
        groups.append(group)
    if include_ambiguous:
        rows = table.filter(table["status"] == analysis.AMBIGUOUS)
        groups.append({"key": "seg:ambiguous", "reason": "ambiguous", "status": "warn",
                       "level": "segqc", "label": "Ambiguous", "color": "#9ca3af",
                       "ids": rows["cell_id"].to_list(), "n": rows.height})
    out["groups"] = groups
    return out


# -- the density map --------------------------------------------------------------------------

#: The categories the density map can show, in the panel's order.
DENSITY_CATEGORIES = ("under", "over", "large", "small", "irregular")
DENSITY_COLORS = {**COLORS, "large": "#22c55e", "small": "#38bdf8", "irregular": "#eab308"}
#: A cell is abnormally large / small / irregular this many robust standard
#: deviations (1.4826 MAD) from the mask's own median, on log area and on
#: circularity; judged against the mask itself, so a mask of big cells is not
#: all "large".
OUTLIER_Z = 3.0
#: The spread is never taken as less than these: about +-15% of area, and
#: 0.05 of circularity.
LOG_AREA_FLOOR = 0.15
CIRCULARITY_FLOOR = 0.05
MAX_DENSITY_BINS = 256
_DENSITY_CACHE: dict = {}


def _robust_z(values, floor):
    """(values - median) / max(1.4826 MAD, floor). The floor keeps a very
    uniform mask -- where the MAD is nearly nothing -- from calling ordinary
    cells outliers over a few percent of size."""
    values = np.asarray(values, dtype=np.float64)
    finite = np.isfinite(values)
    if finite.sum() < 10:
        return np.zeros_like(values)
    median = float(np.median(values[finite]))
    mad = max(float(np.median(np.abs(values[finite] - median))) * 1.4826, float(floor))
    return np.where(finite, (values - median) / mad, 0.0)


def _density_table(project, fp):
    """(y, x, masks by category) of the current result, cached per result."""
    with _GUARD:
        held = _DENSITY_CACHE.get((project, fp))
    if held is not None:
        return held
    table = frame(project, fp)
    if table is None or "cx" not in table.columns:
        return None
    area_z = _robust_z(np.log(np.maximum(table["area_px"].to_numpy().astype(np.float64), 1.0)),
                       LOG_AREA_FLOOR)
    circ_z = _robust_z(table["circularity"].to_numpy().astype(np.float64), CIRCULARITY_FLOOR)
    x, y = table["cx"].to_numpy(), table["cy"].to_numpy()
    extent = max(1.0, float((x.max() - x.min()) * (y.max() - y.min()))) if x.size else 1.0
    held = {"spacing": float(np.sqrt(extent / max(1, x.size))),"y": table["cy"].to_numpy().astype(np.float64),
            "x": table["cx"].to_numpy().astype(np.float64),
            "under": table["under_score"].to_numpy().astype(np.float64),
            "over": table["over_score"].to_numpy().astype(np.float64),
            "large": area_z >= OUTLIER_Z, "small": area_z <= -OUTLIER_Z,
            "irregular": circ_z <= -OUTLIER_Z}
    with _GUARD:
        _DENSITY_CACHE.clear()
        _DENSITY_CACHE[(project, fp)] = held
    return held


def density(project, box, *, bins=128, flag_under=None, flag_over=None,
            categories=DENSITY_CATEGORIES) -> dict:
    """Where segmentation problems are concentrated, over `box` (x0, y0, x1,
    y1, full-resolution pixels) on a grid about `bins` cells across its longer
    side -- the panel asks for the view it is showing, so the grid is as fine
    as the zoom.

    Each category's value in a grid cell is the share of the cells there that
    are flagged for it (after a light smoothing of both counts, so a single
    cell does not make a hot pixel), sent as uint8 (255 = every cell), with the
    density of cells per grid cell against a typical one (`support`, 255 =
    typical or more) so the map can fade where there is too little tissue to
    say anything. The grid is never finer than about a cell and a half. Under / Over follow the
    viewing thresholds; large / small / irregular are outliers against the
    mask's own median (`OUTLIER_Z`)."""
    import base64

    from scipy import ndimage

    summary = current(project)
    if summary is None:
        return {"available": False, "reason": "no Segmentation QC result yet"}
    fp = summary.get("fingerprint")
    table = _density_table(project, fp)
    if table is None:
        return {"available": False,
                "reason": "this result predates the density map: run Segmentation QC again"}
    params = {**PARAMS_DEFAULT, **(summary.get("params") or {})}
    stored = float(params["flag"])
    fu = stored if flag_under is None else float(np.clip(float(flag_under), *FLAG_RANGE))
    fo = stored if flag_over is None else float(np.clip(float(flag_over), *FLAG_RANGE))
    x0, y0, x1, y1 = (float(v) for v in box)
    if not (x1 > x0 and y1 > y0):
        raise AgentError("invalid_input", "the box is empty")
    bins = int(min(max(8, int(bins)), MAX_DENSITY_BINS))
    # Never finer than about one and a half cells: a grid cell must hold a
    # few cells for "the share flagged here" to mean anything.
    step = max(max(x1 - x0, y1 - y0) / bins, 1.5 * table["spacing"])
    nx = max(1, int(np.ceil((x1 - x0) / step)))
    ny = max(1, int(np.ceil((y1 - y0) / step)))
    # A margin of two grid cells, so the smoothing at the edge of the view
    # sees the cells just outside it.
    pad = 2 * step
    x, y = table["x"], table["y"]
    keep = (x >= x0 - pad) & (x < x1 + pad) & (y >= y0 - pad) & (y < y1 + pad)
    ex = x0 - pad + step * np.arange(nx + 5)
    ey = y0 - pad + step * np.arange(ny + 5)

    def grid(weights):
        counts, _, _ = np.histogram2d(y[keep], x[keep], bins=(ey, ex), weights=weights)
        return ndimage.gaussian_filter(counts, 0.8)

    total = grid(None)
    flagged = {"under": table["under"] >= fu, "over": table["over"] >= fo,
               "large": table["large"], "small": table["small"],
               "irregular": table["irregular"]}
    inner = (slice(2, 2 + ny), slice(2, 2 + nx))
    layers = {}
    with np.errstate(divide="ignore", invalid="ignore"):
        for name in categories:
            if name not in flagged:
                continue
            share = np.where(total > 0.05, grid(flagged[name][keep].astype(np.float64)) / total,
                             0.0)
            layers[name] = {
                "values": base64.b64encode(np.round(np.clip(share[inner], 0, 1) * 255)
                                           .astype(np.uint8).tobytes()).decode("ascii"),
                "color": DENSITY_COLORS[name],
                "n": int(flagged[name].sum()),
                "pct_cells": round(100.0 * float(flagged[name].mean()), 2)
                if flagged[name].size else 0.0}
    # How many cells a grid cell holds, against a typical occupied one (its
    # 75th percentile): 255 at or above typical, so the map fades where the
    # tissue thins out, at any zoom.
    occupied = total[total > 0.05]
    typical = float(np.percentile(occupied, 75)) if occupied.size else 1.0
    support = np.round(np.clip(total[inner] / max(typical, 1e-6), 0, 1) * 255).astype(np.uint8)
    return {"available": True, "fingerprint": fp, "grid": {
                "x0": x0, "y0": y0, "step": step, "nx": nx, "ny": ny},
            "support": base64.b64encode(support.tobytes()).decode("ascii"),
            "layers": layers, "n_cells": int(x.size),
            "flag_under": fu, "flag_over": fo, "outlier_z": OUTLIER_Z}
