"""Olympus / Evident ETS: the pixels of one cellSens scan (VS110, VS120, VS200
or a cellSens acquisition), opened without its .vsi.

A VSI slide is a `.vsi` file and a folder beside it, `_<name>_/stack<id>/`,
holding one ETS file per scan (vsi.py). An ETS is sometimes sent on its own:
an H&E scan's `frame_t.ets` copied out of its folder. It holds the tiles, the
pyramid, the pixel type and whether the samples are one colour picture -- so
an H&E scan opens as brightfield -- but not the channel names, the pixel size
or the exact size of the scan, which are in the .vsi:

- An ETS still in `_<name>_/stack<id>/` with `<name>.vsi` beside that folder
  takes them from the .vsi, from the stack its folder is.
- Without one, channels are "Channel 1", "Channel 2", ..., there is no scale
  bar, and the image is the whole tile grid: up to one tile wider and taller
  than the scan. cellSens pads its last tiles with the scan's background
  colour, and a tile it did not write is drawn in that colour too.

Each ETS is one image. The reading is vsi.py's (`VsiReader`).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from plexora.server.utils.formats.base import FormatUnreadable, Reader, TileCache
from plexora.server.utils.formats.vsi import VsiReader, _Ets, _stacks

#: The name cellSens gives every scan's pixels: `frame_t.ets`, `frame_t_0.ets`.
_SCANNER_NAME = re.compile(r"frame_t(_\d+)?\.ets", re.I)

#: The tile sides of an image. A stack folder can also hold data files that
#: are not images (`blob_21_f_Frame#0.ets`: 65536 x 1 "tiles").
_TILE_SIDES = range(16, 16385)

#: The most pixels a side of the tile grid may have (a 40x slide is ~200 000).
_GRID_LIMIT = 1 << 22


def _placed(path) -> Optional[tuple]:
    """(slide name, stack id) when `path` is in `_<name>_/stack<id>/`, the
    folders cellSens writes; else None."""
    ets = Path(str(path).replace("\\", "/"))
    stack = re.fullmatch(r"stack(\d+)", ets.parent.name)
    slide = re.fullmatch(r"_(.+)_", ets.parent.parent.name)
    if not stack:
        return None
    return (slide.group(1) if slide else "", int(stack.group(1)))


def name_of(path) -> str:
    """What to call an ETS, without its suffix: its own file name, unless that
    is the name cellSens gives every scan's pixels -- then the slide and the
    stack it is the pixels of, from its folders: `_kidney_/stack1/frame_t.ets`
    is "kidney stack1", so two of them are told apart."""
    name = Path(str(path).replace("\\", "/")).name
    stem = name[:-len(".ets")] if name.lower().endswith(".ets") else Path(name).stem
    if not _SCANNER_NAME.fullmatch(name):
        return stem
    placed = _placed(path)
    if not placed:
        return stem
    slide, ident = placed
    return f"{slide} stack{ident}" if slide else f"stack{ident}"


def _described_by(path):
    """The .vsi's description of the stack this ETS is the pixels of
    (`vsi._Stack`), when the ETS is still in `_<name>_/stack<id>/` with
    `<name>.vsi` beside that folder; else None."""
    placed = _placed(path)
    if not placed or not placed[0]:
        return None
    vsi = Path(str(path).replace("\\", "/")).parent.parent.parent / f"{placed[0]}.vsi"
    if not vsi.is_file():
        return None
    try:
        return _stacks(str(vsi)).get(placed[1])
    except Exception:  # noqa: BLE001 -- the pixels are still readable without names
        return None


class EtsReader(VsiReader):
    format_name = "Olympus ETS"

    def __init__(self, path: str):
        Reader.__init__(self)
        ets = _Ets(path)
        grid_w = (ets.extent[0] + 1) * ets.tile_w
        grid_h = (ets.extent[1] + 1) * ets.tile_h
        if (ets.tile_w not in _TILE_SIDES or ets.tile_h not in _TILE_SIDES
                or max(grid_w, grid_h) > _GRID_LIMIT):
            ets.close()
            raise FormatUnreadable(
                f"{Path(path).name} is not an image but one of cellSens's data files "
                f"(its tiles are {ets.tile_w} x {ets.tile_h} pixels). A scan's pixels "
                "are in a frame_t.ets file.")
        stack = _described_by(path)
        self.images = [stack.name if stack and stack.name else name_of(path)]
        self.image_index = 0
        self._ets = ets
        self._cache = TileCache()
        try:
            self._open(stack)
        except Exception:
            self.close()
            raise


def reader(path: str) -> EtsReader:
    return EtsReader(path)
