"""Stop and remove reach a surface build after its extraction too.

The cancel flag (`surface/store.py` `cancel`, `forget`) was read only while
the mesh was being extracted. Once the build was smoothing, measuring or
writing, a Stop did nothing and the build finished `ready`; removing the
card deleted the folder and then the build wrote its mesh, objects and
manifest into it again, files no layer points at. And a second `create`
of the same surface while it built replaced the build's cancel flag, so a
Stop after it no longer reached the build at all.

Each test holds the build on an Event (inside its Taubin smoothing, after
extraction; or, for the second create, just before the marching pass that
does read the flag), does the thing a user would do, then lets it go.
"""

import threading
import time
from pathlib import Path

import pytest

from plexora.server.models import layer_jobs
from plexora.server.models.project import Project
from plexora.server.surface import mesh as mesh_mod
from plexora.server.surface import store as surface_store

from tests.test_surface_layer import ISO, _project, _settle, client, surfaces_license  # noqa: F401


@pytest.fixture()
def held_smoothing(monkeypatch):
    """Hold a build thread at its first Taubin call until `release` is set.

    Only a layer build thread (`layer-*`) is held: anything else priming the
    kernel runs straight through."""
    entered = threading.Event()
    release = threading.Event()
    real = mesh_mod.taubin

    def holding(*args, **kwargs):
        if threading.current_thread().name.startswith("layer-"):
            entered.set()
            release.wait(30)
        return real(*args, **kwargs)

    monkeypatch.setattr(mesh_mod, "taubin", holding)
    yield entered, release
    release.set()


def _wait_for_thread_end(name, layer_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        alive = [t for t in threading.enumerate()
                 if t.name == f"layer-{name}-{layer_id}" and t.is_alive()]
        if not alive:
            return
        time.sleep(0.05)
    raise AssertionError(f"{layer_id}'s build thread did not end")


def test_removing_a_surface_while_it_smooths_leaves_no_files(tmp_path, client,
                                                             surfaces_license, held_smoothing):
    entered, release = held_smoothing
    _project(tmp_path, name="stoprm")
    layer_id = client.post("/volume/stoprm/surfaces", json=ISO).get_json()["layer"]
    folder = Path(Project.find("stoprm").layer(layer_id).src)
    assert entered.wait(30), layer_jobs.get("stoprm", layer_id)

    # The card's X while the bar says "Smoothing".
    assert client.delete(f"/project/stoprm/layers/{layer_id}").status_code == 200
    assert Project.find("stoprm").layer(layer_id) is None
    release.set()
    _settle("stoprm", layer_id)
    _wait_for_thread_end("stoprm", layer_id)

    assert not folder.exists(), (
        f"the removed surface's folder was written again after the removal: "
        f"{sorted(p.name for p in folder.iterdir())}")


def test_stop_while_a_surface_smooths_does_not_end_ready(tmp_path, client, surfaces_license,
                                                        held_smoothing):
    entered, release = held_smoothing
    _project(tmp_path, name="stopsm")
    layer_id = client.post("/volume/stopsm/surfaces", json=ISO).get_json()["layer"]
    assert entered.wait(30), layer_jobs.get("stopsm", layer_id)

    # The card's Stop while the bar says "Smoothing".
    assert client.post(f"/volume/stopsm/surfaces/{layer_id}/cancel").status_code == 200
    release.set()
    job = _settle("stopsm", layer_id)

    assert job["status"] != "ready", job
    assert Project.find("stopsm").layer(layer_id).status != "ready"


@pytest.fixture()
def held_extraction(monkeypatch):
    """Hold a build thread just before its marching pass, which reads the
    cancel flag at every strip, until `release` is set."""
    from plexora.server.surface import marching

    entered = threading.Event()
    release = threading.Event()
    real = marching.march

    def holding(*args, **kwargs):
        if threading.current_thread().name.startswith("layer-"):
            entered.set()
            release.wait(30)
        return real(*args, **kwargs)

    monkeypatch.setattr(marching, "march", holding)
    yield entered, release
    release.set()


def test_a_second_create_while_building_keeps_stop_reaching_the_build(tmp_path, client,
                                                                     surfaces_license,
                                                                     held_extraction):
    entered, release = held_extraction
    project = _project(tmp_path, name="stoprp")
    layer_id = client.post("/volume/stoprp/surfaces", json=ISO).get_json()["layer"]
    assert entered.wait(30), layer_jobs.get("stoprp", layer_id)

    # The agent's surface_create(replace=true) for the build the UI started:
    # layer_jobs leaves the running build alone and starts nothing.
    again = surface_store.create(Project.find("stoprp") or project, ISO, replace_existing=True)
    assert again["layer"] == layer_id
    running = [t for t in threading.enumerate()
               if t.name == f"layer-stoprp-{layer_id}" and t.is_alive()]
    assert len(running) == 1

    # Then Stop: it must reach the one build that is running.
    surface_store.cancel("stoprp", layer_id)
    release.set()
    job = _settle("stoprp", layer_id)

    assert job["status"] != "ready", job
