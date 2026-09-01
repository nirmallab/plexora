"""The OME-Zarr reader: metadata parsing, the level shape the rest of the
codebase indexes, container resolution, and the derived coarse levels.

The contract clauses that look like trivia are the load-bearing ones. `read_tile`
and `quantization_window_of` both branch on `isinstance(pyramid, zarr.Array)`,
and `node/api.py` uses `hasattr(pyramid, "shape")` to mean "a single plane, not a
pyramid" -- so an `NgffPyramid` that grew either would be served as one channel
of full-resolution pixels at every zoom level, silently.
"""

import numpy as np
import pytest
import zarr

from plexora.server.utils import ome_zarr
from tests.ngff_fixtures import (
    write_bioformats2raw_like,
    write_plate_like,
    write_ngff,
    write_spatialdata_like,
)


# -- metadata ------------------------------------------------------------


@pytest.mark.parametrize("version", ["0.4", "0.5"])
def test_reads_both_metadata_layouts(tmp_path, version):
    """0.4 keeps the NGFF keys at the top of .zattrs, 0.5 nests them under
    "ome" in zarr.json. Same image either way."""
    path = write_ngff(tmp_path / f"v{version}.ome.zarr", version=version)
    pyramid = ome_zarr.open_image(path)

    assert len(pyramid) == 3
    assert pyramid.level_shapes == [[256, 256], [128, 128], [64, 64]]
    assert pyramid[0].shape == (3, 256, 256)


def test_level_order_follows_dataset_paths(tmp_path):
    """`datasets[].path` is the authoritative level mapping. Assuming "0".."n"
    would find nothing here -- and where the names sort differently from the
    order they are listed in, it would draw the wrong resolution."""
    path = write_ngff(tmp_path / "s.ome.zarr", names=["s0", "s1", "s2"])
    pyramid = ome_zarr.open_image(path)

    assert pyramid.level_shapes == [[256, 256], [128, 128], [64, 64]]


def test_five_dimensional_image_is_squeezed_to_channel_y_x(tmp_path):
    """t and z pin to 0 -- the same single plane the OME-TIFF path takes from
    `series[0]`."""
    path = write_ngff(tmp_path / "tczyx.ome.zarr", shape=(1, 2, 1, 128, 128),
                      axes="tczyx", levels=2)
    pyramid = ome_zarr.open_image(path)

    level = pyramid[0]
    assert level.shape == (2, 128, 128)
    assert level.ndim == 3
    assert np.asarray(level).shape == (2, 128, 128)


def test_two_dimensional_image_presents_one_channel(tmp_path):
    path = write_ngff(tmp_path / "yx.ome.zarr", shape=(64, 64), axes="yx", levels=1)
    pyramid = ome_zarr.open_image(path)

    assert pyramid[0].shape == (1, 64, 64)
    assert pyramid[0][0, 0:8, 0:8].shape == (8, 8)


def test_missing_multiscales_is_a_readable_error(tmp_path):
    store = tmp_path / "empty.zarr"
    zarr.open_group(str(store), mode="w")
    with pytest.raises(ValueError, match="multiscales"):
        ome_zarr.open_image(store)


# -- the shape every consumer indexes ------------------------------------


def test_pyramid_looks_like_a_zarr_group_and_not_an_array(tmp_path):
    from plexora.server.models import data_model

    path = write_ngff(tmp_path / "a.ome.zarr")
    pyramid = ome_zarr.open_image(path)

    # node/api.py reads `hasattr(pyramid, "shape")` as "single level".
    assert not hasattr(pyramid, "shape")
    # read_tile and quantization_window_of both branch on this.
    assert not isinstance(pyramid, zarr.Array)
    # _zarr_level indexes by stringified level number.
    assert data_model._zarr_level(pyramid, 1).shape == (3, 128, 128)
    assert "2" in pyramid
    assert list(pyramid) == ["0", "1", "2"]
    with pytest.raises(KeyError):
        pyramid["9"]


