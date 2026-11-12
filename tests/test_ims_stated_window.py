"""An Imaris file's stated range as a volume's exact window.

Imaris saves each channel's full-resolution histogram with the file, and its
range as HistogramMin/HistogramMax on `DataSet/ResolutionLevel 0/TimePoint t/
Channel c` (arrays of single bytes, as every Imaris attribute is). That range
is the exact window (server/volume/windows.py) without a scan of the stack --
which for a 349 GB stack over SFTP was 2.3 GB fetched for one channel. A
statement no voxel of the type could hold, a minimum past the maximum, or a
maximum below what the coarsest level already shows (a placeholder) is not
believed, and the scan runs as before.
"""

import h5py
import numpy as np
import pytest

from plexora.server.volume import windows
from plexora.server.volume.sources import ims, ims_index

from tests import volume_fixtures as vf
from tests.volume_fixtures import _attr


@pytest.fixture(autouse=True)
def no_index_builder(monkeypatch):
    """No background chunk-index build from `open_ims_volume`."""
    monkeypatch.setattr(ims_index, "ensure", lambda path: False)


def _state(path, channel, high, low="0.000", frames=(0,)):
    """Write HistogramMin/HistogramMax the way Imaris does, on level 0."""
    with h5py.File(str(path), "a") as handle:
        for t in frames:
            group = handle[f"DataSet/ResolutionLevel 0/TimePoint {t}/Channel {channel}"]
            if high is not None:
                group.attrs["HistogramMax"] = _attr(high)
            if low is not None:
                group.attrs["HistogramMin"] = _attr(low)


def _stack(tmp_path, name="s.ims", **kwargs):
    kwargs.setdefault("channels", 2)
    kwargs.setdefault("depth", 6)
    kwargs.setdefault("height", 64)
    kwargs.setdefault("width", 48)
    return vf.write_ims(tmp_path / name, **kwargs)


def _ceiling(path, channel):
    source = ims.open_ims_volume(path)
    try:
        return source.stated_ceiling(channel)
    finally:
        source.close()


# -- what the file states -------------------------------------------------------

def test_the_stated_maximum_is_the_ceiling(tmp_path):
    path, _data = _stack(tmp_path)
    _state(path, 0, "1234.000")
    _state(path, 1, "3210.500", low="12.000")
    assert _ceiling(path, 0) == pytest.approx(1234.0)
    assert _ceiling(path, 1) == pytest.approx(3210.5)


def test_over_several_frames_the_ceiling_is_the_largest_stated(tmp_path):
    path, _data = _stack(tmp_path, frames=3)
    with h5py.File(str(path), "a") as handle:
        for t, high in enumerate(("1500.000", "2500.000", "2000.000")):
            group = handle[f"DataSet/ResolutionLevel 0/TimePoint {t}/Channel 0"]
            group.attrs["HistogramMin"] = _attr("0.000")
            group.attrs["HistogramMax"] = _attr(high)
    assert _ceiling(path, 0) == pytest.approx(2500.0)


def test_the_attribute_is_stored_as_imaris_stores_it(tmp_path):
    """An array of single bytes, not a string or a number."""
    path, _data = _stack(tmp_path)
    _state(path, 0, "1234.000")
    with h5py.File(str(path), "r") as handle:
        stored = handle["DataSet/ResolutionLevel 0/TimePoint 0/Channel 0"].attrs["HistogramMax"]
    assert stored.dtype == np.dtype("S1") and b"".join(stored) == b"1234.000"


@pytest.mark.parametrize("high,low", [
    pytest.param(None, "0.000", id="no-maximum"),
    pytest.param("0.000", "0.000", id="maximum-zero"),
    pytest.param("-5.000", None, id="maximum-negative"),
    pytest.param("3000.000", "5000.000", id="minimum-past-maximum"),
    pytest.param("70000.000", "0.000", id="past-uint16"),
])
def test_what_is_not_a_statement_is_none(tmp_path, high, low):
    path, data = _stack(tmp_path)
    assert data.dtype == np.uint16
    _state(path, 0, high, low=low)
    assert _ceiling(path, 0) is None


