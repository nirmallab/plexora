"""The channel image, read from a web address through the chunk cache.

Still a *local* provider in this package's sense (`is_local = True`): every
computation happens in this process and only the bytes travel, so nothing
here is proxied to a node and `data_model._remote` stays False. What differs
from a file on disk is three things, and they are the three overrides below:

* **Opening** dispatches on `remote_image.kind_of`, by name before any probe:
  an OME-Zarr store, a DICOM slide (one `.dcm` or a folder of instances,
  streamed through `remote_store.RemoteFile`), or a TIFF-family file read by
  `tiff_region`. Never a Xenium focus directory or an OpenSlide-only slide:
  those are read by path.
* **Identity** comes from the metadata document's (or the file's) ETag or
  Last-Modified, or a DICOM folder's listing -- not a stat
  (`Fingerprint.of_remote`).
* **The overview plane** is not downloaded while the project opens. Every
  tile request waits behind that open (it holds `data_model.load_lock`), and
  fetching a mid-resolution level first was seven of the eight seconds a
  first open of the IDR example took. `LazyOverview` fetches it the first
  time something reads it -- a channel histogram, which does not block tiles.
* **The quantisation ceiling** cannot come from reading every pixel of level 0
  -- for a large image that is gigabytes over a network before the first tile
  draws. It is read from the coarsest level plus a spread sample of level-0
  chunks, with headroom. When the store has been made available offline the
  exact full read is local-speed again, and is what runs.
"""

from __future__ import annotations

import math
import threading

import numpy as np

from plexora.server.providers.base import LOCAL, Fingerprint, ResourceLocator
from plexora.server.providers.local import LocalImageProvider

#: Level-0 chunks sampled for the quantisation ceiling. The first tile of a
#: channel waits for this sample, so it is sized for a slow link: 32 chunks
#: of a 5-channel IDR plate was ~48 MB, a minute and a half at the ~0.5 MB/s
#: IDR serves here -- past the viewer's 90 s tile timeout, and a tile that
#: times out is a hole until the page is reloaded.
WINDOW_SAMPLE_CHUNKS = 8

#: Headroom over the sampled maximum. A single bright pixel outside the sample
#: can still saturate; this makes a near miss very unlikely without darkening
#: every channel much.
WINDOW_HEADROOM = 1.25

#: Most a window estimate reads from level 0, per channel.
REMOTE_WINDOW_SCAN_BYTES = 12 * 1024 ** 2


class LazyOverview:
    """`overview_plane(pyramid)`, computed the first time it is read.

    Stands in for the numpy array the local providers hand back as `zarray`.
    Its readers index it (`zarray[channel]`) or convert it (`np.asarray`), and
    both land here; so does `.shape`. Computed once, under a lock, so two
    histogram requests arriving together download the level once.
    """

    def __init__(self, pyramid, compute=None):
        self._pyramid = pyramid
        #: The format's own `overview_plane`; OME-Zarr's when not given.
        self._compute = compute
        self._array = None
        self._lock = threading.Lock()

    def materialize(self):
        if self._array is None:
            with self._lock:
                if self._array is None:
                    compute = self._compute
                    if compute is None:
                        from plexora.server.utils import ome_zarr

                        compute = ome_zarr.overview_plane
                    self._array = compute(self._pyramid)
                    self._pyramid = None
        return self._array

    def __getitem__(self, index):
        return self.materialize()[index]

    def __array__(self, dtype=None, copy=None):
        array = self.materialize()
        return array if dtype is None else array.astype(dtype)

    def __len__(self):
        return len(self.materialize())

    @property
    def shape(self):
        return self.materialize().shape

    @property
    def dtype(self):
        return self.materialize().dtype


