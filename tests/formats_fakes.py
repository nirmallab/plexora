"""Small fakes of the vendor packages the CZI, ND2, LIF, OIR, OIB and MCD
readers sit on (`czifile`, `nd2`, `liffile`, `oirfile`, `oiffile`, `readimc`).

None of those packages is installed where the tests run, and CI must not need
vendor sample files, so each test installs a fake in `sys.modules`
(`install`). A fake behaves as the real package was observed to on real files
-- the contract is in each class's docstring -- and holds only a tiny numpy
array whose every pixel says where it came from (`plane`), so a read that takes
the wrong channel, plane, level or offset cannot pass. Expected values in a
test are written from the arrays here and from the numbers in the test, never
from the reader under test.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import numpy as np


def install(monkeypatch, package: str, **attrs):
    """Put a module called `package` with `attrs` in `sys.modules` for one test."""
    module = types.ModuleType(package)
    for name, value in attrs.items():
        setattr(module, name, value)
    monkeypatch.setitem(sys.modules, package, module)
    return module


def touch(directory, name: str) -> Path:
    """A file the readers can stat; the fakes never read its bytes."""
    path = Path(directory) / name
    path.write_bytes(b"fake")
    return path


def plane(key: int, height: int, width: int, dtype=np.uint32) -> np.ndarray:
    """A `(height, width)` plane (each side at most 64) whose pixel is
    `key * 4096 + y * 64 + x`: the key names the plane, the rest the pixel."""
    ys = np.arange(height, dtype=np.uint64)[:, None]
    xs = np.arange(width, dtype=np.uint64)[None, :]
    return (np.uint64(key) * 4096 + ys * 64 + xs).astype(dtype)


def tcz(t: int, c: int, z: int) -> int:
    """The key of channel `c`, plane `z`, time `t`."""
    return (t * 8 + c) * 8 + z


def colour(height: int, width: int, level: int = 0, dtype=np.uint8, shift: int = 0) -> np.ndarray:
    """A `(h, w, 3)` colour picture whose three samples differ everywhere. A
    16-bit one holds the same pattern in its high byte (`pattern * 257`)."""
    ys = np.arange(height)[:, None, None]
    xs = np.arange(width)[None, :, None]
    ss = np.arange(3)[None, None, :]
    pattern = (ys * 7 + xs * 3 + 60 * ss + 11 * level + shift) % 256
    if np.dtype(dtype) == np.uint16:
        return (pattern * 257).astype(np.uint16)
    return pattern.astype(dtype)


def colour_pattern(height: int, width: int, level: int = 0, shift: int = 0) -> np.ndarray:
    """What `colour` is as 8 bits, however it was stored."""
    return colour(height, width, level, np.uint8, shift)


# ---------------------------------------------------------------------------
# czifile
# ---------------------------------------------------------------------------


class _CziView:
    def __init__(self, sub, dims, y_range, x_range):
        self._sub, self.dims = sub, tuple(dims)
        self._ys, self._ye = y_range
        self._xs, self._xe = x_range

    def asarray(self, fillvalue=0):
        dims = self.dims
        iy, ix = dims.index("Y"), dims.index("X")
        shape = list(self._sub.shape)
        shape[iy], shape[ix] = self._ye - self._ys, self._xe - self._xs
        out = np.full(shape, fillvalue, dtype=self._sub.dtype)
        # the stored rows/columns that fall inside the level
        lo_y, hi_y = max(self._ys, 0), min(self._ye, self._sub.shape[iy])
        lo_x, hi_x = max(self._xs, 0), min(self._xe, self._sub.shape[ix])
        if lo_y < hi_y and lo_x < hi_x:
            src = [slice(None)] * out.ndim
            dst = [slice(None)] * out.ndim
            src[iy], src[ix] = slice(lo_y, hi_y), slice(lo_x, hi_x)
            dst[iy], dst[ix] = slice(lo_y - self._ys, hi_y - self._ys), slice(lo_x - self._xs, hi_x - self._xs)
            out[tuple(dst)] = self._sub[tuple(src)]
        return out


class FakeCziLevel:
    """One level of a scene: its own (downsampled) array. Called with a level-0
    stage `roi`, as czifile's images are."""

    def __init__(self, owner, index, data):
        self._owner, self._index, self._data = owner, index, data
        self.dims, self.dtype, self.shape = owner.dims, data.dtype, data.shape
        self.sizes = dict(zip(owner.dims, data.shape))
        self.bbox = owner.bbox

    def __call__(self, roi=None, **selection):
        return self._owner._view(self._index, self._data, roi, selection)


