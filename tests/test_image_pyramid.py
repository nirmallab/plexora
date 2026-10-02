"""Pyramidized copies of flat multiplex images.

A large channel-stack TIFF written with one resolution level used to be
registered as it was: one level for the viewer, and an overview built from the
whole stack in memory. These pin the copy that replaces it -- readable the way
the tile server reads it, pixel-exact against the reference reducer, adopted
rather than rebuilt, never left half-written -- and the import path that
offers it, builds it and opens it.
"""

import errno
import json
import os

import numpy as np
import pytest
import tifffile as tf
import zarr

from plexora import datasource
from plexora.server.models import data_model, layer_jobs
from plexora.server.models.data_model import IMAGE_PYRAMID_STAGES, _staged_reporter
from plexora.server.models.import_proposal import inspect_paths
from plexora.server.models.project import Project
from plexora.server.utils import image_pyramid, ome_zarr, segmentation_pyramid
from tests.helpers import use_data_root


@pytest.fixture
def small_threshold(monkeypatch):
    """Anything wider than 300 px is "large", so fixtures stay small."""
    monkeypatch.setattr(image_pyramid, "FLAT_IMAGE_MAX_SIDE", 300)


def _flat(path, shape=(3, 700, 530), dtype=np.uint16, **kwargs):
    rng = np.random.default_rng(0)
    if np.dtype(dtype).kind == "f":
        pixels = rng.normal(500, 100, shape).astype(dtype)
    else:
        pixels = rng.integers(0, min(np.iinfo(dtype).max, 60000), shape,
                              dtype=dtype, endpoint=True)
    tf.imwrite(path, pixels, **kwargs)
    return pixels


def _reference_level(previous):
    return np.stack([ome_zarr._cast_like(ome_zarr._reduce2(plane), previous.dtype)
                     for plane in previous])


def _levels(path):
    with tf.TiffFile(str(path), is_ome=False) as tiff:
        group = zarr.open(tiff.series[0].aszarr(), mode="r")
        keys = sorted(group.keys(), key=int)
        return [np.asarray(group[key][:]) for key in keys], group[keys[0]].chunks


# -- the reducer ------------------------------------------------------------

@pytest.mark.parametrize("dtype", [np.uint8, np.uint16, np.int16, np.uint32,
                                   np.int32, np.float32])
@pytest.mark.parametrize("shape", [(7, 9), (8, 8), (1, 5), (5, 1), (1, 1), (33, 64)])
def test_reduce2_matches_the_reference_reducer(dtype, shape):
    """Integer arithmetic, but the same numbers -- round-half-to-even and the
    edge replication included -- as `_cast_like(_reduce2(...))`."""
    rng = np.random.default_rng(1)
    if np.dtype(dtype).kind == "f":
        block = rng.normal(100, 50, shape).astype(dtype)
    else:
        info = np.iinfo(dtype)
        block = rng.integers(info.min, info.max, shape, dtype=dtype, endpoint=True)
    expected = ome_zarr._cast_like(ome_zarr._reduce2(block), np.dtype(dtype))
    got = image_pyramid.reduce2(block)
    assert got.dtype == np.dtype(dtype)
    if np.dtype(dtype).kind == "f":
        np.testing.assert_allclose(got, expected, rtol=1e-6)
    else:
        np.testing.assert_array_equal(got, expected)


def test_the_parallel_reducer_is_the_serial_one():
    from concurrent.futures import ThreadPoolExecutor

    block = np.random.default_rng(2).integers(0, 65535, (1023, 517), dtype=np.uint16)
    with ThreadPoolExecutor(4) as pool:
        got = image_pyramid.reduce2_parallel(block, block.dtype, pool, 4)
    np.testing.assert_array_equal(got, image_pyramid.reduce2(block))


# -- detection --------------------------------------------------------------

def test_only_a_large_single_level_stack_has_gaps():
    gaps = image_pyramid.pyramid_gaps
    assert gaps((3, 5000, 4000), 1, np.uint16)
    assert gaps((3, 5000, 4000), 2, np.uint16) == []          # already a pyramid
    assert gaps((3, 4096, 4096), 1, np.uint16) == []          # small enough
    assert gaps((5000, 4000), 1, np.uint16) == []             # not (C, Y, X)
    assert gaps((3, 5000, 4000), 1, np.complex64) == []       # not writable
    assert gaps((0, 5000, 4000), 1, np.uint16) == []


