"""Olympus FluoView OIB and OIF: FV300 to FV1200 (about 2003 to 2015).

An OIB is one OLE2 compound file holding a TIFF per plane and the settings
text; an OIF is the same as a folder (`<name>.oif` beside `<name>.oif.files/`).
Read with `oiffile` (Christoph Gohlke, BSD-3); loaded once and served from
memory (`base.WholeArrayReader`). The settings give each channel's dye
(`[Channel n Parameters] DyeName`) and the pixel size and z step
(`[Reference Image Parameter]`, `[Axis n Parameters Common]`). FluoView writes
numbers in the acquiring machine's locale: "0,207" is 0.207.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from plexora.server.utils.formats.base import (WholeArrayReader, as_tzcyx, chosen,
                                               names_or_numbered)

_UNITS = {"um": 1.0, "µm": 1.0, "nm": 1e-3, "mm": 1e3}


def _number(value):
    """A FluoView number, which may use a decimal comma ("0,207.0")."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().strip('"')
    if "," in text:
        text = re.sub(r"\.0+$", "", text).replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


class OibReader(WholeArrayReader):
    format_name = "Olympus OIB"

    def __init__(self, path: str, index: int = 0):
        super().__init__()
        import oiffile

        chosen([Path(path).stem], index, path)      # one image a file
        self._path = path
        if Path(path).suffix.lower() == ".oif":
            self.format_name = "Olympus OIF"
        with oiffile.OifFile(path) as handle:
            self._dims = tuple(handle.axes)
            sizes = dict(zip(self._dims, handle.shape))
            self.dtype = np.dtype(handle.dtype)
            self.channels = int(sizes.get("C", 1))
            self.z_count = int(sizes.get("Z", 1))
            self.t_count = int(sizes.get("T", 1))
            self.levels = [(int(sizes["Y"]), int(sizes["X"]))]
            main = handle.mainfile
            reference = main.get("Reference Image Parameter", {}) or {}
            x = _number(reference.get("WidthConvertValue"))
            y = _number(reference.get("HeightConvertValue"))
            scale = _UNITS.get(str(reference.get("WidthUnit") or "um").strip('"').lower(), 1.0)
            if x and x > 0:
                self.pixel_size = (x * scale, (y or x) * scale)
            names = []
            for key in sorted((key for key in main if re.fullmatch(r"Channel \d+ Parameters", key)),
                              key=lambda key: int(key.split()[1])):
                section = main[key]
                names.append(str(section.get("DyeName") or section.get("CH Name") or "").strip('"'))
            if len(names) != self.channels:
                names = []
            self.channel_names = names_or_numbered(names, self.channels)
            if self.z_count > 1:
                for key, section in main.items():
                    if key.startswith("Axis ") and str(section.get("AxisCode", "")).strip('"') == "Z":
                        step = _number(section.get("Interval"))
                        unit = _UNITS.get(str(section.get("UnitName") or "nm").strip('"').lower(), 1e-3)
                        if step:
                            self.z_step = abs(step) * unit
        self.images = [Path(path).stem]

    def _load(self, out=None):
        import oiffile

        with oiffile.OifFile(self._path) as handle:
            return as_tzcyx(np.asarray(handle.asarray()), self._dims)


def reader(path: str, index: int = 0) -> OibReader:
    return OibReader(path, index)
