"""Nikon ND2: NIS-Elements files from the JPEG 2000 era (format 1.0, about
2003-2008) through the chunk-mapped 2.x and 3.x files written today.

Read with `nd2` (Talley Lambert, BSD-3), which knows all three layouts and
their metadata. A frame is one (time point, position, z) of every channel; the
reader here keeps the last frames it decoded, because a legacy file's frame is
one JPEG 2000 image (a third of a second to decode) and a viewer asks for many
tiles of the same one. A modern uncompressed file's frame is a view on the
file, so a tile reads only its own rows.

- **Positions.** A multi-point acquisition's positions are separate fields of
  view, not one image; each is an image of its own (`reader(path, index)`),
  and the import offers each as a sample.
- **No pyramid.** ND2 has none; a frame larger than a tile gets coarse levels
  derived at import like any flat image's.
- **Calibration.** Pixel size and z step only where the file says they were
  calibrated (an uncalibrated ND2 states 1 µm).
- **Colour.** An RGB camera's frames (brightfield) are a colour picture.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from plexora.server.utils.formats.base import Reader, TileCache, chosen, names_or_numbered

_LOOPS = ("T", "P", "Z")


class Nd2Reader(Reader):
    format_name = "Nikon ND2"

    def __init__(self, path: str, index: int = 0):
        super().__init__()
        import nd2

        try:
            self._file = nd2.ND2File(path)
        except OSError:
            # A file system that refuses to map a file (the website's: "[Errno
            # 43] No such device") -- nd2 reads an open file by seek and read
            # instead, and closes it with itself.
            self._file = nd2.ND2File(open(path, "rb"))
        try:
            self._setup(Path(path), index)
        except Exception:
            self.close()
            raise

    def _setup(self, path: Path, index: int = 0):
        handle = self._file
        sizes = dict(handle.sizes)
        self._loop_names = [name for name in sizes if name not in ("C", "Y", "X", "S")]
        self._loop_sizes = [int(sizes[name]) for name in self._loop_names]
        self.rgb = bool(handle.is_rgb)
        self.channels = 3 if self.rgb else int(sizes.get("C", 1))
        self.z_count = int(sizes.get("Z", 1))
        self.t_count = int(sizes.get("T", 1))
        positions = int(sizes.get("P", 1))
        self.images = ([f"Position {number + 1}" for number in range(positions)]
                       if positions > 1 else [path.stem])
        self.image_index = chosen(self.images, index, path)
        self.dtype = np.dtype(handle.dtype)
        self.levels = [(int(sizes["Y"]), int(sizes["X"]))]
        metadata = handle.metadata
        calibrated = (True, True, True)
        names = []
        if metadata is not None and getattr(metadata, "channels", None):
            first = metadata.channels[0]
            calibrated = tuple(getattr(first.volume, "axesCalibrated", calibrated) or calibrated)
            names = [getattr(channel.channel, "name", "") or "" for channel in metadata.channels]
        voxel = handle.voxel_size()
        if calibrated[0] and voxel.x and voxel.x > 0:
            self.pixel_size = (float(voxel.x), float(voxel.y or voxel.x))
        if self.z_count > 1 and voxel.z and voxel.z > 0 and (calibrated[2] or voxel.z != 1.0):
            self.z_step = float(voxel.z)
        self.channel_names = (["Red", "Green", "Blue"] if self.rgb
                              else names_or_numbered(names, self.channels))
        self._frames = TileCache(capacity=4)

    def _frame(self, z: int, t: int) -> np.ndarray:
        """`(C, Y, X)`, or `(Y, X, 3)` for colour, of this reader's position."""
        coords = {"T": int(t), "Z": int(z), "P": int(self.image_index)}
        index = int(np.ravel_multi_index(
            [min(coords.get(name, 0), size - 1) for name, size in
             zip(self._loop_names, self._loop_sizes)], self._loop_sizes)) if self._loop_sizes else 0

        def make():
            with self._lock:
                frame = self._file.read_frame(index)
            if not self.rgb and frame.ndim == 2:
                frame = frame[None]
            return frame

        return self._frames.get(index, make)

    def read(self, level, channel, y0, y1, x0, x1, z=0, t=0) -> np.ndarray:
        if self.rgb:
            return np.ascontiguousarray(self.read_rgb(level, y0, y1, x0, x1, z, t)[..., int(channel)])
        frame = self._frame(z, t)
        return np.ascontiguousarray(frame[int(channel), y0:y1, x0:x1])

    def read_rgb(self, level, y0, y1, x0, x1, z=0, t=0) -> np.ndarray:
        if not self.rgb:
            return super().read_rgb(level, y0, y1, x0, x1, z, t)
        block = np.asarray(self._frame(z, t)[y0:y1, x0:x1, :3])
        if block.dtype != np.uint8:
            from plexora.server.utils.brightfield import colour_bytes

            block = colour_bytes(block)
        return np.ascontiguousarray(block)

    def close(self):
        handle, self._file = getattr(self, "_file", None), None
        if handle is not None:
            handle.close()


def reader(path: str, index: int = 0) -> Nd2Reader:
    return Nd2Reader(path, index)
