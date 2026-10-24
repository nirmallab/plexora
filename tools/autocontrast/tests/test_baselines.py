import numpy as np
import pytest

from biological_autocontrast import baselines
from biological_autocontrast.synthetic import quantize, scene


@pytest.fixture(scope="module")
def plane():
    canon, _ = scene(seed=1, size=256)
    return quantize(canon[1], np.uint16, 16)


def test_gmm_matches_the_viewer_auto_button(plane):
    from plexora.server.models.data_model import channel_gmm_of

    np.random.seed(0)  # channel_gmm_of's GaussianMixture draws from the global state
    theirs = channel_gmm_of(plane, 0, 65535)
    ours = baselines.gmm_window(plane, random_state=0)
    assert ours == [float(theirs["vmin"]), float(theirs["vmax"])]


def test_percentile_is_plexoras_calibration(plane):
    from plexora.agent.evidence.calibration import _stats, channel_window

    assert baselines.percentile_window(plane, False) == channel_window(_stats(plane), "marker")
    assert baselines.percentile_window(plane, True) == channel_window(_stats(plane), "nuclear")


def test_windows_survive_empty_and_constant_planes():
    zeros = np.zeros((64, 64), np.uint16)
    assert baselines.gmm_window(zeros) is None
    out = baselines.all_windows(zeros, False)
    for name in ("percentile", "fixed"):
        if out[name]:
            assert out[name][1] > out[name][0]
    nan = np.full((64, 64), np.nan, np.float32)
    assert baselines.all_windows(nan, False)["percentile"] is None
