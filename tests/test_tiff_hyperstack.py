"""Reading a TIFF whose series is not (channel, y, x).

Plexora's TIFF readers index `series[0]` positionally, which is right for the
two layouts the format usually arrives in and wrong for the third: an ImageJ
hyperstack stores `(T, C, Y, X)` or `(Z, C, Y, X)`, so a CODEX cycle stack of
23 x 4 planes registered as 23 channels four pixels tall and then failed to
load at all. These tests pin the axes-aware read (`server/utils/tiff_series.py`)
and the two guards in the overview heuristic that a bad shape used to trip.

The identity assertions matter as much as the flattening ones: a series that is
already CYX must come back as the *same object*, because that is what keeps a
pyramidal OME-TIFF -- the format everything else here is built around -- on
exactly the code path it was on before.
"""

import numpy as np
import pytest
import tifffile

from plexora import datasource
from plexora.server.models import data_model
from plexora.server.providers.local import image_geometry
from plexora.server.utils import tiff_series
from tests.helpers import use_data_root


def _write_hyperstack(path, frames=5, channels=4, height=64, width=48, axes="TCYX"):
    """An ImageJ hyperstack, the layout an Akoya/CODEX export writes."""
    rng = np.random.default_rng(0)
    data = rng.integers(1, 900, size=(frames, channels, height, width),
                        dtype=np.uint16)
    tifffile.imwrite(path, data, imagej=True, metadata={"axes": axes})
    return data


def _pages(path):
    with tifffile.TiffFile(str(path), is_ome=False) as handle:
        return [page.asarray() for page in handle.series[0].pages]


# -- the reader itself ---------------------------------------------------


def test_a_plain_channel_stack_is_handed_back_untouched(tmp_path):
    path = tmp_path / "stack.tif"
    tifffile.imwrite(path, np.zeros((3, 32, 32), dtype=np.uint8))

    with tifffile.TiffFile(str(path), is_ome=False) as handle:
        # Identity, not equality: the CYX path must not be rebuilt at all.
        assert tiff_series.channel_series(handle) is handle.series[0]


def test_an_ome_tiff_is_handed_back_untouched(tmp_path):
    path = tmp_path / "image.ome.tif"
    tifffile.imwrite(path, np.zeros((4, 32, 32), dtype=np.uint8), ome=True)

    with tifffile.TiffFile(str(path), is_ome=False) as handle:
        assert tiff_series.channel_series(handle) is handle.series[0]


def test_a_pyramidal_ome_tiff_keeps_its_levels(tmp_path):
    """The regression this whole module has to not cause.

    A pyramid opens as a zarr *group*, not an array, and `maxLevel` is read
    from its length -- so a reader that rebuilt the series would flatten the
    format Plexora is actually built around down to one level and make every
    zoomed-out tile decode full resolution.
    """
    import zarr

    path = tmp_path / "pyramid.ome.tif"
    base = np.zeros((4, 1024, 1024), dtype=np.uint16)
    with tifffile.TiffWriter(path, ome=True, bigtiff=True) as writer:
        writer.write(base, subifds=2, tile=(256, 256),
                     photometric="minisblack")
        for step in (2, 4):
            writer.write(base[:, ::step, ::step], subfiletype=1,
                         tile=(256, 256), photometric="minisblack")

    with tifffile.TiffFile(str(path), is_ome=False) as handle:
        series = tiff_series.channel_series(handle)
        assert series is handle.series[0]
        assert len(series.levels) == 3
        assert isinstance(zarr.open(series.aszarr()), zarr.Group)

    assert data_model.convertOmeTiff(str(path))["maxLevel"] == 3
    assert image_geometry(str(path))["levels"] == 3


def test_a_hyperstack_collapses_its_leading_axes_into_channels(tmp_path):
    path = tmp_path / "hyperstack.tif"
    _write_hyperstack(path, frames=5, channels=4, height=64, width=48)

    with tifffile.TiffFile(str(path), is_ome=False) as handle:
        assert handle.series[0].shape == (5, 4, 64, 48)

        series = tiff_series.channel_series(handle)

        assert series.shape == (20, 64, 48)
        assert series.axes == "CYX"


def test_a_flattened_hyperstack_reads_its_planes_in_file_order(tmp_path):
    # The order is the whole point: an Akoya channelNames.txt lists cycle 1's
    # four channels, then cycle 2's, which is page order. A flatten that
    # transposed T and C would silently label every marker wrong.
    path = tmp_path / "hyperstack.tif"
    _write_hyperstack(path, frames=3, channels=4, height=16, width=16)
    pages = _pages(path)

    import zarr

    with tifffile.TiffFile(str(path), is_ome=False) as handle:
        array = zarr.open(tiff_series.channel_series(handle).aszarr())
        for index, page in enumerate(pages):
            assert np.array_equal(np.asarray(array[index]), page)


