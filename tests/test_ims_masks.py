"""An Imaris (.ims) file as a segmentation mask.

Imaris writes no label images itself, but the tools that hand segmentations to
Imaris users do -- a label TIFF run through the Imaris File Converter, a
pipeline writing with PyImarisWriter -- so a mask arrives as an .ims with one
channel of ids, or with that channel beside the stains. Plexora read an .ims
as an image only, and refused one offered as a mask.

Now a channel of ids is found (the only channel, or the one that reads as ids,
preferring one named as a mask) and stored the way a TIFF of the same shape
is: a (Z, Y, X) or (T, Z, Y, X) mask as a label volume, one plane as a 2-D
label pyramid. Float32 ids are taken -- Imaris stores 32-bit data as float, so
a mask of more than 65,535 cells arrives that way -- and checked to be whole.
"""

import time

import h5py
import numpy as np
import pytest
import tifffile

from plexora.server.models import data_model, import_proposal
from plexora.server.models.project import Project
from plexora.server.utils import segmentation_pyramid as sp
from plexora.server.volume import labels as volume_labels
from plexora.server.volume.sources import ims

from tests.helpers import use_data_root
from tests.volume_fixtures import _attr

DEPTH, HEIGHT, WIDTH = 5, 70, 90


def _cells(depth=DEPTH, height=HEIGHT, width=WIDTH, offset=0, dtype=np.uint16):
    """Three cells spanning planes, one id each across every plane."""
    volume = np.zeros((depth, height, width), dtype=dtype)
    volume[:, 5:30, 5:30] = 1 + offset
    volume[1:4, 35:65, 10:50] = 2 + offset
    volume[0:2, 10:60, 60:85] = 3 + offset
    return volume


def _stain(depth=DEPTH, height=HEIGHT, width=WIDTH, seed=0):
    rng = np.random.default_rng(seed)
    return rng.integers(1, 4000, (depth, height, width)).astype(np.uint16)


def write_ims(path, channels, *, frames=1, pad=6, chunks=(4, 32, 32)):
    """An Imaris 5.5 file: `channels` is [(name, (Z, Y, X) or (T, Z, Y, X))].

    Padded past the real extent and chunked several planes deep, as Imaris
    writes, with a second level so the reader's level walk has one to skip.
    """
    with h5py.File(str(path), "w") as handle:
        first = np.asarray(channels[0][1])
        depth, height, width = first.shape[-3:]
        info = handle.create_group("DataSetInfo/Image")
        for key, value in (("X", width), ("Y", height), ("Z", depth), ("Unit", "um"),
                           ("ExtMin0", 0), ("ExtMin1", 0), ("ExtMin2", 0),
                           ("ExtMax0", width * 0.5), ("ExtMax1", height * 0.5),
                           ("ExtMax2", depth * 2.0)):
            info.attrs[key] = _attr(value)
        for c, (name, _data) in enumerate(channels):
            handle.create_group(f"DataSetInfo/Channel {c}").attrs["Name"] = _attr(name)
        for level in range(2):
            step = 2 ** level
            for t in range(frames):
                for c, (_name, data) in enumerate(channels):
                    data = np.asarray(data)
                    volume = (data[t] if data.ndim == 4 else data)[:, ::step, ::step]
                    d, h, w = volume.shape
                    padded = np.zeros((d, h + pad, w + pad), dtype=volume.dtype)
                    padded[:, :h, :w] = volume
                    group = handle.create_group(
                        f"DataSet/ResolutionLevel {level}/TimePoint {t}/Channel {c}")
                    group.create_dataset("Data", data=padded, chunks=(
                        min(d, chunks[0]), min(h + pad, chunks[1]), min(w + pad, chunks[2])),
                        compression="gzip")
                    for key, value in (("ImageSizeX", w), ("ImageSizeY", h),
                                       ("ImageSizeZ", d)):
                        group.attrs[key] = _attr(value)
    return path


def _volume(path, tmp_path):
    built = data_model.resolve_outline_segmentation(str(path), tmp_path,
                                                    mode=sp.MODE_FILLED)
    assert volume_labels.is_label_volume(built)
    return volume_labels.open_label_volume(built)


