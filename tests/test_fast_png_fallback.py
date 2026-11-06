"""`fast_png` without imagecodecs: the stdlib's deflate, the same PNG.

imagecodecs has no WebAssembly build, so the website's Python encodes tiles
with zlib (P9.md). The image must be the same; only the bytes of the deflate
stream may differ.
"""

import importlib
import io
import sys

import numpy as np
import pytest
from PIL import Image


@pytest.fixture
def without_imagecodecs(monkeypatch):
    from plexora.server.utils import fast_png

    monkeypatch.setitem(sys.modules, "imagecodecs", None)  # import raises ImportError
    reloaded = importlib.reload(fast_png)
    yield reloaded
    monkeypatch.delitem(sys.modules, "imagecodecs")
    importlib.reload(fast_png)


def test_without_imagecodecs_the_png_decodes_to_the_same_pixels(without_imagecodecs):
    fast_png = without_imagecodecs
    assert fast_png._deflate.__module__ == fast_png.__name__  # the stdlib stand-in
    gray = np.arange(64 * 32, dtype=np.uint16).reshape(32, 64)
    decoded = np.asarray(Image.open(io.BytesIO(fast_png.encode_gray16_png(gray))))
    assert np.array_equal(decoded, gray)
    rgba = np.random.default_rng(0).integers(0, 255, (16, 16, 4), dtype=np.uint8)
    decoded = np.asarray(Image.open(io.BytesIO(fast_png.encode_rgba8_png(rgba))))
    assert np.array_equal(decoded, rgba)
