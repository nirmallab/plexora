"""Settings > Data's locations: every place projects are kept, through the routes.

`GET /settings/locations` lists them -- the data root, the workspace this
computer shares with the website, the website's older nested folder, the
workspaces the user added, shared roots -- and says where new projects go.
The rest change the list: add a workspace, take one off (no file is touched),
choose the default save location, and tidy the older nested folder up into
its workspace. On the website the list is read the same way and every change
is refused with a sentence: the page keeps its folders itself.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import tifffile

import plexora
from plexora import paths
from plexora.server.models.project import Project

LOCATION_KEYS = {"id", "kind", "label", "path", "workspace", "available", "writable",
                 "project_count", "is_default", "rule"}


def _root(path, *names):
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.json").write_text(json.dumps({name: {"channelFile": f"/picked/1/{name}.tif"}
                                                  for name in names}), encoding="utf-8")
    return path


@pytest.fixture
def desk(tmp_path, monkeypatch):
    """A desktop account: a data root of its own, Documents/Plexora as the
    workspace it shares with the website (marked), and nothing else yet."""
    own = tmp_path / "own"
    own.mkdir()
    lib = tmp_path / "Documents" / "Plexora" / "projects"
    _root(lib, "web_project")
    paths.write_workspace(lib.parent, "website")
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(own))
    monkeypatch.delenv("PLEXORA_LIBRARY_PATH", raising=False)
    monkeypatch.setattr(paths, "website_library", lambda: lib)
    paths.reset()
    yield SimpleNamespace(own=own.resolve(), lib=lib.resolve(), tmp=tmp_path,
                          client=plexora.app.test_client())
    paths.reset()


def _listed(client):
    answer = client.get("/settings/locations")
    assert answer.status_code == 200
    return answer.get_json()


def _by_kind(state, kind):
    return [place for place in state["locations"] if place["kind"] == kind]


def _workspace(path, *names, marked=True):
    _root(path / "projects", *names)
    if marked:
        paths.write_workspace(path, "desktop")
    return path


def test_the_list_and_where_new_projects_go(desk):
    state = _listed(desk.client)
    assert set(state) >= {"default_save", "locations", "website", "env_override"}
    assert state["website"] is False and state["env_override"] == ""
    assert all(set(place) == LOCATION_KEYS for place in state["locations"])
    data, library = state["locations"][:2]
    assert data["kind"] == "data_root" and data["path"] == str(desk.own) and data["is_default"]
    assert library["kind"] == "library" and library["path"] == str(desk.lib)
    assert library["id"] == paths.read_workspace(desk.lib.parent)["id"]
    assert library["label"] == "Documents/Plexora" and library["project_count"] == 1
    assert library["workspace"] == str(desk.lib.parent) and library["writable"]
    assert state["default_save"] == {"id": data["id"], "label": data["label"], "path": str(desk.own)}


def test_ids_are_stable(desk):
    first = [place["id"] for place in _listed(desk.client)["locations"]]
    paths.reset()
    assert [place["id"] for place in _listed(desk.client)["locations"]] == first


def test_adding_a_workspace(desk):
    lab = _workspace(desk.tmp / "Lab", "lab_project", marked=False)
    answer = desk.client.post("/settings/locations", json={"path": str(lab)})
    assert answer.status_code == 200, answer.get_json()
    added = answer.get_json()["location"]
    marker = paths.read_workspace(lab)                          # written, since it had none
    assert added["id"] == marker["id"] and added["kind"] == "workspace"
    assert added["path"] == str((lab / "projects").resolve()) and added["project_count"] == 1
    assert added["id"] in [place["id"] for place in answer.get_json()["locations"]]
    record = paths.read_settings()["locations"]
    assert record["version"] == 1
    assert {"id", "kind", "path", "label", "addedAt"} <= set(record["locations"][0])
    assert Project.load("lab_project").home_root == (lab / "projects").resolve()
    # Its projects folder, picked instead, is the same workspace, listed once.
    again = desk.client.post("/settings/locations", json={"path": str(lab / "projects")})
    assert again.status_code == 200 and again.get_json()["location"]["id"] == added["id"]
    assert len(paths.read_settings()["locations"]["locations"]) == 1


def test_adding_documents_plexora_before_it_holds_projects(first_run):
    """An account whose new projects stay in its data folder does not list
    an empty Documents/Plexora until the user adds it."""
    assert first_run.lib not in [Path(place["path"]) for place in _listed(first_run.client)["locations"]]
    answer = first_run.client.post("/settings/locations", json={"path": str(first_run.lib.parent)})
    assert answer.status_code == 200, answer.get_json()
    added = answer.get_json()["location"]
    assert added["kind"] == "library" and added["available"] and added["project_count"] == 0
    assert added["id"] == paths.read_workspace(first_run.lib.parent)["id"]
    assert first_run.client.delete(f"/settings/locations/{added['id']}").status_code == 200
    assert first_run.lib.is_dir()


@pytest.mark.parametrize("what", ["empty", "missing", "plain"])
def test_adding_something_that_is_not_a_workspace(desk, what):
    plain = desk.tmp / "Plain"
    plain.mkdir()
    path = {"empty": "", "missing": str(desk.tmp / "nowhere"), "plain": str(plain)}[what]
    answer = desk.client.post("/settings/locations", json={"path": path})
    assert answer.status_code == 400 and answer.get_json()["error"]
    assert "locations" not in paths.read_settings()


def test_taking_a_workspace_off_the_list_touches_no_file(desk):
    lab = _workspace(desk.tmp / "Lab", "lab_project")
    added = desk.client.post("/settings/locations", json={"path": str(lab)}).get_json()["location"]
    assert desk.client.post("/settings/locations/default",
                            json={"id": added["id"]}).status_code == 200
    answer = desk.client.delete(f"/settings/locations/{added['id']}")
    assert answer.status_code == 200
    assert answer.get_json()["removed"]["id"] == added["id"]
    assert added["id"] not in [place["id"] for place in answer.get_json()["locations"]]
    assert (lab / "projects" / "config.json").is_file() and (lab / "workspace.json").is_file()
    assert "new_projects_dir" not in paths.read_settings()      # the default went with it
    assert paths.new_projects_root() == desk.own
    assert desk.client.delete(f"/settings/locations/{added['id']}").status_code == 404
    data_id = _by_kind(_listed(desk.client), "data_root")[0]["id"]
    refused = desk.client.delete(f"/settings/locations/{data_id}")
    assert refused.status_code == 400 and refused.get_json()["error"]


def test_choosing_the_default_save_location(desk):
    lab = _workspace(desk.tmp / "Lab")
    (lab / "projects" / "config.json").unlink()                   # a workspace with no projects yet
    added = desk.client.post("/settings/locations", json={"path": str(lab)}).get_json()["location"]
    assert added["available"] and added["project_count"] == 0
    answer = desk.client.post("/settings/locations/default", json={"id": added["id"]})
    assert answer.status_code == 200
    state = answer.get_json()
    assert state["location"]["is_default"] and state["default_save"]["id"] == added["id"]
    assert paths.new_projects_root() == (lab / "projects").resolve()
    assert desk.client.post("/settings/locations/default", json={"id": "nope"}).status_code == 404


def test_the_older_folder_and_a_shared_one_cannot_be_the_default(desk, monkeypatch):
    _root(desk.tmp / "Documents" / "Plexora" / "Plexora" / "projects", "old_project")
    shared = _root(desk.tmp / "site", "site_project")
    monkeypatch.setenv("PLEXORA_SHARED_PATH", str(shared))
    paths.reset()
    state = _listed(desk.client)
    for kind in ("legacy", "shared"):
        place = _by_kind(state, kind)[0]
        answer = desk.client.post("/settings/locations/default", json={"id": place["id"]})
        assert answer.status_code == 400 and answer.get_json()["error"]
    assert "new_projects_dir" not in paths.read_settings()


def _legacy_folder(desk, *, pyramids=("k1",)):
    old = desk.tmp / "Documents" / "Plexora" / "Plexora"
    _root(old / "projects", "old_project")
    (old / "projects" / "old_project").mkdir()
    (old / "projects" / "old_project" / "old_project.db").write_bytes(b"x" * 100)
    for key in pyramids:
        (old / "pyramids" / key).mkdir(parents=True)
        (old / "pyramids" / key / "slide.ome.tif").write_bytes(b"p" * 50)
    paths.reset()
    return old


@pytest.fixture
def first_run(tmp_path, monkeypatch):
    """Documents/Plexora as the desktop app's first run leaves it (a marker
    and an empty projects/), with the website's older nested folder in it."""
    own = tmp_path / "own"
    own.mkdir()
    lib = tmp_path / "Documents" / "Plexora" / "projects"
    lib.mkdir(parents=True)
    (lib / ".DS_Store").write_bytes(b"\0")
    paths.write_workspace(lib.parent, "desktop")
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(own))
    monkeypatch.delenv("PLEXORA_LIBRARY_PATH", raising=False)
    monkeypatch.setattr(paths, "website_library", lambda: lib)
    paths.reset()
    yield SimpleNamespace(own=own.resolve(), lib=lib.resolve(), tmp=tmp_path,
                          client=plexora.app.test_client())
    paths.reset()


