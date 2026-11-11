"""Leica LIF, LOF and XLEF: LAS AF and LAS X files, about 2005 to today
(SP5/SP8/Stellaris confocals, Thunder, Mica).

Read with `liffile` (Christoph Gohlke, BSD-3), which parses the XML header
and memory blocks of LIF versions 1 and 2, the single-image LOF, and the
XLEF/XLIF folder exports whose frames are LOF or TIFF files. What this module
adds:

- **Which image.** A LIF is a project: many images. Each is an image of its
  own (`reader(path, index)`, counting only items with Y and X), and the
  import offers each as a sample.
- **Tile scans.** LAS X stores a tile scan as separate tiles with their stage
  positions (and flip and swap flags); they are placed by position into one
  mosaic, later tiles over earlier ones, as LAS X shows an unmerged scan.
- **No pyramid.** LIF has none; an image larger than a tile gets coarse
  levels derived at import like any flat image's.
- **Scale.** Pixel size and z step from the dimension coordinates (metres).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from plexora.server.utils.formats.base import Reader, TileCache, chosen, names_or_numbered

#: Dimensions read at the default plane / channel; the rest at index 0.
_OWN = ("Y", "X", "S", "C", "Z", "T", "M")


def _step(coords, name):
    values = coords.get(name) if coords else None
    if values is None or len(values) < 2:
        return None
    try:
        step = abs(float(values[1]) - float(values[0]))
    except (TypeError, ValueError):
        return None
    return step * 1e6 if step else None       # metres -> µm


class LifReader(Reader):
    format_name = "Leica LIF"

    def __init__(self, path: str, index: int = 0):
        super().__init__()
        import liffile

        self.format_name = {".lof": "Leica LOF", ".xlef": "Leica XLEF"}.get(
            Path(path).suffix.lower(), "Leica LIF")
        self._file = liffile.LifFile(path)
        try:
            self._file.set_lock(True)
            images = [image for image in self._file.images
                      if "Y" in image.dims and "X" in image.dims]
            if not images:
                from plexora.server.utils.formats.base import FormatUnreadable

                raise FormatUnreadable(f"{Path(path).name} holds no image")
            self.images = [str(image.name or f"Image {index + 1}")
                           for index, image in enumerate(images)]
            self.image_index = chosen(self.images, index, path)
            self._setup(images[self.image_index])
        except Exception:
            self.close()
            raise

    def _setup(self, image):
        self._image = image
        sizes = dict(image.sizes)
        self._sizes = sizes
        self._others = {name: 0 for name in image.dims if name not in _OWN}
        samples = int(sizes.get("S", 1))
        self.rgb = samples >= 3
        self.channels = 3 if self.rgb else int(sizes.get("C", 1))
        self.z_count = int(sizes.get("Z", 1))
        self.t_count = int(sizes.get("T", 1))
        self.dtype = np.dtype(image.dtype)
        coords = image.coords or {}
        x_um, y_um = _step(coords, "X"), _step(coords, "Y")
        if x_um:
            self.pixel_size = (x_um, y_um or x_um)
        if self.z_count > 1:
            self.z_step = _step(coords, "Z")
        names = [str(name) for name in coords.get("C", [])] if "C" in coords else []
        self.channel_names = (["Red", "Green", "Blue"] if self.rgb
                              else names_or_numbered(names, self.channels))
        self._tile_h, self._tile_w = int(sizes["Y"]), int(sizes["X"])
        self._places = self._mosaic(image) if int(sizes.get("M", 1)) > 1 else None
        if self._places:
            height = max(y for _m, y, _x in self._places) + self._tile_h
            width = max(x for _m, _y, x in self._places) + self._tile_w
        else:
            height, width = self._tile_h, self._tile_w
        self.levels = [(int(height), int(width))]
        self._frames = TileCache(capacity=max(8, 2 * len(self._places or ())))

    def _mosaic(self, image):
        """`[(m, y, x)]`: each tile's top-left corner in pixels, or None when
        the file does not place them (its tiles are then shown one at a time
        as the first)."""
        scan = getattr(image, "tilescan", None)
        tiles = getattr(scan, "tiles", None) if scan is not None else None
        count = int(self._sizes.get("M", 1))
        if tiles is None or len(tiles) != count or not self.pixel_size:
            return None
        pos_x = np.asarray(tiles["pos_x"], dtype=np.float64) * 1e6
        pos_y = np.asarray(tiles["pos_y"], dtype=np.float64) * 1e6
        if getattr(scan, "swap_xy", False):
            pos_x, pos_y = pos_y, pos_x
        if getattr(scan, "flip_x", False):
            pos_x = -pos_x
        if getattr(scan, "flip_y", False):
            pos_y = -pos_y
        xs = np.rint((pos_x - pos_x.min()) / self.pixel_size[0]).astype(int)
        ys = np.rint((pos_y - pos_y.min()) / self.pixel_size[1]).astype(int)
        if not (xs.any() or ys.any()):
            return None
        return [(m, int(ys[m]), int(xs[m])) for m in range(count)]

    def _frame(self, m, channel, z, t) -> np.ndarray:
        indices = dict(self._others)
        for name, value in (("M", m), ("C", channel), ("Z", z), ("T", t)):
            if name in self._sizes:
                indices[name] = int(value)
        if self.rgb:
            indices.pop("C", None)
        key = tuple(sorted(indices.items()))

        def make():
            with self._lock:
                return np.asarray(self._image.frame(**indices))

        return self._frames.get(key, make)

    def _region(self, channel, y0, y1, x0, x1, z, t, samples=0) -> np.ndarray:
        if not self._places:
            frame = self._frame(0, channel, z, t)
            return frame[y0:y1, x0:x1]
        shape = (y1 - y0, x1 - x0) + ((samples,) if samples else ())
        out = np.zeros(shape, dtype=self.dtype)
        for m, ty, tx in self._places:
            sy0, sy1 = max(y0, ty), min(y1, ty + self._tile_h)
            sx0, sx1 = max(x0, tx), min(x1, tx + self._tile_w)
            if sy0 >= sy1 or sx0 >= sx1:
                continue
            frame = self._frame(m, channel, z, t)
            out[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = frame[sy0 - ty:sy1 - ty, sx0 - tx:sx1 - tx]
        return out

    def read(self, level, channel, y0, y1, x0, x1, z=0, t=0) -> np.ndarray:
        if self.rgb:
            return np.ascontiguousarray(self.read_rgb(level, y0, y1, x0, x1, z, t)[..., int(channel)])
        return np.ascontiguousarray(self._region(int(channel), y0, y1, x0, x1, z, t))

    def read_rgb(self, level, y0, y1, x0, x1, z=0, t=0) -> np.ndarray:
        if not self.rgb:
            return super().read_rgb(level, y0, y1, x0, x1, z, t)
        block = np.asarray(self._region(0, y0, y1, x0, x1, z, t, samples=3))[..., :3]
        if block.dtype != np.uint8:
            from plexora.server.utils.brightfield import colour_bytes

            block = colour_bytes(block)
        return np.ascontiguousarray(block)

    def close(self):
        handle, self._file = getattr(self, "_file", None), None
        if handle is not None:
            handle.close()


def reader(path: str, index: int = 0) -> LifReader:
    return LifReader(path, index)
