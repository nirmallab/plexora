"""Zeiss LSM: the confocal files of AIM and ZEN black (LSM 510, 5 Live,
Pascal, 700, 710, 780, 880), about 1999 to 2015, when CZI replaced them.

An LSM is a TIFF: one page per plane holding every channel of that plane as
its own strip, each page followed by a thumbnail page, and the Carl Zeiss
header (`CZ_LSMINFO`) giving the dimensions, the scan type and the voxel size,
with pointers to the channel names and the scan settings. Read with
`tifffile` (Christoph Gohlke, BSD-3; already Plexora's), which parses the
header and the scan information and unwraps the 32-bit strip offsets of a
file over 4 GB (`TiffFile._lsm_fix_strip_offsets`). What this module adds:

- **One plane at a time.** A plane is the data page at (position, t, z),
  thumbnails skipped; one channel of it is its own strip, read and decoded
  on its own and kept (`TileCache`) for the tiles around it. Opening reads
  the header and the page directory, no pixels; a time series of gigabytes
  reads only the planes looked at.
- **Scan types.** The page is the image: an x-y plane, an x-z section (rows
  are z, scaled by the z voxel size) or a line or point scan (rows are time,
  so no scale). The pages are indexed by z and t; a lambda stack's
  wavelengths are its channels.
- **Names.** The dye of a track that records one channel (from ZEN's dye
  database: "DAPI", "Alexa Fluor 488"), else the name ZEN shows for the
  channel ("Ch1-T1"; a lambda channel's centre wavelength, "423 nm"), else
  the data channel's name, else "Channel n".
- **Positions and tile scans.** A multi-position or tile-scan file
  (`DimensionP`, `DimensionM`) is shown one position or tile at a time:
  `images` names them and `index` picks one. Tiles are not stitched.
- **Scale.** `VoxelSizeX/Y/Z` (metres), else the scan information's sample,
  line and plane spacing (µm); `time_interval` (seconds) from the time
  stamps, else the header's interval.
- **No pyramid.** LSM has none; a plane larger than a tile gets coarse levels
  derived at import like any flat image's.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from plexora.server.utils.formats.base import (FormatUnreadable, Reader, TileCache,
                                               names_or_numbered)

#: CZ_LSMINFO dimension fields, by axis.
_DIMENSIONS = {"Z": "DimensionZ", "T": "DimensionTime", "P": "DimensionP",
               "M": "DimensionM", "C": "DimensionChannels"}
#: A voxel size below this (metres) is ZEN's placeholder, not a scale: a
#: single plane states its z as 1e-19 m.
_SMALLEST_METRES = 1e-10
#: What the decoded channel planes kept for the tiles around them may hold.
_CACHE_BYTES = 256 << 20
_NUMBER = re.compile(r"\d+(\.\d+)?")


def _truthy(value) -> bool:
    """ZEN's yes is 1 or -1 (a COM boolean)."""
    try:
        return int(value) != 0
    except (TypeError, ValueError):
        return bool(value)


