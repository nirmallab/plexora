"""The quantization window of an 8-bit image on the website is (0, 255),
read off nothing: every value 8-bit samples can hold, so tiles are encoded as
stored; scanning a large slide's full resolution for its brightest pixel held
the website's one Python thread for minutes. Elsewhere the exact window stays."""
import numpy as np
import zarr

from plexora.server.models import data_model


def _plane(dtype, peak):
    array = zarr.zeros((2, 64, 48), dtype=dtype)
    array[1, 10, 20] = peak
    return array


def test_eight_bit_on_the_website_is_the_whole_range_unread(monkeypatch):
    monkeypatch.setenv("PLEXORA_BACKEND", "browser")
    plane = _plane(np.uint8, 40)
    assert data_model.quantization_window_of(plane, 1) == (0.0, 255.0)


def test_eight_bit_on_the_desktop_keeps_its_exact_ceiling(monkeypatch):
    monkeypatch.delenv("PLEXORA_BACKEND", raising=False)
    assert data_model.quantization_window_of(_plane(np.uint8, 40), 1) == (0.0, 40.0)


def test_sixteen_bit_on_the_website_still_reads_its_ceiling(monkeypatch):
    monkeypatch.setenv("PLEXORA_BACKEND", "browser")
    assert data_model.quantization_window_of(_plane(np.uint16, 3000), 1) == (0.0, 3000.0)
