"""The viewer's contrast fit (`/get_channel_gmm`): the same every time, and kept
beside the project so a reopened project gets its windows at once.

Two things went wrong before (tests/browser/contrast.mjs found them, on the
website and the desktop alike):

- The GaussianMixture behind the fit started from an unseeded k-means, so the
  same channel of the same image came back in one of two windows from one fit
  to the next (a z-stack's DNA at 267 or 1593, about one fit in ten): the
  website and the desktop opened one image at different levels, and the 3-D
  view's picture differed between them for no reason in the voxels.
- The fit cost 0.5-0.6 s per channel on a whole slide (seconds on the
  website) on every open, because it lived in memory only.

The store is keyed on what the fit is a pure function of -- the overview
plane's bytes, the window and the fitting code -- so a changed file, a changed
overview or a new version of the code is a miss, never a stale fit.
"""

import collections

import numpy as np
import pytest
import tifffile

from plexora import datasource
from plexora.server.models import data_model, database_model
from tests.helpers import use_data_root


def _stack_like_plane(seed=5):
    """A plane whose unseeded fit lands in two places (the stack fixture of
    tests/browser/volume_web.py does): dim background, a few bright cells."""
    rng = np.random.default_rng(seed)
    plane = rng.gamma(2.0, 120.0, size=(301, 257))
    for _ in range(60):
        y, x = rng.integers(0, 301), rng.integers(0, 257)
        r = rng.uniform(6, 14)
        yy, xx = np.ogrid[:301, :257]
        plane[(yy - y) ** 2 + (xx - x) ** 2 < r * r] += rng.uniform(1500, 4500)
    return np.clip(plane, 0, 65535).astype(np.uint16)


def test_the_fit_is_the_same_whatever_the_global_random_state():
    plane = _stack_like_plane()
    seen = collections.Counter()
    for seed in range(16):
        np.random.seed(seed)
        packet = data_model.channel_gmm_of(plane, 0.0, float(plane.max()))
        seen[(float(packet["vmin"]), float(packet["vmax"]))] += 1
    assert len(seen) == 1, seen


def _register(tmp_path, monkeypatch, name="fitted"):
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    use_data_root(monkeypatch, data_dir)
    image = tmp_path / f"{name}.tif"
    plane = _stack_like_plane()
    tifffile.imwrite(image, np.stack([plane, plane[::-1].copy()]))
    datasource.register_image_datasource(name=name, image=image, data_dir=data_dir)
    return image


def _reopen(name):
    """What a new session does: the project loaded again, memory empty."""
    data_model.load_datasource(name, reload=True)


def test_a_reopened_project_gets_its_fit_from_the_store(tmp_path, monkeypatch):
    _register(tmp_path, monkeypatch)
    _reopen("fitted")
    first = data_model.get_channel_gmm("Channel 1", "fitted")
    stored = data_model._stored_fits("fitted")
    assert set(stored) == {"Channel 1"}

    fits = []
    real = data_model.channel_gmm_of
    monkeypatch.setattr(data_model, "channel_gmm_of", lambda *a: fits.append(a) or real(*a))
    _reopen("fitted")
    again = data_model.get_channel_gmm("Channel 1", "fitted")
    assert fits == [], "a reopened project fitted again"
    # The very packet, field for field (JSON keeps a float exactly).
    assert data_model._plain(again) == data_model._plain(first)
    # A channel never fitted is fitted, and stored beside the first.
    data_model.get_channel_gmm("Channel 2", "fitted")
    assert len(fits) == 1
    assert set(data_model._stored_fits("fitted")) == {"Channel 1", "Channel 2"}


def test_a_changed_image_is_fitted_again(tmp_path, monkeypatch):
    image = _register(tmp_path, monkeypatch)
    _reopen("fitted")
    before = data_model.get_channel_gmm("Channel 1", "fitted")
    # The same file, other pixels.
    plane = _stack_like_plane(seed=9)
    tifffile.imwrite(image, np.stack([plane, plane]))
    fits = []
    real = data_model.channel_gmm_of
    monkeypatch.setattr(data_model, "channel_gmm_of", lambda *a: fits.append(a) or real(*a))
    _reopen("fitted")
    after = data_model.get_channel_gmm("Channel 1", "fitted")
    assert len(fits) == 1, "a changed image was served its old fit"
    assert (after["vmin"], after["vmax"]) != (before["vmin"], before["vmax"])


@pytest.mark.parametrize("change", ["window", "code"])
def test_a_changed_window_or_code_is_a_miss(tmp_path, monkeypatch, change):
    _register(tmp_path, monkeypatch)
    _reopen("fitted")
    data_model.get_channel_gmm("Channel 1", "fitted")
    if change == "window":
        real_window = data_model.get_channel_quantization_window
        monkeypatch.setattr(data_model, "get_channel_quantization_window",
                            lambda c, d, *a: (0.0, real_window(c, d, *a)[1] + 1.0))
    else:
        monkeypatch.setattr(data_model, "_FIT_VERSION", data_model._FIT_VERSION + 1)
    fits = []
    real = data_model.channel_gmm_of
    monkeypatch.setattr(data_model, "channel_gmm_of", lambda *a: fits.append(a) or real(*a))
    _reopen("fitted")
    data_model.get_channel_gmm("Channel 1", "fitted")
    assert len(fits) == 1


def test_an_unreadable_store_is_nothing_stored(tmp_path, monkeypatch):
    _register(tmp_path, monkeypatch)
    _reopen("fitted")
    database_model.save_list(database_model.ChannelFit, datasource="fitted", cells=b"not json")
    packet = data_model.get_channel_gmm("Channel 1", "fitted")
    assert packet["vmax"] > packet["vmin"]
    assert "Channel 1" in data_model._stored_fits("fitted")


def test_an_integer_overview_is_fitted_in_float64():
    """An overview that is the image itself (a small image, a z-stack's) is
    integer, and the log of uint16 is float32, in which the fit stopped
    where float32 lost its tolerance -- a different point in Pyodide and in
    CPython (one stack's DNA rounded to 268 on the website, 267 on the
    desktop). Fitted in float64 it is the float64 plane's fit exactly."""
    plane = _stack_like_plane()
    top = float(plane.max())
    as_int = data_model.channel_gmm_of(plane, 0.0, top)
    as_float = data_model.channel_gmm_of(plane.astype(np.float64), 0.0, top)
    assert data_model._plain(as_int) == data_model._plain(as_float)
