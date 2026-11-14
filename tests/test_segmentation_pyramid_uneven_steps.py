"""A user's label pyramid whose levels do not halve is not served as it is.

The viewer addresses a mask's level `n` as the picture at 1/2**n, the same
halving chain as the image beside it (`size >> level` in the client's tile
source), and the tile route reads a servable mask level for level
(`label_overlay.label_region`: `pyramid[str(level)][rows, cols]`). A pyramid
that steps by 4 -- `bfconvert -pyramid-scale 4`, a Leica SCN, any writer with
another factor -- holds at its level 1 the whole mask in a quarter of the area
the viewer expects there, so every zoomed-out view drew the cells shrunk and
displaced, with nothing said. The image path already refuses such levels
(`tiff_series.steps_unevenly`, `ome_zarr.dyadic_prefix`); a mask must be
converted instead, like any other one the tile route cannot serve.
"""

import numpy as np
import tifffile
import zarr

from plexora.server.models import data_model
from plexora.server.utils import segmentation_pyramid as sp


def _grid_cells(size=2048, cell=32):
    labels = np.zeros((size, size), dtype=np.uint32)
    next_id = 1
    for y in range(0, size, cell):
        for x in range(0, size, cell):
            labels[y + 2:y + cell - 2, x + 2:x + cell - 2] = next_id
            next_id += 1
    return labels


def _write_step_four_tiff(path, labels):
    """A tiled label TIFF with SubIFD levels at 1/4 and 1/16."""
    with tifffile.TiffWriter(path) as writer:
        writer.write(labels, tile=(256, 256), subifds=2)
        writer.write(labels[::4, ::4], tile=(256, 256), subfiletype=1)
        writer.write(labels[::16, ::16], tile=(256, 256), subfiletype=1)
    return path


def test_a_tiff_mask_pyramid_stepping_by_four_is_not_servable_as_is(tmp_path):
    """Its gaps name the uneven steps, so import converts it rather than
    handing the viewer levels at the wrong scale."""
    path = _write_step_four_tiff(tmp_path / "mask_step4.tif", _grid_cells())

    assert sp.label_pyramid_gaps(path), (
        "a pyramid whose levels do not halve passed as servable untouched")
    assert data_model._servable_as_is(str(path), sp.MODE_FILLED) is False


def test_a_zarr_label_group_stepping_by_four_is_not_servable_as_is(tmp_path):
    """The same for a zarr label group whose levels 0, 1, 2 are 1, 1/4, 1/16."""
    labels = _grid_cells()
    path = tmp_path / "mask_step4.zarr"
    group = zarr.open_group(str(path), mode="w", zarr_format=2)
    for level, step in enumerate((1, 4, 16)):
        plane = labels[::step, ::step]
        array = group.create_array(str(level), shape=plane.shape, dtype="uint32",
                                   chunks=(256, 256))
        array[:] = plane

    assert sp.label_pyramid_gaps(path), (
        "a zarr label group whose levels do not halve passed as servable untouched")


def test_a_mask_served_as_is_answers_level_one_with_the_half_resolution_cells(tmp_path):
    """Whatever the import decides, a mask the tile route serves must answer
    level 1, tile (1, 1) with the cells at half resolution there -- the
    labels the halving chain puts under that tile. Read the way the tile
    route reads a servable mask (`resolve_outline_segmentation`, then
    tifffile's zarr view and `read_tile`)."""
    labels = _grid_cells()
    source = _write_step_four_tiff(tmp_path / "mask_step4.tif", labels)

    served = data_model.resolve_outline_segmentation(
        str(source), dataDirectory=str(tmp_path / "derived"), mode=sp.MODE_FILLED)

    with tifffile.TiffFile(served, is_ome=False) as reader:
        pyramid = zarr.open(reader.series[0].aszarr(), mode="r")
        tile = data_model.read_tile(pyramid, None, 1, "1_1", 512, 512)
    got = np.ascontiguousarray(tile).view("<u4").reshape(tile.shape[:2])

    expected = labels[::2, ::2][512:1024, 512:1024]
    assert got.shape == expected.shape, (
        f"level 1 tile (1, 1) came back {got.shape}, not {expected.shape}: "
        "the mask's level 1 is a quarter of the image, not a half")
    assert np.array_equal(got, expected)