@pytest.mark.parametrize("axes,shape", [("cyx", (3, 256, 256)),
                                        ("tczyx", (1, 3, 1, 256, 256))])
def test_level_serves_the_three_index_patterns(tmp_path, axes, shape):
    """The three ways a level is read in the runtime: a tile, a row slab for
    the quantization window scan, and a whole plane."""
    path = write_ngff(tmp_path / "p.ome.zarr", shape=shape, axes=axes, levels=1)
    level = ome_zarr.open_image(path)[0]

    assert level[1, 0:64, 0:64].shape == (64, 64)
    assert level[1, 0:16].shape == (16, 256)
    assert np.asarray(level[2]).shape == (256, 256)
    assert level.dtype == np.dtype("uint16")
    # A level keeps the store's own chunking -- the 1024 tile grid the viewer
    # requests is virtual, exactly as it is for a TIFF (see `geometry`).
    assert len(level.chunks) == 3


def test_squeezed_level_reads_the_same_pixels_as_the_source(tmp_path):
    path = write_ngff(tmp_path / "q.ome.zarr", shape=(1, 2, 1, 64, 64),
                      axes="tczyx", levels=1)
    level = ome_zarr.open_image(path)[0]
    raw = zarr.open_group(str(path), mode="r")["0"]

    assert np.array_equal(level[1, 8:24, 8:24], raw[0, 1, 0, 8:24, 8:24])


def test_level_rejects_a_non_integer_channel(tmp_path):
    path = write_ngff(tmp_path / "r.ome.zarr", shape=(1, 2, 1, 32, 32),
                      axes="tczyx", levels=1)
    with pytest.raises(TypeError, match="channel"):
        ome_zarr.open_image(path)[0][slice(0, 2), 0:8, 0:8]


# -- channel names and units ---------------------------------------------


def test_channel_labels_from_omero(tmp_path):
    path = write_ngff(tmp_path / "n.ome.zarr", labels=["DNA", "CD3", "CD8"])
    assert ome_zarr.channel_labels(path, 3) == ["DNA", "CD3", "CD8"]


def test_channel_labels_rejected_when_incomplete(tmp_path):
    """Same rule as OME-XML names: a partial or wrong-length list mislabels
    channels more convincingly than "Channel 3" does."""
    short = write_ngff(tmp_path / "short.ome.zarr", labels=["DNA", "CD3", "CD8"])
    blank = write_ngff(tmp_path / "blank.ome.zarr", labels=["DNA", "", "CD8"])
    none = write_ngff(tmp_path / "none.ome.zarr")

    assert ome_zarr.channel_labels(short, 2) is None
    assert ome_zarr.channel_labels(blank, 3) is None
    assert ome_zarr.channel_labels(none, 3) is None


def test_physical_metadata_uses_the_keys_the_scale_bar_reads(tmp_path):
    path = write_ngff(tmp_path / "u.ome.zarr", unit="micrometer", scale=0.325)
    metadata = ome_zarr.physical_metadata(ome_zarr.open_image(path))

    assert metadata["physical_size_x"] == pytest.approx(0.325)
    assert metadata["physical_size_x_unit"] == "µm"


def test_physical_metadata_empty_for_an_unknown_unit(tmp_path):
    """No scale bar beats a wrong one -- and an empty dict is the state the
    viewer already hides it for."""
    path = write_ngff(tmp_path / "w.ome.zarr", unit="unit")
    assert ome_zarr.physical_metadata(ome_zarr.open_image(path)) == {}


# -- container resolution ------------------------------------------------


def test_resolve_passes_a_file_through(tmp_path):
    image = tmp_path / "slide.ome.tiff"
    image.write_bytes(b"")
    assert ome_zarr.resolve_image_path(image) == image


def test_resolve_is_identity_for_a_multiscale_group(tmp_path):
    path = write_ngff(tmp_path / "a.ome.zarr")
    assert ome_zarr.resolve_image_path(path) == path


