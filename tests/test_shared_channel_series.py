"""On the website one opened TIFF serves the viewer and an agent's reader.

Opening a pyramidal OME-TIFF walks every page's directory; for LSP11385 (40
channels, 7 levels) picked from disk that is 1,217 reads through the page and
about a second, paid again by the first calibration for an answer the viewer
already holds. With real threads (the desktop, a node) each reader keeps a
handle of its own: tifffile reads one handle behind a lock.
"""

import os

import numpy as np
import pytest
import tifffile

from plexora.server.utils import tiff_series
from plexora.server.utils.source_image import SourceImage


@pytest.fixture(autouse=True)
def _nothing_shared_between_tests():
    tiff_series._SHARED.clear()
    yield
    tiff_series._SHARED.clear()


def _pyramid(path, seed=0):
    rng = np.random.default_rng(seed)
    base = rng.integers(0, 4000, size=(3, 256, 256), dtype=np.uint16)
    with tifffile.TiffWriter(path, ome=True) as tif:
        tif.write(base, subifds=1, tile=(64, 64), metadata={"axes": "CYX"})
        tif.write(base[:, ::2, ::2], subfiletype=1, tile=(64, 64))
    return base


def test_on_the_website_every_reader_of_a_file_shares_one_open(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXORA_WORK", "cooperative")
    path = tmp_path / "image.ome.tif"
    _pyramid(path)
    first, series, shared = tiff_series.open_channel_series(path)
    again, series_again, shared_again = tiff_series.open_channel_series(path)
    assert shared and shared_again
    assert again is first and series_again is series


def test_a_changed_file_is_opened_again(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXORA_WORK", "cooperative")
    path = tmp_path / "image.ome.tif"
    _pyramid(path)
    first, _, _ = tiff_series.open_channel_series(path)
    _pyramid(path, seed=1)
    stat = os.stat(path)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
    second, _, _ = tiff_series.open_channel_series(path)
    assert second is not first


def test_with_threads_each_reader_has_its_own_handle(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXORA_WORK", "threads")
    path = tmp_path / "image.ome.tif"
    _pyramid(path)
    first, _, shared = tiff_series.open_channel_series(path)
    second, _, _ = tiff_series.open_channel_series(path)
    assert not shared and second is not first
    first.close()
    second.close()


def test_an_agents_reader_takes_the_viewers_open_and_leaves_it_open(tmp_path, monkeypatch):
    from plexora.server.models import data_model
    from tests.agent_fixtures import make_synthetic_project

    monkeypatch.setenv("PLEXORA_WORK", "cooperative")
    make_synthetic_project(tmp_path, "one")
    data_model.load_datasource("one")
    assert len(tiff_series._SHARED) == 1
    (viewer, _series), = tiff_series._SHARED.values()
    before = np.asarray(data_model.channels["0"][0, :32, :32])
    reader = SourceImage("one")
    assert reader._file is viewer and reader._shared_file
    region = np.asarray(reader._zarr["0"][0, :32, :32])
    assert np.array_equal(region, before)
    reader.close()
    assert not viewer.filehandle.closed
    # The viewer reads on through the file the reader let go of.
    assert np.array_equal(np.asarray(data_model.channels["0"][0, :32, :32]), before)
