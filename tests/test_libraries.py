"""The website's projects on this computer, as the desktop app's own.

When the website keeps its projects in Documents/Plexora/projects, the
desktop app lists them beside its own and opens them in place -- read and
written there, state included -- so either app's work is the other's on its
next load. New projects made on this computer go there too (paths.
new_projects_root), while what registering builds and every cache stay in the
data root, which is never that folder.
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