def test_tidying_up_the_older_nested_folder(first_run):
    old = _legacy_folder(first_run)
    (first_run.tmp / "Documents" / "Plexora" / "pyramids" / "k2").mkdir(parents=True)
    legacy = _by_kind(_listed(first_run.client), "legacy")[0]
    assert legacy["label"] == "Documents/Plexora/Plexora" and legacy["project_count"] == 1
    answer = first_run.client.post("/settings/locations/tidy", json={"id": legacy["id"]})
    assert answer.status_code == 200, answer.get_json()
    tidied = answer.get_json()["tidied"]
    assert tidied["moved"] == ["projects", "pyramids/k1"] and tidied["left"] == []
    assert tidied["location"]["path"] == str(first_run.lib)
    assert not old.exists()
    assert (first_run.lib / "old_project" / "old_project.db").read_bytes() == b"x" * 100
    assert (first_run.lib.parent / "pyramids" / "k1" / "slide.ome.tif").is_file()
    assert (first_run.lib.parent / "pyramids" / "k2").is_dir()
    assert Project.load("old_project").home_root == first_run.lib
    assert not _by_kind(answer.get_json(), "legacy")


def test_tidying_up_across_two_filesystems_copies_checks_then_removes(first_run, monkeypatch):
    from plexora.server.models import library

    old = _legacy_folder(first_run)
    monkeypatch.setattr(library, "_same_filesystem", lambda *a: False)
    legacy = _by_kind(_listed(first_run.client), "legacy")[0]
    answer = first_run.client.post("/settings/locations/tidy", json={"id": legacy["id"]})
    assert answer.status_code == 200, answer.get_json()
    assert not old.exists()
    assert (first_run.lib / "old_project" / "old_project.db").read_bytes() == b"x" * 100


