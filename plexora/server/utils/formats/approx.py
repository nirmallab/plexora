"""Plexora's smaller images (`.approx`): what a `.plexora` file carries with
"Smaller images" on (server/utils/approximate_image.py), read where it lies.

Nothing is written back on import: the container is opened here, on the
desktop and on the website alike -- its tiles need numpy and zstd only. Its
levels are the original image's own pyramid; each 1024 x 1024 tile of a
channel is decoded when a read first touches it and kept in a `TileCache`.
Channel names and the pixel size come from the original's description
(OME-XML) the index keeps, else from its resolution tags.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from plexora.server.utils import approximate_image, ome_xml
from plexora.server.utils.formats.base import FormatUnreadable, Reader, TileCache, assemble, chosen

#: µm in one of each OME length unit.
_MICRONS = {"µm": 1.0, "um": 1.0, "micron": 1.0, "nm": 1e-3, "mm": 1e3, "cm": 1e4, "m": 1e6}
#: µm in one resolution unit of a TIFF (2: inch, 3: centimetre).
_PER_UNIT = {2: 25400.0, 3: 10000.0}


def _pixel_size(index: dict):
    """(x, y) in µm, from the OME-XML or the resolution tags; None if neither."""
    description = index.get("description")
    found = ome_xml.pixels(description) if description else {}
    x = found.get("physical_size_x")
    if x:
        y = found.get("physical_size_y") or x
        try:
            return (float(x) * _MICRONS.get(found.get("physical_size_x_unit") or "µm", 1.0),
                    float(y) * _MICRONS.get(found.get("physical_size_y_unit") or "µm", 1.0))
        except (TypeError, ValueError):
            pass
    resolution, unit = index.get("resolution"), index.get("resolutionunit")
    if resolution and unit in _PER_UNIT and all(resolution):
        return tuple(_PER_UNIT[unit] / float(value) for value in resolution)
    return None


class ApproxReader(Reader):
    format_name = "Plexora smaller image"

    def __init__(self, path: str, index: int = 0):
        super().__init__()
        chosen([Path(path).name], index, path)      # one image a file
        try:
            self._container = approximate_image.Container(path)
        except (OSError, ValueError, KeyError) as error:
            raise FormatUnreadable(f"{Path(path).name}: {error}") from error
        shapes = self._container.shapes
        self.channels = int(shapes[0][0]) if len(shapes[0]) == 3 else 1
        self.levels = [(int(shape[-2]), int(shape[-1])) for shape in shapes]
        self.dtype = np.dtype(np.uint16)
        meta = self._container.index
        names = ome_xml.channel_names(meta.get("description")) if meta.get("description") else []
        self.channel_names = [name or f"Channel {number + 1}" for number, name in enumerate(names)] \
            if len(names) == self.channels else [f"Channel {number + 1}" for number in range(self.channels)]
        self.pixel_size = _pixel_size(meta)
        self.images = [Path(path).name]
        self._cache = TileCache()

    def read(self, level, channel, y0, y1, x0, x1, z=0, t=0) -> np.ndarray:
        level, channel, size = int(level), int(channel), approximate_image.TILE

        def tile(ty, tx):
            return self._cache.get((level, channel, ty, tx),
                                   lambda: self._container.tile(level, channel, ty, tx))

        return assemble(tile, size, size, int(y0), int(y1), int(x0), int(x1), self.dtype)

    def close(self):
        container, self._container = getattr(self, "_container", None), None
        if container is not None:
            container.close()


def reader(path: str, index: int = 0) -> ApproxReader:
    return ApproxReader(path, index)