def _metres_as_um(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value * 1e6 if value > _SMALLEST_METRES else None


def _positive(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _tracks(metadata) -> list:
    info = metadata.get("ScanInformation")
    tracks = info.get("Tracks") if isinstance(info, dict) else None
    return [track for track in tracks or () if isinstance(track, dict)]


def _data_channels(metadata, count: int) -> list:
    """`[(dye, data channel name)]` per image channel, in the file's channel
    order (the acquiring tracks' data channels, in order), or [] when the
    scan information does not account for every channel (a lambda stack
    names one data channel for all its wavelengths)."""
    out = []
    for track in _tracks(metadata):
        if not _truthy(track.get("Acquire")) or _truthy(track.get("IsBleachTrack")):
            continue
        data = [channel for channel in track.get("DataChannels") or ()
                if isinstance(channel, dict)]
        dyes = [str(channel.get("DyeName") or "").strip()
                for channel in track.get("DetectionChannels") or ()
                if isinstance(channel, dict) and _truthy(channel.get("Acquire"))]
        # A dye belongs to a channel only when the track records one channel
        # through one detector: otherwise which detector made which channel
        # is not written down.
        dye = dyes[0] if len(data) == 1 and len(dyes) == 1 else ""
        out.extend((dye, str(channel.get("Name") or "").strip()) for channel in data)
    return out if len(out) == count else []


def _wavelengths(metadata, count: int) -> list:
    """A lambda channel's centre wavelength in nm, or None."""
    ranges = metadata.get("ChannelWavelength")
    try:
        ranges = np.asarray(ranges, dtype=np.float64)
    except (TypeError, ValueError):
        return [None] * count
    if ranges.shape != (count, 2) or not np.all(ranges > 0):
        return [None] * count
    return [float(centre) * 1e9 for centre in ranges.mean(axis=1)]


def _channel_names(metadata, count: int) -> list:
    colours = metadata.get("ChannelColors")
    colours = colours.get("ColorNames") if isinstance(colours, dict) else None
    colours = ([str(name or "").strip() for name in colours]
               if isinstance(colours, (list, tuple)) and len(colours) == count else [""] * count)
    channels = _data_channels(metadata, count) or [("", "")] * count
    spectral = _truthy(metadata.get("SpectralScan"))
    waves = _wavelengths(metadata, count) if spectral else [None] * count
    names = []
    for index in range(count):
        dye, data_name = channels[index]
        name = dye or colours[index] or data_name
        if spectral and (not name or _NUMBER.fullmatch(name)):
            if name:
                name = f"{name} nm"
            elif waves[index]:
                name = f"{waves[index]:.0f} nm"
        names.append(name)
    return names_or_numbered(names, count)


class LsmReader(Reader):
    format_name = "Zeiss LSM"

    def __init__(self, path: str, index: int = 0):
        super().__init__()
        import tifffile

        name = Path(path).name
        try:
            self._tif = tifffile.TiffFile(path)
        except tifffile.TiffFileError as exc:
            raise FormatUnreadable(f"{name} is not a readable TIFF ({exc})") from exc
        try:
            self._setup(Path(path), int(index), tifffile)
        except Exception:
            self.close()
            raise

    def _setup(self, path: Path, index: int, tifffile):
        tif = self._tif
        metadata = tif.lsm_metadata if tif.is_lsm else None
        if not metadata:
            raise FormatUnreadable(f"{path.name} has no Zeiss LSM header (CZ_LSMINFO): "
                                   "it is not an LSM file")
        count = len(tif.pages)
        if not count:
            raise FormatUnreadable(f"{path.name} holds no image")
        keyframe = tif.pages[0]
        if keyframe.is_reduced:
            raise FormatUnreadable(f"{path.name} starts with a thumbnail, not an image")
        if keyframe.dtype is None:
            bits = keyframe.bitspersample
            if isinstance(bits, tuple):
                raise FormatUnreadable(
                    f"{path.name} stores its channels at different bit depths "
                    f"({', '.join(str(b) for b in bits)}), which is not read")
            raise FormatUnreadable(f"{path.name}: this LSM pixel type ({bits}-bit) is not read")
        # Every image page is followed by its thumbnail.
        step = 2 if count > 1 and tif.pages[1].is_reduced else 1
        self._page_index = range(0, count, step)
        self._keyframe = keyframe

        scan = tifffile.TIFF.CZ_LSMINFO_SCANTYPE.get(int(metadata.get("ScanType") or 0),
                                                     "TZCYX")
        rows_axis = scan[-2]                     # Y, or Z (x-z scans), or T (line scans)
        prefix = [axis for axis in scan[:-2] if axis != "C"]
        if int(metadata.get("DimensionP") or 0) > 0:
            prefix.insert(0, "P")
        if int(metadata.get("DimensionM") or 0) > 0:
            prefix.insert(0, "M")
        sizes = [max(1, int(metadata.get(_DIMENSIONS[axis]) or 1)) for axis in prefix]
        self._prefix, self._sizes = prefix, sizes
        dims = dict(zip(prefix, sizes))

        tiles, positions = dims.get("M", 1), dims.get("P", 1)
        if tiles * positions > 1:
            self.images = []
            for tile in range(tiles):
                for position in range(positions):
                    parts = ([f"Position {position + 1}"] if positions > 1 else []) + \
                            ([f"Tile {tile + 1}"] if tiles > 1 else [])
                    self.images.append(", ".join(parts))
        else:
            self.images = [path.stem]
        if not 0 <= index < len(self.images):
            raise FormatUnreadable(
                f"{path.name} holds {len(self.images)} image"
                f"{'s' if len(self.images) != 1 else ''}; there is no image {index + 1}")
        self.image_index = index
        tile, position = divmod(index, positions)
        self._fixed = {"M": tile, "P": position}

        self.z_count = dims.get("Z", 1)
        self.t_count = dims.get("T", 1)
        available = len(self._page_index)
        expected = int(np.prod(sizes)) if sizes else 1
        if available < expected and "T" in dims:
            # An acquisition stopped early: the header counts the time points
            # it was set up for, the file holds fewer. Show the ones written
            # (when time is the outermost axis; missing planes read as zeros).
            at = prefix.index("T")
            inner = int(np.prod(sizes[at + 1:])) if at + 1 < len(sizes) else 1
            if int(np.prod(sizes[:at])) == 1 and available >= inner:
                self.t_count = max(1, min(self.t_count, available // inner))

        samples = int(keyframe.samplesperpixel)
        self._contig = keyframe.planarconfig == 1 and samples > 1
        self.channels = samples
        self.rgb = False
        self.dtype = np.dtype(keyframe.dtype).newbyteorder("=")
        height, width = int(keyframe.imagelength), int(keyframe.imagewidth)
        self.levels = [(height, width)]
        self.channel_names = _channel_names(metadata, samples)

        info = metadata.get("ScanInformation")
        info = info if isinstance(info, dict) else {}
        x_um = _metres_as_um(metadata.get("VoxelSizeX")) or _positive(info.get("SampleSpacing"))
        y_um = _metres_as_um(metadata.get("VoxelSizeY")) or _positive(info.get("LineSpacing"))
        z_um = _metres_as_um(metadata.get("VoxelSizeZ")) or _positive(info.get("PlaneSpacing"))
        if x_um and rows_axis == "Y":
            self.pixel_size = (x_um, y_um or x_um)
        elif x_um and rows_axis == "Z" and z_um:
            self.pixel_size = (x_um, z_um)
        # A line scan's rows are time: it has no vertical scale.
        if self.z_count > 1 and z_um:
            self.z_step = z_um
        self.time_interval = None
        if self.t_count > 1:
            stamps = metadata.get("TimeStamps")
            if isinstance(stamps, np.ndarray) and stamps.ndim == 1 and stamps.size >= 2 \
                    and stamps.size >= self.t_count:
                step_s = float(np.diff(stamps[:self.t_count].astype(np.float64)).mean())
                self.time_interval = step_s if step_s > 0 else None
            if self.time_interval is None:
                self.time_interval = _positive(metadata.get("TimeIntervall"))

        plane_bytes = max(1, height * width * self.dtype.itemsize)
        self._planes = TileCache(capacity=max(4, min(256, _CACHE_BYTES // plane_bytes)))

    # -- planes ---------------------------------------------------------------

    def _page_number(self, z: int, t: int) -> int:
        """Which data page holds (this image's position or tile, t, z)."""
        coords = dict(self._fixed, Z=int(z), T=int(t))
        number = 0
        for axis, size in zip(self._prefix, self._sizes):
            number = number * size + min(max(int(coords.get(axis, 0)), 0), size - 1)
        return number

    def _decode(self, number: int, channel: int) -> np.ndarray:
        """One channel of one data page, `(h, w)`; zeros where the file holds
        nothing (a page never written, a strip cut short)."""
        height, width = self.levels[0]
        out = np.zeros((height, width), dtype=self.dtype)
        if number >= len(self._page_index):
            return out
        keyframe = self._keyframe
        with self._lock:
            tif = self._tif
            if tif is None:
                raise FormatUnreadable("the LSM file is closed")
            page = tif.pages[self._page_index[number]]
            offsets, counts = page.dataoffsets, page.databytecounts
            per_sample = len(offsets) if self._contig else max(1, len(offsets) // self.channels)
            first = 0 if self._contig else channel * per_sample
            chunks = []
            handle = tif.filehandle
            for segment in range(first, min(first + per_sample, len(offsets))):
                offset, size = int(offsets[segment]), int(counts[segment])
                if offset <= 0 or size <= 0:
                    continue
                handle.seek(offset)
                chunks.append((segment, handle.read(size)))
        rows_per_strip = int(keyframe.rowsperstrip or height)
        for segment, data in chunks:
            row = (segment % per_sample) * rows_per_strip
            try:
                block, _where, _shape = keyframe.decode(data, segment)
            except Exception as exc:
                if keyframe.compression != 1:
                    raise FormatUnreadable(f"an LSM strip could not be decoded ({exc})") from exc
                block = self._raw_rows(data)     # an uncompressed strip cut short
            if block is None:
                continue
            block = block[0] if block.ndim == 4 else block
            block = block[..., channel if self._contig else 0]
            rows = min(block.shape[0], height - row)
            if rows > 0:
                out[row:row + rows] = block[:rows, :width]
        return out

    def _raw_rows(self, data):
        """The whole rows of an uncompressed strip the file cuts short, or
        None when not one row is there."""
        _height, width = self.levels[0]
        samples = self.channels if self._contig else 1
        dtype = self.dtype.newbyteorder(self._tif.byteorder if self._tif else "<")
        row_bytes = width * samples * dtype.itemsize
        rows = len(data) // row_bytes
        if not rows:
            return None
        block = np.frombuffer(data, dtype=dtype, count=rows * width * samples)
        return block.reshape(rows, width, samples).astype(self.dtype)

    def _plane(self, channel: int, z: int, t: int) -> np.ndarray:
        number = self._page_number(z, t)
        return self._planes.get((number, int(channel)),
                                lambda: self._decode(number, int(channel)))

    def read(self, level, channel, y0, y1, x0, x1, z=0, t=0) -> np.ndarray:
        plane = self._plane(int(channel), int(z), int(t))
        return np.ascontiguousarray(plane[int(y0):int(y1), int(x0):int(x1)])

    def close(self):
        handle, self._tif = getattr(self, "_tif", None), None
        if handle is not None:
            handle.close()


def reader(path: str, index: int = 0) -> LsmReader:
    return LsmReader(path, index)
