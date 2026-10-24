import numpy as np

from biological_autocontrast import features as feat
from biological_autocontrast import sampling
from biological_autocontrast.config import Config
from biological_autocontrast.io import open_image


def _selection(path, c=1, seed=42, cfg=None):
    cfg = cfg or Config()
    with open_image(path) as image:
        ctx = feat.ImageContext.build(image, 0, 0, cfg)
        f = feat.extract_channel(ctx, c, seed=seed)
    return f


def test_quotas_are_met_on_a_large_enough_image(fx_large):
    f = _selection(fx_large)
    cats = [p["category"] for p in f.selection.patches]
    assert len(cats) == 24
    for name, quota in Config().quotas.items():
        assert cats.count(name) == quota, (name, cats)
    # A category may borrow from the wider tissue pool (bright cells are also
    # the textured ones), and says so; it never comes up short or crowds.
    assert "short" not in f.selection.fallbacks
    assert "min_distance_relaxed" not in f.selection.fallbacks
    assert f.patches.shape == (24, 128, 128)


def test_patches_keep_their_distance(fx_large):
    f = _selection(fx_large)
    spacing = f.selection.footprint * Config().min_distance
    pts = [(p["cx"], p["cy"]) for p in f.selection.patches]
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            gap = max(abs(pts[i][0] - pts[j][0]), abs(pts[i][1] - pts[j][1]))
            assert gap >= spacing - 1e-9


def test_same_seed_same_boxes_other_seed_other_boxes(fx_large):
    a = [p["box"] for p in _selection(fx_large, seed=7).selection.patches]
    b = [p["box"] for p in _selection(fx_large, seed=7).selection.patches]
    c = [p["box"] for p in _selection(fx_large, seed=8).selection.patches]
    assert a == b
    assert a != c


def test_a_small_image_records_its_fallbacks(fx):
    f = _selection(fx["synth16"])
    assert f.selection.fallbacks  # 512 px cannot hold 24 spaced 128 px patches
    assert len(f.selection.patches) <= 24


def test_high_patches_are_brighter_than_background(fx_large):
    f = _selection(fx_large)
    by = {}
    for p, patch in zip(f.selection.patches, f.patches):
        by.setdefault(p["category"], []).append(np.percentile(patch, 99))
    assert np.mean(by["high"]) > np.mean(by["background"])


def test_select_patches_without_candidates():
    empty = {k: np.zeros(0) for k in ("cy", "cx", "mean", "p90", "logstd", "texture", "tissue")}
    patches, fallbacks = sampling.select_patches(empty, 16, Config(), 0, "x")
    assert patches == [] and fallbacks
