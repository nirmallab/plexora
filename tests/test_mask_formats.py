"""A segmentation mask in any format that can hold one.

A mask is one plane of whole-number cell ids, and any file that stores such a
plane exactly is read as one: a TIFF, OME-Zarr labels and a PNG, on disk or at
a web address, and an Imaris file on disk (tests/test_ims_masks.py). Plexora read a TIFF and a zarr store laid out the SpatialData
way and nothing else -- a PNG, which is what Cellpose writes (`_cp_masks.png`),
was refused before a pixel was read, and an NGFF label image whose levels were
not 2-D arrays named 0, 1, 2... passed as servable and drew nothing.

The formats an IMAGE can be in that a mask cannot -- a JPEG, a scanner's slide,
a DICOM slide, a colour PNG -- are refused up front with the
reason, where they used to fail inside the conversion job with a reader's
error about a file it was never going to understand.
"""

import time

import numpy as np
import pytest
import tifffile
import zarr
from PIL import Image

from plexora.server.models import data_model, import_proposal
from plexora.server.models.project import Project
from plexora.server.utils import memory_pyramid, segmentation_pyramid as sp
from tests.remote_fixtures import cache_root, http_store  # noqa: F401


def _labels(height=96, width=128, dtype=np.uint16):
    """Three cells and background -- ids a 8-bit file can hold too."""
    plane = np.zeros((height, width), dtype=dtype)
    plane[10:40, 10:40] = 1
    plane[50:80, 20:60] = 2
    plane[20:70, 80:120] = 300 if np.dtype(dtype).itemsize > 1 else 3
    return plane


def _converted(source, tmp_path, **kwargs):
    out = sp.pyramidize_segmentation_mask(source, tmp_path / "out.ome.tiff",
                                          tile_size=64, outline=False, **kwargs)
    return tifffile.imread(out, level=0)


# -- PNG ----------------------------------------------------------------------

def test_a_16_bit_png_converts_with_every_id_kept(tmp_path):
    plane = _labels()
    source = tmp_path / "field_cp_masks.png"
    Image.fromarray(plane).save(source)

    assert np.array_equal(_converted(source, tmp_path), plane)


@pytest.mark.parametrize("full_read", [True, False])
def test_both_read_strategies_take_a_png(tmp_path, full_read):
    """A PNG is decoded whole either way: it has no tiles to stream."""
    plane = _labels()
    source = tmp_path / "mask.png"
    Image.fromarray(plane).save(source)
    assert np.array_equal(_converted(source, tmp_path, full_read=full_read), plane)


def test_a_palette_png_is_read_as_its_indices(tmp_path):
    """Annotation tools save labels as palette PNGs, coloured for looking at.
    The ids are the indices; imagecodecs would have handed back the colours."""
    plane = _labels(dtype=np.uint8)
    picture = Image.fromarray(plane, mode="P")
    picture.putpalette([v for i in range(256) for v in (i * 7 % 256, 40, 200)])
    source = tmp_path / "labels.png"
    picture.save(source)

    assert np.array_equal(_converted(source, tmp_path), plane)


def test_a_colour_png_is_refused_with_the_reason(tmp_path):
    source = tmp_path / "overlay.png"
    Image.fromarray(np.zeros((8, 8, 3), np.uint8)).save(source)

    reason = sp.mask_format_refusal(source)
    assert reason.startswith("overlay.png is a colour PNG")
    with pytest.raises(ValueError, match="colour PNG"):
        _converted(source, tmp_path)


def test_a_png_is_measured_and_judged_from_its_header(tmp_path):
    source = tmp_path / "mask.png"
    Image.fromarray(_labels()).save(source)

    assert sp.plane_size(source) == (128, 96)
    assert sp.label_pyramid_gaps(source) == [
        "it has only one resolution level (no pyramid)", "it is not tiled"]
    assert not sp.can_serve_in_place(source)


def test_an_outline_png_is_converted_rather_than_served(tmp_path):
    """The tile route cannot open a PNG, so one that already looks like
    outlines -- served as-is in outline mode, were it a TIFF -- is converted."""
    plane = np.zeros((600, 600), np.uint16)
    for i, top in enumerate(range(20, 560, 60)):
        plane[top, 20:580] = i + 1          # thin lines: sparse, no interior
    source = tmp_path / "outlines.png"
    Image.fromarray(plane).save(source)
    assert sp.looks_like_outline_mask(source)

    served = data_model.resolve_outline_segmentation(
        str(source), tmp_path, mode=sp.MODE_OUTLINES)
    assert served != str(source) and served.endswith(sp.OUTLINE_SUFFIX)


# -- formats that cannot be a mask ---------------------------------------------

