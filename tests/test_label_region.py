"""A label mask's tiles at every level, including levels the mask does not have.

A single-level mask used to be read at full resolution for every level, so a
zoomed-out tile showed a patch 2**level times too small, stretched over the
whole tile: another part of the slide's cells drawn over the tissue. A mask
with fewer levels than the image raised KeyError instead.
"""

from __future__ import annotations

import numpy as np
import zarr

from plexora.server.models import data_model


def _labels(size=2048):
    return np.arange(size * size, dtype=np.uint32).reshape(size, size)


def _ids(tile):
    """Undo read_tile's RGBA packing."""
    tile = np.ascontiguousarray(tile)
    return tile.view(np.uint32)[..., 0]


def test_a_single_level_mask_is_sampled_at_the_requested_level():
    labels = _labels()
    mask = zarr.array(labels, chunks=(512, 512))
    tile = data_model.read_tile(mask, None, 2, "1_0", 256, 256)
    assert (_ids(tile) == labels[::4, ::4][0:256, 256:512]).all()


def test_level_zero_is_read_as_it_always_was():
    labels = _labels(512)
    mask = zarr.array(labels, chunks=(256, 256))
    tile = data_model.read_tile(mask, None, 0, "1_1", 256, 256)
    assert (_ids(tile) == labels[256:512, 256:512]).all()


def test_a_pyramid_short_of_levels_derives_the_rest_from_its_coarsest():
    labels = _labels()
    group = zarr.group()
    group.create_array("0", data=labels, chunks=(512, 512))
    group.create_array("1", data=labels[::2, ::2], chunks=(512, 512))
    tile = data_model.read_tile(group, None, 3, "0_0", 256, 256)
    assert (_ids(tile) == labels[::8, ::8][:256, :256]).all()
    # A level it has is read from that level.
    tile = data_model.read_tile(group, None, 1, "1_0", 512, 512)
    assert (_ids(tile) == labels[::2, ::2][0:512, 512:1024]).all()


def test_an_edge_tile_is_cut_at_the_mask_edge():
    labels = _labels(1000)
    mask = zarr.array(labels, chunks=(256, 256))
    tile = data_model.read_tile(mask, None, 1, "1_1", 256, 256)
    expected = labels[::2, ::2][256:512, 256:512]
    assert tile.shape[:2] == expected.shape
    assert (_ids(tile) == expected).all()


def test_ids_past_2_24_keep_their_high_byte():
    """The fourth byte travels as alpha. It was dropped until 2026-11: a mask
    with ids from 16,777,216 up drew that cell as background and 16,777,217
    as cell 1. Every id below 2^24 is packed as before (alpha 0)."""
    import io

    from PIL import Image

    labels = np.zeros((512, 512), dtype=np.uint32)
    ids = [1, (1 << 24) - 1, 1 << 24, (1 << 24) + 1, (1 << 31) + 5, (1 << 32) - 1]
    for i, cell in enumerate(ids):
        labels[i * 64:(i + 1) * 64, :64] = cell
    mask = zarr.array(labels, chunks=(256, 256))
    tile = data_model.read_tile(mask, None, 0, "0_0", 512, 512)
    assert tile.dtype == np.uint8 and tile.shape == (512, 512, 4)
    assert (_ids(tile) == labels).all()
    assert not tile[labels < (1 << 24), 3].any()
    png, mimetype = data_model.encode_tile_array(tile, True, "png")
    assert mimetype == "image/png"
    decoded = np.asarray(Image.open(io.BytesIO(png)).convert("RGBA"))
    assert (_ids(decoded) == labels).all()
    # A narrower mask packs as before.
    small = zarr.array(labels.astype(np.uint16) % 1000, chunks=(256, 256))
    assert (_ids(data_model.read_tile(small, None, 0, "0_0", 512, 512)) == labels.astype(np.uint16) % 1000).all()


def test_a_tile_wholly_past_the_mask_edge_is_empty_not_an_error():
    """A mask smaller than its image: the viewer asks for the image's tiles,
    and one beyond the mask packed an empty array and raised (a 500 per tile,
    locally and from a data node alike). No cells there, so no ids."""
    labels = _labels(1000)
    mask = zarr.array(labels, chunks=(256, 256))
    for level, tile in ((0, "1_4"), (0, "4_0"), (1, "2_2"), (3, "9_9")):
        array = data_model.read_tile(mask, None, level, tile, 256, 256)
        assert array.dtype == np.uint8 and array.shape[-1] == 4, (level, tile)
        assert not _ids(array).any(), (level, tile)
        assert data_model.encode_tile_array(array, True, "png"), (level, tile)


def test_a_plane_of_a_label_volume_past_its_edge_is_empty_too(tmp_path):
    import tifffile

    from plexora.server.volume import labels as volume_labels

    volume = np.zeros((3, 300, 600), np.uint16)
    volume[:, 10:50, 20:90] = 4
    tifffile.imwrite(tmp_path / "m.tif", volume, photometric="minisblack")
    planes = volume_labels.open_label_volume(
        volume_labels.resolve_label_volume(tmp_path / "m.tif", [tmp_path])).plane(1)
    assert _ids(data_model.read_tile(planes, None, 0, "0_0", 256, 256))[10:50, 20:90].all()
    assert not _ids(data_model.read_tile(planes, None, 0, "2_1", 256, 256)).any()