def test_resolve_picks_the_sole_bioformats2raw_series(tmp_path):
    store = write_bioformats2raw_like(tmp_path / "bf.zarr", levels=2)
    assert ome_zarr.resolve_image_path(store) == store / "0"


def test_resolve_picks_the_sole_spatialdata_image(tmp_path):
    store = write_spatialdata_like(tmp_path / "sd.zarr", levels=1)
    assert ome_zarr.resolve_image_path(store) == store / "images" / "morphology"


def test_resolve_names_the_candidates_when_there_are_several(tmp_path):
    store = write_spatialdata_like(tmp_path / "two.zarr",
                                   elements=("dapi", "morphology"), levels=1)
    with pytest.raises(ValueError) as caught:
        ome_zarr.resolve_image_path(store)

    message = str(caught.value)
    assert "dapi" in message and "morphology" in message
    assert "images/dapi" in message


def test_resolve_names_a_field_of_view_for_a_plate(tmp_path):
    """A plate holds hundreds of images, so there is nothing to auto-pick -- but
    it is not "no image in this store" either, and the message has to be the
    difference. Listing all of them would be useless; one pasteable path is not.
    """
    store = write_plate_like(tmp_path / "screen.zarr", wells=("B/2", "B/3", "C/2"),
                             fields=("0", "1"), levels=1)
    with pytest.raises(ValueError) as caught:
        ome_zarr.resolve_image_path(store)

    message = str(caught.value)
    assert "plate" in message
    assert "3 wells" in message and "6 images" in message
    assert "screen.zarr/B/2/0" in message


def test_resolve_picks_a_plates_sole_field(tmp_path):
    store = write_plate_like(tmp_path / "one.zarr", wells=("B/2",), fields=("0",),
                             levels=1)
    assert ome_zarr.resolve_image_path(store) == store / "B" / "2" / "0"


def test_a_plate_field_opens_as_an_ordinary_image(tmp_path):
    """Nothing below the resolver knows what a plate is: a field is a plain
    multiscale group and is read as one."""
    store = write_plate_like(tmp_path / "screen.zarr", wells=("B/2",),
                             fields=("0", "1"), levels=2, shape=(3, 128, 128),
                             labels=["DAPI", "Tubulin", "Actin"])
    field = store / "B" / "2" / "1"

    assert ome_zarr.resolve_image_path(field) == field
    pyramid = ome_zarr.open_image(field)
    assert len(pyramid) == 2
    assert ome_zarr.channel_labels(field, 3) == ["DAPI", "Tubulin", "Actin"]


def test_resolve_rejects_a_plain_directory(tmp_path):
    folder = tmp_path / "not_zarr"
    folder.mkdir()
    with pytest.raises(ValueError, match="folder"):
        ome_zarr.resolve_image_path(folder)


def test_resolve_rejects_a_zarr_store_with_no_image(tmp_path):
    store = tmp_path / "tables_only.zarr"
    zarr.open_group(str(store), mode="w")
    zarr.open_group(str(store / "tables"), mode="w")
    with pytest.raises(ValueError, match="no OME-Zarr image"):
        ome_zarr.resolve_image_path(store)


def test_suggest_name_puts_the_store_and_well_back_on_a_field(tmp_path):
    """A field is called "0" and so is the one in the next well; on its own name
    the project says nothing and the second collides with the first."""
    assert ome_zarr.suggest_name("/data/screen.zarr/B/2/0") == "screen_B_2_0"
    assert ome_zarr.suggest_name("/data/screen.zarr/B/2/1") == "screen_B_2_1"
    assert ome_zarr.suggest_name("/data/screen.zarr/C/3/0") == "screen_C_3_0"
    # `images/` is structure, not something anybody named.
    assert ome_zarr.suggest_name("/data/s.zarr/images/morphology") == "s_morphology"