# -- a volume of ids ------------------------------------------------------------

def test_a_one_channel_ims_mask_becomes_a_label_volume(tmp_path):
    cells = _cells()
    path = write_ims(tmp_path / "cells.ims", [("(name not specified)", cells)])

    assert volume_labels.label_volume_shape(path) == (DEPTH, HEIGHT, WIDTH)
    volume = _volume(path, tmp_path)
    assert (volume.depth, volume.height, volume.width) == (DEPTH, HEIGHT, WIDTH)
    for z in range(DEPTH):
        # The chunk padding is not part of the mask.
        assert np.array_equal(np.asarray(volume.plane(z)["0"]), cells[z])


def test_float32_ids_past_65535_arrive_whole(tmp_path):
    cells = _cells(offset=70_000, dtype=np.float32)
    path = write_ims(tmp_path / "cells.ims", [("mask", cells)])

    volume = _volume(path, tmp_path)
    plane = np.asarray(volume.plane(1)["0"])      # every cell crosses plane 1
    assert plane.dtype == np.uint32
    assert set(np.unique(plane)) == {0, 70_001, 70_002, 70_003}


def test_fractional_values_are_refused_as_ids(tmp_path):
    values = _cells(dtype=np.float32)
    values[values > 0] += 0.25
    path = write_ims(tmp_path / "odd.ims", [("mask", values)])

    labels = ims.ImsLabels(path, channel=0)
    try:
        with pytest.raises(ValueError, match="not whole numbers"):
            labels.plane(0, 1)
    finally:
        labels.close()


def test_a_time_series_of_masks_is_one_volume_per_frame(tmp_path):
    frames = np.stack([_cells(), _cells(offset=10)])
    path = write_ims(tmp_path / "track.ims", [("labels", frames)], frames=2)

    assert volume_labels.label_volume_shape(path) == (2, DEPTH, HEIGHT, WIDTH)
    volume = _volume(path, tmp_path)
    assert volume.frames == 2
    assert np.array_equal(np.asarray(volume.frame(1).plane(1)["0"]), frames[1, 1])


def test_planes_come_from_one_slab_read_per_chunk_depth(tmp_path):
    """A plane alone would inflate every chunk it crosses for one plane of
    each; the slab is read once and the planes after it come out of memory."""
    path = write_ims(tmp_path / "cells.ims", [("mask", _cells())])
    labels = ims.ImsLabels(path)
    try:
        labels.plane(0, 0)
        slab = labels._slab
        labels.plane(0, 3)
        assert labels._slab is slab          # planes 0..3 share the first chunk row
        labels.plane(0, 4)
        assert labels._slab is not slab
    finally:
        labels.close()


# -- one plane ------------------------------------------------------------------

def test_a_single_plane_ims_mask_takes_the_2d_conversion(tmp_path):
    cells = _cells(depth=1)
    path = write_ims(tmp_path / "plane.ims", [("cells", cells)])

    assert volume_labels.label_volume_shape(path) is None
    assert sp.plane_size(path) == (WIDTH, HEIGHT)
    assert sp.label_pyramid_gaps(path) == ["it is an Imaris file"]
    assert not sp.can_serve_in_place(path)
    served = data_model.resolve_outline_segmentation(str(path), tmp_path,
                                                     mode=sp.MODE_FILLED)
    assert served.endswith("plane" + sp.FILLED_SUFFIX)
    assert np.array_equal(tifffile.imread(served, level=0), cells[0])


# -- which channel ---------------------------------------------------------------

def test_the_channel_of_ids_is_found_beside_the_stains(tmp_path):
    path = write_ims(tmp_path / "sample.ims", [
        ("DAPI", _stain()), ("CD3", _stain(seed=1)), ("whole cell", _cells())])

    labels = ims.ImsLabels(path)
    try:
        assert labels.channel == 2
    finally:
        labels.close()
    # Stains with one channel of ids beside them: a mask or an image, asked --
    # unless a name says which.
    assert ims.mask_verdict(path) is None
    named = write_ims(tmp_path / "named.ims", [
        ("DAPI", _stain()), ("cell mask", _cells())])
    assert ims.mask_verdict(named) is True


