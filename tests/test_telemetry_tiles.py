"""Five hundred tiles, zero network requests, and no SQLite on a request thread."""

import threading
import time

import plexora
from plexora.telemetry import client as client_module
from plexora.telemetry.queue import Queue
from tests.brightfield_fixtures import write_planar_fluorescence


def test_tiles_never_touch_sqlite_or_the_network(telemetry_enabled, tmp_path, monkeypatch):
    t, fake = telemetry_enabled
    from plexora.datasource import register_image_datasource

    path = write_planar_fluorescence(tmp_path / "panel.ome.tif", height=512, width=512)
    entry = register_image_datasource("panel", path)
    key = entry["imageData"][0]["src"].rstrip("/").rsplit("/", 1)[-1]

    threads = []
    original = Queue.add_counters

    def spy(self, window, rows):
        threads.append(threading.current_thread().name)
        return original(self, window, rows)

    monkeypatch.setattr(Queue, "add_counters", spy)
    monkeypatch.setattr(client_module, "FLUSH_SECONDS", 0.2)
    t.start(plexora.app, "terminal", upload=True)
    client = plexora.app.test_client()
    request_threads = set()
    for i in range(500):
        response = client.get(f"/generated/data/panel/{key}/0/0_0.png?i={i % 7}")
        assert response.status_code == 200
        request_threads.add(threading.current_thread().name)

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        counted = [r for r in t.queue.snapshot(limit=100000)[0]
                   if r[1] == "server.summary" and r[2] == "tile_cache"]
        if sum(r[5] for r in counted) >= 500:
            break
        time.sleep(0.05)
    counted = {__import__("json").loads(r[3])["result"]: r[5] for r in counted}
    assert counted.get("hit", 0) + counted.get("miss", 0) == 500
    assert threads and set(threads) == {"plexora-telemetry-writer"}
    assert not request_threads & set(threads)
    # The uploader's first attempt is a minute after start: nothing yet.
    assert fake.requests == []