def test_suggest_name_declines_anything_that_is_not_inside_a_store():
    """None means "the ordinary rule already has this right" -- the store root
    included, which quick view names for what the user pointed at."""
    assert ome_zarr.suggest_name("/data/sample.ome.zarr") is None
    assert ome_zarr.suggest_name("/data/sample.zarr") is None
    assert ome_zarr.suggest_name("/data/slide.ome.tif") is None
    assert ome_zarr.suggest_name("/data/plain/folder") is None
    assert ome_zarr.suggest_name("") is None
    assert ome_zarr.suggest_name(None) is None


def test_is_zarr_image_path(tmp_path):
    named = write_ngff(tmp_path / "a.ome.zarr")
    element = write_spatialdata_like(tmp_path / "sd.zarr", levels=1) / "images" / "morphology"
    plain = tmp_path / "plain"
    plain.mkdir()
    tiff = tmp_path / "slide.tif"
    tiff.write_bytes(b"")

    assert ome_zarr.is_zarr_image_path(named)
    # An element path is a zarr group whose name says nothing about it.
    assert ome_zarr.is_zarr_image_path(element)
    assert not ome_zarr.is_zarr_image_path(plain)
    assert not ome_zarr.is_zarr_image_path(tiff)
    assert not ome_zarr.is_zarr_image_path(tmp_path / "missing")
    assert not ome_zarr.is_zarr_image_path(None)


# -- extension pyramids --------------------------------------------------


def test_dyadic_prefix_stops_at_the_first_non_halving_step():
    assert ome_zarr.dyadic_prefix([[100, 100], [50, 50], [25, 25]]) == 3
    # Odd dimensions round differently between writers; one pixel is tolerated.
    assert ome_zarr.dyadic_prefix([[101, 101], [50, 51]]) == 2
    assert ome_zarr.dyadic_prefix([[100, 100], [25, 25]]) == 1


def test_needs_extension_only_for_a_coarsest_level_still_big(tmp_path):
    big = ome_zarr.open_image(
        write_ngff(tmp_path / "big.ome.zarr", shape=(2, 3000, 2500), levels=1))
    small = ome_zarr.open_image(
        write_ngff(tmp_path / "small.ome.zarr", shape=(2, 512, 512), levels=2))

    assert ome_zarr.needs_extension(big)
    assert not ome_zarr.needs_extension(small)


def test_build_extension_adds_only_the_missing_levels(tmp_path):
    source = write_ngff(tmp_path / "big.ome.zarr", shape=(2, 3000, 2500), levels=1)
    pyramid = ome_zarr.open_image(source)

    built = ome_zarr.build_extension(pyramid, tmp_path / "ext.zarr")
    derived = zarr.open_group(str(built), mode="r")

    # Named by ABSOLUTE level index, and level 0 is never duplicated.
    assert sorted(derived.array_keys()) == ["1", "2"]
    assert derived["1"].shape == (2, 1500, 1250)
    assert derived["2"].shape == (2, 750, 625)
    assert derived["1"].dtype == np.dtype("uint16")
    assert derived["1"].chunks == (1, 1024, 1024)
    assert derived.attrs["base_levels"] == 1


def test_build_extension_stitches_back_onto_the_source(tmp_path):
    source = write_ngff(tmp_path / "big.ome.zarr", shape=(2, 3000, 2500), levels=1)
    built = ome_zarr.build_extension(ome_zarr.open_image(source), tmp_path / "ext.zarr")

    pyramid = ome_zarr.open_image(source, extension=built)

    assert len(pyramid) == 3
    assert pyramid.level_shapes == [[3000, 2500], [1500, 1250], [750, 625]]
    assert pyramid.base_levels == 1
    assert pyramid[2][1, 0:64, 0:64].shape == (64, 64)
    assert not ome_zarr.needs_extension(pyramid)


