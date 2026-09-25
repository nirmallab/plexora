"""The channel image, read from a web address through the chunk cache.

Still a *local* provider in this package's sense (`is_local = True`): every
computation happens in this process and only the bytes travel, so nothing
here is proxied to a node and `data_model._remote` stays False. What differs
from a file on disk is three things, and they are the three overrides below:

* **Opening** is always the OME-Zarr branch -- a web address is never a TIFF,
  a DICOM folder or a Xenium focus directory here.
* **Identity** comes from the metadata document's ETag or Last-Modified, not a
  stat (`Fingerprint.of_remote`).
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
    """`ome_zarr.overview_plane(pyramid)`, computed the first time it is read.

    Stands in for the numpy array the local providers hand back as `zarray`.
    Its readers index it (`zarray[channel]`) or convert it (`np.asarray`), and
    both land here; so does `.shape`. Computed once, under a lock, so two
    histogram requests arriving together download the level once.
    """

    def __init__(self, pyramid):
        self._pyramid = pyramid
        self._array = None
        self._lock = threading.Lock()

    def materialize(self):
        if self._array is None:
            with self._lock:
                if self._array is None:
                    from plexora.server.utils import ome_zarr

                    self._array = ome_zarr.overview_plane(self._pyramid)
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
    """An OME-Zarr image at an https/s3/gs/az address."""

    is_local = True

    def __init__(self, url, pyramid=None):
        from plexora.server.utils import remote_store

        super().__init__(remote_store.canonical_url(url), pyramid, rgb=False)

    @property
    def locator(self) -> ResourceLocator:
        return ResourceLocator(kind="image", provider=LOCAL, path=self._path)

    def store(self):
        from plexora.server.utils import remote_store

        return remote_store.open_store(self._path)[0]

    def _missing_pyramid(self):
        """The derived coarse levels, rebuilt into the project if they went.

        Same rule as the local provider, minus the DICOM and brightfield probes
        that would try to read a URL as a file.
        """
        from pathlib import Path

        from plexora.server.utils import ome_zarr

        if not self._pyramid or Path(self._pyramid).exists():
            return self._pyramid
        try:
            return ome_zarr.build_extension(ome_zarr.open_image(self._path), self._pyramid)
        except Exception:  # noqa: BLE001 -- fewer levels beats no viewer
            return None

    def open(self):
        from plexora.server.utils import ome_zarr

        channels = ome_zarr.open_image(self._path, extension=self._missing_pyramid())
        return (channels, LazyOverview(channels), ome_zarr.physical_metadata(channels))

    def fingerprint(self):
        return Fingerprint.of_remote(self._path)

    def quantization_window(self, channel_index, channels=None):
        """(0, ceiling) for one channel, without reading all of level 0.

        `channels` is the open pyramid when the caller already has it.
        """
        from plexora.server.models import remote_sources
        from plexora.server.utils import ome_zarr

        pyramid = channels if channels is not None else ome_zarr.open_image(self._path)
        if remote_sources.is_pinned(self._path):
            from plexora.server.models import data_model

            return data_model.quantization_window_of(pyramid, channel_index)
        return sampled_window(pyramid, channel_index)


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
