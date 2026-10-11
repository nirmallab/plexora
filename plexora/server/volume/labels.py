"""3-D label volumes: a segmentation whose objects are cells in depth.

A 2-D mask is one plane of ids drawn on every plane of a stack -- right for a
segmentation made on one plane, wrong for a z-stack segmented in 3-D, where a
cell occupies several planes and has ONE id across all of them. This module
stores the second kind and serves it plane by plane through the same
`[y, x]` levels the 2-D label path already reads (`label_overlay.
label_region`, `read_tile`), so outlines, picking and colour-by work on a
volume with no change below the tile route.

Storage: an OME-Zarr image of axes (z, y, x), uint32, one plane per chunk row
(chunks (1, 1024, 1024)), with XY levels made by taking every other row and
column of each plane -- labels are SUBSAMPLED, never averaged (the mean of two
cell ids is a third cell's id), the rule `segmentation_pyramid` follows in 2-D.
Z is never downsampled.

Built once at registration by `build_label_volume`; opened by
`open_label_volume`, which returns a `LabelVolume`. Its `plane(z)` is the
2-D pyramid of one plane: what `data_model.seg` was for a 2-D mask.

A TIME SERIES of label volumes -- a (T, Z, Y, X) mask, one segmentation per
frame -- is the same store with each frame in a group of its own (`t0`,
`t1`, ...) and `frames` on the root; `frame(t)` opens one. A frame's ids are
its own: the same id in two frames is the same cell only once the frames
have been linked (`server/volume/tracking.py`). A single-frame store keeps
its levels at the root, as every store built before time series did.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import zarr

#: The attribute that marks a store as one of these.
MARKER = "plexora_label_volume"

#: Smallest level side the pyramid goes down to.
TARGET = 512

CHUNK = 1024


def _level_shapes(height: int, width: int, target: int = TARGET):
    shapes = [(height, width)]
    while max(shapes[-1]) > target:
        h, w = shapes[-1]
        shapes.append((-(-h // 2), -(-w // 2)))
    return shapes


def label_volume_shape(path) -> Optional[tuple]:
    """`(Z, Y, X)` when `path` is a TIFF holding one integer label volume, or
    `(T, Z, Y, X)` for one label volume per frame; else None. Header only."""
    import tifffile as tf

    try:
        with tf.TiffFile(str(path), is_ome=False) as handle:
            series = handle.series[0]
            shape = tuple(int(s) for s in series.shape)
            dtype = np.dtype(series.dtype)
    except Exception:
        return None
    if dtype.kind not in "ui" or shape[-1] in (3, 4):
        return None
    if len(shape) == 3 and shape[0] >= 2:
        return shape
    if len(shape) == 4 and shape[0] >= 2:
        return shape
    return None


def _write_frame(group, array, shapes, progress, done, total):
    """One (Z, Y, X) label volume's levels into `group`; returns `done`."""
    depth = int(array.shape[0])
    arrays = []
    for index, (h, w) in enumerate(shapes):
        arrays.append(group.create_array(
            str(index), shape=(depth, h, w), dtype=np.uint32,
            chunks=(1, min(CHUNK, h), min(CHUNK, w)), fill_value=0))
    for z in range(depth):
        plane = np.asarray(array[z])
        if plane.dtype.itemsize > 4 and plane.size and int(plane.max()) > 2**32 - 1:
            raise ValueError("a label id does not fit in 32 bits")
        plane = plane.astype(np.uint32, copy=False)
        for index in range(len(shapes)):
            step = 2 ** index
            arrays[index][z] = plane[::step, ::step]
            done += 1
            if progress is not None:
                progress(done, total)
    return done


def build_label_volume(source, dest, *, progress: Optional[Callable] = None,
                       target: int = TARGET) -> str:
    """Write the label volume at `source` (a (Z, Y, X) or (T, Z, Y, X) TIFF or
    array) to `dest`.

    Written to a sibling temp directory and renamed into place, so a reader
    never finds a half-built store under the final name.
    """
    import tifffile as tf

    dest = Path(dest)
    tmp = dest.with_name(dest.name + ".building")
    if tmp.exists():
        shutil.rmtree(tmp)
    # An array, or anything array-like enough to read a plane at a time
    # (`shape`, `dtype`, `[z]`): a label volume made here rather than a file.
    in_memory = isinstance(source, np.ndarray) or (
        hasattr(source, "shape") and hasattr(source, "dtype") and hasattr(source, "__getitem__")
        and not isinstance(source, (str, os.PathLike)))
    if in_memory:
        array = source
        close = None
    else:
        handle = tf.TiffFile(str(source), is_ome=False)
        array = zarr.open(handle.series[0].aszarr(), mode="r")
        close = handle.close
    try:
        shape = tuple(int(s) for s in array.shape)
        if len(shape) == 4 and shape[0] == 1:
            array, shape = array[0], shape[1:]
        if len(shape) not in (3, 4):
            raise ValueError(f"a label volume is (z, y, x) or (t, z, y, x), not {shape}")
        frames = shape[0] if len(shape) == 4 else 1
        depth, height, width = shape[-3:]
        dtype = np.dtype(array.dtype)
        if dtype.kind not in "ui":
            raise TypeError(f"a label volume has integer ids, not {dtype}")
        shapes = _level_shapes(height, width, target)
        group = zarr.open_group(str(tmp), mode="w")
        total = frames * depth * len(shapes)
        if frames == 1:
            _write_frame(group, array, shapes, progress, 0, total)
        else:
            done = 0
            for t in range(frames):
                child = group.create_group(f"t{t}")
                done = _write_frame(child, array[t], shapes, progress, done, total)
                child.update_attributes({"frame": t, "depth": depth})
        datasets = [{"path": str(index), "coordinateTransformations": [
            {"type": "scale", "scale": [1.0, float(2 ** index), float(2 ** index)]}]}
            for index in range(len(shapes))]
        group.update_attributes({
            "multiscales": [{"version": "0.4", "name": "labels",
                             "axes": [{"name": "z", "type": "space"},
                                      {"name": "y", "type": "space"},
                                      {"name": "x", "type": "space"}],
                             "datasets": datasets}],
            "image-label": {"version": "0.4", "source": {"image": "../../"}},
            MARKER: True,
            "depth": depth,
            "frames": frames,
            "source": None if in_memory else str(source),
        })
    finally:
        if close is not None:
            close()
    if dest.exists():
        shutil.rmtree(dest)
    os.replace(tmp, dest)
    return str(dest)