def test_two_channels_of_ids_prefer_the_one_named_a_mask(tmp_path):
    path = write_ims(tmp_path / "both.ims", [
        ("DAPI", _stain()), ("cells", _cells()), ("objects", _cells(offset=5))])
    labels = ims.ImsLabels(path)
    try:
        assert labels.channel == 1
    finally:
        labels.close()


def test_two_unnamed_channels_of_ids_are_a_sentence_not_a_guess(tmp_path):
    path = write_ims(tmp_path / "both.ims", [
        ("a", _cells()), ("b", _cells(offset=5))])
    with pytest.raises(ValueError, match="2 of them hold ids \\(a, b\\)"):
        ims.ImsLabels(path)


def test_an_ims_of_stains_only_is_an_image(tmp_path):
    path = write_ims(tmp_path / "stack.ims", [("DAPI", _stain()), ("CD3", _stain(seed=1))])

    assert ims.mask_verdict(path) is False
    assert import_proposal.looks_like_label_image(path) is False
    with pytest.raises(ValueError, match="none of them holds cell ids"):
        sp._open_level_zero(path)


def test_a_one_channel_ims_of_ids_is_recognised_as_a_mask(tmp_path):
    path = write_ims(tmp_path / "segmentation.ims", [("(name not specified)", _cells())])
    assert import_proposal.looks_like_label_image(path) is True

    layers, _questions, _bundle, warnings = import_proposal._detect_image(
        path, {}, declared="mask")
    [layer] = layers
    assert layer.role == "mask" and not warnings


def test_an_ims_at_a_web_address_says_it_needs_a_disk():
    reason = sp.mask_format_refusal("https://example.org/data/cells.ims")
    assert "can only be read from a disk" in reason


# -- end to end -------------------------------------------------------------------

def _wait_for_mask(name, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = data_model.get_segmentation_job_status(name)
        if status.get("status") in ("ready", "error"):
            return status
        time.sleep(0.1)
    raise AssertionError(f"mask job for {name} did not finish")


def test_attaching_an_ims_mask_to_a_project(tmp_path, monkeypatch):
    import plexora
    from plexora.server.routes.import_routes import attach_segmentation

    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    tifffile.imwrite(source / "slide.ome.tif", np.zeros((2, HEIGHT, WIDTH), np.uint16))
    mask = write_ims(source / "slide_cells.ims", [("cells", _cells())])
    plexora.import_sample(source / "slide.ome.tif", name="imaris")

    attach_segmentation("imaris", str(mask))
    status = _wait_for_mask("imaris")

    assert status["status"] == "ready", status
    derived = Project.load("imaris").segmentation.derived
    assert volume_labels.is_label_volume(derived)
    assert derived.endswith("slide_cells_labels_v1.zarr")


# -- telling ids from stain -----------------------------------------------------

def _discs(radius, gap=3):
    """A window packed with round cells of `radius`, one id each."""
    window = np.zeros((256, 256), np.uint16)
    yy, xx = np.mgrid[-5:6, -5:6]
    disc = yy ** 2 + xx ** 2 <= radius ** 2
    step = int(2 * radius + gap)
    for k, (cy, cx) in enumerate(((y, x) for y in range(8, 248, step)
                                  for x in range(8, 248, step)), start=1):
        window[cy - 5:cy + 6, cx - 5:cx + 6][disc] = k
    return window


@pytest.mark.parametrize("radius", [1.5, 2, 4])
def test_small_cells_read_as_ids(radius):
    """Down to 9-pixel cells: a flat-neighbour share sat right on its
    threshold for these, which is why it is not what decides."""
    assert ims.looks_like_labels(_discs(radius))


def test_a_binary_mask_and_noise_do_not():
    assert not ims.looks_like_labels((_discs(4) > 0).astype(np.uint8))
    rng = np.random.default_rng(0)
    noise = rng.integers(0, 4000, (256, 256)).astype(np.uint16)
    noise[rng.random((256, 256)) < 0.3] = 0
    assert not ims.looks_like_labels(noise)
