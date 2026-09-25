"""Opening a project, reported while it runs, and a remote open kept short.

The viewer's first data request waits on load_datasource. For an image read
from the web that used to be ten seconds of an empty canvas: most of it
downloading an overview level nothing needed yet, and none of it reported.
"""

import threading

import numpy as np
import pytest

from plexora import app, datasource
from plexora.server.models import data_model
from plexora.server.providers.remote import LazyOverview, RemoteImageProvider
from plexora.server.utils import remote_store
from tests.ngff_fixtures import write_ngff
from tests.remote_fixtures import cache_root, http_store  # noqa: F401


@pytest.fixture
def web_image(tmp_path, http_store):
    write_ngff(tmp_path / "served" / "slide.ome.zarr", shape=(2, 600, 600), levels=2,
               labels=["DNA", "CD3"], version="0.5")
    server = http_store()
    datasource.register_image_datasource(name="web", image=server.url("slide.ome.zarr"))
    return server


def test_an_unknown_project_reads_as_ready():
    assert data_model.get_load_status("never-opened")["status"] == "ready"


def test_a_remote_open_reports_its_steps_and_its_downloads(web_image, monkeypatch):
    seen = []
    real_stage = data_model.LoadProgress.stage

    def spy(self, key):
        real_stage(self, key)
        seen.append(dict(data_model.get_load_status(self.name)))

    data_model.load_datasource("web")          # whatever registration left
    monkeypatch.setattr(data_model.LoadProgress, "stage", spy)
    data_model.load_datasource("web", reload=True)

    assert [s["stage"] for s in seen] == ["segmentation", "image"]
    assert all(s["status"] == "pending" and s["remote"] for s in seen)
    assert seen[0]["message"] == "Opening the segmentation mask"
    done = data_model.get_load_status("web")
    # Still says it read from the web, and still counts: the first tiles come
    # over the network after the load, and the chip follows them.
    assert done["status"] == "ready" and done["remote"] is True
    assert done["downloaded_bytes"] >= 0


def test_a_failed_open_is_reported(web_image, monkeypatch):
    def boom(self):
        raise RuntimeError("no")

    monkeypatch.setattr(RemoteImageProvider, "open", boom)
    with pytest.raises(RuntimeError):
        data_model.load_datasource("web", reload=True)
    status = data_model.get_load_status("web")
    assert status["status"] == "error" and "no" in status["error"]


def test_the_route(web_image):
    data_model.load_datasource("web", reload=True)
    with app.test_client() as client:
        answer = client.get("/get_load_status?datasource=web").get_json()
    assert answer["status"] == "ready" and answer["remote"] is True


def test_the_overview_is_not_downloaded_by_the_open(web_image):
    data_model.load_datasource("web", reload=True)
    overview = data_model.zarray
    assert isinstance(overview, LazyOverview) and overview._array is None
    # ...and reads like the array it stands for once something asks.
    plane = data_model._require_overview("web")[0]
    assert plane.ndim == 2 and np.asarray(overview).shape[0] == 2
    stats = data_model.get_image_channel_stats(
        data_model.real_channels("web")[0]["fullname"], "web")
    assert stats["image_histogram"]


def test_concurrent_readers_compute_the_overview_once(web_image, monkeypatch):
    from plexora.server.utils import ome_zarr

    calls = []
    real = ome_zarr.overview_plane
    monkeypatch.setattr(ome_zarr, "overview_plane",
                        lambda pyramid: calls.append(1) or real(pyramid))
    data_model.load_datasource("web", reload=True)
    overview = data_model.zarray
    threads = [threading.Thread(target=lambda: overview[0]) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(calls) == 1


def test_downloaded_bytes_are_counted(web_image):
    before = remote_store.bytes_fetched()
    # Process-wide and only ever added to, so measured as a difference.
    data_model.load_datasource("web", reload=True)
    np.asarray(data_model.zarray)
    assert remote_store.bytes_fetched() > before
