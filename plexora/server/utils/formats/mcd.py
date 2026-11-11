"""Imaging mass cytometry `.mcd`: Fluidigm / Standard BioTools Hyperion and
Hyperion XTi, MCD schema of every version so far.

Read with `readimc` (Bodenmiller group, MIT). An MCD holds a slide's
acquisitions (ROIs), each a block of float32 ion counts per pixel and channel;
each is an image of its own (`reader(path, index)`, counting every slide's
acquisitions in order), and the import offers each as a sample. An acquisition is small (a
few hundred to a few thousand pixels a side) and stored pixel-interleaved, so
it is read whole once and served from memory (`base.WholeArrayReader`); its
float counts are drawn through the value map every float image gets
(`value_range`).

Channel names: the panel's label where one was entered ("CD3"), else the
metal and mass ("Er170"). Pixel size: the ablation step (1 µm on every
Hyperion unless the run changed it).
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from plexora.server.utils.formats.base import (FormatUnreadable, WholeArrayReader, chosen,
                                               names_or_numbered)


def _name(label: str, metal: str) -> str:
    """"CD3((1234))Er170" or "CD3" -> "CD3"; nothing entered -> "Er170"."""
    label = re.sub(r"\(\(\d+\)\)\s*\w*$", "", str(label or "")).strip()
    if not label or label == metal or label.endswith(metal) and len(label) <= len(metal) + 3:
        return metal
    return label


class McdReader(WholeArrayReader):
    format_name = "Hyperion MCD"

    def __init__(self, path: str, index: int = 0):
        super().__init__()
        import readimc

        self._path = path
        try:
            with readimc.MCDFile(path) as handle:
                acquisitions = [acquisition for slide in handle.slides
                                for acquisition in slide.acquisitions]
        except OSError:
            # readimc maps the file to find its schema; a file system that
            # refuses to map one (the website's: "[Errno 43] No such device")
            # is read by seek and read instead.
            with _seeking(readimc)(path) as handle:
                acquisitions = [acquisition for slide in handle.slides
                                for acquisition in slide.acquisitions]
        if not acquisitions:
            raise FormatUnreadable(f"{Path(path).name} holds no acquisition")
        self.images = [str(acquisition.description or f"Acquisition {acquisition.id}")
                       for acquisition in acquisitions]
        self.image_index = chosen(self.images, index, path)
        acquisition = acquisitions[self.image_index]
        self._acquisition = acquisition
        self.dtype = np.dtype(np.float32)
        self.channels = int(acquisition.num_channels)
        self.levels = [(int(acquisition.height_px), int(acquisition.width_px))]
        metals = [str(name) for name in acquisition.channel_names]
        labels = list(acquisition.channel_labels or [""] * len(metals))
        self.channel_names = names_or_numbered(
            [_name(label, metal) for label, metal in zip(labels, metals)], self.channels)
        x = getattr(acquisition, "pixel_size_x_um", None)
        y = getattr(acquisition, "pixel_size_y_um", None)
        if x and float(x) > 0:
            self.pixel_size = (float(x), float(y or x))

    def _load(self, out=None):
        import readimc

        try:
            with readimc.MCDFile(self._path) as handle:
                # `strict=False`: an acquisition stopped early holds fewer pixels
                # than its size says; the rest read as zero, as the MCD viewer shows.
                array = handle.read_acquisition(self._acquisition, strict=False)
        except OSError:
            # Mapping refused (see `__init__`): the same records, read.
            array = _read_acquisition(self._path, self._acquisition)
        return np.asarray(array, dtype=np.float32)[np.newaxis, np.newaxis]


def _seeking(readimc):
    """readimc's `MCDFile`, finding its schema by seek and read rather than
    by mapping the file: the last `<MCDSchema ...</MCDSchema>` in it, UTF-16,
    as readimc takes it (a version 1 file holds several; the last is current)."""

    class SeekingMCDFile(readimc.MCDFile):
        def _read_schema_xml(self, encoding="utf-16-le", start_sub="<MCDSchema",
                             end_sub="</MCDSchema>"):
            handle = self._fh
            if handle is None:
                raise IOError(f"MCD file '{self.path.name}' has not been opened")
            size = handle.seek(0, 2)
            start_bytes, end_bytes = start_sub.encode(encoding), end_sub.encode(encoding)
            window = 1 << 20
            while True:
                low = max(0, size - window)
                handle.seek(low)
                tail = handle.read(size - low)
                start = tail.rfind(start_bytes)
                if start != -1 or low == 0:
                    break
                window *= 4
            if start == -1:
                raise IOError(f"MCD file '{self.path.name}' corrupted: no {start_sub}")
            end = tail.rfind(end_bytes, start)
            if end == -1:
                raise IOError(f"MCD file '{self.path.name}' corrupted: no {end_sub}")
            return tail[start:end + len(end_bytes)].decode(encoding)

    return SeekingMCDFile


def _read_acquisition(path, acquisition) -> np.ndarray:
    """An acquisition's counts as `(channels, y, x)` float32, read by seek and
    read: the pixel records between its `DataStartOffset` and `DataEndOffset`,
    each X, Y, Z and then one float32 per channel, placed at their own X and Y
    (what readimc reads by mapping the file). Records an acquisition stopped
    early never wrote read as zero."""
    meta = acquisition.metadata
    try:
        start, end = int(meta["DataStartOffset"]), int(meta["DataEndOffset"])
        width, height = int(meta["MaxX"]), int(meta["MaxY"])
        value_bytes = int(meta.get("ValueBytes", 4))
    except (KeyError, ValueError) as error:
        raise FormatUnreadable(
            f"{Path(path).name}: acquisition {acquisition.id} says nowhere where its data is") from error
    if value_bytes != 4:
        raise FormatUnreadable(
            f"{Path(path).name}: acquisition {acquisition.id} stores {value_bytes}-byte values")
    channels = int(acquisition.num_channels)
    image = np.zeros((channels, height, width), dtype=np.float32)
    record = (channels + 3) * 4
    with open(path, "rb") as handle:
        end = min(end, handle.seek(0, 2))
        count = max(0, (end - start) // record)
        handle.seek(start)
        data = np.frombuffer(handle.read(count * record), dtype="<f4")
    count = data.size // (channels + 3)
    if not count:
        return image
    data = data[:count * (channels + 3)].reshape(count, channels + 3)
    xs, ys = data[:, 0].astype(np.int64), data[:, 1].astype(np.int64)
    inside = (xs >= 0) & (xs < width) & (ys >= 0) & (ys < height)
    image[:, ys[inside], xs[inside]] = data[inside, 3:].T
    return image


def reader(path: str, index: int = 0) -> McdReader:
    return McdReader(path, index)