@pytest.mark.parametrize("name, says", [
    ("cells.jpg", "JPEG compression"), ("cells.jpeg", "JPEG compression"),
    ("slide.svs", "slide scanner"), ("slide.ndpi", "slide scanner"),
    ("slide.mrxs", "slide scanner"), ("slide.dcm", "DICOM"),
    ("scan.qptiff", "Akoya"),
])
def test_a_format_that_cannot_hold_ids_says_why(tmp_path, name, says):
    reason = sp.mask_format_refusal(tmp_path / name)
    assert says in reason and reason.endswith("or point at OME-Zarr labels.")


@pytest.mark.parametrize("name", ["cells.tif", "cells.ome.tiff", "cells.zarr",
                                  "cells.ims", "cells.parquet",
                                  "cell_boundaries.geojson"])
def test_a_format_that_can_is_not_refused(tmp_path, name):
    assert sp.mask_format_refusal(tmp_path / name) is None


def test_attaching_a_jpeg_as_the_mask_records_nothing(tmp_path):
    from plexora.server.routes.import_routes import attach_segmentation

    jpeg = tmp_path / "mask.jpg"
    Image.fromarray(np.zeros((8, 8), np.uint8)).save(jpeg)
    with pytest.raises(ValueError, match="JPEG"):
        attach_segmentation("anything", str(jpeg))


def test_an_in_memory_float_mask_is_refused():
    with pytest.raises(ValueError, match="integer cell ids"):
        memory_pyramid.label_pyramid(np.zeros((8, 8), np.float32))


# -- the import form ----------------------------------------------------------

def test_a_png_mask_is_recognised_as_one(tmp_path):
    source = tmp_path / "field.png"
    Image.fromarray(_labels()).save(source)
    assert import_proposal.looks_like_label_image(source) is True


def test_an_8_bit_grey_png_is_read_by_its_pixels_and_named_only_on_a_tie(tmp_path):
    """An 8-bit plane is a small mask or a greyscale picture, and the pixels
    say which: cells are flat, compact blobs of one id each, a picture is a
    continuum. Only a plane that cannot say -- one value over a background,
    which a one-cell mask and a thresholded picture both are -- falls back on
    its name."""
    cells = tmp_path / "field.png"
    Image.fromarray(_labels(dtype=np.uint8)).save(cells)
    assert import_proposal.looks_like_label_image(cells) is True

    picture = tmp_path / "photo_mask.png"
    Image.fromarray(np.random.default_rng(5).integers(
        0, 255, (96, 128), dtype=np.uint8)).save(picture)
    assert import_proposal.looks_like_label_image(picture) is False

    flat = np.zeros((96, 128), dtype=np.uint8)
    flat[20:60, 30:90] = 1
    plain, named = tmp_path / "blob.png", tmp_path / "blob_mask.png"
    Image.fromarray(flat).save(plain)
    Image.fromarray(flat).save(named)
    assert import_proposal.looks_like_label_image(plain) is None
    assert import_proposal.looks_like_label_image(named) is True


def test_a_png_added_as_a_mask_is_proposed_as_one_with_its_size(tmp_path):
    source = tmp_path / "cells.png"
    Image.fromarray(_labels()).save(source)

    layers, _questions, _bundle, warnings = import_proposal._detect_image(
        source, {}, declared="mask")

    [layer] = layers
    assert layer.role == "mask" and not warnings
    assert (layer.geometry["width"], layer.geometry["height"]) == (128, 96)


def test_a_jpeg_added_as_a_mask_says_why_it_is_an_image(tmp_path):
    source = tmp_path / "cells.jpg"
    Image.fromarray(np.zeros((8, 8), np.uint8)).save(source)

    _layers, _questions, _bundle, warnings = import_proposal._detect_image(
        source, {}, declared="mask")

    assert warnings and "JPEG compression changes pixel values" in warnings[0]


# -- OME-Zarr labels on disk ---------------------------------------------------

def _ngff_labels(path, *, axes, names):
    """An NGFF label image with `axes` and level arrays called `names`."""
    group = zarr.open_group(str(path), mode="w", zarr_format=2)
    shape = {"yx": (96, 128), "cyx": (1, 96, 128), "tczyx": (1, 1, 3, 96, 128)}[axes]
    plane = _labels(dtype=np.uint32)
    datasets = []
    for level, name in enumerate(names):
        step = 2 ** level
        level_plane = plane[::step, ::step]
        array = group.create_array(name, shape=shape[:-2] + level_plane.shape,
                                   dtype="uint32", chunks=shape[:-2] + (32, 32))
        array[:] = np.broadcast_to(level_plane, shape[:-2] + level_plane.shape)
        datasets.append({"path": name, "coordinateTransformations": [
            {"type": "scale", "scale": [1.0] * (len(shape) - 2) + [step, step]}]})
    group.attrs["multiscales"] = [{
        "version": "0.4", "datasets": datasets,
        "axes": [{"name": a, "type": "space" if a in "zyx" else
                  ("time" if a == "t" else "channel")} for a in axes]}]
    return path, plane