def test_the_report_reads_the_header_only(tmp_path, small_threshold):
    _flat(tmp_path / "slide.tif")
    report = image_pyramid.flat_image_report(tmp_path / "slide.tif")
    assert report["channels"] == 3
    assert (report["height"], report["width"]) == (700, 530)
    assert report["dtype"] == "uint16"
    assert report["bytes"] == 3 * 700 * 530 * 2
    assert report["levels"] == 1  # one 1024 tile already covers 700 x 530

    tf.imwrite(tmp_path / "small.tif", np.zeros((2, 200, 200), np.uint16))
    assert image_pyramid.flat_image_report(tmp_path / "small.tif") is None


def test_the_copy_is_named_for_the_original():
    name = image_pyramid.derived_output_path
    assert name("/a/slide.ome.tiff").name == "slide.pyramid.ome.tiff"
    assert name("/a/slide.OME.TIF").name == "slide.pyramid.ome.tiff"
    assert name("/a/run.qptiff").name == "run.pyramid.ome.tiff"
    assert str(name("/a/slide.tif", "/project")) == "/project/slide.pyramid.ome.tiff"


# -- the copy ---------------------------------------------------------------

def test_the_copy_is_a_pyramid_the_tile_server_can_read(tmp_path):
    pixels = _flat(tmp_path / "slide.tif", shape=(3, 1100, 900), rowsperstrip=37)
    copy = image_pyramid.pyramidize_image(
        tmp_path / "slide.tif", tmp_path / "slide.pyramid.ome.tiff", tile_size=256)

    levels, chunks = _levels(copy)
    assert chunks == (1, 256, 256)
    assert [level.shape for level in levels] == [
        (3, 1100, 900), (3, 550, 450), (3, 275, 225), (3, 138, 113)]
    np.testing.assert_array_equal(levels[0], pixels)
    expected = pixels
    for level in levels[1:]:
        expected = _reference_level(expected)
        np.testing.assert_array_equal(level, expected)


@pytest.mark.parametrize("dtype", [np.uint8, np.float32])
def test_every_pixel_type_round_trips(tmp_path, dtype):
    pixels = _flat(tmp_path / "slide.tif", shape=(2, 600, 520), dtype=dtype)
    copy = image_pyramid.pyramidize_image(
        tmp_path / "slide.tif", tmp_path / "out.pyramid.ome.tiff", tile_size=256)
    levels, _ = _levels(copy)
    assert levels[0].dtype == np.dtype(dtype)
    np.testing.assert_array_equal(levels[0], pixels)
    np.testing.assert_allclose(levels[1], _reference_level(pixels), rtol=1e-6)


def test_a_slab_too_wide_for_the_budget_is_read_in_windows(tmp_path, monkeypatch):
    """The column-window path: two tiles per window on a five-tile row."""
    monkeypatch.setattr(image_pyramid, "_SLAB_BYTES", 2 * 256 * 256 * 2)
    pixels = _flat(tmp_path / "slide.tif", shape=(2, 600, 1200))
    copy = image_pyramid.pyramidize_image(
        tmp_path / "slide.tif", tmp_path / "out.pyramid.ome.tiff", tile_size=256)
    levels, _ = _levels(copy)
    np.testing.assert_array_equal(levels[0], pixels)
    np.testing.assert_array_equal(levels[1], _reference_level(pixels))


def test_scratch_levels_on_disk_are_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(image_pyramid, "_IN_MEMORY_SCRATCH_BYTES", 0)
    pixels = _flat(tmp_path / "slide.tif", shape=(2, 700, 600))
    copy = image_pyramid.pyramidize_image(
        tmp_path / "slide.tif", tmp_path / "out.pyramid.ome.tiff", tile_size=128)
    levels, _ = _levels(copy)
    np.testing.assert_array_equal(levels[1], _reference_level(pixels))
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "out.pyramid.ome.tiff", "slide.tif"]


def test_channel_names_and_pixel_size_are_carried(tmp_path):
    _flat(tmp_path / "slide.tif")
    copy = image_pyramid.pyramidize_image(
        tmp_path / "slide.tif", tmp_path / "slide.pyramid.ome.tiff",
        channel_names=["DAPI", "CD3", "CD20"], tile_size=256,
        physical={"physical_size_x": 0.65, "physical_size_x_unit": "µm",
                  "physical_size_y": 0.65, "physical_size_y_unit": "µm"})
    assert datasource._channel_names_from_image_metadata(copy, 3) == ["DAPI", "CD3", "CD20"]
    from plexora.server.utils import brightfield

    assert brightfield.physical_metadata(copy)["physical_size_x"] == pytest.approx(0.65)
    # Not a mask, whatever else reads it.
    assert segmentation_pyramid.generated_mask_kind(copy) is None


