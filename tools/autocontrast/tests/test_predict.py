import json
import math

import numpy as np
import pytest

from biological_autocontrast import predict as predict_mod
from biological_autocontrast.cli import main
from biological_autocontrast.config import Config
from biological_autocontrast.io import file_identity


def _rows(path, **kw):
    return predict_mod.predict_contrast(path, **kw)


def test_twin_images_give_the_same_canonical_windows(fx):
    r8, r16 = _rows(fx["synth8"]), _rows(fx["synth16"])
    for a, b in zip(r8, r16):
        assert a["display_min_norm"] == pytest.approx(b["display_min_norm"], abs=1e-6)
        assert a["display_max_norm"] == pytest.approx(b["display_max_norm"], abs=1e-6)
        assert b["display_min_raw"] == pytest.approx(a["display_min_raw"] * 257, rel=1e-6)
        assert b["display_max_raw"] == pytest.approx(a["display_max_raw"] * 257, rel=1e-6)


def test_dark_channel_abstains_and_keeps_the_nominal_range(fx):
    rows = _rows(fx["synth_dark"])
    dark = rows[1]
    assert dark["abstain"] and dark["abstain_reason"] == "dark_channel"
    assert dark["full_scale"] == 65535
    assert not rows[0]["abstain"]


def test_blank_image_abstains(fx):
    assert all(r["abstain"] for r in _rows(fx["synth_blank"]))


@pytest.mark.parametrize("name", ["synth8", "synth16", "synth12", "synth14", "synth12_nometa",
                                  "synthf", "synth_dark", "synth_mixed", "synth_blank", "stack"])
def test_every_window_is_finite_ordered_and_in_range(fx, name):
    for r in _rows(fx[name], z=0 if name == "stack" else None):
        assert math.isfinite(r["display_min_raw"]) and math.isfinite(r["display_max_raw"])
        assert r["display_min_raw"] < r["display_max_raw"]
        assert 0.0 <= r["display_min_norm"] < r["display_max_norm"] <= 2.0
        assert r["gamma"] == 1.0
        assert r["model_available"] is False and r["method"].startswith("heuristic")
        if r["normalization_method"] == "fixed_scale":
            assert r["display_max_norm"] <= 1.0
        else:
            assert r["display_min_raw"] >= r["offset"]


def test_mixed_ranges_in_one_container(fx):
    rows = _rows(fx["synth_mixed"])
    # ch0 holds 12-bit numbers in a uint16 container with no metadata: its
    # window sits low in the 16-bit range, and is still a real window.
    assert rows[0]["display_max_raw"] <= 4095
    assert rows[2]["display_max_raw"] > rows[0]["display_max_raw"] / 4


def test_float_window_sits_above_the_offset(fx):
    rows = _rows(fx["synthf"])
    assert rows[1]["nan_fraction"] > 0
    assert rows[1]["display_min_raw"] >= rows[1]["offset"]


def test_z_is_explicit_and_never_projected(fx):
    r0 = _rows(fx["stack"], z=0)
    r3 = _rows(fx["stack"], z=3)
    assert r3[0]["z"] == 3
    assert r3[0]["raw_max"] > r0[0]["raw_max"]  # plane 3 is brighter in the fixture
    with pytest.raises(ValueError):
        _rows(fx["stack"], z=9)
    with pytest.warns(UserWarning, match="no z given"):
        rows = _rows(fx["stack"], force=True)
    assert rows[0]["z"] == 0 and any("no z given" in w for w in rows[0]["warnings"])


def test_overview_level_changes_little(fx_large):
    a = _rows(fx_large, config=Config(overview_min_pixels=1_000_000))   # level 0
    b = _rows(fx_large, config=Config(overview_min_pixels=200_000))     # level 1
    assert a[0]["overview_level"] != b[0]["overview_level"]
    for x, y in zip(a, b):
        if x["abstain"] or y["abstain"]:
            continue
        assert abs(math.log(x["display_max_norm"] / y["display_max_norm"])) < 0.25


def test_cache_hit_reads_nothing(fx, monkeypatch):
    first = _rows(fx["synth16"])

    def refuse(*a, **k):
        raise AssertionError("opened the image on a cache hit")

    monkeypatch.setattr(predict_mod, "open_image", refuse)
    again = _rows(fx["synth16"])
    assert again == first
    with pytest.raises(AssertionError):
        _rows(fx["synth16"], force=True)


def test_commands_never_touch_the_source(fx, tmp_path):
    before = {k: file_identity(v) for k, v in fx.items()}
    main(["inspect", fx["synth16"], "--json"])
    main(["propose", fx["synth16"], "--output", str(tmp_path / "o"), "--review-html"])
    main(["propose", fx["stack"], "--z", "2", "--output", str(tmp_path / "s")])
    after = {k: file_identity(v) for k, v in fx.items()}
    assert before == after


def test_outputs_say_heuristic(fx, tmp_path):
    out = tmp_path / "o"
    main(["propose", fx["synth8"], "--output", str(out), "--review-html"])
    assert (out / "contrast_predictions.heuristic.csv").exists()
    rows = json.loads((out / "contrast_predictions.heuristic.json").read_text())
    assert {r["method"] for r in rows} == {"heuristic_percentile"}
    page = (out / "overview.html").read_text()
    assert "not learned windows" in page and "<img" in page
    assert list((out / "contact_sheets").glob("*.windowed.png"))


def test_baseline_windows_are_recorded(fx):
    r = _rows(fx["synth16"])[1]
    assert set(r["baseline_windows"]) >= {"percentile", "gmm", "fixed"}
    assert r["baseline_windows"]["percentile"] == pytest.approx(
        [r["display_min_raw"], r["display_max_raw"]])
    assert np.all(np.diff(r["baseline_windows"]["gmm"]) > 0)


def test_model_predictions_are_labelled_and_bounded(fx, onnx_model, tmp_path):
    rows = _rows(fx["synth_dark"], checkpoint_path=onnx_model)
    for r in rows:
        assert r["method"] == "model" and r["model_available"] is True
        assert r["model_valid"] is False  # no metrics.json beside it: not validated
        assert r["checkpoint_id"] and r["model_version"] == "untrained"
        assert 0 <= r["display_min_norm"] < r["display_max_norm"] <= 1
        assert 0 <= r["confidence"] <= 1
    # the data rule runs before the model and still wins
    assert rows[1]["abstain_reason"] == "dark_channel"
    out = tmp_path / "m"
    main(["predict", fx["synth16"], "--checkpoint", onnx_model, "--output", str(out), "--review-html"])
    assert (out / "contrast_predictions.csv").exists()
    assert "not validated" in (out / "overview.html").read_text()


def test_model_and_heuristic_do_not_share_a_cache(fx, onnx_model):
    a = _rows(fx["synth16"])
    b = _rows(fx["synth16"], checkpoint_path=onnx_model)
    assert {r["method"] for r in a} != {r["method"] for r in b}