def test_levels_named_otherwise_are_converted_not_served(tmp_path):
    path, plane = _ngff_labels(tmp_path / "labels.zarr", axes="yx", names=["s0", "s1"])

    assert sp.label_pyramid_gaps(path) == ["its levels are not named 0, 1, 2..."]
    assert np.array_equal(_converted(path, tmp_path), plane)


def test_a_five_dimensional_label_image_on_disk_reads_as_one_plane(tmp_path):
    path, plane = _ngff_labels(tmp_path / "labels.zarr", axes="tczyx", names=["0", "1"])

    assert sp.label_pyramid_gaps(path) == ["its levels are 5-D arrays, not single planes"]
    assert sp.plane_size(path) == (128, 96)
    assert np.array_equal(_converted(path, tmp_path), plane)


def test_a_spatialdata_shaped_label_pyramid_is_still_served_as_it_is(tmp_path):
    path, _plane = _ngff_labels(tmp_path / "labels.zarr", axes="yx", names=["0", "1"])
    assert sp.label_pyramid_gaps(path) == []


def test_a_bioformats2raw_label_store_reads_its_first_series(tmp_path):
    root = tmp_path / "labels.ome.zarr"
    zarr.open_group(str(root), mode="w", zarr_format=2).attrs["bioformats2raw.layout"] = 3
    _path, plane = _ngff_labels(root / "0", axes="cyx", names=["0"])
    # The root's own group has to be re-opened over the series just written.
    assert np.array_equal(_converted(root, tmp_path), plane)


# -- at a web address ----------------------------------------------------------

def _wait_for_mask(name, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = data_model.get_segmentation_job_status(name)
        if status.get("status") in ("ready", "error"):
            return status
        time.sleep(0.1)
    raise AssertionError(f"mask job for {name} did not finish")


@pytest.mark.parametrize("name", ["cells_mask.tif", "cells_mask.png"])
def test_a_remote_mask_file_is_read_through_the_cache(tmp_path, http_store, name):
    plane = _labels()
    served = tmp_path / "served" / name
    if name.endswith(".png"):
        Image.fromarray(plane).save(served)
    else:
        tifffile.imwrite(served, plane, tile=(32, 32))
    url = http_store().url(name)

    array, close = sp._open_level_zero(url)
    try:
        assert array.shape == (96, 128)
        assert np.array_equal(np.asarray(array), plane)
    finally:
        close()
    assert sp.plane_size(url) == (128, 96)
    assert np.array_equal(_converted(url, tmp_path), plane)


def test_a_remote_tiff_mask_streams_tile_by_tile(tmp_path, http_store):
    """The per-tile path (`oindex`) a mask too big to read whole takes."""
    plane = _labels()
    tifffile.imwrite(tmp_path / "served" / "big_mask.tif", plane, tile=(32, 32))
    url = http_store().url("big_mask.tif")
    assert np.array_equal(_converted(url, tmp_path, full_read=False), plane)


def test_a_remote_mask_is_proposed_as_one_and_named_like_a_file(tmp_path, http_store):
    tifffile.imwrite(tmp_path / "served" / "cells_mask.tif", _labels(), tile=(32, 32))
    url = http_store().url("cells_mask.tif")

    layers, _questions, _bundle, warnings = import_proposal._detect_remote(url, {})

    [layer] = layers
    assert layer.role == "mask" and layer.src == url and not warnings
    target = sp.resolve_derived_mask(url, tmp_path, mode=sp.MODE_FILLED).target
    assert target.name == "cells_mask" + sp.FILLED_SUFFIX


def test_a_remote_image_named_like_a_mask_stays_an_image(tmp_path, http_store):
    """`cells` is a mask hint, but a stack of intensities is not one plane of
    ids -- read off the header, no pixels fetched."""
    tifffile.imwrite(tmp_path / "served" / "cells.tif",
                     np.zeros((3, 64, 64), np.uint16), tile=(32, 32),
                     photometric="minisblack")
    url = http_store().url("cells.tif")

    layers, _questions, _bundle, _warnings = import_proposal._detect_remote(url, {})

    assert [layer.role for layer in layers] == ["image"]


def test_a_remote_png_mask_converts_into_the_project(tmp_path, http_store):
    from plexora import import_sample

    tifffile.imwrite(tmp_path / "served" / "slide.ome.tif",
                     np.zeros((2, 96, 128), np.uint16))
    Image.fromarray(_labels()).save(tmp_path / "served" / "slide_mask.png")
    server = http_store()
    import_sample(server.url("slide.ome.tif"), name="web")

    from plexora.server.routes.import_routes import attach_segmentation

    attach_segmentation("web", server.url("slide_mask.png"))
    status = _wait_for_mask("web")

    assert status["status"] == "ready", status
    derived = Project.load("web").segmentation.derived
    assert derived.endswith("slide_mask" + sp.FILLED_SUFFIX)
    assert np.array_equal(tifffile.imread(derived, level=0), _labels())
