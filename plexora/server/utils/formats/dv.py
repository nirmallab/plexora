"""DeltaVision (`.dv`, `.r3d`): softWoRx / Priism files, 1990s to today.

A 1024-byte header (the Priism layout, a relative of MRC's), an extended
header of per-plane records, then the planes uncompressed, one after another.
The header says everything the viewer needs and has not changed in thirty
years:

- the plane size (bytes 0-7), the plane count (8), the pixel type (12:
  0 uint8, 1 int16, 2 float32, 6 uint16, 7 int32);
- the pixel size in µm (40, 44, 48: x, y, z);
- the extended header's size (92), so where the planes start (1024 + it);
- the byte order, from the DeltaVision mark (96: 0xC0A0 as written);
- the time points (180), the order the planes are in (182: 0 z-t-wavelength,
  1 wavelength-z-t, 2 z-wavelength-t), the wavelength count (196) and each
  wavelength in nm (198-207), which is what names its channels.

A region is a seek and one read of its rows: nothing is decoded. DeltaVision
images are camera frames (512 to 2048 px), so they have no pyramid; a larger
one gets its coarse levels derived at import like any flat image.
"""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

from plexora.server.utils.formats.base import FormatUnreadable, Reader, chosen

_MARK = 0xC0A0
_TYPES = {0: np.uint8, 1: np.int16, 2: np.float32, 6: np.uint16, 7: np.int32}


class DvReader(Reader):
    format_name = "DeltaVision"

    def __init__(self, path: str, index: int = 0):
        super().__init__()
        chosen([Path(path).name], index, path)      # one image a file
        self._handle = open(path, "rb")
        header = self._handle.read(1024)
        if len(header) < 1024:
            raise FormatUnreadable(f"{Path(path).name} is shorter than a DeltaVision header")
        for order in "<>":
            if struct.unpack_from(order + "H", header, 96)[0] == _MARK:
                break
        else:
            raise FormatUnreadable(f"{Path(path).name} has no DeltaVision mark")
        width, height, sections, mode = struct.unpack_from(order + "4i", header, 0)
        dx, dy, dz = struct.unpack_from(order + "3f", header, 40)
        extended = struct.unpack_from(order + "i", header, 92)[0]
        times, sequence = struct.unpack_from(order + "2h", header, 180)
        waves = struct.unpack_from(order + "h", header, 196)[0]
        wavelengths = struct.unpack_from(order + "5h", header, 198)
        if mode not in _TYPES:
            raise FormatUnreadable(f"DeltaVision pixel type {mode} is not read")
        waves = max(1, int(waves))
        times = max(1, int(times))
        depth = max(1, int(sections) // (waves * times))
        if depth * waves * times != sections:
            # A header that undercounts (an aborted acquisition): read what
            # the plane count supports, as a stack of z.
            waves, times, depth = 1, 1, max(1, int(sections))
        self.dtype = np.dtype(_TYPES[mode]).newbyteorder(order)
        self._start = 1024 + max(0, int(extended))
        self._row = int(width) * self.dtype.itemsize
        self._plane = self._row * int(height)
        self._sequence = int(sequence) if sequence in (0, 1, 2) else 0
        self._sizes = (depth, waves, times)
        self.levels = [(int(height), int(width))]
        self.channels, self.z_count, self.t_count = waves, depth, times
        self.dtype = np.dtype(self.dtype.newbyteorder("="))
        self._order = order
        if dx > 0:
            self.pixel_size = (float(dx), float(dy if dy > 0 else dx))
        if depth > 1 and dz > 0:
            self.z_step = float(dz)
        names = [f"{int(wave)} nm" for wave in wavelengths[:waves] if wave > 0]
        self.channel_names = names if len(names) == waves else \
            [f"Channel {index + 1}" for index in range(waves)]
        self.images = [Path(path).name]

    def _index(self, z: int, w: int, t: int) -> int:
        depth, waves, times = self._sizes
        if self._sequence == 1:          # wavelength fastest, then z, then t
            return w + waves * (z + depth * t)
        if self._sequence == 2:          # z, then wavelength, then t
            return z + depth * (w + waves * t)
        return z + depth * (t + times * w)   # z, then t, then wavelength

    def read(self, level, channel, y0, y1, x0, x1, z=0, t=0) -> np.ndarray:
        plane = self._index(int(z), int(channel), int(t))
        offset = self._start + plane * self._plane + int(y0) * self._row
        count = (int(y1) - int(y0)) * self._row
        with self._lock:
            self._handle.seek(offset)
            data = self._handle.read(count)
        width = self._row // self.dtype.itemsize
        data = data[:len(data) // self._row * self._row]   # a file cut mid-row
        rows = np.frombuffer(data, dtype=np.dtype(self.dtype).newbyteorder(self._order),
                             count=len(data) // self.dtype.itemsize)
        rows = rows.reshape(-1, width)[:, int(x0):int(x1)]
        out = np.zeros((int(y1) - int(y0), int(x1) - int(x0)), dtype=self.dtype)
        out[:rows.shape[0]] = rows      # a truncated file reads as zeros past its end
        return out

    def close(self):
        handle, self._handle = getattr(self, "_handle", None), None
        if handle is not None:
            handle.close()


def reader(path: str, index: int = 0) -> DvReader:
    return DvReader(path, index)
