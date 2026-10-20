"""The per-cell feature matrix one marker's classifier learns from.

Two groups, each used when it can be:

- **table** (always): the marker's value, its log, its rank, the cell's size
  when the table has one, and the value of its nearest neighbours -- a
  neighbour much brighter than the cell is how glow looks in a table.
- **pixels** (when the marker is an image channel and the project has a
  mask): the marker's own pixels inside each cell -- mean, spread, peak, edge
  against interior (membrane), against the tissue around it (local
  background, contrast), texture, and with a nuclear channel how much of it
  sits on the nucleus. Built by core's `utils.cell_features` here for a local
  image and on the data node for one held there, then cached beside the
  run files: a second run on the same marker reads no pixel.

The matrix is row aligned to the table; a row the pixels did not reach (no
label, a cell off the mask) is NaN there, and the learner imputes it.
"""

from __future__ import annotations

import hashlib
import os
import threading

import numpy as np

from plexora.plugins.gating.server.classifier import store

#: Bumped when the table group changes; the pixel group carries core's own.
TABLE_VERSION = 1

#: Nearest neighbours whose values describe a cell's surroundings.
NEIGHBOURS = 6

_EPS = 1e-6


# -- fingerprints: what a run's calls were made from -----------------------


def table_fingerprint(ds) -> str:
    from plexora.plugins.gating.server.classifier.geometry import cells

    geometry = cells(ds)
    digest = hashlib.sha1()
    digest.update(str(ds.table.expression_fingerprint).encode("utf-8"))
    digest.update(str(geometry.n).encode("utf-8"))
    digest.update(np.ascontiguousarray(geometry.ids).tobytes())
    return digest.hexdigest()[:16]


def mask_fingerprint(ds) -> str:
    record = ds.project
    seg = record.segmentation
    if not seg.available:
        return ""
    binding = record.resources.get("segmentation")
    if binding is not None:
        return f"node:{binding.node}:{binding.resource_id}"
    path = str(seg.derived or "")
    try:
        stat = os.stat(path)
        return f"{path}|{int(stat.st_mtime)}|{stat.st_size}"
    except OSError:
        return path


def image_fingerprint(ds) -> str:
    from plexora.server.utils import source_image

    try:
        return str(source_image.SHELF.identity_of(ds) or "")
    except Exception:
        return ""


# -- table features ----------------------------------------------------------


def _signed_log(values):
    return np.sign(values) * np.log1p(np.abs(values))


def table_features(ds, marker):
    """(matrix float32 [rows, k], names) from the table alone."""
    from plexora.plugins.gating.server.classifier.geometry import cells

    def compute():
        geometry = cells(ds)
        raw = np.asarray(ds.table.columns([marker])[marker], dtype=np.float64)
        n = raw.shape[0]
        finite = np.isfinite(raw)
        columns, names = [], []
        columns.append(raw)
        names.append("value")
        if not ds.table.log_transformed:
            columns.append(_signed_log(raw))
            names.append("log_value")
        rank = np.full(n, np.nan)
        if finite.any():
            order = np.argsort(raw[finite], kind="stable")
            ranks = np.empty(order.size)
            ranks[order] = np.arange(order.size) / max(order.size - 1, 1)
            rank[finite] = ranks
        columns.append(rank)
        names.append("rank")
        if geometry.area is not None and geometry.area.shape[0] == n:
            columns.append(np.log1p(np.clip(geometry.area, 0, None)))
            names.append("log_area")
        mean, peak = _neighbour_values(geometry, raw)
        if mean is not None:
            columns += [mean, peak, _signed_log(raw) - _signed_log(mean)]
            names += ["neighbour_mean", "neighbour_max", "value_over_neighbours"]
        matrix = np.column_stack(columns).astype(np.float32)
        return matrix, names

    return ds.cached(("classifier.table_features", marker, TABLE_VERSION), compute)


def _neighbour_values(geometry, values):
    """Mean and max of `values` over each cell's nearest neighbours (itself
    left out), or (None, None) without positions."""
    from scipy.spatial import cKDTree

    valid = geometry.valid & np.isfinite(values)
    if int(valid.sum()) <= NEIGHBOURS:
        return None, None
    rows = np.flatnonzero(geometry.valid)
    points = np.column_stack([geometry.xs[valid], geometry.ys[valid]])
    tree = cKDTree(points)
    source = values[valid]
    mean = np.full(values.shape[0], np.nan)
    peak = np.full(values.shape[0], np.nan)
    query = np.column_stack([geometry.xs[rows], geometry.ys[rows]])
    for start in range(0, rows.size, 262_144):
        chunk = slice(start, start + 262_144)
        _dist, index = tree.query(query[chunk], k=NEIGHBOURS + 1)
        near = source[index[:, 1:]]
        mean[rows[chunk]] = near.mean(axis=1)
        peak[rows[chunk]] = near.max(axis=1)
    return mean, peak