def is_label_volume(path) -> bool:
    if not path:
        return False
    try:
        root = Path(path)
        for name in (".zattrs", "zarr.json"):
            meta = root / name
            if meta.is_file():
                attrs = json.loads(meta.read_text(encoding="utf-8"))
                attrs = attrs.get("attributes", attrs)
                return bool(attrs.get(MARKER))
    except Exception:
        return False
    return False


class _PlaneLevel:
    """One level of one plane, indexed `[rows, cols]` like a 2-D mask level."""

    ndim = 2

    def __init__(self, array, z: int):
        self._array = array
        self._z = int(z)
        self.shape = (int(array.shape[1]), int(array.shape[2]))
        self.dtype = np.dtype(array.dtype)
        self.chunks = tuple(array.chunks[1:])

    def __getitem__(self, index):
        if not isinstance(index, tuple):
            index = (index,)
        rows = index[0] if index else slice(None)
        cols = index[1] if len(index) > 1 else slice(None)
        return np.asarray(self._array[self._z, rows, cols])

    def __array__(self, dtype=None, copy=None):
        plane = np.asarray(self._array[self._z])
        return plane.astype(dtype) if dtype is not None else plane


class _PlanePyramid:
    """A plane of the volume, shaped like a 2-D mask's zarr group."""

    def __init__(self, arrays, z: int):
        self._levels = [_PlaneLevel(array, z) for array in arrays]
        self.z = int(z)

    def __len__(self):
        return len(self._levels)

    def __iter__(self):
        return iter(str(index) for index in range(len(self._levels)))

    def __contains__(self, key):
        try:
            return 0 <= int(key) < len(self._levels)
        except (TypeError, ValueError):
            return False

    def __getitem__(self, key):
        try:
            index = int(key)
        except (TypeError, ValueError):
            raise KeyError(key) from None
        if not 0 <= index < len(self._levels):
            raise KeyError(key)
        return self._levels[index]

    def keys(self):
        return list(iter(self))


class LabelVolume:
    """An opened label volume. `plane(z)` is one plane's 2-D pyramid; the
    volume itself reads `(Z, H, W)` blocks for measurement. A store with a
    label volume per frame opens at frame `t` (0 by default); `frame(t)` is
    another frame's, on the same store."""

    def __init__(self, path, t: int = 0):
        self.path = str(path)
        root = zarr.open_group(self.path, mode="r")
        self.frames = max(1, int(dict(root.attrs).get("frames") or 1))
        self.t = max(0, min(self.frames - 1, int(t))) if self.frames > 1 else 0
        self._group = root[f"t{self.t}"] if self.frames > 1 else root
        self._frames = {self.t: self}
        keys = sorted((key for key in self._group.array_keys()), key=int)
        self._arrays = [self._group[key] for key in keys]
        self.depth = int(self._arrays[0].shape[0])
        self.height = int(self._arrays[0].shape[1])
        self.width = int(self._arrays[0].shape[2])
        self._planes = {}

    @property
    def levels(self) -> int:
        return len(self._arrays)

    def level_shapes(self):
        return [[int(a.shape[1]), int(a.shape[2])] for a in self._arrays]

    def plane(self, z: int) -> _PlanePyramid:
        z = max(0, min(self.depth - 1, int(z)))
        cached = self._planes.get(z)
        if cached is None:
            cached = self._planes[z] = _PlanePyramid(self._arrays, z)
            if len(self._planes) > 64:
                self._planes.pop(next(iter(self._planes)))
        return cached

    def default_plane(self) -> _PlanePyramid:
        return self.plane(self.depth // 2)

    def frame(self, t: int) -> "LabelVolume":
        """Frame `t`'s label volume (this one for a single-frame store: one
        segmentation drawn on every frame)."""
        if self.frames == 1:
            return self
        t = max(0, min(self.frames - 1, int(t)))
        held = self._frames.get(t)
        if held is None:
            held = LabelVolume(self.path, t)
            held._frames = self._frames
            self._frames[t] = held
        return held

    def read(self, level: int, z0: int, z1: int, rows: slice, cols: slice) -> np.ndarray:
        """`(Z, H, W)` for planes z0..z1 inclusive at `level`."""
        return np.asarray(self._arrays[level][int(z0):int(z1) + 1, rows, cols])

    # What `data_model.seg` callers ask of a 2-D mask, answered for the
    # default plane so a caller that knows nothing of volumes still works.
    def __getitem__(self, key):
        return self.default_plane()[key]

    def __len__(self):
        return self.levels

    def __iter__(self):
        return iter(self.default_plane())

    def __contains__(self, key):
        return key in self.default_plane()


def open_label_volume(path, t: int = 0) -> LabelVolume:
    return LabelVolume(path, t)