def test_build_extension_values_are_a_two_by_two_mean(tmp_path):
    source = write_ngff(tmp_path / "m.ome.zarr", shape=(1, 3000, 2048), levels=1)
    built = ome_zarr.build_extension(ome_zarr.open_image(source), tmp_path / "ext.zarr")

    raw = np.asarray(zarr.open_group(str(source), mode="r")["0"][0, 0:4, 0:4], dtype=float)
    expected = np.rint(raw.reshape(2, 2, 2, 2).mean(axis=(1, 3)))
    got = zarr.open_group(str(built), mode="r")["1"][0, 0:2, 0:2]

    assert np.array_equal(got, expected.astype("uint16"))


def test_a_non_halving_level_is_never_served(tmp_path):
    """The viewer's tile source computes a level's size as `size >> level`, so a
    4x-step level would draw the wrong rectangle at the wrong zoom -- silently,
    which is the reason it is dropped rather than served."""
    source = write_ngff(tmp_path / "quad.ome.zarr", shape=(1, 1000, 1000),
                        levels=2, factor=4)
    pyramid = ome_zarr.open_image(source)

    assert pyramid.level_shapes == [[1000, 1000]]
    # Small enough that nothing needs deriving either, so this is what the
    # viewer is handed: one honest level rather than two, one of them a lie.
    assert not ome_zarr.needs_extension(pyramid)


def test_build_extension_replaces_a_non_halving_pyramid(tmp_path):
    source = write_ngff(tmp_path / "quad.ome.zarr", shape=(1, 4096, 4096),
                        levels=2, factor=4)
    pyramid = ome_zarr.open_image(source)
    assert len(pyramid) == 1

    built = ome_zarr.build_extension(pyramid, tmp_path / "ext.zarr")
    stitched = ome_zarr.open_image(source, extension=built)

    assert stitched.base_levels == 1
    assert stitched.level_shapes == [[4096, 4096], [2048, 2048], [1024, 1024]]


def test_build_extension_returns_none_when_nothing_is_missing(tmp_path):
    source = write_ngff(tmp_path / "ok.ome.zarr", shape=(1, 512, 512), levels=2)
    assert ome_zarr.build_extension(
        ome_zarr.open_image(source), tmp_path / "ext.zarr") is None


def test_extension_records_the_source_fingerprint(tmp_path):
    from plexora.server.utils import segmentation_pyramid

    source = write_ngff(tmp_path / "big.ome.zarr", shape=(1, 3000, 3000), levels=1)
    built = ome_zarr.build_extension(ome_zarr.open_image(source), tmp_path / "ext.zarr")

    assert ome_zarr.extension_source_key(built) == \
        segmentation_pyramid.source_fingerprint(source)


def test_odd_dimensions_survive_the_downsample(tmp_path):
    source = write_ngff(tmp_path / "odd.ome.zarr", shape=(1, 2049, 2051), levels=1)
    built = ome_zarr.build_extension(ome_zarr.open_image(source), tmp_path / "ext.zarr")
    stitched = ome_zarr.open_image(source, extension=built)

    assert stitched.level_shapes == [[2049, 2051], [1025, 1026], [513, 513]]
    # The edge-replicated final row must be real data, not a black seam.
    assert int(np.asarray(stitched[1][0, 1024:1025, :]).max()) > 0


# -- geometry and overview -----------------------------------------------


def test_geometry_reports_the_virtual_tile_grid(tmp_path):
    path = write_ngff(tmp_path / "g.ome.zarr", shape=(4, 300, 200), levels=2)
    geometry = ome_zarr.geometry(ome_zarr.open_image(path))

    assert geometry == {
        "levels": 2,
        "num_channels": 4,
        "height": 300,
        "width": 200,
        "tile_height": 1024,
        "tile_width": 1024,
        "level_shapes": [[300, 200], [150, 100]],
    }


def test_overview_plane_is_bounded(tmp_path):
    path = write_ngff(tmp_path / "o.ome.zarr", shape=(2, 1000, 1000), levels=1)
    overview = ome_zarr.overview_plane(ome_zarr.open_image(path))

    assert overview.shape[0] == 2
    assert overview.shape[1] <= 400 and overview.shape[2] <= 400
