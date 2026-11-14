"""`local.image_geometry` reads an image's shape and lets the file go.

It answers "how big is it" for the import dialog (`/import/inspect`, every
picked image and mask) and for a data node's geometry route on every attach.
Whatever it opens to read the shape it has to close before it returns: a
TiffFile left to the garbage collector keeps its OS handle until a cyclic
collection runs, and on Windows the user's file cannot be renamed, moved,
deleted or re-saved until then; on a node the handles pile up.
"""

import gc
import os
import sys

import numpy as np
import pytest
import tifffile as tf

from plexora.server.providers.local import image_geometry
from plexora.server.utils import tiff_series


def _plain_cyx(path):
    tf.imwrite(path, np.zeros((3, 64, 64), np.uint16), photometric="minisblack",
              metadata={"axes": "CYX"})


def _subifd_pyramid(path):
    data = np.zeros((2, 256, 256), np.uint16)
    with tf.TiffWriter(path, bigtiff=True) as writer:
        writer.write(data, subifds=2, tile=(64, 64), photometric="minisblack",
                     metadata={"axes": "CYX"})
        writer.write(data[:, ::2, ::2], subfiletype=1, tile=(64, 64))
        writer.write(data[:, ::4, ::4], subfiletype=1, tile=(64, 64))


def _rgb(path):
    tf.imwrite(path, np.zeros((128, 128, 3), np.uint8), photometric="rgb")


@pytest.fixture
def opened_tiffs(monkeypatch):
    """Every `ImageTiff` constructed while the test runs."""
    seen = []

    class _Recording(tiff_series.ImageTiff):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            seen.append(self)

    monkeypatch.setattr(tiff_series, "ImageTiff", _Recording)
    return seen


@pytest.mark.parametrize("name, write, rgb", [
    ("plain.tif", _plain_cyx, False),
    ("pyramid.tif", _subifd_pyramid, False),
    ("brightfield.tif", _rgb, True),
])
def test_image_geometry_leaves_no_tiff_open(tmp_path, opened_tiffs, name, write, rgb):
    """After image_geometry returns, every TIFF it opened is closed and the
    file can be removed at once -- without waiting for a garbage collection."""
    path = tmp_path / name
    write(str(path))

    gc.disable()
    try:
        geometry = image_geometry(str(path), rgb=rgb)
        assert geometry["height"] > 0 and geometry["width"] > 0
        assert opened_tiffs, "image_geometry should have opened the TIFF"
        still_open = [tiff for tiff in opened_tiffs if not tiff.filehandle.closed]
        assert not still_open, (
            f"image_geometry left {len(still_open)} of {len(opened_tiffs)} "
            f"TIFF handle(s) open on {name}")
        if sys.platform == "win32":
            os.remove(path)  # WinError 32 while a handle is still open
    finally:
        gc.enable()
        gc.collect()