def test_a_file_without_the_attributes_states_nothing(tmp_path):
    path, _data = _stack(tmp_path)
    assert _ceiling(path, 0) is None
    assert _ceiling(path, 1) is None


# -- the window ---------------------------------------------------------------

def _recording(monkeypatch, source):
    """Every level read, through the scan or straight from the source."""
    levels = []
    scan = windows._scan_level

    def scanning(source_, level, channel, planes, gate=None):
        levels.append(("scan", level))
        return scan(source_, level, channel, planes, gate)

    monkeypatch.setattr(windows, "_scan_level", scanning)
    read_block = source.read_block

    def reading(level, *args, **kwargs):
        levels.append(("read_block", level))
        return read_block(level, *args, **kwargs)

    source.read_block = reading
    level_array = source.level_array

    def array(t, z, level, *args, **kwargs):
        levels.append(("level_array", level))
        return level_array(t, z, level, *args, **kwargs)

    source.level_array = array
    return levels


@pytest.mark.parametrize("above", [0, 2000], ids=["exactly-the-brightest", "above-it"])
def test_a_stated_maximum_is_the_exact_window_without_scanning_the_stack(
        tmp_path, monkeypatch, above):
    path, data = _stack(tmp_path)
    brightest = float(data[:, 0].max())
    stated = brightest + above
    _state(path, 0, f"{stated:.3f}")

    source = ims.open_ims_volume(path)
    try:
        coarsest = len(source.volume.levels) - 1
        assert coarsest > 0
        levels = _recording(monkeypatch, source)
        window, how = windows.volume_window(source, 0)
    finally:
        source.close()

    assert window == (0.0, pytest.approx(stated))
    assert how["exact"] is True
    assert how.get("stated") is True
    # Only the coarsest level may be read: the full-resolution stack never is.
    assert {level for _kind, level in levels} <= {coarsest}


def test_stated_window_alone_says_the_same(tmp_path):
    path, data = _stack(tmp_path)
    _state(path, 1, "4000.000")
    source = ims.open_ims_volume(path)
    try:
        stated = windows.stated_window(source, 1)
    finally:
        source.close()
    assert stated is not None
    window, how = stated
    assert window == (0.0, 4000.0) and how["exact"] is True and how.get("stated") is True


def test_a_placeholder_below_the_coarsest_level_is_not_believed(tmp_path):
    path, data = _stack(tmp_path)
    brightest = float(data[:, 0].max())
    coarse_brightest = float(data[:, 0, :, ::2, ::2].max())
    placeholder = coarse_brightest / 4
    _state(path, 0, f"{placeholder:.3f}")

    source = ims.open_ims_volume(path)
    try:
        assert source.stated_ceiling(0) == pytest.approx(placeholder)
        assert windows.stated_window(source, 0) is None
        window, how = windows.volume_window(source, 0)
    finally:
        source.close()

    # The scan's answer: the true maximum.
    assert window == (0.0, brightest)
    assert not how.get("stated")
    assert how["exact"] is True


def test_a_file_without_the_attributes_gets_the_scan_as_before(tmp_path, monkeypatch):
    path, data = _stack(tmp_path)
    source = ims.open_ims_volume(path)
    try:
        assert windows.stated_window(source, 0) is None
        levels = _recording(monkeypatch, source)
        windows_seen = [windows.volume_window(source, c) for c in range(2)]
    finally:
        source.close()

    for c, (window, how) in enumerate(windows_seen):
        assert window == (0.0, float(data[:, c].max()))
        assert how == {"exact": True, "level": 0}
    # The scan read the full-resolution stack.
    assert any(level == 0 for _kind, level in levels)


def test_a_mixed_file_scans_only_the_channel_that_says_nothing(tmp_path):
    path, data = _stack(tmp_path)
    _state(path, 0, "9000.000")
    source = ims.open_ims_volume(path)
    try:
        stated, how_stated = windows.volume_window(source, 0)
        scanned, how_scanned = windows.volume_window(source, 1)
    finally:
        source.close()
    assert stated == (0.0, 9000.0) and how_stated.get("stated") is True
    assert scanned == (0.0, float(data[:, 1].max())) and not how_scanned.get("stated")
