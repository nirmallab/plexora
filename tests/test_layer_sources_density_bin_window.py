"""A transcript density drawn in one colour, with bins bigger than a pixel.

"One colour per gene" with no gene picked (or every picked gene hidden) is the
plain whole-panel density: the client sends `color=`, `bin=`, `dlo=` and
`dhi=` and no `genes=`/`ramp=`. That request is binned at the asked-for bin
size, so it has to be stretched against the ceiling for THAT bin size -- the
one the ramp and per-gene paths use -- and the Low/High window fractions have
to move the picture, exactly as they do on the other two paths.
"""

import types

import numpy as np
import pytest

from plexora.server.models import data_model, layer_sources
from plexora.server.models import transcript_tiles as tt

from tests.helpers import use_data_root


GENES = ["EPCAM", "CD3E", "PTPRC", "KRT5"]
BIN = 32


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """A built cache of 4,000 molecules over 512x512, 128-pixel tiles."""
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    rng = np.random.default_rng(7)
    n = 4_000
    gene_index = rng.integers(0, len(GENES), size=n).astype(np.uint16)
    x = rng.uniform(0, 512, size=n).astype(np.float32)
    y = rng.uniform(0, 512, size=n).astype(np.float32)
    q = rng.integers(20, 41, size=n).astype(np.uint8)

    source = tmp_path / "transcripts.parquet"
    source.write_bytes(b"not really a parquet, only its mtime is read")
    expected = tt.expected_manifest(source, width=512, height=512,
                                    tile_size=128, layer_id="tx")
    return tt.build("demo", "tx", genes=GENES, gene_index=gene_index,
                    x=x, y=y, q=q, expected=expected)


def _drawn(monkeypatch, **extra):
    """The RGB array `_points_tile` hands the encoder for the client's style."""
    captured = []
    monkeypatch.setattr(
        data_model, "encode_tile_array",
        lambda array, *a, **k: captured.append(np.array(array)) or (b"", "x"))
    # Exactly what transcriptLayer.densityStyle sends with "One colour per
    # gene" and nothing drawn: no genes=, no colors=, no ramp=.
    raw = {"color": "4da3ff", "minq": "20", "bin": str(BIN),
           "dlo": "0.0000", "dhi": "1.0000"}
    raw.update(extra)
    style = layer_sources.parse_style(raw)
    assert not style["genes"] and not style["ramp"]
    layer = types.SimpleNamespace(id="tx", render={})
    layer_sources._points_tile("demo", layer, 0, "0_0", "fast", style)
    assert len(captured) == 1
    return captured[0]


def test_a_single_colour_density_is_not_saturated_by_its_own_bin_size(
        cache, monkeypatch):
    """A 32-pixel bin holds ~16 molecules on average here; the ceiling is
    eight times the average bin, so a typical bin sits well below full
    brightness. Stretched against a one-pixel bin's window instead, every bin
    with a molecule in it is pure colour and the map is a flat slab."""
    tile = _drawn(monkeypatch)
    blue = tile[..., 2]
    drawn = blue[blue > 0]
    assert drawn.size, "the tile drew nothing at all"
    saturated = float((drawn == 255).mean())
    assert saturated < 0.5, (
        f"{saturated:.0%} of the drawn bins are at full brightness")


def test_the_density_window_moves_the_single_colour_picture(cache, monkeypatch):
    """Lowering the High fraction stretches the same counts brighter, as it
    does on the ramp and per-gene paths; a slider that changes the url but
    not the picture is a broken control."""
    full = _drawn(monkeypatch, dhi="1.0000")
    half = _drawn(monkeypatch, dhi="0.5000")
    assert not np.array_equal(full, half), (
        "dhi=0.5 drew exactly the same tile as dhi=1")