def test_the_original_is_never_written(tmp_path):
    _flat(tmp_path / "slide.tif")
    before = (tmp_path / "slide.tif").stat()
    image_pyramid.pyramidize_image(
        tmp_path / "slide.tif", tmp_path / "slide.pyramid.ome.tiff", tile_size=256)
    after = (tmp_path / "slide.tif").stat()
    assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)


def test_the_stages_are_reported_in_order(tmp_path):
    _flat(tmp_path / "slide.tif", shape=(2, 600, 600))
    seen = []
    image_pyramid.pyramidize_image(
        tmp_path / "slide.tif", tmp_path / "out.pyramid.ome.tiff", tile_size=256,
        stage_callback=lambda key, detail=None: seen.append((key, detail)))
    keys = [key for key, _ in seen]
    assert keys[0] == "inspecting" and keys[1] == "preparing" and keys[-1] == "writing"
    details = [detail for key, detail in seen if key == "building"]
    assert details == ["Building pyramid levels, level 1 of 3",
                       "Building pyramid levels, level 2 of 3",
                       "Building pyramid levels, level 3 of 3"]


# -- adoption ---------------------------------------------------------------

def test_a_finished_copy_is_adopted(tmp_path, small_threshold):
    _flat(tmp_path / "slide.tif")
    copy = tmp_path / "slide.pyramid.ome.tiff"
    assert image_pyramid.resolve_derived_image(tmp_path / "slide.tif").existing is None
    image_pyramid.pyramidize_image(tmp_path / "slide.tif", copy, tile_size=256)
    found = image_pyramid.resolve_derived_image(tmp_path / "slide.tif")
    assert found.existing == copy
    assert image_pyramid.is_image_copy(copy)
    assert image_pyramid.copy_source(copy) == str(tmp_path / "slide.tif")


def test_a_copy_of_a_changed_source_is_not_adopted(tmp_path):
    _flat(tmp_path / "slide.tif")
    copy = tmp_path / "slide.pyramid.ome.tiff"
    image_pyramid.pyramidize_image(tmp_path / "slide.tif", copy, tile_size=256)
    stat = (tmp_path / "slide.tif").stat()
    os.utime(tmp_path / "slide.tif", ns=(stat.st_atime_ns, stat.st_mtime_ns + 10 ** 9))
    assert not image_pyramid.is_adoptable_image(copy, tmp_path / "slide.tif")


def test_a_foreign_file_under_the_copys_name_is_not_adopted(tmp_path):
    _flat(tmp_path / "slide.tif")
    tf.imwrite(tmp_path / "slide.pyramid.ome.tiff", np.zeros((3, 700, 530), np.uint16))
    assert not image_pyramid.is_adoptable_image(
        tmp_path / "slide.pyramid.ome.tiff", tmp_path / "slide.tif")


def test_old_leftovers_of_an_interrupted_build_are_swept(tmp_path):
    _flat(tmp_path / "slide.tif")
    stale = tmp_path / ".slide.pyramid.ome.tiff-abc.tmp.ome.tiff"
    fresh = tmp_path / ".slide.pyramid.ome.tiff-def.tmp.ome.tiff"
    scratch = tmp_path / ".slide.pyramid.ome.tiff-scratch-xyz"
    stale.write_bytes(b"partial")
    fresh.write_bytes(b"partial")
    scratch.mkdir()
    old = 0
    os.utime(stale, (old, old))
    os.utime(scratch, (old, old))
    found = image_pyramid.resolve_derived_image(tmp_path / "slide.tif")
    assert found.existing is None
    assert not stale.exists() and not scratch.exists()
    assert fresh.exists()  # might be a build that is still running


# -- where it goes, and failing cleanly -------------------------------------

def test_an_unwritable_source_folder_falls_back_to_the_project(tmp_path, monkeypatch):
    from plexora import paths

    _flat(tmp_path / "slide.tif")
    project = tmp_path / "project"
    monkeypatch.setattr(paths, "is_writable", lambda root: str(root) == str(project))
    found = image_pyramid.resolve_derived_image(tmp_path / "slide.tif", project)
    assert found.writable and found.target == project / "slide.pyramid.ome.tiff"


def test_too_little_space_anywhere_is_said_up_front(tmp_path, monkeypatch):
    _flat(tmp_path / "slide.tif")
    monkeypatch.setattr(image_pyramid, "_free_bytes", lambda directory: 10)
    found = image_pyramid.resolve_derived_image(
        tmp_path / "slide.tif", tmp_path / "project", needed_bytes=5 * 1024 ** 3)
    assert not found.writable
    assert "5.0 GB" in found.reason and "free" in found.reason


