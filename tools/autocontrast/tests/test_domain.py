import numpy as np
import pytest

from biological_autocontrast import domain as d
from biological_autocontrast import features as feat
from biological_autocontrast.config import Config
from biological_autocontrast.io import open_image


def _domain(path, c, assume_bits=None):
    with open_image(path) as image:
        ctx = feat.ImageContext.build(image, 0, 0, Config())
        return feat.extract_channel(ctx, c, assume_bits=assume_bits, with_patches=False).domain


def test_8bit_and_16bit_windows_are_the_same_canonical_window(fx):
    d8, d16 = _domain(fx["synth8"], 1), _domain(fx["synth16"], 1)
    assert (d8.full_scale, d16.full_scale) == (255.0, 65535.0)
    for low8, low16 in ((32, 8224), (200, 51400)):
        assert d.to_norm(low8, d8) == pytest.approx(d.to_norm(low16, d16), abs=1e-6)
        # exact inverse, both ways
        assert d.to_raw(d.to_norm(low8, d8), d8) == pytest.approx(low8, abs=1e-9)
        assert d.to_raw(d.to_norm(low8, d8), d16) == pytest.approx(low16, abs=1e-6)


def test_effective_bits_from_ome_metadata(fx):
    d12, d14 = _domain(fx["synth12"], 0), _domain(fx["synth14"], 0)
    assert d12.full_scale == 4095 and d12.bits_source == "ome:SignificantBits"
    assert d14.full_scale == 16383 and d14.effective_bits == 14
    assert d12.normalization_confidence == 1.0


def test_12bit_without_metadata_keeps_the_dtype_range_and_says_so(fx):
    dom = _domain(fx["synth12_nometa"], 0)
    assert dom.full_scale == 65535 and dom.bits_source == "dtype_nominal"
    assert dom.normalization_confidence < 1.0
    assert any("no effective bit depth" in w for w in dom.warnings)


def test_user_bits_are_recorded_as_user(fx):
    dom = _domain(fx["synth12_nometa"], 0, assume_bits=12)
    assert dom.full_scale == 4095 and dom.bits_source == "user"


def test_never_normalised_by_the_observed_max(fx):
    dom = _domain(fx["synth_dark"], 1)  # a uint16 channel whose max is 350
    assert dom.raw_max <= 350
    assert dom.full_scale == 65535


def test_float_with_negative_offset_and_nan(fx):
    nan_dom = _domain(fx["synthf"], 1)
    assert nan_dom.offset < 0 and nan_dom.normalization_method == "sampled_range"
    assert nan_dom.nan_fraction > 0 and nan_dom.finite_fraction < 1
    assert any("NaN" in w for w in nan_dom.warnings)
    inf_dom = _domain(fx["synthf"], 2)
    assert inf_dom.inf_fraction > 0
    plane = np.array([[np.nan, -100.0], [np.inf, 900.0]], dtype=np.float32)
    norm, finite = d.normalize(plane, nan_dom)
    assert finite.tolist() == [[False, True], [False, True]]
    assert np.isfinite(norm).all()


def test_metadata_bits_below_the_pixels_fall_back(fx):
    with open_image(fx["synth16"]) as image:
        image.significant_bits = 8  # lies: pixels reach ~50000
        ctx = feat.ImageContext.build(image, 0, 0, Config())
        dom = feat.extract_channel(ctx, 1, with_patches=False).domain
    assert dom.full_scale == 65535 and dom.bits_source == "dtype_nominal"
    assert any("falling back" in w for w in dom.warnings)


def test_domain_roundtrips_through_a_dict(fx):
    dom = _domain(fx["synth12"], 2)
    again = d.IntensityDomain.from_dict(dom.to_dict())
    assert again == dom
