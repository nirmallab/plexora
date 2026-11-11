"""Hamamatsu NDPIS: a NanoZoomer fluorescence scan, one NDPI file per channel.

The `.ndpis` is a short text file naming the set's NDPI files
(`NoImages=3`, `Image0=<file>.ndpi`, ...), each an ordinary NDPI pyramid of
one channel. The scanner stores each channel as a colour picture in the colour
it displays it in (DAPI blue, FITC green, TRITC red), so a channel's intensity
is the brightest of a pixel's three samples -- the one its colour lives in, and
the right answer for a channel shown in a mixed colour too.

Channel names come from each NDPI's own name tag (65434, "DAPI 2 (387)"), the
pixel size from the first file's resolution, and the levels are the files' own:
nothing is converted.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from plexora.server.utils.formats.base import (FormatUnreadable, Reader, chosen,
                                               names_or_numbered)

_NAME_TAG = 65434


def _members(path: Path) -> list:
    entries = {}
    count = None
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            key, sep, value = line.strip().partition("=")
            if not sep:
                continue
            key, value = key.strip(), value.strip()
            if key.lower() == "noimages":
                try:
                    count = int(value)
                except ValueError:
                    count = None
            elif key.lower().startswith("image") and key[5:].isdigit():
                entries[int(key[5:])] = value
    names = [entries[index] for index in sorted(entries)]
    if count is not None:
        names = names[:count]
    return [path.parent / name for name in names]


class NdpisReader(Reader):
    format_name = "Hamamatsu NDPIS"

    def __init__(self, path: str, index: int = 0):
        super().__init__()
        from plexora.server.utils import brightfield, tiff_region

        # One scan: its NDPI files are its channels, not images of their own.
        chosen([Path(path).stem], index, path)
        members = _members(Path(path))
        missing = [member.name for member in members if not member.is_file()]
        if not members or missing:
            raise FormatUnreadable(
                f"{Path(path).name} names its channels' NDPI files, and "
                + (f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not beside it."
                   if missing else "it names none."))
        self._tiffs = []
        self._natives = []
        names = []
        try:
            for member in members:
                tiff = tiff_region.open_tiff(str(member))
                self._tiffs.append(tiff)
                natives = tiff_region._native_levels(tiff, tiff.series[0])
                self._natives.append(natives)
                tag = tiff.pages[0].tags.get(_NAME_TAG)
                names.append(str(tag.value).strip() if tag is not None else member.stem)
        except Exception:
            self.close()
            raise
        shapes = [[(level.height, level.width) for level in natives] for natives in self._natives]
        if any(shape != shapes[0] for shape in shapes):
            self.close()
            raise FormatUnreadable(
                f"The channels named by {Path(path).name} are not the same size, "
                "so they are not one scan.")
        self.levels = shapes[0]
        self.channels = len(members)
        first = self._natives[0][0]
        self.dtype = np.dtype(first.dtype)
        self.channel_names = names_or_numbered(names, self.channels)
        self.images = [Path(path).stem]
        metadata = brightfield.physical_metadata(str(members[0]))
        size_x = metadata.get("physical_size_x")
        if size_x:
            self.pixel_size = (float(size_x), float(metadata.get("physical_size_y") or size_x))

    def read(self, level, channel, y0, y1, x0, x1, z=0, t=0) -> np.ndarray:
        native = self._natives[int(channel)][int(level)]
        with self._lock:
            if native.count == 1:
                return native.read(0, y0, y1, x0, x1)
            block = native.read_rgb(y0, y1, x0, x1)
        return np.ascontiguousarray(block.max(axis=-1))

    def close(self):
        from plexora.server.utils import tiff_region

        tiffs, self._tiffs = getattr(self, "_tiffs", []), []
        for tiff in tiffs:
            tiff_region.close_tiff(tiff)


def reader(path: str, index: int = 0) -> NdpisReader:
    return NdpisReader(path, index)