def test_a_full_disk_leaves_nothing_behind(tmp_path, monkeypatch):
    _flat(tmp_path / "slide.tif")
    real_write = tf.TiffWriter.write

    def full(self, *args, **kwargs):
        real_write(self, *args, **kwargs)
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(tf.TiffWriter, "write", full)
    with pytest.raises(segmentation_pyramid.PyramidError, match="ran out of space"):
        image_pyramid.pyramidize_image(
            tmp_path / "slide.tif", tmp_path / "slide.pyramid.ome.tiff", tile_size=256)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["slide.tif"]


def test_an_unreadable_source_is_one_sentence(tmp_path):
    (tmp_path / "slide.tif").write_bytes(b"not a tiff at all")
    with pytest.raises(segmentation_pyramid.PyramidError, match="could not be opened"):
        image_pyramid.pyramidize_image(
            tmp_path / "slide.tif", tmp_path / "slide.pyramid.ome.tiff")
    assert not (tmp_path / "slide.pyramid.ome.tiff").exists()


# -- progress ---------------------------------------------------------------

def test_the_bands_are_ordered_contiguous_and_end_below_a_hundred():
    bands = list(IMAGE_PYRAMID_STAGES.values())
    for (_, end, _label), (start, _, _next) in zip(bands, bands[1:]):
        assert end == start
    assert bands[0][0] == 0 and bands[-1][1] < 100
    assert list(IMAGE_PYRAMID_STAGES) == [
        "opening", "inspecting", "preparing", "building", "writing", "registering"]


def test_a_sticky_detail_stays_on_the_bar_while_it_moves():
    seen = []
    stage, report = _staged_reporter(
        IMAGE_PYRAMID_STAGES, lambda percent, key, message: seen.append(message))
    stage("building", "Building pyramid levels, level 2 of 6", sticky=True)
    report(1, 2)
    assert seen[-1].startswith("Building pyramid levels, level 2 of 6 (")
    stage("writing")
    assert seen[-1].startswith("Finalizing the file")


# -- registration -----------------------------------------------------------

def test_a_large_flat_image_is_registered_as_its_copy(tmp_path, monkeypatch, small_threshold):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    use_data_root(monkeypatch, data_dir)
    images = tmp_path / "images"
    images.mkdir()
    pixels = _flat(images / "slide.tif", shape=(3, 2100, 1500))

    entry = datasource.register_image_datasource(
        name="flat", image=images / "slide.tif", data_dir=data_dir)

    copy = images / "slide.pyramid.ome.tiff"
    assert entry["channelFile"] == str(copy)
    assert entry["imageSource"] == str(images / "slide.tif")
    assert entry["imageSourceKey"] == segmentation_pyramid.source_fingerprint(images / "slide.tif")
    assert entry["maxLevel"] == 3  # 2100 -> 1050 -> 525, one 1024 tile
    assert (entry["width"], entry["height"], entry["num_channels"]) == (1500, 2100, 3)
    levels, _ = _levels(copy)
    np.testing.assert_array_equal(levels[0], pixels)

    # Re-picking the original is the same image.
    config = json.loads((data_dir / "config.json").read_text())
    assert datasource._find_existing_datasource_for_image(images / "slide.tif", config) == "flat"

    # The edit page's Image type control re-reads the copy and keeps the origin.
    datasource.reregister_image("flat", data_dir=data_dir)
    project = Project.find("flat", data_dir)
    assert project.image.src == str(copy)
    assert project.image.source == str(images / "slide.tif")


def test_import_as_is_registers_the_original(tmp_path, monkeypatch, small_threshold):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    use_data_root(monkeypatch, data_dir)
    _flat(tmp_path / "slide.tif", shape=(2, 900, 700))
    entry = datasource.register_image_datasource(
        name="as_is", image=tmp_path / "slide.tif", data_dir=data_dir, pyramidize=False)
    assert entry["channelFile"] == str(tmp_path / "slide.tif")
    assert entry["maxLevel"] == 1
    assert "imageSource" not in entry
    assert not (tmp_path / "slide.pyramid.ome.tiff").exists()


def test_a_failed_build_is_a_sentence_and_registers_nothing(tmp_path, monkeypatch, small_threshold):
    from plexora import paths

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    use_data_root(monkeypatch, data_dir)
    _flat(tmp_path / "slide.tif", shape=(2, 900, 700))
    monkeypatch.setattr(image_pyramid, "_free_bytes", lambda directory: 0)
    with pytest.raises(ValueError, match="nowhere to write a pyramidized copy"):
        datasource.register_image_datasource(
            name="nospace", image=tmp_path / "slide.tif", data_dir=data_dir)
    assert Project.find("nospace", data_dir) is None


