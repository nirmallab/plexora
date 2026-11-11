"""Zeiss CZI: ZEN 2011 to today -- Axioscan slide scans, LSM confocal stacks,
Celldiscoverer and widefield acquisitions.

Read with `czifile` (Christoph Gohlke, BSD-3), which knows the format's
versions and codecs (uncompressed, JPEG, JPEG XR, zstd, zstd with hi/lo byte
packing), composites a mosaic's overlapping tiles, and serves a region of any
pyramid level a slide scan stored. What this module adds is the viewer's
vocabulary:

- **Scenes.** A slide scan's scenes are its tissue regions; each is an image
  of its own (`reader(path, index)`), and the import offers each as a sample.
- **Levels.** A scan's own pyramid levels are used as they are; an
  acquisition without one (a confocal stack) gets coarse levels derived at
  import like any flat image's.
- **Colour.** A brightfield scan's samples (BGR in the file, RGB from
  czifile) are a colour picture; 16-bit colour is brought to 8 bits.
- **Coordinates.** CZI places every scene on the stage, so a region is asked
  for in level-0 stage coordinates, `bbox` offset and level-scaled.
- **Names.** Each channel's name from the metadata (the dye or the channel
  name ZEN shows), and the pixel size from its scaling (metres, as µm).

On the website, JPEG XR is decoded by jxrlib in WebAssembly in the Python
worker (client/src/js/workers/jpegxr.js, through imagecodecs_shim).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from plexora.server.utils.formats.base import (FormatUnreadable, Reader, chosen,
                                               names_or_numbered)

#: Subblocks kept decoded: a viewer tile is a corner of one, and its
#: neighbours ask for the same one.
_CACHE_SUBBLOCKS = 64


class CziReader(Reader):
    format_name = "Zeiss CZI"

    def __init__(self, path: str, index: int = 0):
        super().__init__()
        import czifile

        self._czi = czifile.CziFile(path, squeeze=False)
        try:
            self._czi.set_lock(True)
            self._czi.maxcache = _CACHE_SUBBLOCKS
            scenes = self._czi.scenes
            keys = list(scenes.keys())
            if not keys:
                raise FormatUnreadable(f"{Path(path).name} holds no image")
            self.images = [f"Scene {key + 1}" for key in keys] if len(keys) > 1 else [Path(path).stem]
            self.image_index = chosen(self.images, index, path)
            image = scenes[keys[self.image_index]]
            self._open(image)
        except Exception:
            self.close()
            raise

    def _open(self, image):
        dims = tuple(image.dims)
        sizes = dict(image.sizes)
        if "Y" not in dims or "X" not in dims:
            raise FormatUnreadable("This CZI image has no Y and X axes")
        self._image = image
        self._levels = list(getattr(image, "levels", None) or [image])
        self._dims = dims
        samples = int(sizes.get("S", 1))
        self.rgb = samples >= 3 and int(sizes.get("C", 1)) == 1
        self._samples = samples
        if self.rgb:
            self.channels = 3
        else:
            self.channels = int(sizes.get("C", 1))
        self.z_count = int(sizes.get("Z", 1))
        self.t_count = int(sizes.get("T", 1))
        self.dtype = np.dtype(image.dtype)
        y, x = dims.index("Y"), dims.index("X")
        self.levels = [(int(level.shape[y]), int(level.shape[x])) for level in self._levels]
        x0, y0, width, height = (int(value) for value in image.bbox)
        self._origin = (x0, y0)
        self._full = (height, width)
        mpp = getattr(image, "mpp", None)
        if mpp and mpp[0]:
            self.pixel_size = (float(mpp[0]), float(mpp[1] or mpp[0]))
        names = list((getattr(image, "channels", None) or {}).keys())
        self.channel_names = (["Red", "Green", "Blue"] if self.rgb
                              else names_or_numbered(names, self.channels))
        if self.z_count > 1:
            self.z_step = _z_step(image)
        self._fill = 255 if self.rgb else 0

    def _region(self, level, y0, y1, x0, x1, selection) -> np.ndarray:
        """`(h, w, samples)` of `level` at level coordinates."""
        height, width = self.levels[level]
        scale_y = self._full[0] / float(height)
        scale_x = self._full[1] / float(width)
        roi = (self._origin[0] + int(round(x0 * scale_x)),
               self._origin[1] + int(round(y0 * scale_y)),
               max(1, int(round((x1 - x0) * scale_x))),
               max(1, int(round((y1 - y0) * scale_y))))
        out_h, out_w = int(y1 - y0), int(x1 - x0)
        try:
            view = self._levels[level](roi=roi, **selection)
            block = np.asarray(view.asarray(fillvalue=self._fill))
            dims = tuple(view.dims)
        except ValueError as exc:
            if "no subblocks" in str(exc):     # a region of the stage nothing was scanned at
                return np.full((out_h, out_w, self._samples), self._fill, dtype=self.dtype)
            raise
        order = [dims.index("Y"), dims.index("X")] + ([dims.index("S")] if "S" in dims else [])
        rest = [axis for axis in range(block.ndim) if axis not in order]
        block = np.transpose(block, rest + order).reshape(block.shape[order[0]],
                                                           block.shape[order[1]], -1)
        if block.shape[0] != out_h or block.shape[1] != out_w:
            # czifile rounds a level's region to its own pixel grid: trim or
            # pad the pixel the two roundings disagree by.
            fixed = np.full((out_h, out_w, block.shape[2]), self._fill, dtype=block.dtype)
            h, w = min(out_h, block.shape[0]), min(out_w, block.shape[1])
            fixed[:h, :w] = block[:h, :w]
            block = fixed
        return block

    def _selection(self, channel=None, z=0, t=0) -> dict:
        selection = {}
        for name in self._dims:
            if name in ("Y", "X", "S"):
                continue
            if name == "C":
                selection["C"] = int(channel or 0)
            elif name == "Z":
                selection["Z"] = int(z)
            elif name == "T":
                selection["T"] = int(t)
            elif name != "M":
                # Phase, illumination, view, block, ...: the first of each.
                selection[name] = 0
        return selection

    def read(self, level, channel, y0, y1, x0, x1, z=0, t=0) -> np.ndarray:
        if self.rgb:
            return np.ascontiguousarray(self.read_rgb(level, y0, y1, x0, x1, z, t)[..., int(channel)])
        block = self._region(int(level), y0, y1, x0, x1, self._selection(channel, z, t))
        return np.ascontiguousarray(block[..., 0])

    def read_rgb(self, level, y0, y1, x0, x1, z=0, t=0) -> np.ndarray:
        if not self.rgb:
            return super().read_rgb(level, y0, y1, x0, x1, z, t)
        block = self._region(int(level), y0, y1, x0, x1, self._selection(0, z, t))[..., :3]
        if block.dtype != np.uint8:
            from plexora.server.utils.brightfield import colour_bytes

            block = colour_bytes(block)
        return np.ascontiguousarray(block)

    def close(self):
        czi, self._czi = getattr(self, "_czi", None), None
        if czi is not None:
            czi.close()


def _z_step(image):
    """The z spacing in µm, from the image's coordinate scale for Z."""
    try:
        scales = image.coord_scales
        units = getattr(image, "coord_units", {}) or {}
    except Exception:
        return None
    value = (scales or {}).get("Z") if isinstance(scales, dict) else None
    if not value:
        return None
    unit = str(units.get("Z", "") if isinstance(units, dict) else "").lower()
    value = float(value)
    if unit in ("m", "meter", "metre"):
        value *= 1e6
    elif unit in ("nm",):
        value /= 1000.0
    elif not unit and value < 1e-3:       # metres, by its size
        value *= 1e6
    return abs(value) or None


def reader(path: str, index: int = 0) -> CziReader:
    return CziReader(path, index)
