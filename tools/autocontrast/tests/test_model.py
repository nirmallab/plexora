"""Augmentation, split and tolerance tests (numpy only; torch ones are in test_nets.py)."""

import numpy as np
import pytest

from biological_autocontrast import augment, features as feat, metrics, train

N, S = 24, 128


def _item(rng, level=0.05, target=(0.01, 0.2)):
    patches = np.clip(rng.gamma(2.0, level / 2, (N, S, S)), 0, 1).astype(np.float32)
    pixels = np.clip(rng.gamma(2.0, level / 2, 20000), 0, 1).astype(np.float32)
    item = {"patches": patches, "categories": np.arange(N) % 5, "pixels": pixels,
            "dom": np.zeros(feat.N_DOM, np.float32), "dom_mask": np.ones(feat.N_DOM, np.float32),
            "target_min": target[0], "target_max": target[1]}
    return augment.refresh(item)


def test_augmentation_moves_targets_with_the_pixels():
    rng = np.random.default_rng(0)
    base = _item(rng)
    for _ in range(20):
        aug = augment.augment(base, rng)
        g, o = aug["_aug"]["gain"], aug["_aug"]["offset"]
        assert 0.7 <= g <= 1.4 and 0 <= o <= 0.02
        assert aug["target_max"] == pytest.approx(min(base["target_max"] * g + o, 1.0))
        assert aug["target_min"] == pytest.approx(base["target_min"] * g + o)
        # pixel mean follows the same affine map (noise is zero-mean, quantisation unbiased)
        assert np.mean(aug["pixels"]) == pytest.approx(np.mean(base["pixels"]) * g + o, rel=0.05)
        assert 0 <= aug["target_min"] < aug["target_max"] <= 1
        np.testing.assert_allclose(aug["hist"], feat.histogram(aug["pixels"]))


def test_quantisation_rederives_the_domain():
    dom = augment.requantised_dom(np.zeros(feat.N_DOM, np.float32), 12)
    assert dom[0] == 12 / 16 and dom[4] == 1 and dom[3] == 0 and dom[5] == 1
    x = np.array([0.0, 0.5, 1.0])
    assert augment.quantise(x, 8).tolist() == [0.0, 128 / 255, 1.0]


def test_folds_never_leak():
    meta = {"examples": [{"specimen": f"s{i % 7}", "channel_name": f"m{i % 11}"} for i in range(77)]}
    for train_idx, val_idx in train._folds(meta, "specimen", 5, 0):
        a = {meta["examples"][i]["specimen"] for i in train_idx}
        b = {meta["examples"][i]["specimen"] for i in val_idx}
        assert not a & b
    for train_idx, val_idx in train._folds(meta, "channels", 6, 0):
        a = {meta["examples"][i]["channel_name"] for i in train_idx}
        b = {meta["examples"][i]["channel_name"] for i in val_idx}
        assert not a & b


def test_tolerance_is_symmetric_and_floored():
    assert metrics.within([0.01], [0.2], [0.01], [0.2])[0]
    assert not metrics.within([0.01], [0.3], [0.01], [0.2])[0]
    # two near-zero black points are the same black on screen
    assert metrics.within([0.0001], [0.2], [0.0003], [0.2])[0]