# -- pixel features -----------------------------------------------------------


class _PixelPlan:
    """What a pixel pass for one marker would read, or why it cannot."""

    def __init__(self, ds, marker):
        from plexora.agent import presets
        from plexora.agent.errors import AgentError
        from plexora.agent.render import resolve_channel
        from plexora.ai import vocabulary
        from plexora.server.utils import cell_features, source_image

        self.reason = None
        self.ds = ds
        record = ds.project
        if not record.segmentation.available:
            self.reason = "no segmentation mask"
            return
        if record.image.is_blank:
            self.reason = "no image"
            return
        names = [c.get("fullname") or c.get("name") for c in record.image.real_channels]
        channel = vocabulary.match_name(marker, names)
        if channel is None:
            self.reason = f"{marker} is not an image channel"
            return
        nuclear = presets.nuclear_channel(names)
        records = list(record.image.real_channels)
        try:
            _i, found = resolve_channel(channel, records)
            nuclear_record = (resolve_channel(nuclear, records)[1]
                              if nuclear and nuclear != channel else None)
        except AgentError:
            self.reason = f"{marker}'s channel could not be found in the image"
            return
        with source_image.SHELF.reader(ds) as source:
            if source.is_brightfield:
                self.reason = "the image is brightfield"
                return
            self.marker_index = source.channel_index(source_image.channel_key(found))
            self.dna_index = (source.channel_index(source_image.channel_key(nuclear_record))
                              if nuclear_record is not None else None)
            if self.marker_index is None:
                self.reason = f"{marker}'s channel could not be found in the image"
                return
            self.level = cell_features.choose_level(source.width, source.height,
                                                    source.levels)
            self.height, self.width = (int(v) for v in source.level_shape(self.level))
        self.mask_level = self.level + int(record.segmentation.extra_levels or 0)
        self.channel = channel
        self.nuclear = nuclear if nuclear_record is not None else None

    def key(self):
        from plexora.server.utils.cell_features import FEATURE_VERSION

        digest = hashlib.sha1("|".join(str(v) for v in (
            "pixels", FEATURE_VERSION, image_fingerprint(self.ds), mask_fingerprint(self.ds), self.marker_index,
            self.dna_index, self.level, self.mask_level)).encode("utf-8"))
        return digest.hexdigest()[:16]


def _cache_path(ds, marker, key):
    return store.folder(ds.name) / "features" / f"{store.safe_name(marker)}_{key}.npz"


def _accumulate_local(ds, plan, progress, cancelled):
    from plexora.agent import render
    from plexora.server.utils import cell_features, source_image

    provider, _status, reason, _locator = render._mask_for(ds.project)
    if provider is None:
        raise RuntimeError(reason or "the mask could not be read")

    def reader(index):
        def read(box):
            with source_image.SHELF.reader(ds) as source:
                plane, clipped = source.read(index, plan.level, box)
            return cell_features.pad_to_box(plane, clipped, box)
        return read

    def read_labels(box):
        return provider.read_region(plan.mask_level, box)

    return cell_features.accumulate(
        reader(plan.marker_index), read_labels, width=plan.width, height=plan.height,
        read_dna=reader(plan.dna_index) if plan.dna_index is not None else None,
        progress=progress, cancelled=cancelled)


def _accumulate_node(ds, plan, progress, cancelled):
    from plexora.server.providers.node import cell_features_on_node

    record = ds.project
    seg_binding = record.resources.get("segmentation")
    if seg_binding is None:
        raise RuntimeError("the image is on a data node but its mask is not; pixel "
                           "features need both in one place")
    return cell_features_on_node(
        record.resource("image"), seg_binding, marker_index=plan.marker_index,
        dna_index=plan.dna_index, level=plan.level, mask_level=plan.mask_level,
        progress=progress, cancelled=cancelled)


def _transform_pixels(features):
    """Pixel sums on scales a linear model can weigh: intensities and sizes
    logged, ratios as log ratios, contrast signed-log."""
    out = {}
    for name, values in features.items():
        values = values.astype(np.float64)
        if name in ("px_mean", "px_std", "px_max", "px_background", "px_gradient", "px_area"):
            out[name] = np.log1p(np.clip(values, 0, None))
        elif name.endswith("_ratio"):
            out[name] = np.log(np.clip(values, _EPS, None))
        elif name == "px_contrast":
            out[name] = _signed_log(values)
        else:
            out[name] = values
    return out


