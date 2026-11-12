"""What the website's Python may keep, and the builds it refuses before they start.

The website's worker stops at 4 GiB of WebAssembly memory, and the data root
(/data, every disk cache) is held in the same tab. `boot.environment` sets the
website's cache budgets through the variables each module already reads
(`boot.WEBSITE_LIMITS`), so the desktop is untouched; the builders that write
in proportion to an image ask `tab_room.check_build` (and `check_read`) first
and are refused there before writing a byte. The page says so
(services/handoffNotice.js, `refused`), and says why Python stopped
(`stopped`, with the worker's cause).
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import tifffile

from plexora.server.utils import tab_room
from plexora.server.utils.tab_room import TooLargeForTab

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests" / "js" / "envelope_probe.mjs"

#: Every variable `boot.environment` sets or removes, besides WEBSITE_LIMITS.
_BOOT_KEYS = (
    "PLEXORA_BASE_URL", "PLEXORA_DATA_PATH", "PLEXORA_ALLOWED_ORIGINS", "PLEXORA_SITE_ORIGIN",
    "PLEXORA_WORK", "PLEXORA_BACKEND", "PLEXORA_NO_NUMBA", "BIOCOGNIA_DIR",
    "BIOCOGNIA_NO_HEARTBEAT", "PLEXORA_LIBRARY_PATH", "PLEXORA_SHARED_PATH",
    "PLEXORA_AUTH_TOKEN", "PLEXORA_TRUSTED_HOSTS", "PLEXORA_PLUGINS", "PLEXORA_TELEMETRY",
    "PLEXORA_TELEMETRY_ENDPOINT", "DO_NOT_TRACK",
)


@pytest.fixture
def booted(monkeypatch):
    """`boot.environment` run, with every variable it touches put back after."""
    from plexora import paths
    from plexora.browser import boot

    keys = set(_BOOT_KEYS) | set(boot.WEBSITE_LIMITS)
    original = {key: os.environ.get(key) for key in keys}
    for key in keys:
        # setenv records the value before (or that there was none), which the
        # monkeypatch's undo restores; environment() then sets or removes it.
        monkeypatch.setenv(key, os.environ.get(key, ""))
    before = dict(os.environ)
    boot.environment("https://x.example")
    changed = {key for key in set(before) | set(os.environ)
               if before.get(key) != os.environ.get(key)}
    assert changed <= keys, f"environment() touched variables this fixture does not restore: {changed - keys}"
    yield dict(os.environ)
    # Put back now as well, not only at the monkeypatch's undo: the suite's
    # own teardowns run before that, and must not see /data as the root.
    for key, value in original.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    paths.reset()


# -- D. the website's cache budgets -------------------------------------------------

def test_boot_sets_the_website_limits(booted):
    # The remote cache is not capped here: on the website it lives in the
    # site's storage, not in memory (tests/test_browser_buckets.py).
    assert "PLEXORA_REMOTE_CACHE_BYTES" not in booted
    assert booted["PLEXORA_CHUNK_CACHE_MB"] == "256"
    assert booted["PLEXORA_IMS_CHUNK_CACHE_MB"] == "24"
    assert booted["PLEXORA_LEVEL_CACHE_MB"] == "128"
    assert booted["PLEXORA_VOLUME_BRICKS"] == "0"
    assert booted["PLEXORA_BACKEND"] == "browser"
    assert tab_room.on_website()


def test_the_modules_read_the_website_limits(booted):
    from plexora.server.volume import bricks, chunk_cache

    assert chunk_cache._env_int("PLEXORA_CHUNK_CACHE_MB", 0) == 256
    assert bricks.enabled() is False


def test_the_desktop_keeps_its_budgets(monkeypatch):
    from plexora.server.volume import bricks

    for key in ("PLEXORA_VOLUME_BRICKS", "PLEXORA_BACKEND"):
        monkeypatch.delenv(key, raising=False)
    assert bricks.enabled() is True


def test_the_website_keeps_two_overviews_and_the_desktop_eight(monkeypatch):
    from plexora.server.models import data_model

    monkeypatch.setenv("PLEXORA_BACKEND", "browser")
    assert data_model._overview_cache_max() == 2
    monkeypatch.delenv("PLEXORA_BACKEND")
    assert data_model._overview_cache_max() == 8


# -- E. the builders refuse before writing ------------------------------------------

@pytest.fixture
def small_tab(monkeypatch):
    """The website, with budgets a tiny fixture is past."""
    monkeypatch.setenv("PLEXORA_BACKEND", "browser")
    monkeypatch.setattr(tab_room, "BUILD_BUDGET", 1024)
    monkeypatch.setattr(tab_room, "READ_LIMIT", 1024)
    told = []
    tab_room.set_teller(told.append)
    yield told
    tab_room.set_teller(None)


@pytest.fixture
def desktop(monkeypatch):
    monkeypatch.delenv("PLEXORA_BACKEND", raising=False)
    # The same small budgets: off the website they are never asked.
    monkeypatch.setattr(tab_room, "BUILD_BUDGET", 1024)
    monkeypatch.setattr(tab_room, "READ_LIMIT", 1024)


def _missing_levels_image(tmp_path):
    """A 2-D OME-Zarr of one level, coarse levels missing below a 64 px target."""
    from plexora.server.utils import ome_zarr
    from tests.ngff_fixtures import write_ngff

    source = write_ngff(tmp_path / "flat.ome.zarr", shape=(2, 300, 250), levels=1)
    return ome_zarr.open_image(source)


def test_missing_pyramid_levels_are_refused_on_the_website_before_a_level_is_written(
        tmp_path, small_tab):
    from plexora.server.utils import ome_zarr

    pyramid = _missing_levels_image(tmp_path)
    dest = tmp_path / "ext.zarr"
    with pytest.raises(TooLargeForTab, match="missing pyramid levels"):
        ome_zarr.build_extension(pyramid, dest, target=64)
    assert not dest.exists()
    assert [notice["reason"] for notice in small_tab] == ["tab-limit"]


def test_missing_pyramid_levels_are_built_off_the_website(tmp_path, desktop):
    from plexora.server.utils import ome_zarr

    pyramid = _missing_levels_image(tmp_path)
    built = ome_zarr.build_extension(pyramid, tmp_path / "ext.zarr", target=64)
    assert built is not None and Path(built).exists()


def _label_volume():
    labels = np.zeros((4, 64, 64), np.uint16)
    labels[:, 5:30, 5:30] = 1
    labels[1:3, 35:60, 10:50] = 2
    return labels


def test_a_3d_label_volume_is_refused_on_the_website_before_it_is_written(tmp_path, small_tab):
    from plexora.server.volume import labels as volume_labels

    dest = tmp_path / "labels.zarr"
    with pytest.raises(TooLargeForTab, match="3-D label volume"):
        volume_labels.build_label_volume(_label_volume(), dest)
    assert not dest.exists()
    assert not dest.with_name(dest.name + ".building").exists()


def test_a_3d_label_volume_whose_planes_cannot_be_read_whole_is_refused(
        tmp_path, small_tab, monkeypatch):
    """Room to write it, but one plane read whole is past READ_LIMIT."""
    from plexora.server.volume import labels as volume_labels

    monkeypatch.setattr(tab_room, "BUILD_BUDGET", 2**40)
    dest = tmp_path / "labels.zarr"
    with pytest.raises(TooLargeForTab, match="planes"):
        volume_labels.build_label_volume(_label_volume(), dest)
    assert not dest.exists()
    assert not dest.with_name(dest.name + ".building").exists()


def test_a_3d_label_volume_is_built_off_the_website(tmp_path, desktop):
    from plexora.server.volume import labels as volume_labels

    labels = _label_volume()
    dest = volume_labels.build_label_volume(labels, tmp_path / "labels.zarr")
    volume = volume_labels.open_label_volume(dest)
    assert np.array_equal(np.asarray(volume.plane(1)["0"][:, :]), labels[1])


def _mask_tiff(tmp_path):
    plane = np.zeros((96, 128), np.uint16)
    plane[10:40, 10:40] = 1
    plane[50:80, 20:60] = 2
    plane[20:70, 80:120] = 300
    source = tmp_path / "cells_mask.tif"
    tifffile.imwrite(source, plane)
    return source, plane


def test_a_mask_pyramid_is_refused_on_the_website_before_it_is_written(tmp_path, small_tab):
    from plexora.server.utils import segmentation_pyramid as sp

    source, _plane = _mask_tiff(tmp_path)
    out = tmp_path / "out.ome.tiff"
    with pytest.raises(TooLargeForTab, match="mask's pyramid"):
        sp.pyramidize_segmentation_mask(source, out, tile_size=64, outline=False)
    assert not out.exists()
    assert [notice["reason"] for notice in small_tab] == ["tab-limit"]


def test_a_mask_pyramid_is_built_off_the_website(tmp_path, desktop):
    from plexora.server.utils import segmentation_pyramid as sp

    source, plane = _mask_tiff(tmp_path)
    out = sp.pyramidize_segmentation_mask(source, tmp_path / "out.ome.tiff",
                                          tile_size=64, outline=False)
    assert np.array_equal(tifffile.imread(out, level=0), plane)


def test_a_whole_mask_read_on_the_website_is_capped_at_the_read_budget(monkeypatch):
    from plexora.server.utils import segmentation_pyramid as sp

    monkeypatch.delenv("PLEXORA_MASK_FULL_READ_MAX_BYTES", raising=False)
    monkeypatch.setenv("PLEXORA_BACKEND", "browser")
    assert sp._full_read_budget() == tab_room.READ_BUDGET
    monkeypatch.setenv("PLEXORA_MASK_FULL_READ_MAX_BYTES", str(2**40))
    assert sp._full_read_budget() == tab_room.READ_BUDGET
    monkeypatch.delenv("PLEXORA_BACKEND")
    assert sp._full_read_budget() == 2**40


# -- F. the page's notices -------------------------------------------------------

@pytest.fixture(scope="module")
def probe():
    if not shutil.which("node"):
        pytest.skip("needs node")
    done = subprocess.run(["node", str(PROBE)], capture_output=True, text=True,
                          encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_a_refusal_is_said_with_its_sentence_and_the_desktop_app(probe):
    assert probe["refusedWired"] is True
    assert probe["refusedShown"] is True
    [toast] = probe["refusedToasts"]
    assert toast["title"] == "Too large for a browser tab"
    assert toast["note"] == "This mask's pyramid needs about 3.4 GB here."
    assert toast["actions"][0] == "*Desktop app"
    assert len(toast["actions"]) <= 3


def test_python_stopping_says_its_cause(probe):
    [toast] = probe["stoppedToasts"]
    assert toast["title"] == "Plexora stopped in this tab"
    assert "RangeError: x" in toast["note"]
