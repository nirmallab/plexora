"""The uint16 -> uint8 quantization tables are bounded (audit lead S14).

`data_model._quantize_to_uint8` caches one 64 KiB table per (qmin, span)
window. Every contrast-slider position is a window of its own, so an unbounded
cache grew by 64 KiB per distinct window for the life of the server."""

import numpy as np

from plexora.server.models import data_model


def test_the_table_cache_keeps_only_the_recent_windows(monkeypatch):
    monkeypatch.setattr(data_model, "_quantize_lut_cache", type(data_model._quantize_lut_cache)())
    tile = np.arange(0, 65536, 257, dtype=np.uint16).reshape(16, 16)
    for qmin in range(500):
        out = data_model._quantize_to_uint8(tile, qmin, 1000.0)
        assert out.dtype == np.uint8
    assert len(data_model._quantize_lut_cache) <= data_model._QUANTIZE_LUT_LIMIT
    # The table still answers the arithmetic.
    expected = np.clip(np.rint((tile.astype(np.float32) - 499) / 1000.0 * 255), 0, 255).astype(np.uint8)
    assert np.array_equal(data_model._quantize_to_uint8(tile, 499, 1000.0), expected)