def test_a_copy_that_does_not_match_is_undone(first_run, monkeypatch):
    from plexora.server.models import library

    old = _legacy_folder(first_run)
    monkeypatch.setattr(library, "_same_filesystem", lambda *a: False)
    monkeypatch.setattr(library, "_census", lambda folder: (0, id(folder)))
    legacy = _by_kind(_listed(first_run.client), "legacy")[0]
    answer = first_run.client.post("/settings/locations/tidy", json={"id": legacy["id"]})
    assert answer.status_code == 400 and "did not match" in answer.get_json()["error"]
    assert (old / "projects" / "config.json").is_file()
    assert first_run.lib.is_dir() and not any(first_run.lib.iterdir())


def test_tidying_up_keeps_a_pyramid_already_in_place(first_run):
    old = _legacy_folder(first_run, pyramids=("k1", "k2"))
    kept = first_run.tmp / "Documents" / "Plexora" / "pyramids" / "k1"
    kept.mkdir(parents=True)
    legacy = _by_kind(_listed(first_run.client), "legacy")[0]
    tidied = first_run.client.post("/settings/locations/tidy",
                                   json={"id": legacy["id"]}).get_json()["tidied"]
    assert tidied["moved"] == ["projects", "pyramids/k2"] and tidied["left"] == ["pyramids/k1"]
    assert (old / "pyramids" / "k1" / "slide.ome.tif").is_file()      # left where it was
    assert not any(kept.iterdir())


