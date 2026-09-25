"""What `image_status` says about an image at a web address.

A stat answers "missing" and "inaccessible" for a file; for a URL a two-second
probe of the metadata document does. The new answer is `offline`: the host
does not respond and the image cannot open from the cache. A host that does
not respond while the image CAN open from the cache is `ok` with
`offline: true` -- cached parts draw, and the viewer says so.
"""

import time

import numpy as np

from plexora import datasource
from plexora.server.models import data_model
from plexora.server.models.project import ImageSpec, Project
from tests.ngff_fixtures import write_ngff
from tests.remote_fixtures import cache_root, closed_port_url, http_store  # noqa: F401


def _register(tmp_path, http_store, mode="gateway", name="web"):
    write_ngff(tmp_path / "served" / "slide.ome.zarr", shape=(2, 256, 256), levels=2,
               version="0.5")
    server = http_store(mode)
    entry = datasource.register_image_datasource(name=name, image=server.url("slide.ome.zarr"))
    return server, entry


def test_a_reachable_image_is_ok(tmp_path, http_store):
    _register(tmp_path, http_store)
    status = data_model.image_status("web")
    assert status["status"] == "ok"
    assert status["offline"] is False
    assert status["remote"]["url"].endswith("slide.ome.zarr")


def test_a_host_that_is_not_there_is_offline_and_says_so_quickly(tmp_path, cache_root):
    url = closed_port_url("slide.ome.zarr")
    Project(name="gone", image=ImageSpec(src=url, kind="ome_zarr", width=10, height=10,
                                         max_level=1, num_channels=1)).save()
    started = time.monotonic()
    status = data_model.image_status("gone")
    assert status["status"] == "offline"
    assert time.monotonic() - started < 5
    assert "127.0.0.1" in status["detail"]


def test_an_opened_image_keeps_drawing_through_an_outage(tmp_path, http_store):
    server, entry = _register(tmp_path, http_store)
    data_model.load_datasource("web", reload=True)
    key = entry["imageData"][0]["src"].rstrip("/").rsplit("/", 1)[-1]
    first, _ = data_model.encode_tile("web", key, 1, "0_0.png", "default")

    with server.outage():
        from plexora.server.utils import remote_store

        remote_store._probes.clear()
        status = data_model.image_status("web")
        assert status["status"] == "ok" and status["offline"] is True
        again, _ = data_model.encode_tile("web", key, 1, "0_0.png", "default")
    assert again == first


def test_a_cached_image_reopens_offline(tmp_path, http_store, cache_root):
    server, entry = _register(tmp_path, http_store)
    data_model.load_datasource("web", reload=True)
    np.asarray(data_model.channels[1])  # the coarse level is now cached
    from plexora.server.utils import remote_store

    remote_store.cache_index().flush()
    remote_store._reset_for_tests(cache_root)  # a restart
    data_model._loaded_source = None

    with server.outage():
        status = data_model.image_status("web")
    assert status["status"] == "ok" and status["offline"] is True


def test_nothing_at_the_address_is_missing(tmp_path, http_store):
    server, _ = _register(tmp_path, http_store)
    project = Project.load("web")
    Project(name="moved", image=ImageSpec(
        src=server.url("elsewhere.ome.zarr"), kind="ome_zarr", width=10, height=10,
        max_level=1, num_channels=1)).save()
    assert data_model.image_status("moved")["status"] == "missing"
    assert project.image.src != server.url("elsewhere.ome.zarr")


def test_a_forbidden_address_is_inaccessible(tmp_path, http_store):
    server = http_store("forbidden")
    Project(name="private", image=ImageSpec(
        src=server.url("locked.ome.zarr"), kind="ome_zarr", width=10, height=10,
        max_level=1, num_channels=1)).save()
    assert data_model.image_status("private")["status"] == "inaccessible"


def test_classify_offline_before_unavailable():
    from plexora.server.providers.base import RemoteUnreachable
    from plexora.server.utils.remote_store import RemoteSupportMissing

    assert data_model.classify_image_error(RemoteUnreachable("down"))[0] == "offline"
    assert data_model.classify_image_error(
        RemoteSupportMissing("gs", "gcsfs"))[0] == "inaccessible"
    assert "offline" in data_model.IMAGE_FAILURE_STATUSES
