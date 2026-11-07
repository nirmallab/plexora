"""The website's projects on this computer, as the desktop app's own.

When the website keeps its projects in Documents/Plexora/projects, the
desktop app lists them beside its own and opens them in place -- read and
written there, state included -- so either app's work is the other's on its
next load. And an account that has no projects yet simply uses that folder.
"""

import json

import pytest

from plexora import paths
from plexora.server.models.project import Project


def _root(path, *names):
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.json").write_text(json.dumps({name: {"channelFile": f"/picked/1/{name}.tif"}
                                                  for name in names}), encoding="utf-8")
    return path


@pytest.fixture
def library(tmp_path, monkeypatch):
    own = _root(tmp_path / "own", "desktop_project", "both")
    lib = _root(tmp_path / "Documents" / "Plexora" / "projects", "web_project", "both")
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(own))
    monkeypatch.setenv("PLEXORA_LIBRARY_PATH", str(lib))
    paths.reset()
    yield own.resolve(), lib.resolve()
    paths.reset()


def test_listed_after_the_users_own_and_never_shared(library):
    own, lib = library
    assert paths.roots()[:2] == [own, lib]
    assert paths.owned_roots() == [own, lib]
    homes = Project.load_roots()
    assert homes["web_project"] == lib and homes["desktop_project"] == own
    assert homes["both"] == own                                   # the user's own copy wins
    project = Project.load("web_project")
    assert project.home_root == lib and not project.is_shared


def test_its_state_is_kept_in_place(library):
    own, lib = library
    assert paths.project_state_dir("web_project") == lib / "web_project"
    assert paths.project_state_dir("desktop_project") == own / "desktop_project"
    assert paths.project_state_dir("both") == own / "both"
    assert paths.project_state_dir("new_one") == own / "new_one"


def test_a_save_goes_back_to_the_library(library):
    own, lib = library
    from dataclasses import replace

    replace(Project.load("web_project"), title="Renamed on the desktop").save()
    stored = json.loads((lib / "config.json").read_text(encoding="utf-8"))
    assert stored["web_project"]["title"] == "Renamed on the desktop"
    assert "web_project" not in json.loads((own / "config.json").read_text(encoding="utf-8"))


def test_none_when_the_folder_holds_no_projects(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXORA_LIBRARY_PATH", str(tmp_path / "empty"))
    paths.reset()
    assert paths.library_roots() == []
    monkeypatch.setenv("PLEXORA_LIBRARY_PATH", "")
    paths.reset()
    assert paths.library_roots() == []


def test_an_account_with_nothing_yet_uses_the_websites_folder(tmp_path, monkeypatch):
    lib = _root(tmp_path / "Documents" / "Plexora" / "projects", "web_project")
    default = tmp_path / "AppData" / "plexora"
    monkeypatch.delenv("PLEXORA_DATA_PATH", raising=False)
    monkeypatch.delenv("PLEXORA_LIBRARY_PATH", raising=False)
    monkeypatch.setattr(paths, "website_library", lambda: lib)
    monkeypatch.setattr(paths, "user_data_dir", lambda *a, **k: str(default))
    monkeypatch.setattr(paths, "_stored_data_dir", lambda: None)
    monkeypatch.setattr(paths, "_suggested_data_dir", lambda: None)
    paths.reset()
    try:
        chosen = paths._candidate_data_root()
        assert chosen.path == lib.resolve() and chosen.rule == paths.RULE_WEBSITE_LIBRARY
        # An account that already has projects keeps its own folder, and sees
        # the website's as a second library instead.
        _root(default, "desktop_project")
        assert paths._candidate_data_root().rule == paths.RULE_PLATFORM_DEFAULT
    finally:
        paths.reset()
