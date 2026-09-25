"""The background fills: warm on open, and make available offline.

Both run on `layer_jobs.start_task`, which is `layer_jobs.start` without the
layer: the same record shape and the same `layer-*` thread naming (so the suite
joins them at teardown), but no project record to update and no effect on the
sample's `pending` flag.
"""

import threading

import numpy as np
import pytest

from plexora import datasource
from plexora.server.models import data_model, layer_jobs, remote_sources
from plexora.server.utils import remote_store
from tests.ngff_fixtures import write_ngff
from tests.remote_fixtures import cache_root, http_store  # noqa: F401

#: Captured at import, before the suite's autouse stub replaces it per test.
_REAL_START_WARM = remote_sources.start_warm


def _wait(sample, task_id, timeout=30):
    for thread in threading.enumerate():
        if thread.name == f"layer-{sample}-{task_id}":
            thread.join(timeout)
    return layer_jobs.get(sample, task_id)


def test_start_task_runs_and_reports():
    seen = []

    def work(stage, report, cancel):
        stage("reading")
        report(1, 2)
        seen.append(cancel.is_set())

    first = layer_jobs.start_task("demo", "__t__", work)
    assert first["status"] == "pending"
    final = _wait("demo", "__t__")
    assert final["status"] == "ready" and final["progress"] == 100
    assert seen == [False]
    # A task is not a layer: the sample's pending flag is untouched.
    assert "__t__" not in layer_jobs.status("demo")["layers"]


def test_stop_task_cancels_between_steps():
    started = threading.Event()

    def work(stage, report, cancel):
        started.set()
        cancel.wait(10)

    layer_jobs.start_task("demo", "__slow__", work)
    started.wait(5)
    assert layer_jobs.stop_task("demo", "__slow__", timeout=5)
    assert layer_jobs.get("demo", "__slow__")["status"] == "cancelled"


def test_a_failing_task_carries_its_install_line():
    def work(stage, report, cancel):
        raise remote_store.RemoteSupportMissing("gs", "gcsfs")

    layer_jobs.start_task("demo", "__fail__", work)
    final = _wait("demo", "__fail__")
    assert final["status"] == "failed"
    assert final["install"] == "pip install 'plexora[remote]'"


def test_warm_fetches_coarse_levels_within_its_budget(tmp_path, http_store, monkeypatch):
    write_ngff(tmp_path / "served" / "slide.ome.zarr", shape=(1, 512, 512), levels=3,
               version="0.5")
    server = http_store()
    url = server.url("slide.ome.zarr")
    # Room for the two coarse levels (16 + 4 chunks of 8 KiB), not level 0's 64.
    monkeypatch.setattr(remote_sources, "WARM_BUDGET_BYTES", 30 * 64 * 64 * 2)

    store, keys = remote_sources.warm_plan(url, cache_budget=10 ** 12)

    assert any(k.startswith("slide.ome.zarr/2/c/") or k.startswith("2/c/") for k in keys)
    assert any("/1/c/" in f"/{k}" for k in keys)
    assert not any("/0/c/" in f"/{k}" for k in keys)


def test_warm_on_open(tmp_path, http_store, monkeypatch):
    write_ngff(tmp_path / "served" / "slide.ome.zarr", shape=(1, 256, 256), levels=2,
               version="0.5")
    server = http_store()
    datasource.register_image_datasource(name="warm", image=server.url("slide.ome.zarr"))
    monkeypatch.setattr(remote_sources, "start_warm", _REAL_START_WARM)

    data_model.load_datasource("warm", reload=True)

    final = _wait("warm", remote_sources.REMOTE_WARM_ID)
    assert final["status"] == "ready"
    server.clear()
    np.asarray(data_model.channels[0])
    assert server.count(method="GET") == 0


def test_pin_refuses_what_the_budget_cannot_hold(tmp_path, http_store, monkeypatch):
    write_ngff(tmp_path / "served" / "slide.ome.zarr", shape=(1, 256, 256), levels=1,
               version="0.5")
    server = http_store()
    monkeypatch.setenv("PLEXORA_REMOTE_CACHE_BYTES", "1000")
    with pytest.raises(remote_sources.OverBudget) as refused:
        remote_sources.start_pin(server.url("slide.ome.zarr"))
    assert "GB" in str(refused.value)


def test_pin_brings_everything_and_it_all_serves_offline(tmp_path, http_store):
    write_ngff(tmp_path / "served" / "slide.ome.zarr", shape=(2, 256, 256), levels=2,
               version="0.5")
    server = http_store()
    url = server.url("slide.ome.zarr")
    record = remote_sources.start_pin(url)
    store_id = remote_store.store_id_of(url)
    assert record["status"] == "pending"
    final = _wait(remote_sources.PIN_SAMPLE, f"{remote_sources.REMOTE_PIN_ID}:{store_id}")
    assert final["status"] == "ready"
    assert remote_sources.is_pinned(url)

    with server.outage():
        from plexora.server.utils import ome_zarr

        pyramid = ome_zarr.open_image(url)
        for level in range(len(pyramid)):
            assert np.asarray(pyramid[level]).shape[0] == 2

    remote_sources.unpin(url)
    assert not remote_sources.is_pinned(url)


def test_clear_stops_what_is_filling(tmp_path, http_store):
    write_ngff(tmp_path / "served" / "slide.ome.zarr", shape=(1, 256, 256), levels=1,
               version="0.5")
    server = http_store()
    server.delay = 0.05
    url = server.url("slide.ome.zarr")
    remote_sources.start_pin(url)
    remote_sources.clear()
    assert not layer_jobs.running_tasks(remote_sources.REMOTE_PIN_ID)
    assert remote_store.cache_index().usage()["used_bytes"] == 0
