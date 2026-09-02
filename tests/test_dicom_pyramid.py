"""The pyramid a DICOM slide is read through.

The contract is not written down anywhere as an interface -- it is what
`data_model._zarr_level`, `read_tile`'s isinstance branches and `node/api.py`'s
`hasattr(pyramid, "shape")` test have always assumed about the zarr group
tifffile hands back. Satisfying it is the whole reason DICOM needs no second
viewer, so it is checked here directly rather than only through the routes.
"""

import numpy as np
import pytest

pytest.importorskip("wsidicom")
pytest.importorskip("pydicom")

from plexora.server.utils import dicom_wsi  # noqa: E402
from tests.dicom_fixtures import (  # noqa: E402
    SlideIds,
    write_associated_images,
    write_he_slide,
    write_if_slide,
)


@pytest.fixture
def multiplex(tmp_path):
    pyramid = dicom_wsi.open_image(write_if_slide(tmp_path / "if"))
    yield pyramid
    pyramid.close()


# -- the duck contract ---------------------------------------------------


def test_a_pyramid_records_how_its_slide_was_reached(multiplex):
    """The facts that are properties of the slide rather than of any level --
    and, for `source`, the one thing a DICOMweb slide would differ in."""
    assert multiplex.source.kind == "files"
    assert len(multiplex.source.files) == 3
    assert multiplex.pyramid_count == 1
    assert multiplex.has_label is False
    assert multiplex.has_overview is False
    assert multiplex.is_color is False


def test_a_pyramid_has_no_shape(multiplex):
    """`node/api.py` reads `hasattr(pyramid, "shape")` as "this is a single
    plane, not a pyramid". A DicomPyramid that grew one would be served as its
    own level 0 forever."""
    assert not hasattr(multiplex, "shape")


def test_levels_are_addressed_by_string(multiplex):
    assert multiplex["0"].shape == (3, 256, 320)
    assert multiplex[0].shape == (3, 256, 320)


def test_a_pyramid_is_sized_iterated_and_tested(multiplex):
    assert len(multiplex) == 1
    assert list(multiplex) == ["0"]
    assert "0" in multiplex
    assert "1" not in multiplex
    assert "not a level" not in multiplex


def test_a_missing_level_is_a_key_error(multiplex):
    with pytest.raises(KeyError):
        multiplex["7"]


def test_a_level_looks_like_a_zarr_array(multiplex):
    level = multiplex["0"]

    assert level.ndim == 3
    assert level.dtype == np.uint16
    # The virtual 1024 grid every other format's levels advertise -- a tile
    # request is a slice, and it does not have to land on a DICOM frame.
    assert level.chunks == (1, dicom_wsi.TILE_SIZE, dicom_wsi.TILE_SIZE)


# -- reading pixels ------------------------------------------------------


def test_each_optical_path_reads_as_its_own_channel(multiplex):
    """The point of the whole exercise: three files that are three markers,
    presented as three planes of one array, each with its blob where the
    fixture put it. A reader that ignored `path=` would return the same plane
    three times and every assertion below but the first would fail."""
    level = multiplex["0"]

    peaks = []
    for channel in range(3):
        plane = level[channel, :, :]
        assert plane.shape == (256, 320)
        peaks.append(np.unravel_index(int(plane.argmax()), plane.shape))

    assert len(set(peaks)) == 3


def test_a_region_is_the_same_pixels_as_the_whole_plane(multiplex):
    level = multiplex["0"]
    whole = level[1, :, :]

    assert np.array_equal(level[1, 64:128, 96:160], whole[64:128, 96:160])


def test_the_last_tile_of_a_row_comes_back_short(multiplex):
    """The viewer's tile grid is `ceil(size / 1024)` wide, so the last tile of
    every row and column asks for pixels that are not there. A short array is
    the right answer -- it is what a numpy or zarr slice gives -- and wsidicom
    raises on an out-of-bounds region, so this is a clip the reader has to make
    rather than one it inherits."""
    level = multiplex["0"]

    assert level[0, 200:1224, 300:1324].shape == (56, 20)


