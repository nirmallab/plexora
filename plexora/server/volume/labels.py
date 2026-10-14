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
Z is never downsampled. The levels go down to 512 px a side, or further when
the builder is told the image's level count (`min_levels`), so the mask has a
level for every level of the image it is drawn on; a store built without it
is served past its coarsest level by striding (`label_overlay`).

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

from plexora._transient_locks import past_transient_locks

#: The attribute that marks a store as one of these.
MARKER = "plexora_label_volume"

#: Smallest level side the pyramid goes down to.
TARGET = 512

CHUNK = 1024


def _level_shapes(height: int, width: int, target: int = TARGET,
                  min_levels: Optional[int] = None):
    shapes = [(height, width)]
    while max(shapes[-1]) > target or (min_levels and len(shapes) < int(min_levels)
                                       and max(shapes[-1]) > 1):
        h, w = shapes[-1]
        shapes.append((-(-h // 2), -(-w // 2)))
    return shapes


def label_volume_shape(path) -> Optional[tuple]:
    """`(Z, Y, X)` when `path` is a TIFF holding one integer label volume, or
    `(T, Z, Y, X)` for one label volume per frame; else None. Header only --
    for an .ims, the header and one window per channel, to find the ids."""
    import tifffile as tf

    if str(path).lower().endswith(".ims"):
        from plexora.server.volume.sources import ims

        return ims.label_shape(path)
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
                       target: int = TARGET, min_levels: Optional[int] = None) -> str:
    """Write the label volume at `source` (a (Z, Y, X) or (T, Z, Y, X) TIFF,
    .ims or array) to `dest`.

    `min_levels` is the level count of the image the mask is drawn on: the
    pyramid goes on halving past `target` until it has that many levels.

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
    elif str(source).lower().endswith(".ims"):
        # One channel of an Imaris file (sources/ims.ImsLabels), read a
        # plane at a time like everything else here.
        from plexora.server.volume.sources import ims

        array = ims.ImsLabels(source)
        close = array.close
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
        shapes = _level_shapes(height, width, target, min_levels)
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
    # Windows refuses to move a directory while a scanner still has one of the
    # chunk files just written open (_transient_locks.py); it clears in moments.
    past_transient_locks(lambda: os.replace(tmp, dest))
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

    @property
    def oindex(self):
        """Orthogonal indexing, `[rows, cols]` by integer arrays, as a 2-D
        mask level's zarr array has it: what `label_overlay.label_region`
        strides a finer level with when the pyramid is shorter than the
        image's."""
        return _PlaneOindex(self._array, self._z)


class _PlaneOindex:
    def __init__(self, array, z):
        self._array = array
        self._z = z

    def __getitem__(self, index):
        rows, cols = index if isinstance(index, tuple) else (index, slice(None))
        return np.asarray(self._array.oindex[self._z, rows, cols])


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

    # A 2-D mask's zarr group's name for the same thing: label_overlay asks it
    # when the image has more levels than the label pyramid (a 3-D mask stops
    # at 512 px a side; the image may go coarser).
    array_keys = keys


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

    def at_view(self, token) -> _PlanePyramid:
        """The plane a view token names: a plane's own, a projection's middle
        plane, this frame's default for an empty token or a section. The rule
        `data_model.generate_zarr_png` applies to a local mask, here for a
        mask whose tiles a data node serves (node/api.seg_tile). The frame is
        the token's, clamped like `frame` -- one segmentation drawn on every
        frame answers every t. BadView for a token that is not one."""
        from plexora.server.volume.viewspec import ViewSpec

        if not str(token or "").strip():
            return self.default_plane()
        spec = ViewSpec.parse(token)
        volume = self.frame(spec.t)
        if spec.kind == "plane":
            return volume.plane(volume.depth // 2 if spec.z < 0 else spec.z)
        if spec.kind == "slab":
            z1 = volume.depth - 1 if spec.z1 is None else spec.z1
            return volume.plane(((spec.z0 or 0) + z1) // 2)
        return volume.default_plane()

    def describe(self) -> dict:
        """What a data node says about this mask, so the primary can read it
        as one (providers/node.NodeLabelVolume) without opening anything."""
        return {"depth": self.depth, "frames": self.frames, "height": self.height,
                "width": self.width, "level_shapes": self.level_shapes()}

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

    def array_keys(self):
        return self.default_plane().array_keys()


def open_label_volume(path, t: int = 0) -> LabelVolume:
    return LabelVolume(path, t)


def resolve_label_volume(source, folders, *, progress: Optional[Callable] = None,
                         stage: Optional[Callable] = None,
                         min_levels: Optional[int] = None) -> str:
    """The label-volume store for the (Z, Y, X) or (T, Z, Y, X) mask at
    `source`, adopted or built.

    `<stem>_labels_v1.zarr` in one of `folders`: the first that already holds
    one built from this very file (same path, same fingerprint) is adopted;
    otherwise it is built in the first that accepts a write. One rule for the
    viewer's own machine (`data_model._resolve_label_volume`: the project's
    folder) and a data node (`node/app._convert_mask_if_needed`: beside the
    mask, then the node's own folder for it), so a mask converted by either is
    found by both.

    `min_levels` (the image's level count) makes a new build go as coarse as
    the image does (`build_label_volume`); a store adopted from an earlier
    build keeps its levels.
    """
    from plexora import paths
    from plexora.server.utils import segmentation_pyramid

    source = Path(source)
    stem = source.name.split(".")[0]
    key = segmentation_pyramid.source_fingerprint(source)
    targets = [Path(folder) / f"{stem}_labels_v1.zarr" for folder in folders]
    for target in targets:
        if is_label_volume(target):
            try:
                attrs = dict(zarr.open_group(str(target), mode="r").attrs)
            except Exception:
                continue
            if attrs.get("source_key") == key and attrs.get("source") == str(source):
                return str(target)
    for target in targets:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        if paths.is_writable(target.parent):
            break
    else:
        raise OSError(f"none of {', '.join(str(f) for f in folders)} can be written, "
                      f"so {source.name} cannot be stored as a label volume")
    if stage is not None:
        stage("building")
    built = build_label_volume(source, target, progress=progress, min_levels=min_levels)
    zarr.open_group(built, mode="r+").update_attributes({"source_key": key or ""})
    return built


def project_label_volume(project, t: int = 0):
    """`project`'s mask as a label volume at frame `t`, wherever it is held,
    or None for a 2-D mask or none.

    A local store opens as a `LabelVolume`. A mask a data node holds as one
    (it says so in its resource status, `label_volume`) opens as a
    `providers.node.NodeLabelVolume`, the same face over the wire. What every
    3-D consumer asks -- measurement, sections, QC in depth, `/volume/info`'s
    `ndim` -- instead of testing `seg.derived`, which for a node mask is a
    `node://` locator no path test can answer.
    """
    seg = project.segmentation
    if not seg.available:
        return None
    binding = project.resource("segmentation") if hasattr(project, "resource") else None
    if binding is not None:
        from plexora.server.providers.node import NodeSegmentationProvider

        try:
            return NodeSegmentationProvider(binding).label_volume(t)
        except Exception:
            # An unreachable node cannot say; it reads as no volume here, and
            # the tile path reports the node in its own terms.
            return None
    if not seg.derived or not is_label_volume(seg.derived):
        return None
    return open_label_volume(seg.derived).frame(t)


def project_has_label_volume(project) -> bool:
    """Whether `project`'s mask is a 3-D label volume, local or on a node."""
    return project_label_volume(project) is not None


def project_mask_ndim(project):
    """What `/volume/info` reports of `project`'s mask: 3 for a label volume,
    2 for a 2-D mask, 0 for none -- or None when a data node holds it and
    cannot say yet: unreachable, slow to answer, or still converting it.

    "Not known" is not "2-D". The viewer decides at load whether to draw a
    mask in depth (the section panes' cells, the 3-D view's cell surfaces),
    and a page told 2-D never does; told None, it asks again
    (viewState.refreshLabels). A node whose conversion failed has said what
    it is going to say, and reads as 2-D.
    """
    seg = project.segmentation
    if not seg.available:
        return 0
    binding = project.resource("segmentation") if hasattr(project, "resource") else None
    if binding is None:
        return 3 if project_has_label_volume(project) else 2
    from plexora.server.node.resources import PREPARING
    from plexora.server.providers.node import NodeSegmentationProvider

    try:
        status = NodeSegmentationProvider(binding).status() or {}
    except Exception:
        return None
    if status.get("label_volume"):
        return 3
    if status.get("state") == PREPARING:
        return None
    return 2
