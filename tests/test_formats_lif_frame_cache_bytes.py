"""A LAS X tile scan's frame cache (`plexora/server/utils/formats/lif.py`) is
bounded in bytes, not in frames: reading every tile of every channel -- what
the derived-level build (`ome_zarr.build_extension`, channel by channel in
strips) and panning a loaded mosaic do -- keeps no more decoded frames than a
fixed budget, the 256 MiB LSM's plane cache keeps, however many tiles the
scan has.

The fake image hands out each frame as a broadcast view, so a 2048 x 2048
frame costs a few bytes here while its `nbytes` is what liffile's new array
for it would hold (`LifImage.frame(out=None)` allocates one per call).
"""

import numpy as np

from plexora.server.utils import formats
from tests.formats_fakes import FakeLifImage, install_lif, tilescan, touch

#: The budget LSM's plane cache keeps (`formats/lsm.py` `_CACHE_BYTES`).
BUDGET = 256 << 20

GRID = 20                     # 20 x 20 tiles
TILE = 2048                   # of 2048 x 2048 uint16: 8 MiB a frame
CHANNELS = 3
PIXEL_UM = 0.5


class _BroadcastLifImage(FakeLifImage):
    """`frame()` as a read-only broadcast of the frame's own number: the full
    logical size, almost no memory."""

    def frame(self, **indices):
        self.frame_calls.append(dict(indices))
        key = indices.get("M", 0) * 16 + indices.get("C", 0)
        return np.broadcast_to(np.asarray(key, dtype=self.dtype),
                               (self.sizes["Y"], self.sizes["X"]))


def _scan():
    positions = [(col * TILE, row * TILE) for row in range(GRID) for col in range(GRID)]
    coords = {"X": np.arange(TILE) * PIXEL_UM * 1e-6, "Y": np.arange(TILE) * PIXEL_UM * 1e-6}
    return _BroadcastLifImage(
        "Tiles", ("M", "C", "Y", "X"), dict(M=GRID * GRID, C=CHANNELS, Y=TILE, X=TILE),
        dtype=np.uint16, coords=coords, tilescan=tilescan(positions, PIXEL_UM))


def _held_bytes(reader) -> int:
    return sum(frame.nbytes for frame in reader._frames._tiles.values())


def test_reading_a_whole_tile_scan_keeps_a_bounded_number_of_bytes(monkeypatch, tmp_path):
    image = _scan()
    install_lif(monkeypatch, [image])
    reader = formats.open_reader(touch(tmp_path, "scan.lif"))
    try:
        height, width = reader.levels[0]
        assert (height, width) == (GRID * TILE, GRID * TILE)
        most = 0
        for channel in range(CHANNELS):
            for row in range(GRID):
                # one pixel row across the whole width touches every tile of this row
                y = row * TILE
                reader.read(0, channel, y, y + 1, 0, width)
                most = max(most, _held_bytes(reader))
        assert len(image.frame_calls) == GRID * GRID * CHANNELS     # every frame was read
        assert most <= BUDGET, (
            f"the frame cache held {most / 2**30:.1f} GiB "
            f"({len(reader._frames._tiles)} frames of {TILE}x{TILE} uint16) "
            f"against a {BUDGET >> 20} MiB budget")
    finally:
        reader.close()


def test_a_tile_row_read_again_comes_from_the_cache(monkeypatch, tmp_path):
    """The budget still holds one row of tiles, so the strips of a build that
    cross the same row do not decode its frames again."""
    image = _scan()
    install_lif(monkeypatch, [image])
    reader = formats.open_reader(touch(tmp_path, "scan.lif"))
    try:
        width = reader.levels[0][1]
        reader.read(0, 0, 0, 1, 0, width)
        first = len(image.frame_calls)
        assert first == GRID
        reader.read(0, 0, 1, 2, 0, width)
        assert len(image.frame_calls) == first
    finally:
        reader.close()