def test_a_region_entirely_off_the_edge_is_empty_not_an_error(multiplex):
    assert multiplex["0"][0, 900:1000, 900:1000].shape == (0, 0)


def test_a_level_materializes_as_channel_y_x(multiplex):
    """`__array__` is what `build_extension` and the overview call. CYX, and
    uint16 -- a fluorescence plane squeezed to uint8 here would compute every
    quantization window from the wrong numbers."""
    array = np.asarray(multiplex["0"])

    assert array.shape == (3, 256, 320)
    assert array.dtype == np.uint16


def test_a_level_rejects_a_non_integer_channel(multiplex):
    with pytest.raises(TypeError):
        multiplex["0"][:, 0:8, 0:8]


def test_two_index_slicing_defaults_the_columns(multiplex):
    """`ome_zarr.build_extension` indexes `level[channel, rows]` with no column
    term at all. A level that required three would fail only when a slide big
    enough to need derived levels turned up."""
    assert multiplex["0"][0, 0:16].shape == (16, 320)


# -- what is not in the pyramid ------------------------------------------


def test_label_and_overview_images_are_not_levels_or_channels(tmp_path):
    """Photographs of the glass and its barcode. They share every identifier
    the specimen instances have, so they arrive with the slide -- and they must
    not arrive as a fourth marker or a coarser level. They are recorded as
    flags, which is all anything needs from them."""
    ids = SlideIds(container="WITH-LABEL")
    slide = write_if_slide(tmp_path / "slide", ids=ids)
    write_associated_images(slide, ids=ids)

    pyramid = dicom_wsi.open_image(slide)
    try:
        assert pyramid["0"].shape == (3, 256, 320)
        assert len(pyramid) == 1
        assert pyramid.has_label and pyramid.has_overview
    finally:
        pyramid.close()


def test_focal_planes_are_pinned_and_counted_never_shown_as_channels(tmp_path):
    """A z-stack of three planes and three markers is three channels, not nine.

    The one confusion this module exists to prevent: Z and channel are both
    "extra frames" in the file, and flattening them together would present
    somebody's focal series as six markers they do not have.
    """
    slide = write_if_slide(tmp_path / "z", focal_planes=3)

    pyramid = dicom_wsi.open_image(slide)
    try:
        assert pyramid["0"].shape[0] == 3
        assert pyramid.focal_plane_count == 3
        assert pyramid.focal_plane is not None
    finally:
        pyramid.close()


# -- brightfield ---------------------------------------------------------


@pytest.fixture
def he(tmp_path):
    pyramid = dicom_wsi.open_image(write_he_slide(tmp_path / "he"), rgb=True)
    yield pyramid
    pyramid.close()


def test_a_colour_slide_presents_three_uint8_planes(he):
    assert he.is_color
    assert he["0"].shape == (3, 256, 320)
    assert he["0"].dtype == np.uint8


def test_the_rgb_accessor_returns_interleaved_samples(he):
    """The one seam where colour leaves the reader: `read_tile` hands these
    bytes straight to a WebP encoder without a quantization window."""
    block = he["0"].rgb[0:32, 0:32]

    assert block.shape == (32, 32, 3)
    assert block.dtype == np.uint8
    # The fixture's background is a pale field, not black -- which is what a
    # brightfield tile served as three additive channels would look like.
    assert tuple(block[0, 0]) == (236, 236, 236)


def test_the_plane_view_is_the_same_pixels_as_the_colour_view(he):
    """What makes a "Fluorescence" override of an H&E slide honest rather than
    special-cased: the same pyramid, read as three planes, by code that was
    never told."""
    block = he["0"].rgb[0:32, 0:32]

    for channel in range(3):
        assert np.array_equal(he["0"][channel, 0:32, 0:32], block[..., channel])