def test_a_flattened_series_still_opens_as_a_plain_zarr_array(tmp_path):
    # `read_tile`, `quantization_window_of` and node/api.py all branch on this
    # exact test. A wrapper object that merely behaved like an array would
    # send every one of them down the pyramid-group path instead.
    import zarr

    path = tmp_path / "hyperstack.tif"
    _write_hyperstack(path, frames=2, channels=3, height=16, width=16)

    with tifffile.TiffFile(str(path), is_ome=False) as handle:
        array = zarr.open(tiff_series.channel_series(handle).aszarr())

    assert isinstance(array, zarr.Array)
    assert hasattr(array, "shape")


def test_a_lone_plane_reads_as_one_channel_not_as_its_own_height(tmp_path):
    path = tmp_path / "single.tif"
    tifffile.imwrite(path, np.zeros((40, 24), dtype=np.uint8))

    with tifffile.TiffFile(str(path), is_ome=False) as handle:
        series = tiff_series.channel_series(handle)

    # Positionally this used to be 40 channels of height 24 and no width at
    # all -- an IndexError on shape[2] before anything could report it.
    assert series.shape == (1, 40, 24)


def test_an_interleaved_colour_layout_is_left_alone(tmp_path):
    # Axes are `YXS`, which no reshape here is obviously right for. It is also
    # already taken out of this path upstream by `is_rgb_layout`, so the rule
    # is simply: unrecognised layout, no opinion.
    path = tmp_path / "rgb.tif"
    tifffile.imwrite(path, np.zeros((32, 32, 3), dtype=np.uint8),
                     photometric="rgb")

    with tifffile.TiffFile(str(path), is_ome=False) as handle:
        assert tiff_series.channel_series(handle) is handle.series[0]


# -- what registration records ------------------------------------------


def test_registering_a_hyperstack_records_every_plane_as_a_channel(tmp_path):
    path = tmp_path / "hyperstack.tif"
    _write_hyperstack(path, frames=5, channels=4, height=64, width=48)

    info = data_model.convertOmeTiff(str(path))

    assert info["num_channels"] == 20
    assert info["height"] == 64
    assert info["width"] == 48
    assert len(info["channel_names"]) == 20


def test_a_hyperstacks_geometry_agrees_with_its_conversion(tmp_path):
    # A node answers "how big is it" from image_geometry while the primary
    # records the conversion's numbers, and a project whose two disagree is
    # rejected at the attach screen. They have to read the file the same way.
    path = tmp_path / "hyperstack.tif"
    _write_hyperstack(path, frames=3, channels=4, height=64, width=48)

    info = data_model.convertOmeTiff(str(path))
    geometry = image_geometry(str(path))

    assert geometry["num_channels"] == info["num_channels"] == 12
    assert geometry["height"] == info["height"] == 64
    assert geometry["width"] == info["width"] == 48


def test_a_registered_hyperstack_serves_each_plane_as_its_own_channel(
        tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    use_data_root(monkeypatch, data_dir)
    path = tmp_path / "hyperstack.tif"
    _write_hyperstack(path, frames=3, channels=4, height=64, width=48)
    pages = _pages(path)

    entry = datasource.register_image_datasource(
        name="hyperstack", image=path, data_dir=data_dir)
    assert len(entry["imageData"]) == 12

    data_model.load_datasource("hyperstack", reload=True)
    config = data_model.config["hyperstack"]
    for index in (0, 1, 5, 11):
        tile = data_model.read_tile(
            data_model.channels, index, 0, "0_0",
            config["tileWidth"], config["tileHeight"])
        expected = pages[index][:config["tileHeight"], :config["tileWidth"]]
        assert np.array_equal(np.asarray(tile), expected)


# -- the overview heuristic's two guards ---------------------------------


def test_an_image_smaller_than_the_overview_floor_still_loads(
        tmp_path, monkeypatch):
    # No pyramid level has both dimensions >= 200, which used to raise
    # StopIteration out of the middle of the load and turn every tile request
    # into a 500 for the life of the project.
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    use_data_root(monkeypatch, data_dir)
    path = tmp_path / "small.tif"
    tifffile.imwrite(path, np.full((3, 64, 64), 7, dtype=np.uint16))

    datasource.register_image_datasource(
        name="small", image=path, data_dir=data_dir)
    data_model.load_datasource("small", reload=True)

    assert data_model.channels is not None
    assert data_model.zarray is not None


def test_a_long_thin_image_loads_without_a_zero_sized_reduction(
        tmp_path, monkeypatch):
    # One dimension past 400 while the other is under 200 makes the smaller
    # of the two block-reduce factors 0, and block_reduce raises on a zero
    # block size.
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    use_data_root(monkeypatch, data_dir)
    path = tmp_path / "thin.tif"
    tifffile.imwrite(path, np.full((2, 100, 900), 5, dtype=np.uint16))

    datasource.register_image_datasource(
        name="thin", image=path, data_dir=data_dir)
    data_model.load_datasource("thin", reload=True)

    assert data_model.zarray is not None
    assert np.asarray(data_model.zarray).size > 0