#: {(project, marker): lock}: one pixel pass per marker at a time, so a
#: gating session reading ahead and a wand press share it.
_PASS_LOCKS: dict = {}
_PASS_GUARD = threading.Lock()


def _pass_lock(ds, marker):
    with _PASS_GUARD:
        return _PASS_LOCKS.setdefault((ds.name, marker), threading.Lock())


def pixels_ready(ds, marker) -> tuple:
    """(ready, reason): whether the marker's pixel features are cached (or
    cannot exist, so there is nothing to wait for -- `reason` says why)."""
    try:
        plan = _PixelPlan(ds, marker)
    except Exception as exc:  # noqa: BLE001
        return True, f"pixels unreadable: {exc}"
    if plan.reason:
        return True, plan.reason
    return _cache_path(ds, marker, plan.key()).is_file(), None


def pixel_features(ds, marker, *, progress=None, cancelled=None):
    """(matrix float32 [rows, k] or None, names, info)."""
    from plexora.plugins.gating.server.classifier.geometry import cells
    from plexora.server.utils import cell_features

    plan = _PixelPlan(ds, marker)
    if plan.reason:
        return None, [], {"pixels": False, "reason": plan.reason}
    path = _cache_path(ds, marker, plan.key())
    where = "cache"
    with _pass_lock(ds, marker):
        cached = None
        if path.is_file():
            try:
                cached = cell_features.unpack(path.read_bytes())
            except (OSError, ValueError, KeyError):
                cached = None
        if cached is None:
            image_binding = ds.project.resource("image")
            where = "node" if image_binding is not None else "local"
            if image_binding is not None:
                labels, features = _accumulate_node(ds, plan, progress, cancelled)
            else:
                labels, features = _accumulate_local(ds, plan, progress, cancelled)
            store.write_atomic(path, cell_features.pack(labels, features))
            # One cached matrix per marker: an older one was built from pixels
            # or a mask that are no longer the project's.
            for old in path.parent.glob(f"{store.safe_name(marker)}_*.npz"):
                if old != path:
                    old.unlink(missing_ok=True)
        else:
            labels, features = cached
    geometry = cells(ds)
    ids = geometry.ids
    rows_hit = np.zeros(ids.shape[0], dtype=bool)
    positions = np.zeros(ids.shape[0], dtype=np.int64)
    if labels.size:
        positions = np.clip(np.searchsorted(labels, ids), 0, labels.size - 1)
        rows_hit = (ids >= 0) & (labels[positions] == ids)
    transformed = _transform_pixels(features)
    names = [name for name in cell_features.NAMES if name in transformed]
    matrix = np.full((ids.shape[0], len(names)), np.nan, dtype=np.float32)
    for column, name in enumerate(names):
        matrix[rows_hit, column] = transformed[name][positions[rows_hit]]
    info = {"pixels": True, "where": where, "channel": plan.channel,
            "nuclear": plan.nuclear, "level": plan.level,
            "cells_on_mask": int(rows_hit.sum()),
            "feature_version": cell_features.FEATURE_VERSION}
    return matrix, names, info


def feature_matrix(ds, marker, *, progress=None, cancelled=None, pixels=True):
    """(matrix float32 [rows, k], names, info) for one marker.

    A pixel pass that fails (an unreadable mask, a node that went away) does
    not fail the run: the table group still trains, and `info` says why the
    pixels were left out.
    """
    table, table_names = table_features(ds, marker)
    info = {"groups": ["table"], "table_version": TABLE_VERSION}
    if not pixels:
        info["pixel_reason"] = "pixels not asked for"
        return table, list(table_names), info
    try:
        matrix, names, pixel_info = pixel_features(ds, marker, progress=progress,
                                                   cancelled=cancelled)
    except InterruptedError:
        raise
    except Exception as exc:  # noqa: BLE001 -- reported on the run, not raised
        matrix, names, pixel_info = None, [], {"pixels": False,
                                               "reason": f"pixels unreadable: {exc}"}
    if matrix is None:
        info["pixel_reason"] = pixel_info.get("reason")
        return table, list(table_names), info
    info["groups"].append("pixels")
    if pixel_info.get("nuclear"):
        info["groups"].append("nuclear")
    info["pixels"] = pixel_info
    return (np.concatenate([table, matrix], axis=1), list(table_names) + names, info)