def test_tidying_up_never_merges_two_registries(desk):
    old = _legacy_folder(desk)
    legacy = _by_kind(_listed(desk.client), "legacy")[0]
    answer = desk.client.post("/settings/locations/tidy", json={"id": legacy["id"]})
    assert answer.status_code == 400 and "already holds projects" in answer.get_json()["error"]
    assert (old / "projects" / "config.json").is_file() and (old / "pyramids" / "k1").is_dir()
    data_id = _by_kind(_listed(desk.client), "data_root")[0]["id"]
    assert desk.client.post("/settings/locations/tidy", json={"id": data_id}).status_code == 400
    assert desk.client.post("/settings/locations/tidy", json={"id": "nope"}).status_code == 404


def test_a_library_list_named_by_the_environment_pins_the_list(desk, monkeypatch):
    monkeypatch.setenv("PLEXORA_LIBRARY_PATH", str(desk.lib))
    paths.reset()
    assert _listed(desk.client)["env_override"] == "PLEXORA_LIBRARY_PATH"
    lab = _workspace(desk.tmp / "Lab", "lab_project")
    answer = desk.client.post("/settings/locations", json={"path": str(lab)})
    assert answer.status_code == 409 and "PLEXORA_LIBRARY_PATH" in answer.get_json()["error"]
    assert desk.client.delete("/settings/locations/whatever").status_code == 409


def test_on_the_website_the_list_is_read_and_never_changed(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    mounted = _root(tmp_path / "library" / "abc", "in_abc")
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(data))
    monkeypatch.setenv("PLEXORA_BACKEND", "browser")
    monkeypatch.setenv("PLEXORA_LIBRARY_PATH", str(mounted))
    paths.reset()
    client = plexora.app.test_client()
    state = _listed(client)
    assert state["website"] is True
    assert [(place["id"], place["kind"]) for place in state["locations"]][1:] == [("abc", "workspace")]
    assert state["locations"][0]["kind"] == "data_root" and state["default_save"]["path"] == str(data.resolve())
    for answer in (client.post("/settings/locations", json={"path": str(tmp_path)}),
                   client.delete("/settings/locations/abc"),
                   client.post("/settings/locations/default", json={"id": "abc"}),
                   client.post("/settings/locations/tidy", json={"id": "abc"})):
        assert answer.status_code == 400
        assert "keeps its folders in this browser" in answer.get_json()["error"]


# -- where a project says it lives, and saving one somewhere chosen -------------


def test_a_project_card_carries_its_id_and_location(desk):
    _root(desk.own, "desktop_project")
    paths.reset()
    Project.load("desktop_project").save()
    cards = {card["name"]: card for card in desk.client.get("/projects").get_json()}
    places = {place["path"]: place for place in _listed(desk.client)["locations"]}
    assert cards["desktop_project"]["id"] == Project.load("desktop_project").id
    assert cards["web_project"]["id"] is None                        # not written since ids
    own, lib = places[str(desk.own)], places[str(desk.lib)]
    assert cards["desktop_project"]["location"] == {"id": own["id"], "label": own["label"],
                                                    "kind": "data_root"}
    assert cards["web_project"]["location"] == {"id": lib["id"], "label": "Documents/Plexora",
                                                "kind": "library"}


@pytest.fixture
def slide(tmp_path):
    path = tmp_path / "source" / "slide.ome.tif"
    path.parent.mkdir()
    tifffile.imwrite(path, np.random.default_rng(0).integers(0, 3000, (2, 64, 64)).astype(np.uint16))
    return path


