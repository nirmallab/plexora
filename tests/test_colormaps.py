"""Named colour ramps, and the one property that makes them usable.

Small enough that the interesting question is not "does it interpolate" but
"is it the same ramp everybody else's viridis is" -- a colormap that is
subtly not the one it is named after produces a picture nobody can compare
with the figure beside it.
"""

import numpy as np
import pytest

from plexora.server.utils import colormaps


def test_a_ramp_is_exact_at_its_anchors():
    """Interpolated rather than written out, so the anchors in the table have
    to be the colours that actually come out -- otherwise the table is a
    comment rather than the definition."""
    ramp = colormaps.ramp("viridis")

    assert ramp.shape == (256, 3)
    assert ramp.dtype == np.uint8
    assert list(ramp[0]) == [0x44, 0x01, 0x54]
    assert list(ramp[-1]) == [0xfd, 0xe7, 0x25]


def test_every_ramp_runs_dark_to_light_in_luminance():
    """The whole reason a density map uses a ramp and not one hue: the eye
    reads lightness far better than saturation, so the ramp has to spend its
    range on lightness. Checked on the two perceptually uniform ones, not on
    the diverging one, which is light in the middle by design."""
    for name in ("viridis", "magma", "cividis"):
        ramp = colormaps.ramp(name).astype(float)
        luma = ramp @ [0.2126, 0.7152, 0.0722]
        assert luma[-1] > luma[0] + 100, name
        # Monotone within a stop of rounding, so no band of the ramp reads as
        # going backwards.
        assert np.diff(luma).min() > -1.0, name


def test_an_unknown_name_falls_back_rather_than_raising():
    """Reached from a query string. A saved view naming a ramp a later
    version dropped should still draw."""
    assert np.array_equal(colormaps.ramp("nonesuch"),
                          colormaps.ramp(colormaps.DEFAULT_RAMP))


def test_a_ramp_is_told_apart_from_the_other_kind_of_colouring():
    """`is_ramp` is what decides whether a density tile is one field read off
    a scale or a gene per colour, and those are different pictures."""
    assert colormaps.is_ramp("Viridis")
    assert colormaps.is_ramp("magma")
    assert not colormaps.is_ramp("genes")
    assert not colormaps.is_ramp("")
    assert not colormaps.is_ramp(None)


def test_applying_a_ramp_is_a_lookup_over_the_whole_raster():
    levels = np.array([[0, 128], [255, 0]], dtype=np.uint8)

    out = colormaps.apply(levels, "viridis")

    assert out.shape == (2, 2, 3)
    assert list(out[0, 0]) == [0x44, 0x01, 0x54]
    assert list(out[1, 0]) == [0xfd, 0xe7, 0x25]
    assert np.array_equal(out[0, 0], out[1, 1])


@pytest.mark.parametrize("name", sorted(colormaps.RAMPS))
def test_no_ramp_has_a_malformed_anchor(name):
    for hexed in colormaps.RAMPS[name]:
        assert len(hexed) == 7 and hexed[0] == "#"
        int(hexed[1:], 16)
