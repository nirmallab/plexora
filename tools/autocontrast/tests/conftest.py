import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes under its own AUTOCONTRAST_HOME, and a stray call into
    Plexora's own data root lands in a temp folder, never the user's install."""
    monkeypatch.setenv("AUTOCONTRAST_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(tmp_path / "plexora-data"))


@pytest.fixture(scope="session")
def fx(tmp_path_factory):
    from biological_autocontrast.synthetic import make_all

    return make_all(tmp_path_factory.mktemp("fixtures"))


@pytest.fixture(scope="session")
def fx_large(tmp_path_factory):
    """A 2048 px scene, big enough for the full 24-patch quota at level 0."""
    import numpy as np

    from biological_autocontrast.synthetic import quantize, scene, write_tiff

    canon, _ = scene(seed=3, size=2048, n_cells=3600)
    path = tmp_path_factory.mktemp("large") / "large16.ome.tif"
    return str(write_tiff(path, quantize(canon, np.uint16, 16)))


def has_torch():
    try:
        import torch  # noqa: F401

        return True
    except Exception:
        return False


os.environ.setdefault("PYTHONHASHSEED", "0")


TRAIN_PY = os.environ.get("AUTOCONTRAST_TRAIN_PY",
                          str(Path.home() / "plexora-autocontrast" / "venv-train" / "bin" / "python"))


@pytest.fixture(scope="session")
def onnx_model(tmp_path_factory):
    """An untrained model.onnx, exported by the training interpreter (torch is
    not in the plexora env). Skips when that interpreter is not there."""
    import subprocess

    if not Path(TRAIN_PY).exists():
        pytest.skip("no training interpreter (set AUTOCONTRAST_TRAIN_PY)")
    out = tmp_path_factory.mktemp("model") / "model.onnx"
    code = ("import torch, sys; torch.manual_seed(0); "
            "from biological_autocontrast.nets import ContrastNet, export_onnx; "
            "export_onnx(ContrastNet().eval(), sys.argv[1], n_patches=24, patch_size=128, "
            "metadata={'model_layout': 'layout-1', 'model_version': 'untrained', "
            "'n_patches': 24, 'patch_size': 128})")
    subprocess.run([TRAIN_PY, "-c", code, str(out)], check=True, cwd=str(ROOT),
                   env={**os.environ, "PYTHONPATH": str(ROOT)}, capture_output=True)
    return str(out)