def test_the_rail_shows_every_step_of_the_build(tmp_path, monkeypatch, small_threshold):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    use_data_root(monkeypatch, data_dir)
    _flat(tmp_path / "slide.tif", shape=(2, 900, 700))
    seen = []
    real = layer_jobs.registration_progress

    def spy(done, total, label=None, *, stage=None):
        seen.append(stage)
        return real(done, total, label, stage=stage)

    monkeypatch.setattr(layer_jobs, "registration_progress", spy)
    datasource.register_image_datasource(
        name="rail", image=tmp_path / "slide.tif", data_dir=data_dir)
    order = [key for index, key in enumerate(seen) if index == 0 or seen[index - 1] != key]
    assert order == list(IMAGE_PYRAMID_STAGES)


# -- the import dialog's proposal -------------------------------------------

def test_the_proposal_offers_the_copy(tmp_path, small_threshold):
    _flat(tmp_path / "slide.tif")
    sample = inspect_paths([str(tmp_path / "slide.tif")]).samples[0]
    question = next(q for q in sample.questions if q.id.startswith("pyramidize:"))
    assert question.id == f"pyramidize:{tmp_path / 'slide.tif'}"
    assert question.kind == "confirm" and question.default == "yes"
    image = next(layer for layer in sample.layers if layer.role == "image")
    assert question.scope == f"layer:{image.id}"
    pyramid = image.render["pyramid"]
    assert pyramid["channels"] == 3 and pyramid["output_name"] == "slide.pyramid.ome.tiff"
    assert question.id not in image.needs  # a confirmation, never outstanding
    assert "No pyramid" in image.detail


def test_a_small_or_pyramidal_image_is_not_offered_one(tmp_path, small_threshold):
    tf.imwrite(tmp_path / "small.tif", np.zeros((2, 200, 200), np.uint16))
    sample = inspect_paths([str(tmp_path / "small.tif")]).samples[0]
    assert not [q for q in sample.questions if q.id.startswith("pyramidize:")]


def test_a_folder_with_the_original_and_its_copy_is_one_image(tmp_path, small_threshold):
    _flat(tmp_path / "slide.tif")
    image_pyramid.pyramidize_image(
        tmp_path / "slide.tif", tmp_path / "slide.pyramid.ome.tiff", tile_size=256)
    proposal = inspect_paths([str(tmp_path / "slide.tif"),
                              str(tmp_path / "slide.pyramid.ome.tiff")])
    images = [layer for sample in proposal.samples for layer in sample.layers
              if layer.role == "image"]
    assert [layer.src for layer in images] == [str(tmp_path / "slide.tif")]
    # Already built, so nothing to ask.
    assert not [q for s in proposal.samples for q in s.questions
                if q.id.startswith("pyramidize:")]


def test_the_dialogs_no_opens_the_original(tmp_path, monkeypatch, small_threshold):
    from plexora.server.models.import_sample import import_sample

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    use_data_root(monkeypatch, data_dir)
    _flat(tmp_path / "slide.tif", shape=(2, 900, 700))
    pick = str(tmp_path / "slide.tif")
    result = import_sample([pick], answers={f"pyramidize:{pick}": "no"}, name="said_no")
    project = Project.find("said_no", data_dir)
    assert project.image.src == pick
    assert not (tmp_path / "slide.pyramid.ome.tiff").exists()
    assert result


def test_memory_is_bounded_by_the_slab_not_the_image(tmp_path, monkeypatch):
    """The point of the streaming pass: the heap holds a few tile rows, the
    writer's compression batch and the reducer's temporaries -- never the
    stack. Scratch is forced to disk and the batch small, so the bound is
    about this module rather than about tifffile's 512 MB default."""
    import tracemalloc

    monkeypatch.setattr(image_pyramid, "_IN_MEMORY_SCRATCH_BYTES", 0)
    monkeypatch.setattr(image_pyramid, "_WRITE_BUFFER_BYTES", 1)
    shape = (2, 4000, 3000)
    _flat(tmp_path / "slide.tif", shape=shape, rowsperstrip=64)
    raw = 2 * shape[0] * shape[1] * shape[2]
    tracemalloc.start()
    try:
        image_pyramid.pyramidize_image(
            tmp_path / "slide.tif", tmp_path / "slide.pyramid.ome.tiff",
            tile_size=256, max_workers=2)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < raw / 4, f"peak {peak >> 20} MB for a {raw >> 20} MB image"
