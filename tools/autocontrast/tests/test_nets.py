"""Torch tests: skip without torch; run with the training env."""

import numpy as np
import pytest

from biological_autocontrast import features as feat
from test_model import N, S, _item

torch = pytest.importorskip("torch")


def _batch(rng, b=3):
    return {k: torch.from_numpy(v) for k, v in
            feat.pack([_item(rng) for _ in range(b)], N, S).items()}


def test_model_is_small_and_bounded():
    from biological_autocontrast.nets import MAX_PARAMS, ContrastNet, param_count

    net = ContrastNet().eval()
    assert param_count(net) < MAX_PARAMS
    with torch.no_grad():
        lo, hi, conf, abst, span = net(**_batch(np.random.default_rng(0)))
    assert lo.shape == hi.shape == conf.shape == abst.shape == (3,)
    assert torch.all(lo >= 0) and torch.all(hi > lo) and torch.all(hi <= 1)
    assert torch.all((conf >= 0) & (conf <= 1)) and torch.all((abst >= 0) & (abst <= 1))


def test_onnx_matches_torch_and_loads_for_inference(tmp_path):
    from biological_autocontrast.model import ContrastModel
    from biological_autocontrast.nets import ContrastNet, export_onnx

    path = export_onnx(ContrastNet(), tmp_path / "model.onnx", n_patches=N, patch_size=S,
                       metadata={"model_layout": "layout-1", "model_version": "test",
                                 "n_patches": N, "patch_size": S})
    model = ContrastModel(path)
    assert model.model_version == "test" and model.valid is False
    rng = np.random.default_rng(1)
    out = model.predict(feat.pack([_item(rng), _item(rng)], N, S))
    assert np.all(out["min_norm"] < out["max_norm"])
    # fewer patches than the model's N: masked, still a window
    item = _item(rng)
    item["patches"], item["categories"] = item["patches"][:5], item["categories"][:5]
    out = model.predict(feat.pack([item], N, S))
    assert 0 <= out["min_norm"][0] < out["max_norm"][0]


def test_patch_order_does_not_matter():
    from biological_autocontrast.nets import ContrastNet

    net = ContrastNet().eval()
    batch = _batch(np.random.default_rng(2), b=1)
    perm = torch.randperm(N)
    shuffled = dict(batch, patches=batch["patches"][:, perm], patch_cat=batch["patch_cat"][:, perm],
                    patch_mask=batch["patch_mask"][:, perm])
    with torch.no_grad():
        a, b = net(**batch), net(**shuffled)
    for x, y in zip(a, b):
        assert torch.allclose(x, y, atol=1e-5)