class RemoteImageProvider(LocalImageProvider):
    """An image at an https/s3/gs/az address: OME-Zarr, DICOM, TIFF.

    `rgb` is the project's "read as colour" decision, carried exactly as the
    local provider carries it; only a TIFF consults it (DICOM states its own
    samples, and zarr has no interleaved layout).
    """

    is_local = True

    def __init__(self, url, pyramid=None, rgb=False):
        from plexora.server.utils import remote_store

        super().__init__(remote_store.canonical_url(url), pyramid, rgb=rgb)
        self._kind_memo = None

    @property
    def locator(self) -> ResourceLocator:
        return ResourceLocator(kind="image", provider=LOCAL, path=self._path)

    def store(self):
        from plexora.server.utils import remote_store

        return remote_store.open_store(self._path)[0]

    def kind(self) -> str:
        """`remote_image.kind_of` the address, asked once per provider."""
        if self._kind_memo is None:
            from plexora.server.utils import remote_image

            self._kind_memo = remote_image.kind_of(self._path)
        return self._kind_memo

    def _reads_colour(self) -> bool:
        from plexora.server.utils import brightfield

        return self._rgb or brightfield.is_rgb_layout(self._path)

    def _open_pyramid(self, extension=None):
        """The pyramid for this address, by kind, with `extension` appended."""
        from plexora.server.utils import (brightfield, dicom_wsi, ome_zarr,
                                          remote_image, tiff_region)

        kind = self.kind()
        if kind == remote_image.ZARR:
            return ome_zarr.open_image(self._path, extension=extension)
        if kind == remote_image.DICOM:
            return dicom_wsi.open_image(self._path, extension=extension, rgb=self._rgb)
        if kind == remote_image.TIFF:
            if self._reads_colour():
                return brightfield.open_rgb(self._path, extension=extension)
            return tiff_region.open_image(self._path, extension=extension)
        raise ValueError(
            f"{self._path} is a picture, which is drawn whole rather than "
            "served as tiles.")

    def _missing_pyramid(self):
        """The derived coarse levels, rebuilt into the project if they went.

        Same rule as the local provider, through the reader for this kind.
        """
        from pathlib import Path

        from plexora.server.utils import ome_zarr

        if not self._pyramid or Path(self._pyramid).exists():
            return self._pyramid
        try:
            pyramid = self._open_pyramid()
            try:
                return ome_zarr.build_extension(pyramid, self._pyramid)
            finally:
                _close(pyramid)
        except Exception:  # noqa: BLE001 -- fewer levels beats no viewer
            return None

    def open(self):
        from plexora.server.utils import (brightfield, dicom_wsi, ome_zarr,
                                          remote_image, tiff_region)

        kind = self.kind()
        channels = self._open_pyramid(self._missing_pyramid())
        if kind == remote_image.ZARR:
            return (channels, LazyOverview(channels),
                    ome_zarr.physical_metadata(channels))
        if kind == remote_image.DICOM:
            return (channels, LazyOverview(channels, compute=dicom_wsi.overview_plane),
                    dicom_wsi.physical_metadata(channels))
        compute = brightfield.overview_plane if isinstance(
            channels, brightfield.RgbPyramid) else tiff_region.overview_plane
        return (channels, LazyOverview(channels, compute=compute),
                brightfield.physical_metadata(self._path))

    def fingerprint(self):
        return Fingerprint.of_remote(self._path)

    def quantization_window(self, channel_index, channels=None):
        """(0, ceiling) for one channel, without reading all of level 0.

        `channels` is the open pyramid when the caller already has it.
        """
        from plexora.server.models import remote_sources

        pyramid = channels if channels is not None else self._open_pyramid()
        if remote_sources.is_pinned(self._path):
            from plexora.server.models import data_model

            return data_model.quantization_window_of(pyramid, channel_index)
        return sampled_window(pyramid, channel_index)


def _close(pyramid) -> None:
    close = getattr(pyramid, "close", None)
    if close is not None:
        try:
            close()
        except Exception:  # noqa: BLE001
            pass

def sampled_window(pyramid, channel_index) -> tuple:
    """(0, ceiling) from the coarsest level and a sample of level-0 chunks.

    Deterministic -- the same chunks every time -- so a restart computes the
    same window and tiles encoded before and after it agree.
    """
    from plexora.server.utils import ome_zarr

    levels = len(pyramid)
    coarsest = pyramid[levels - 1]
    ome_zarr.prefetch_level(coarsest, [channel_index])
    ceiling = float(np.asarray(coarsest[channel_index]).max()) if coarsest.shape[-1] else 0.0

    finest = pyramid[0]
    height, width = int(finest.shape[-2]), int(finest.shape[-1])
    chunks = getattr(finest, "chunks", None) or (1, 1024, 1024)
    tile_h, tile_w = max(1, int(chunks[-2])), max(1, int(chunks[-1]))
    rows, cols = math.ceil(height / tile_h), math.ceil(width / tile_w)
    itemsize = int(getattr(finest.dtype, "itemsize", 2))
    per_chunk = tile_h * tile_w * itemsize
    budget = max(1, REMOTE_WINDOW_SCAN_BYTES // max(1, per_chunk))
    count = min(WINDOW_SAMPLE_CHUNKS, rows * cols, budget)
    # A lattice across the plane: every row band and column band is visited
    # before any is visited twice.
    side = max(1, int(math.ceil(math.sqrt(count))))
    picks = set()
    for i in range(side):
        for j in range(side):
            if len(picks) >= count:
                break
            r = min(rows - 1, int((i + 0.5) * rows / side))
            c = min(cols - 1, int((j + 0.5) * cols / side))
            picks.add((r, c))
    ome_zarr.prefetch(finest, channel_index,
                      [(r * tile_h, c * tile_w) for r, c in sorted(picks)])
    for r, c in sorted(picks):
        block = np.asarray(finest[channel_index, r * tile_h:(r + 1) * tile_h,
                                  c * tile_w:(c + 1) * tile_w])
        if block.size:
            ceiling = max(ceiling, float(block.max()))
    ceiling *= WINDOW_HEADROOM
    dtype = np.dtype(finest.dtype)
    if np.issubdtype(dtype, np.integer):
        ceiling = min(ceiling, float(np.iinfo(dtype).max))
    return (0.0, max(ceiling, 1.0))