def test_a_sample_is_saved_in_the_location_named(desk, slide):
    lab = _workspace(desk.tmp / "Lab")
    (lab / "projects" / "config.json").unlink()                   # nothing saved there yet
    added = desk.client.post("/settings/locations", json={"path": str(lab)}).get_json()["location"]
    answer = desk.client.post("/import/sample", json={"paths": [str(slide)], "name": "to_lab",
                                                      "location": added["id"]})
    assert answer.status_code == 200, answer.get_json()
    root = (lab / "projects").resolve()
    assert "to_lab" in json.loads((root / "config.json").read_text(encoding="utf-8"))
    assert Project.load("to_lab").home_root == root
    assert "to_lab" not in json.loads((desk.own / "config.json").read_text(encoding="utf-8")) \
        if (desk.own / "config.json").exists() else True
    # Without one, where new projects go.
    assert desk.client.post("/import/sample", json={"paths": [str(slide)],
                                                    "name": "default_place"}).status_code == 200
    assert Project.load("default_place").home_root == desk.own


def test_a_sample_is_refused_a_location_it_cannot_be_saved_in(desk, slide):
    old = _root(desk.tmp / "Documents" / "Plexora" / "Plexora" / "projects", "old_project")
    paths.reset()
    legacy = _by_kind(_listed(desk.client), "legacy")[0]
    refused = desk.client.post("/import/sample", json={"paths": [str(slide)], "name": "x",
                                                       "location": legacy["id"]})
    assert refused.status_code == 400 and refused.get_json()["error"]
    unknown = desk.client.post("/import/sample", json={"paths": [str(slide)], "name": "x",
                                                       "location": "nope"})
    assert unknown.status_code == 404
    assert "x" not in json.loads((old / "config.json").read_text(encoding="utf-8"))
    assert Project.find("x") is None


def test_desktop_info_names_the_workspace_and_the_older_folder(first_run):
    old = _legacy_folder(first_run)
    info = first_run.client.get("/desktop/info").get_json()
    assert info["workspace_root"] == str(paths.workspace_root())
    assert info["legacy_nested"] == str((old / "projects").resolve())
    assert info["library_roots"] == [str((old / "projects").resolve())]


def test_a_new_dataset_is_saved_in_the_location_named(desk):
    """The dataset builder's create carries the location into its own task."""
    import time

    from plexora.server.models import dataset_build, dataset_scan, layer_jobs
    from tests import dataset_fixtures as fx

    def poll(url, done):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            body = desk.client.get(url).get_json()
            if done(body):
                return body
            time.sleep(0.05)
        raise AssertionError(f"{url} did not finish")

    lab = _workspace(desk.tmp / "Lab")
    (lab / "projects" / "config.json").unlink()
    added = desk.client.post("/settings/locations", json={"path": str(lab)}).get_json()["location"]
    dataset_scan.forget()
    dataset_build.forget()
    try:
        token = desk.client.post("/datasets/scan", json={
            "paths": [str(fx.layout_e(desk.tmp / "cohort"))]}).get_json()["token"]
        poll(f"/datasets/scan/{token}", lambda body: not body["pending"])
        assert desk.client.post(f"/datasets/scan/{token}/create", json={
            "name": "Cohort", "location": "nope"}).status_code == 404
        started = desk.client.post(f"/datasets/scan/{token}/create", json={
            "name": "Cohort", "location": added["id"]})
        assert started.status_code == 202, started.get_json()
        built = poll(f"/datasets/scan/{token}/create", lambda body: body["status"] != "pending")
        assert built["status"] == "ready", built
        root = (lab / "projects").resolve()
        assert sorted(json.loads((root / "config.json").read_text(encoding="utf-8"))) == ["S1", "S2"]
        assert Project.load("S1").home_root == root
    finally:
        for owner, task_id in layer_jobs.running_tasks():
            if owner in (dataset_scan.TASK_OWNER, dataset_build.TASK_OWNER):
                layer_jobs.stop_task(owner, task_id)
        dataset_scan.forget()
        dataset_build.forget()
