"""Each cell's whole area, so a region's overlap is a fraction of the cell.

`propagate` counts a cell's mask pixels inside a region within the region's
box; the denominator has to be the cell's area over the whole mask, or a large
cell the box cuts reads as mostly inside. One pass over the mask counts every
label (`np.bincount` per block, the same reads `propagate._overlap` makes), in
level-0 pixels, cached per mask fingerprint under the QC store, so a project
pays it once per mask -- not once per region or per derive.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading

import numpy as np

VERSION = "1"
#: [cal] read the mask at level 0 up to this many pixels; above it, the
#: smallest level at which the median cell is still MIN_DIAMETER_PX across.
MAX_LEVEL0_PIXELS = 400_000_000
MIN_DIAMETER_PX = 8.0
KEEP = 2

_GUARD = threading.Lock()
_MEMORY: dict = {}


def mask_fingerprint(record) -> str | None:
    """A mask's identity without reading a pixel: where it is, its stamp,
    scale and mode (`segqc.mask_identity`)."""
    from plexora.plugins.qc.server.segqc.run import mask_identity

    try:
        identity = mask_identity(record)
    except Exception:
        return None
    if not identity.get("where"):
        return None
    blob = json.dumps({"v": VERSION, **identity}, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _folder(project):
    from plexora import api

    path = api.store(project, "qc").directory() / "label_areas"
    path.mkdir(parents=True, exist_ok=True)
    return path


def choose_level(width, height, median_diameter_px=None) -> int:
    """Level 0 when the mask is small enough; else the smallest level whose
    pixel count fits, or at which the median cell keeps its size."""
    pixels = float(width or 0) * float(height or 0)
    if pixels <= MAX_LEVEL0_PIXELS:
        return 0
    level = 0
    while pixels / (4 ** level) > MAX_LEVEL0_PIXELS:
        level += 1
    if median_diameter_px:
        # Never coarser than the cells allow, even if that reads more.
        fine = max(0, int(math.floor(math.log2(max(1.0, float(median_diameter_px)
                                                     / MIN_DIAMETER_PX)))))
        level = min(level, fine)
    return level


class Areas:
    """Level-0 areas by label: `of(labels)` (NaN for a label never counted)."""

    def __init__(self, labels, areas, *, level, fingerprint=None, source="mask"):
        order = np.argsort(labels, kind="stable")
        self.labels = np.asarray(labels, dtype=np.int64)[order]
        self.areas = np.asarray(areas, dtype=np.float64)[order]
        self.level = int(level)
        self.fingerprint = fingerprint
        self.source = source

    def __len__(self):
        return int(self.labels.size)

    def of(self, labels) -> np.ndarray:
        labels = np.asarray(labels, dtype=np.int64)
        out = np.full(labels.shape, np.nan, dtype=np.float64)
        if not self.labels.size or not labels.size:
            return out
        at = np.searchsorted(self.labels, labels)
        at = np.clip(at, 0, self.labels.size - 1)
        hit = self.labels[at] == labels
        out[hit] = self.areas[at[hit]]
        return out


def count(provider, extra, width, height, level, *, block=2048) -> Areas:
    """Pixels per label over the whole mask at `level`, as level-0 areas."""
    from plexora.plugins.qc.server.propagate import DENSE_LABEL_MAX

    div = 2 ** level
    lw, lh = int(math.ceil(width / div)), int(math.ceil(height / div))
    dense = None
    sparse: dict = {}
    for by in range(0, lh, block):
        for bx in range(0, lw, block):
            box = (bx, by, min(lw, bx + block), min(lh, by + block))
            labels = np.asarray(provider.read_region(level + extra, box,
                                                     max_pixels=block * block))
            if labels.ndim > 2:
                labels = labels.reshape(labels.shape[-2:])
            flat = labels.ravel().astype(np.int64)
            if not flat.size:
                continue
            low, high = int(flat.min()), int(flat.max())
            if low >= 0 and high <= DENSE_LABEL_MAX:
                counted = np.bincount(flat, minlength=high + 1)
                if dense is None or dense.size < counted.size:
                    grown = np.zeros(counted.size, dtype=np.int64)
                    if dense is not None:
                        grown[:dense.size] = dense
                    dense = grown
                dense[:counted.size] += counted
                continue
            present, index = np.unique(flat, return_inverse=True)
            for label, n in zip(present.tolist(), np.bincount(index).tolist()):
                sparse[label] = sparse.get(label, 0) + n
    labels, counts = [], []
    if dense is not None:
        dense[0] = 0
        nz = np.flatnonzero(dense)
        labels.append(nz)
        counts.append(dense[nz])
    if sparse:
        sparse.pop(0, None)
        keys = np.fromiter(sparse.keys(), dtype=np.int64, count=len(sparse))
        values = np.fromiter(sparse.values(), dtype=np.int64, count=len(sparse))
        labels.append(keys)
        counts.append(values)
    if labels:
        all_labels = np.concatenate(labels)
        all_counts = np.concatenate(counts).astype(np.float64)
        # A label in both forms (a block above DENSE_LABEL_MAX and one below).
        unique, inverse = np.unique(all_labels, return_inverse=True)
        summed = np.bincount(inverse, weights=all_counts, minlength=unique.size)
    else:
        unique, summed = np.zeros(0, np.int64), np.zeros(0, np.float64)
    return Areas(unique, summed * float(4 ** level), level=level)


def _read(path, fingerprint):
    try:
        with np.load(path) as held:
            meta = json.loads(str(held["meta"]))
            if meta.get("version") != VERSION:
                return None
            return Areas(held["labels"], held["areas"], level=meta.get("level", 0),
                         fingerprint=fingerprint, source=meta.get("source", "mask"))
    except Exception:
        return None


def _write(folder, fingerprint, areas):
    path = folder / f"{fingerprint}.npz"
    tmp = folder / f"{fingerprint}.{os.getpid()}.{threading.get_ident()}.tmp.npz"
    meta = json.dumps({"version": VERSION, "level": areas.level, "n": len(areas),
                       "source": areas.source})
    np.savez(tmp, labels=areas.labels, areas=areas.areas, meta=np.array(meta))
    os.replace(tmp, path)
    stale = sorted((p for p in folder.glob("*.npz") if ".tmp" not in p.name),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for old in stale[KEEP:]:
        try:
            old.unlink()
        except OSError:
            pass


def _from_segqc(project, record):
    """Segmentation QC's own per-cell areas, when it measured this very mask
    at full resolution: no second pass over the mask."""
    from plexora.plugins.qc.server.segqc import run as segqc

    try:
        summary = segqc.current(project)
        if not summary or summary.get("mask") != segqc.mask_identity(record) \
                or int(summary.get("level", 1)) != 0:
            return None
        frame = segqc.frame(project)
        if frame is None or "area_px" not in frame.columns:
            return None
        return Areas(frame["cell_id"].to_numpy(), frame["area_px"].to_numpy(), level=0,
                     source="segqc")
    except Exception:
        return None


def label_areas(ds, provider, *, median_diameter_px=None):
    """The mask's per-label areas (cached), or None when they cannot be had."""
    record = ds.project
    fingerprint = mask_fingerprint(record)
    if fingerprint is None or provider is None:
        return None
    key = (ds.name, fingerprint)
    with _GUARD:
        held = _MEMORY.get(key)
    if held is not None:
        return held
    folder = _folder(ds.name)
    areas = _read(folder / f"{fingerprint}.npz", fingerprint)
    if areas is None:
        areas = _from_segqc(ds.name, record)
        if areas is None:
            extra = int(getattr(record.segmentation, "extra_levels", 0) or 0)
            width, height = record.image.width or 0, record.image.height or 0
            if not width or not height:
                return None
            level = choose_level(width, height, median_diameter_px)
            areas = count(provider, extra, width, height, level)
        areas.fingerprint = fingerprint
        _write(folder, fingerprint, areas)
    with _GUARD:
        _MEMORY[key] = areas
        while len(_MEMORY) > 4:
            _MEMORY.pop(next(iter(_MEMORY)))
    return areas


def forget(project=None):
    """Drop the in-memory copies (tests; a mask replaced in place)."""
    with _GUARD:
        for key in [k for k in _MEMORY if project is None or k[0] == project]:
            _MEMORY.pop(key, None)
