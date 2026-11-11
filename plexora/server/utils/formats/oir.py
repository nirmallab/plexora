"""Olympus / Evident OIR: FluoView FV1200 (2014) to FV3000 and FV4000.

Read with `oirfile` (Christoph Gohlke, BSD-3), which knows the format's
blocks and its companion files (`<name>_00001`, holding further pixel blocks);
keep them beside the `.oir`. The package reads a whole image at once, so it is
loaded once and served from memory (`base.WholeArrayReader`). Pixel size and z
step from the file's axis scales, channel names from its channels.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from plexora.server.utils.formats.base import (FormatUnreadable, WholeArrayReader, as_tzcyx,
                                               chosen, names_or_numbered)

_UNITS = {"micrometer": 1.0, "nanometer": 1e-3, "millimeter": 1e3, "meter": 1e6}


class OirReader(WholeArrayReader):
    format_name = "Olympus OIR"

    def __init__(self, path: str, index: int = 0):
        super().__init__()
        import oirfile

        chosen([Path(path).stem], index, path)      # one image a file
        self._path = path
        with oirfile.OirFile(path) as handle:
            dims = tuple(handle.dims)
            if "Y" not in dims or "X" not in dims:
                raise FormatUnreadable(f"{Path(path).name} holds no image (a map or a line scan)")
            sizes = dict(handle.sizes)
            self._dims = dims
            self.dtype = np.dtype(handle.dtype)
            self.rgb = False
            self.channels = int(sizes.get("C", 1))
            self.z_count = int(sizes.get("Z", 1))
            self.t_count = int(sizes.get("T", 1))
            self.levels = [(int(sizes["Y"]), int(sizes["X"]))]
            scales = dict(handle.coord_scales or {})
            units = dict(handle.coord_units or {})
            x = _micrometres(scales.get("X"), units.get("X"))
            if x:
                self.pixel_size = (x, _micrometres(scales.get("Y"), units.get("Y")) or x)
            if self.z_count > 1:
                self.z_step = _micrometres(scales.get("Z"), units.get("Z"))
            names = [channel.name for channel in (handle.channels or ())]
            if len(names) != self.channels:
                names = []
            self.channel_names = names_or_numbered(names, self.channels)
        self.images = [Path(path).stem]

    def _load(self, out=None):
        import oirfile

        with oirfile.OirFile(self._path) as handle:
            return as_tzcyx(handle.asarray(out=out), self._dims)


def _micrometres(value, unit):
    if not value:
        return None
    return abs(float(value)) * _UNITS.get(str(unit or "micrometer").lower(), 1.0) or None


def reader(path: str, index: int = 0) -> OirReader:
    return OirReader(path, index)
