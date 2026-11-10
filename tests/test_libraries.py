"""The website's projects on this computer, as the desktop app's own.

When the website keeps its projects in Documents/Plexora/projects, the
desktop app lists them beside its own and opens them in place -- read and
written there, state included -- so either app's work is the other's on its
next load. New projects made on this computer go there too (paths.
new_projects_root), while what registering builds and every cache stay in the
data root, which is never that folder.
"""

import json
import os

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


@pytest.fixture
def laptop(tmp_path, monkeypatch):
    """An account on its own computer, its data root at the platform default
    (tmp AppData), the website's library at tmp Documents/Plexora/projects."""
    lib = tmp_path / "Documents" / "Plexora" / "projects"
    default = tmp_path / "AppData" / "plexora"
    for name in ("PLEXORA_DATA_PATH", "PLEXORA_LIBRARY_PATH", "PLEXORA_SERVER_IS_REMOTE",
                 "PLEXORA_NOTEBOOK_MODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(paths, "website_library", lambda: lib)
    monkeypatch.setattr(paths, "user_data_dir", lambda *a, **k: str(default))
    monkeypatch.setattr(paths, "_stored_data_dir", lambda: None)
    monkeypatch.setattr(paths, "_suggested_data_dir", lambda: None)
    paths.reset()
    yield default.resolve(), lib
    paths.reset()


def test_the_websites_folder_is_never_the_data_root(laptop):
    """Even for an account with nothing yet: a data root holds what is one
    machine's (servers, node tokens, the agent's sessions, caches), and the
    website reads its folder whole."""
    default, lib = laptop
    _root(lib, "web_project")
    chosen = paths._candidate_data_root()
    assert chosen.path == default and chosen.rule == paths.RULE_PLATFORM_DEFAULT
    assert paths.library_roots() == [lib.resolve()]


def test_new_projects_go_to_the_websites_folder(laptop):
    default, lib = laptop
    assert not lib.exists()
    assert paths.new_projects_root() == lib.resolve()             # made, so it can be written
    assert lib.is_dir()
    # A name the user already has stays where it is.
    _root(default, "desktop_project")
    paths.reset()
    assert paths.new_projects_root("desktop_project") == default
    assert paths.new_projects_root("another") == lib.resolve()


@pytest.mark.parametrize("env", [("PLEXORA_SERVER_IS_REMOTE", "1"), ("PLEXORA_NOTEBOOK_MODE", "1"),
                                 ("PLEXORA_LIBRARY_PATH", ""), ("PLEXORA_DATA_PATH", "chosen")])
def test_new_projects_stay_in_the_data_root_elsewhere(laptop, tmp_path, monkeypatch, env):
    """A server reached from another computer, a notebook's server, a library
    list somebody named, or a data directory somebody chose."""
    name, value = env
    monkeypatch.setenv(name, str(tmp_path / value) if value == "chosen" else value)
    paths.reset()
    assert paths.new_projects_root() == paths.data_root()
    assert not laptop[1].exists()


def test_a_desktop_project_is_registered_in_the_websites_folder(laptop):
    """Its entry and state in the library the website reads; what registering
    built, and every cache after, in the data root."""
    from plexora.datasource import register_blank_datasource, set_pixel_size

    default, lib = laptop
    register_blank_datasource("made_on_desktop", width=512, height=512)
    lib = lib.resolve()
    assert "made_on_desktop" in json.loads((lib / "config.json").read_text(encoding="utf-8"))
    assert not (default / "config.json").exists() or "made_on_desktop" not in json.loads(
        (default / "config.json").read_text(encoding="utf-8"))
    assert Project.load("made_on_desktop").home_root == lib
    assert paths.project_state_dir("made_on_desktop") == lib / "made_on_desktop"
    assert paths.derived_root("made_on_desktop") == default / "made_on_desktop"
    assert paths.derived_root("made_on_desktop", home_root=lib) == default / "made_on_desktop"
    # An edit with no root named finds it there and saves it back there.
    set_pixel_size("made_on_desktop", 0.65, unit="µm")
    entry = json.loads((lib / "config.json").read_text(encoding="utf-8"))["made_on_desktop"]
    assert "0.65" in json.dumps(entry)


def test_a_website_projects_caches_are_built_in_the_data_root(library):
    """The desktop opening a project the website made: state in place, caches
    out of the folder the website holds in memory."""
    own, lib = library
    (lib / "web_project").mkdir()
    assert paths.project_state_dir("web_project") == lib / "web_project"
    assert paths.derived_root("web_project") == own / "web_project"
    assert paths.derived_root("web_project", home_root=lib) == own / "web_project"
    # A shared, writable root still builds in place.
    assert paths.derived_root("desktop_project", home_root=own) == own / "desktop_project"


def test_the_desktop_identifies_its_files_only_where_the_website_reads(library, tmp_path):
    """So the website can match them in a folder the user grants; the desktop's
    own registry is written exactly as before."""
    own, lib = library
    image = tmp_path / "data" / "slide.tif"
    image.parent.mkdir()
    image.write_bytes(b"x" * 4096)
    from plexora.server.models.project import read_config, write_config

    for root in (lib, own):
        config = read_config(root / "config.json")
        config["new_one"] = {"channelFile": str(image)}
        write_config(root / "config.json", config)
    in_library = json.loads((lib / "config.json").read_text(encoding="utf-8"))["new_one"]
    assert in_library["sources"][str(image)]["size"] == 4096
    assert "sources" not in json.loads((own / "config.json").read_text(encoding="utf-8"))["new_one"]


# -- datasets ---------------------------------------------------------------


def test_a_dataset_moves_to_the_websites_folder_once_it_holds_a_project_from_there(library):
    """The website reads only its folder's datasets.json. A dataset of the
    desktop's own projects stays in the data root; one that holds a project of
    the library is kept in the library, and never moves back."""
    from plexora.server.models import datasets

    own, lib = library
    cohort = datasets.create("Cohort", projects=["desktop_project"])
    assert set(datasets._read(own)) == {cohort.id}
    assert not (lib / datasets.FILENAME).exists()

    datasets.assign(["web_project"], cohort.id)
    assert set(datasets._read(lib)) == {cohort.id}
    assert datasets._read(own) == {}
    assert datasets.get(cohort.id).projects == ("desktop_project", "web_project")

    datasets.assign(["web_project"], None)
    assert set(datasets._read(lib)) == {cohort.id}


def test_the_websites_datasets_are_the_desktops(library):
    """One registry over both files: listed, named and renamed as one."""
    from plexora.server.models import datasets

    own, lib = library
    # Written as the website writes it: its data root is the library.
    made = datasets.create("Web cohort", projects=["web_project"], root=lib,
                           known=["web_project"])
    assert datasets.membership()["web_project"].id == made.id
    with pytest.raises(datasets.DatasetError):
        datasets.create("web cohort")
    datasets.rename(made.id, "Renamed")
    assert datasets._read(lib)[made.id]["name"] == "Renamed"
    assert not (own / datasets.FILENAME).exists()
    # Moving a project between two datasets in different files releases it.
    other = datasets.create("Desktop cohort", projects=["web_project"])
    assert datasets.get(made.id).projects == ()
    assert datasets.get(other.id).projects == ("web_project",)
    assert set(datasets._read(lib)) == {made.id, other.id}


def test_an_empty_dataset_goes_where_new_projects_do(laptop):
    from plexora.server.models import datasets

    default, lib = laptop
    first = datasets.create("Before the website")
    assert first.id in datasets._read(default)
    assert not lib.exists()                                     # asking never made it
    _root(lib, "web_project")
    paths.reset()
    second = datasets.create("After")
    assert second.id in datasets._read(lib.resolve())


def test_a_dataset_made_on_the_desktop_is_grouped_on_the_website(laptop):
    """As New Dataset does it: the dataset first, then each sample registered
    (in the library) and put in it."""
    from plexora.datasource import register_blank_datasource
    from plexora.server.models import datasets

    default, lib = laptop
    cohort = datasets.create("Cohort")
    register_blank_datasource("sample_1", width=64, height=64)
    datasets.assign(["sample_1"], cohort.id)
    lib = lib.resolve()
    assert datasets._read(lib)[cohort.id]["projects"] == ["sample_1"]
    assert cohort.id not in datasets._read(default)


# -- the workspace: Documents/Plexora ------------------------------------------


def _legacy(lib):
    """Documents/Plexora/Plexora/projects, where the website used to nest its
    projects, for the tmp library `lib`."""
    return lib.parent / "Plexora" / "projects"


def test_the_older_nested_folder_is_listed_after_the_library(laptop):
    default, lib = laptop
    _root(lib, "web_project")
    old = _root(_legacy(lib), "old_project")
    assert paths.legacy_nested_library() == _legacy(lib)
    assert [(entry.path, entry.rule) for entry in paths.library_root_resolutions()] == [
        (lib.resolve(), paths.RULE_WEBSITE_LIBRARY),
        (old.resolve(), paths.RULE_LEGACY_NESTED)]
    assert paths.roots()[:3] == [default, lib.resolve(), old.resolve()]
    assert Project.load("old_project").home_root == old.resolve()
    assert paths.project_state_dir("old_project") == old.resolve() / "old_project"
    # Never where a new project goes; one already there stays there.
    assert paths.new_projects_root() == lib.resolve()
    assert paths.new_projects_root("old_project") == old.resolve()


def test_a_library_made_after_the_older_one_is_found_without_a_restart(laptop):
    """The older folder found first must not hide Documents/Plexora/projects
    once the desktop app's first project makes it."""
    default, lib = laptop
    old = _root(_legacy(lib), "old_project")
    assert paths.library_roots() == [old.resolve()]
    _root(lib, "made_now")
    assert paths.library_roots() == [lib.resolve(), old.resolve()]


def test_ensure_workspace_is_idempotent_and_never_nests(laptop):
    default, lib = laptop
    workspace = lib.parent
    made = paths.ensure_workspace()
    assert made == (workspace, paths.WORKSPACE_CREATED)
    marker = json.loads((workspace / "workspace.json").read_text(encoding="utf-8"))
    assert marker["schema"] == 1 and len(marker["id"]) == 32 and marker["createdBy"] == "desktop"
    assert marker["name"] == "Plexora" and marker["createdAt"].endswith("Z")
    assert lib.is_dir() and not (workspace / "Plexora").exists()
    assert paths.read_workspace(workspace) == marker

    again = paths.ensure_workspace()
    assert again == (workspace, paths.WORKSPACE_FOUND)
    assert json.loads((workspace / "workspace.json").read_text(encoding="utf-8")) == marker
    assert sorted(entry.name for entry in workspace.iterdir()) == ["projects", "workspace.json"]


def test_ensure_workspace_marks_an_existing_folder_and_moves_nothing(laptop):
    default, lib = laptop
    old = _root(_legacy(lib), "old_project")
    assert paths.ensure_workspace() == (lib.parent, paths.WORKSPACE_MARKED)
    assert (lib.parent / "workspace.json").is_file()
    assert (old / "config.json").is_file() and not (old.parent / "workspace.json").exists()
    # A marker somebody else wrote is never replaced, even one that does not parse.
    (lib.parent / "workspace.json").write_text("{not json", encoding="utf-8")
    assert paths.ensure_workspace() == (lib.parent, paths.WORKSPACE_FOUND)
    assert (lib.parent / "workspace.json").read_text(encoding="utf-8") == "{not json"


@pytest.mark.parametrize("env", [("PLEXORA_SERVER_IS_REMOTE", "1"), ("PLEXORA_NOTEBOOK_MODE", "1"),
                                 ("PLEXORA_LIBRARY_PATH", ""), ("PLEXORA_DATA_PATH", "chosen")])
def test_ensure_workspace_leaves_other_accounts_alone(laptop, tmp_path, monkeypatch, env):
    name, value = env
    monkeypatch.setenv(name, str(tmp_path / value) if value == "chosen" else value)
    paths.reset()
    assert paths.ensure_workspace() is None
    assert not laptop[1].parent.exists()


def test_write_workspace_never_overwrites(tmp_path):
    first = paths.write_workspace(tmp_path / "ws", "website")
    second = paths.write_workspace(tmp_path / "ws", "desktop")
    assert second == first and first["createdBy"] == "website" and first["name"] == "ws"


def _settings(**values):
    with paths.settings_lock():
        settings = paths.read_settings()
        settings.update(values)
        paths.write_settings(settings)
    paths.reset()


def test_the_chosen_default_save_location_is_honoured(laptop, tmp_path):
    default, lib = laptop
    _root(lib, "web_project")
    _settings(new_projects_dir=str(default))
    assert paths.new_projects_root() == default
    assert paths.new_projects_root("web_project") == lib.resolve()     # stays where it is
    # A registered workspace, chosen.
    other = _root(tmp_path / "Lab" / "projects", "lab_project")
    _settings(locations={"version": 1, "locations": [{"id": "lab", "kind": "workspace",
                                                     "path": str(other.parent), "label": "Lab"}]},
              new_projects_dir=str(other))
    assert paths.new_projects_root() == other.resolve()


@pytest.mark.parametrize("chosen", ["legacy", "elsewhere", "unmounted"])
def test_a_default_that_cannot_be_used_is_passed_over(laptop, tmp_path, chosen):
    default, lib = laptop
    old = _root(_legacy(lib), "old_project")
    target = {"legacy": old, "elsewhere": tmp_path / "random",
              "unmounted": tmp_path / "Volumes" / "Lab" / "projects"}[chosen]
    _settings(locations={"version": 1, "locations": [
        {"id": "lab", "kind": "workspace", "path": str(tmp_path / "Volumes" / "Lab")}]},
        new_projects_dir=str(target))
    assert paths.new_projects_root() == lib.resolve()
    assert not (tmp_path / "Volumes").exists()                       # nothing made at a mount point


def test_new_projects_dir_is_not_honoured_on_a_server_reached_from_elsewhere(laptop, monkeypatch):
    default, lib = laptop
    _root(lib, "web_project")
    _settings(new_projects_dir=str(lib))
    monkeypatch.setenv("PLEXORA_SERVER_IS_REMOTE", "1")
    paths.reset()
    assert paths.new_projects_root() == default


def test_a_registered_workspace_is_one_of_the_roots(laptop, tmp_path, monkeypatch):
    default, lib = laptop
    _root(lib, "web_project")
    lab = _root(tmp_path / "Lab" / "projects", "lab_project")
    shared = _root(tmp_path / "site", "site_project")
    monkeypatch.setenv("PLEXORA_SHARED_PATH", str(shared))
    _settings(locations={"version": 1, "locations": [
        {"id": "lab", "kind": "workspace", "path": str(lab.parent), "label": "Lab"}]})
    assert paths.roots() == [default, lib.resolve(), lab.resolve(), shared.resolve()]
    assert paths.library_root_resolutions()[-1].rule == paths.RULE_REGISTERED_WORKSPACE
    project = Project.load("lab_project")
    assert project.home_root == lab.resolve() and not project.is_shared
    assert paths.is_website_library(lab)


def test_an_unavailable_location_is_skipped_not_removed(laptop, tmp_path):
    from plexora.server.models import library

    default, lib = laptop
    away = tmp_path / "Volumes" / "Lab"
    _settings(locations={"version": 1, "locations": [
        {"id": "lab", "kind": "workspace", "path": str(away), "label": "Lab"}]})
    assert away / "projects" not in paths.roots()
    listed = {place.id: place for place in library.locations()}
    assert listed["lab"].available is False and listed["lab"].label == "Lab"
    assert listed["lab"].project_count is None and not listed["lab"].writable
    assert paths.registered_locations()[0]["id"] == "lab"            # still recorded
    assert not away.exists()
    # Mounted again: listed as a root, with no restart.
    _root(away / "projects", "lab_project")
    assert (away / "projects").resolve() in paths.roots()
    assert {place.id: place for place in library.locations()}["lab"].available is True


def test_the_websites_extra_folders_are_its_libraries(tmp_path, monkeypatch):
    """boot.set_libraries: each folder the page mounted at /library/<id> is a
    library beside /data, and an empty list leaves none."""
    from plexora.browser import boot

    data = tmp_path / "data"
    data.mkdir()
    one = _root(tmp_path / "library" / "abc", "in_abc")
    two = tmp_path / "library" / "def"
    two.mkdir()
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(data))
    monkeypatch.setenv("PLEXORA_BACKEND", "browser")
    assert boot.set_libraries([str(one), str(two)]) == [str(one), str(two)]
    assert paths.library_roots() == [one.resolve()]                  # two has no projects yet
    assert Project.load("in_abc").home_root == one.resolve()
    from plexora.server.models import library

    places = {place.id: place for place in library.locations()}
    assert places["abc"].kind == "workspace" and places["def"].available
    assert library.root_for_new_projects("def") == two.resolve()
    assert boot.set_libraries([]) == []
    assert os.environ["PLEXORA_LIBRARY_PATH"] == "" and paths.library_roots() == []