class FakeCziImage:
    """A CziImage with squeeze=False: every axis present, size-1 ones too.

    `bbox` is `(x, y, w, h)` on the stage in level-0 pixels (x, y may be
    negative). A call's `roi` is always level-0 stage coordinates; the view
    returned covers the level pixels the roi reaches (floor of the start, ceil
    of the end), `shrink`/`grow` make it one short / one over on each side
    (czifile's own rounding, which the reader must trim or pad away). A roi
    wholly outside the bbox or wholly inside one of `holes` (stage rects where
    nothing was scanned) raises ValueError("roi matches no subblocks").
    """

    def __init__(self, dims, sizes, level_shapes, *, bbox, dtype=np.uint32, mpp=(0.5, 0.5),
                 channels=None, coord_scales=None, coord_units=None, holes=(), levels=True,
                 data=None):
        self.dims = tuple(dims)
        self.dtype = np.dtype(dtype)
        self.bbox = tuple(bbox)
        self.mpp = mpp
        self.channels = channels if channels is not None else {}
        self.coord_scales = coord_scales if coord_scales is not None else {}
        self.coord_units = coord_units if coord_units is not None else {}
        self.holes = list(holes)
        self.shrink = 0
        self.grow = 0
        self.error = None
        self.calls = []
        arrays = []
        for k, (h, w) in enumerate(level_shapes):
            level_sizes = dict(sizes, Y=h, X=w)
            arrays.append(data[k] if data is not None else self._make(level_sizes, k))
        self.shape = arrays[0].shape
        self.sizes = dict(zip(self.dims, self.shape))
        self._arrays = arrays
        self._level_objects = [FakeCziLevel(self, k, a) for k, a in enumerate(arrays)]
        if levels:
            self.levels = self._level_objects

    def _make(self, sizes, level):
        dims = self.dims
        shape = [sizes[d] for d in dims]
        arr = np.zeros(shape, dtype=self.dtype)
        h, w = sizes["Y"], sizes["X"]
        if "S" in dims and sizes.get("S", 1) >= 3 and sizes.get("C", 1) == 1 and self.dtype.itemsize <= 2:
            rgb = colour(h, w, level, self.dtype)
            other = [d for d in dims if d not in ("Y", "X", "S")]
            for idx in np.ndindex(*[sizes[d] for d in other]):
                sl = [slice(None)] * len(dims)
                for d, i in zip(other, idx):
                    sl[dims.index(d)] = i
                arr[tuple(sl)] = self._place_rgb(rgb, dims, other)
            return arr
        rest = [d for d in dims if d not in ("Y", "X", "S")]
        for idx in np.ndindex(*[sizes[d] for d in rest]):
            ids = dict(zip(rest, idx))
            key = tcz(ids.get("T", 0), ids.get("C", 0), ids.get("Z", 0))
            key += 1000 * sum(i for d, i in ids.items() if d not in ("T", "C", "Z"))
            key += 100_000 * level
            sl = [slice(None)] * len(dims)
            for d, i in ids.items():
                sl[dims.index(d)] = i
            if "S" in dims:
                sl[dims.index("S")] = 0
            arr[tuple(sl)] = plane(key, h, w, self.dtype)
        return arr

    @staticmethod
    def _place_rgb(rgb, dims, other):
        """`rgb` `(h, w, 3)` as the array for the dims left once `other` axes
        are indexed away: the remaining axes are a subset of Y, X, S in
        `dims` order."""
        remaining = [d for d in dims if d not in other]
        order = {"Y": 0, "X": 1, "S": 2}
        return np.transpose(rgb, [order[d] for d in remaining])

    def __call__(self, roi=None, **selection):
        return self._view(0, self._arrays[0], roi, selection)

    def _view(self, level, data, roi, selection):
        self.calls.append((level, None if roi is None else tuple(int(v) for v in roi), dict(selection)))
        if self.error:
            raise ValueError(self.error)
        dims = self.dims
        bx, by, bw, bh = self.bbox
        lh, lw = data.shape[dims.index("Y")], data.shape[dims.index("X")]
        rx, ry, rw, rh = (bx, by, bw, bh) if roi is None else (int(v) for v in roi)
        outside = rx + rw <= bx or rx >= bx + bw or ry + rh <= by or ry >= by + bh
        inside_hole = any(hx <= rx and rx + rw <= hx + hw and hy <= ry and ry + rh <= hy + hh
                          for hx, hy, hw, hh in self.holes)
        if outside or inside_hole:
            raise ValueError("roi matches no subblocks")
        ys = ((ry - by) * lh) // bh
        ye = -((-(ry + rh - by) * lh) // bh)
        xs = ((rx - bx) * lw) // bw
        xe = -((-(rx + rw - bx) * lw) // bw)
        ye = max(ys + 1, ye + self.grow - self.shrink)
        xe = max(xs + 1, xe + self.grow - self.shrink)
        index = [slice(None)] * data.ndim
        for name, value in selection.items():
            if name not in dims:
                raise KeyError(name)
            if not 0 <= value < data.shape[dims.index(name)]:
                raise IndexError(f"{name}={value}")
            index[dims.index(name)] = slice(value, value + 1)
        return _CziView(data[tuple(index)], dims, (ys, ye), (xs, xe))


def install_czi(monkeypatch, scenes):
    """`czifile` with `scenes` (a list of `FakeCziImage`, keys 0..n-1)."""

    class CziFile:
        instances = []

        def __init__(self, path, squeeze=True, **kwargs):
            self.path, self.squeeze, self.closed = str(path), squeeze, False
            self.locked, self.maxcache = None, None
            self.scenes = dict(enumerate(scenes))
            CziFile.instances.append(self)

        def set_lock(self, value):
            self.locked = bool(value)

        def close(self):
            self.closed = True

    return install(monkeypatch, "czifile", CziFile=CziFile)


# ---------------------------------------------------------------------------
# nd2
# ---------------------------------------------------------------------------


def nd2_key(index: int, channel: int) -> int:
    return index * 4 + channel


class FakeND2File:
    """nd2.ND2File: `.sizes` in loop order; `read_frame(i)` is frame `i` over
    the non-C/Y/X loop axes in `.sizes` order, as `(C, Y, X)` (`(Y, X)` for one
    channel, `(Y, X, 3)` for colour)."""

    def __init__(self, path, sizes, *, dtype=np.uint32, rgb=False, names=None,
                 voxel=(1.0, 1.0, 1.0), calibrated=(True, True, True), metadata=True,
                 frame_fn=None, voxel_error=None):
        self.path = str(path)
        self.sizes = dict(sizes)
        self.dtype = np.dtype(dtype)
        self.is_rgb = rgb
        self.closed = False
        self.frames_read = []
        self._voxel = voxel
        self._voxel_error = voxel_error
        self._frame_fn = frame_fn
        channels = int(self.sizes.get("C", 1))
        if metadata is None:
            self.metadata = None
        else:
            listed = list(names) if names is not None else ["Ch%d" % i for i in range(channels)]
            self.metadata = SimpleNamespace(channels=[
                SimpleNamespace(channel=SimpleNamespace(name=name),
                                volume=SimpleNamespace(axesCalibrated=tuple(calibrated)))
                for name in listed])

    def voxel_size(self):
        if self._voxel_error:
            raise self._voxel_error
        x, y, z = self._voxel
        return SimpleNamespace(x=x, y=y, z=z)

    def read_frame(self, index):
        self.frames_read.append(int(index))
        if self._frame_fn is not None:
            return self._frame_fn(int(index))
        h, w = self.sizes["Y"], self.sizes["X"]
        total = int(np.prod([v for k, v in self.sizes.items() if k not in ("C", "Y", "X", "S")] or [1]))
        assert 0 <= index < total, f"frame {index} of {total}"
        if self.is_rgb:
            return colour(h, w, shift=index, dtype=self.dtype)
        c = int(self.sizes.get("C", 1))
        frames = np.stack([plane(nd2_key(index, k), h, w, self.dtype) for k in range(c)])
        return frames[0] if c == 1 else frames

    def close(self):
        self.closed = True


def install_nd2(monkeypatch, **kwargs):
    """`nd2` whose ND2File(path) is built by `FakeND2File(path, **kwargs)`."""
    made = []

    def ND2File(path):
        handle = FakeND2File(path, **kwargs)
        made.append(handle)
        return handle

    module = install(monkeypatch, "nd2", ND2File=ND2File)
    module.made = made
    return module


# ---------------------------------------------------------------------------
# liffile
# ---------------------------------------------------------------------------

TILE_DTYPE = [("field_y", "i4"), ("field_x", "i4"), ("pos_z", "f8"), ("pos_y", "f8"), ("pos_x", "f8")]


def lif_key(m: int, c: int, z: int, t: int) -> int:
    return ((m * 4 + t) * 8 + c) * 8 + z


def tilescan(positions_px, pixel_um, *, flip_x=False, flip_y=False, swap_xy=False):
    """A tile scan whose tile `m` sits at `positions_px[m] = (x, y)` pixels of
    `pixel_um` µm; the stage positions are in metres."""
    tiles = np.zeros(len(positions_px), dtype=TILE_DTYPE)
    for m, (x, y) in enumerate(positions_px):
        tiles["pos_x"][m] = x * pixel_um * 1e-6
        tiles["pos_y"][m] = y * pixel_um * 1e-6
    return SimpleNamespace(flip_x=flip_x, flip_y=flip_y, swap_xy=swap_xy, tiles=tiles)


class FakeLifImage:
    """A LifImage: `.frame(**indices)` is one `(Y, X)` (or `(Y, X, S)`) frame
    for the M/C/Z/T (and other) indices given; an index not given is 0 and one
    naming a dimension the image does not have is an error."""

    def __init__(self, name, dims, sizes, *, dtype=np.uint32, coords=None, tilescan=None):
        self.name, self.dims, self.sizes = name, tuple(dims), dict(sizes)
        self.dtype, self.coords, self.tilescan = np.dtype(dtype), coords, tilescan
        self.frame_calls = []

    def frame(self, **indices):
        for name in indices:
            if name not in self.dims or name in ("Y", "X", "S"):
                raise TypeError(f"no dimension {name}")
        self.frame_calls.append(dict(indices))
        h, w = self.sizes["Y"], self.sizes["X"]
        m = indices.get("M", 0)
        if "S" in self.dims:
            return colour(h, w, shift=31 * m, dtype=self.dtype)
        key = lif_key(m, indices.get("C", 0), indices.get("Z", 0), indices.get("T", 0))
        key += 1000 * sum(v for k, v in indices.items() if k not in ("M", "C", "Z", "T"))
        return plane(key, h, w, self.dtype)


def install_lif(monkeypatch, images):
    class LifFile:
        instances = []

        def __init__(self, path):
            self.path, self.closed, self.locked = str(path), False, None
            self.images = list(images)
            LifFile.instances.append(self)

        def set_lock(self, value):
            self.locked = bool(value)

        def close(self):
            self.closed = True

    return install(monkeypatch, "liffile", LifFile=LifFile)


# ---------------------------------------------------------------------------
# oirfile, oiffile, readimc
# ---------------------------------------------------------------------------


def coded(shape, dtype=np.uint32) -> np.ndarray:
    """An array in which every element is its own flat index."""
    return np.arange(int(np.prod(shape)), dtype=np.uint64).reshape(shape).astype(dtype)


def install_oir(monkeypatch, *, dims, data=None, shape=None, dtype=np.uint32, scales=None,
                units=None, channels=()):
    array = coded(shape) if data is None else data
    sizes = dict(zip(dims, array.shape))
    state = SimpleNamespace(opened=0, loaded=[], closed=0)

    class OirFile:
        def __init__(self, path):
            self.path = str(path)
            self.dims, self.sizes, self.dtype = tuple(dims), dict(sizes), array.dtype
            self.coord_scales, self.coord_units = scales or {}, units or {}
            self.channels = [SimpleNamespace(name=name) for name in channels]

        def __enter__(self):
            state.opened += 1
            return self

        def __exit__(self, *exc):
            state.closed += 1

        def asarray(self, out=None):
            state.loaded.append(out)
            return array.copy()

    module = install(monkeypatch, "oirfile", OirFile=OirFile)
    module.state, module.array = state, array
    return module


def install_oib(monkeypatch, *, axes, shape, main, dtype=np.uint32):
    array = coded(shape, dtype)
    state = SimpleNamespace(loaded=0)

    class OifFile:
        def __init__(self, path):
            self.path = str(path)
            self.axes, self.shape, self.dtype = axes, tuple(shape), array.dtype
            self.mainfile = main

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def asarray(self):
            state.loaded += 1
            return array.copy()

    module = install(monkeypatch, "oiffile", OifFile=OifFile)
    module.state, module.array = state, array
    return module


def fake_acquisition(acq_id=1, description="ROI", *, height=12, width=16, metals=("Er170", "Yb172"),
                     labels=None, pixel_um=(1.0, 1.0), data=None):
    n = len(metals)
    if data is None:
        data = coded((n, height, width), np.float32) * np.float32(0.5)
    return SimpleNamespace(
        id=acq_id, description=description, width_px=data.shape[2], height_px=data.shape[1],
        num_channels=n, channel_names=list(metals),
        channel_labels=labels if labels is not None else [""] * n,
        pixel_size_x_um=pixel_um[0], pixel_size_y_um=pixel_um[1], data=data)


def install_readimc(monkeypatch, slides):
    """`readimc` with `slides`: a list of lists of acquisitions."""
    state = SimpleNamespace(strict=[], opened=0, closed=0)

    class MCDFile:
        def __init__(self, path):
            self.path = str(path)
            self.slides = [SimpleNamespace(acquisitions=list(group)) for group in slides]

        def __enter__(self):
            state.opened += 1
            return self

        def __exit__(self, *exc):
            state.closed += 1

        def read_acquisition(self, acquisition, strict=True):
            state.strict.append(strict)
            return acquisition.data

    module = install(monkeypatch, "readimc", MCDFile=MCDFile)
    module.state = state
    return module