def test_a_stained_patch_is_darker_than_the_background(he):
    """Legibility, not just shape: a fixture full of noise would satisfy every
    test above and say nothing about whether the right rectangle was read."""
    background = he["0"].rgb[0:8, 0:8].mean()
    patch = he["0"].rgb[80:120, 96:128].mean()

    assert patch < background


# -- geometry and scale --------------------------------------------------


def test_geometry_reports_the_planes_not_the_layers(multiplex):
    assert dicom_wsi.geometry(multiplex) == {
        "levels": 1,
        "num_channels": 3,
        "height": 256,
        "width": 320,
        "tile_height": 1024,
        "tile_width": 1024,
        "level_shapes": [[256, 320]],
    }


def test_pixel_spacing_reaches_the_scale_bar_in_micrometres(multiplex):
    """DICOM states millimetres; wsidicom's `mpp` is micrometres despite the
    name of the type it comes in. The scale bar wants micrometres, so getting
    this wrong is a scale bar out by a thousand."""
    assert dicom_wsi.physical_metadata(multiplex) == {
        "physical_size_x": 0.5, "physical_size_x_unit": "µm",
        "physical_size_y": 0.5, "physical_size_y_unit": "µm",
    }


def test_an_overview_is_bounded_and_keeps_its_depth(multiplex):
    overview = dicom_wsi.overview_plane(multiplex)

    assert overview.shape[0] == 3
    assert max(overview.shape[-2:]) <= 400
    assert overview.dtype == np.uint16


# -- derived levels ------------------------------------------------------


def test_a_slide_that_can_resample_needs_no_derived_levels(tmp_path):
    """Which is nearly every slide, and the reason importing one usually writes
    nothing at all. A coarse level of a *small* source stays affordable however
    large the ratio -- the read is bounded by the source, not by the ratio --
    so the chain only breaks on an image too big to resample a tile from."""
    slide = write_if_slide(tmp_path / "wide", height=1200, width=1400,
                           markers=("DNA", "CD3"))

    pyramid = dicom_wsi.open_image(slide)
    try:
        assert pyramid.level_shapes == [[1200, 1400], [600, 700]]
        assert dicom_wsi.needs_extension(pyramid) is False
    finally:
        pyramid.close()


def test_derived_levels_append_to_the_chain_by_index(tmp_path):
    """`ome_zarr.build_extension` names its arrays by ABSOLUTE level index, and
    the reader appends them by the same number -- so a level served from the
    slide and one served from the store answer to the same key and describe the
    same rectangle.

    The store is written by hand rather than derived, because a fixture big
    enough to actually break the halving chain would be tens of megabytes: what
    is under test here is the append, not the arithmetic that decides when it
    happens (`test_a_slide_that_can_resample_needs_no_derived_levels` covers
    that side, and `build_extension` is `ome_zarr`'s, tested with the rest of
    it).
    """
    import zarr

    slide = write_if_slide(tmp_path / "if")
    store = dicom_wsi.extension_path(tmp_path / "project")
    store.parent.mkdir(parents=True, exist_ok=True)
    group = zarr.open_group(str(store), mode="w")
    derived = group.create_array("1", shape=(3, 128, 160), dtype="uint16",
                                 chunks=(1, 128, 160))
    derived[1, 8:16, 8:16] = 4321

    extended = dicom_wsi.open_image(slide, extension=str(store))
    try:
        assert len(extended) == 2
        assert extended.base_levels == 1
        assert extended.level_shapes == [[256, 320], [128, 160]]
        assert extended["1"].shape == (3, 128, 160)
        assert extended["1"].dtype == np.uint16
        # Read through the same CYX view as a native level, per channel.
        assert int(extended["1"][1, 8:16, 8:16].max()) == 4321
        assert int(extended["1"][0, 8:16, 8:16].max()) == 0
    finally:
        extended.close()


def test_the_extension_is_named_for_what_it_is(tmp_path):
    assert dicom_wsi.extension_path(tmp_path).name == "dicom_pyramid.zarr"
