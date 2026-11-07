"""A picked image's quantization window, from the website's page.

`data_model.quantization_window_of` reads every full-resolution pixel of a
channel; in Pyodide that is Python decoding gigabytes one tile at a time (18 s
for ORION CRC02's 2.8-gigapixel plane). On the website the page's workers scan
the same plane (services/browserNode.js `windowOf`) and Python asks for the
answer (providers/browser.py `page_window`). These pin when it asks, what it
asks with, and that the answer is the window Python would have made.
"""

import types

import numpy as np
import pytest

from plexora.server.providers import browser


@pytest.fixture
def asked(monkeypatch):
    calls = []

    def ask(kind, args):
        calls.append((kind, args))
        return 1200

    monkeypatch.setenv("PLEXORA_BACKEND", "browser")
    browser.set_asker(ask)
    yield calls
    browser.set_asker(None)


def _provider(path):
    return types.SimpleNamespace(_path=path)


def test_a_picked_tiff_is_scanned_by_the_page(asked):
    pyramid = {"0": np.zeros((3, 40, 50), dtype=np.uint16)}
    window = browser.page_window(_provider("/picked/ab/image.ome.tif"), pyramid, 2)
    assert window == (0.0, 1200.0)
    assert asked == [("window", {"path": "/picked/ab/image.ome.tif", "channel": 2,
                                 "shape": [3, 40, 50], "dtype": "uint16"})]


def test_an_empty_channel_is_floored_as_python_floors_it(asked, monkeypatch):
    browser.set_asker(lambda kind, args: 0)
    assert browser.page_window(_provider("/picked/ab/i.tif"), {"0": np.zeros((1, 4, 4), np.uint8)}, 0) == (0.0, 1.0)


@pytest.mark.parametrize("path, pyramid", [
    ("C:/data/image.ome.tif", {"0": np.zeros((1, 4, 4), np.uint16)}),      # not a pick
    ("/picked/ab/image.zarr", {"0": np.zeros((1, 4, 4), np.uint16)}),      # not a TIFF
    ("/picked/ab/image.tif", np.zeros((1, 4, 4), np.uint16)),              # one level, no group
    ("/picked/ab/image.tif", {"0": np.zeros((1, 4, 4), np.float32)}),      # not 8- or 16-bit
])
def test_anything_else_is_left_to_python(asked, path, pyramid):
    assert browser.page_window(_provider(path), pyramid, 0) is None
    assert asked == []


def test_off_the_website_it_never_asks(monkeypatch):
    browser.set_asker(lambda kind, args: pytest.fail("asked the page"))
    monkeypatch.delenv("PLEXORA_BACKEND", raising=False)
    try:
        assert browser.page_window(_provider("/picked/ab/i.tif"), {"0": np.zeros((1, 4, 4), np.uint16)}, 0) is None
    finally:
        browser.set_asker(None)


def test_the_page_that_cannot_answer_leaves_it_to_python(asked):
    browser.set_asker(lambda kind, args: None)
    assert browser.page_window(_provider("/picked/ab/i.tif"), {"0": np.zeros((1, 4, 4), np.uint16)}, 0) is None
