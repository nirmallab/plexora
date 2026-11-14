"""A pyramidized copy's memory-mapped scratch levels leave nothing behind.

Levels 1..n of a large flat image are reduced into `.npy` files memory-mapped
in a dot-folder beside the copy (image_pyramid `_Scratch`), together about a
third of the image's size. They must be gone when `pyramidize_image` returns
or raises -- on Windows too, where a file that is still mapped cannot be
deleted -- rather than waiting for a rebuild of the same file a day later
(`_sweep_leftovers`).
"""

import numpy as np
import pytest
import tifffile as tf

from plexora.server.utils import image_pyramid, segmentation_pyramid


def _flat(path, shape=(2, 700, 600)):
    pixels = np.random.default_rng(0).integers(0, 60000, shape, dtype=np.uint16)
    tf.imwrite(path, pixels)
    return pixels


def _scratch_left(folder):
    return sorted(
        f"{entry.name}/{child.name}"
        for entry in folder.iterdir() if "-scratch-" in entry.name
        for child in ([entry] if entry.is_file() else entry.iterdir()))


def test_no_scratch_level_is_left_beside_a_finished_copy(tmp_path, monkeypatch):
    """Scratch forced to disk: after a successful build only the original
    and its copy are in the folder, no `.npy` level."""
    monkeypatch.setattr(image_pyramid, "_IN_MEMORY_SCRATCH_BYTES", 0)
    _flat(tmp_path / "slide.tif")
    image_pyramid.pyramidize_image(
        tmp_path / "slide.tif", tmp_path / "slide.pyramid.ome.tiff", tile_size=128)
    assert _scratch_left(tmp_path) == []


def test_no_scratch_level_is_left_when_the_build_fails_partway(tmp_path, monkeypatch):
    """The writer fails while it reads level 1 out of the scratch: the build
    raises, and the scratch levels it had filled are removed all the same."""
    monkeypatch.setattr(image_pyramid, "_IN_MEMORY_SCRATCH_BYTES", 0)
    _flat(tmp_path / "slide.tif")
    real = segmentation_pyramid.write_tiled_pyramid

    def failing_writer(destination, *, block, **kwargs):
        def guarded(channel, factor, *args):
            piece = block(channel, factor, *args)
            if factor > 1:
                raise RuntimeError("the writer failed on level 1")
            return piece
        return real(destination, block=guarded, **kwargs)

    monkeypatch.setattr(segmentation_pyramid, "write_tiled_pyramid", failing_writer)
    message = None
    try:
        image_pyramid.pyramidize_image(
            tmp_path / "slide.tif", tmp_path / "slide.pyramid.ome.tiff",
            tile_size=128, max_workers=1)
    except Exception as error:  # noqa: BLE001 -- whatever the writer raised
        message = str(error)
    assert message is not None and "level 1" in message
    assert not (tmp_path / "slide.pyramid.ome.tiff").exists()
    assert _scratch_left(tmp_path) == []
