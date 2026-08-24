"""Projects on a shared root: visible to everyone, owned by nobody who opens them.

The shape this pins is the one a workstation or HPC install needs. A site
provisions a directory of common datasets; every user sees them beside their
own, opens them, and explores them -- and everything that exploration produces
lands in the user's own root, because the shared one is not theirs to write.

The single-user case is the same code with one root, which is why almost none
of the rest of the suite mentions any of this.
"""

import json

import pytest

import plexora
from plexora import paths
from plexora import api
from plexora.server.models import data_model, database_model
from plexora.server.models.project import Project
from tests.helpers import ALL_CONFIRMED, csv_spec, entry, use_shared_roots


@pytest.fixture
def site(tmp_path, monkeypatch):
    """A shared root beside the user's own, both registered and empty."""
    shared = tmp_path / "site"
    use_shared_roots(monkeypatch, shared)
    return shared


def _register(root, name, **kwargs):
    """Write a project entry straight into `root`'s registry."""
    config_path = root / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    config[name] = entry(name, confirmed=ALL_CONFIRMED, **kwargs)
    root.mkdir(parents=True, exist_ok=True)
    (root / name).mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config), encoding="utf-8")


# -- discovery -----------------------------------------------------------


def test_a_shared_project_is_listed_beside_the_users_own(site, tmp_path):
    _register(site, "common_atlas")
    _register(tmp_path, "my_slide")

    names = set(plexora.get_config())

    assert names == {"common_atlas", "my_slide"}


def test_a_project_the_user_owns_shadows_a_shared_one_of_the_same_name(site, tmp_path):
    """Somebody who has made their own copy means to open theirs."""
    _register(site, "sample1", width=111)
    _register(tmp_path, "sample1", width=222)

    project = Project.load("sample1")

    assert project.home_root == paths.data_root()
    assert project.is_shared is False
    assert project.image.width == 222


def test_a_shared_project_knows_which_root_it_came_from(site):
    _register(site, "common_atlas")

    project = Project.load("common_atlas")

    assert project.home_root == site.resolve()
    assert project.is_shared is True
    assert project.read_dir == site.resolve() / "common_atlas"


def test_the_listing_says_which_projects_are_shared(site, tmp_path):
    """The Open Project page decides what to offer from this, so it cannot be
    left for the client to infer."""
    _register(site, "common_atlas")
    _register(tmp_path, "my_slide")

    listing = plexora.app.test_client().get("/projects").get_json()
    shared = {row["name"]: row["shared"] for row in listing}

    assert shared == {"common_atlas": True, "my_slide": False}


# -- where writes go -----------------------------------------------------


def test_a_users_own_state_for_a_shared_project_stays_in_their_root(site, tmp_path):
    """A gate set on somebody else's dataset is this user's work, and the
    shared root is usually not writable anyway."""
    _register(site, "common_atlas")

    api.store("common_atlas", "gating").put_state(b"my thresholds")

    assert (tmp_path / "common_atlas" / "common_atlas.db").exists()
    assert not (site / "common_atlas" / "common_atlas.db").exists()
    assert api.store("common_atlas", "gating").get_state() == b"my thresholds"


def test_a_plugins_files_for_a_shared_project_stay_in_the_users_root(site, tmp_path):
    _register(site, "common_atlas")

    directory = api.store("common_atlas", "roi").directory()

    assert directory == tmp_path / "common_atlas" / "plugins" / "roi"
    assert site not in directory.parents


def test_figures_are_never_written_to_a_shared_root(site, tmp_path):
    """A figure can draw on several datasources or none, so no project owns one
    and there is nothing for a site-managed root to hold."""
    assert paths.figures_root() == tmp_path / ".figures"


def test_two_users_of_one_shared_project_do_not_share_a_database(site, tmp_path,
                                                                  monkeypatch):
    """The isolation the split exists for: the same shared project, two data
    roots, two sets of saved work."""
    _register(site, "common_atlas")
    api.store("common_atlas", "gating").put_state(b"first user")

    second = tmp_path / "second-user"
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(second))
    paths.reset()

    assert api.store("common_atlas", "gating").get_state() is None
    api.store("common_atlas", "gating").put_state(b"second user")
    assert (second / "common_atlas" / "common_atlas.db").exists()


# -- what a shared project refuses ---------------------------------------


def test_deleting_a_shared_project_is_refused(site):
    """It belongs to whoever provisioned the root. Refused with a sentence
    rather than left to fail against a read-only filesystem -- and on a
    WRITABLE shared root, refusing is what stops one user removing a dataset
    everybody else is working from.
    """
    _register(site, "common_atlas")

    response = plexora.app.test_client().post("/project/common_atlas/delete")

    assert response.status_code == 403
    assert "shared" in response.get_json()["error"].lower()
    assert (site / "common_atlas").is_dir()
    assert "common_atlas" in Project.load_all()


def test_deleting_a_project_of_the_users_own_still_works(site, tmp_path):
    _register(tmp_path, "my_slide")

    response = plexora.app.test_client().post("/project/my_slide/delete")

    assert response.status_code == 200
    assert "my_slide" not in Project.load_all()
    assert not (tmp_path / "my_slide").exists()


def test_saving_a_shared_project_targets_its_own_root_not_the_users(site):
    """Redirecting the write into the user's root would fork the entry and
    leave two projects with one name -- worse than the write failing."""
    _register(site, "common_atlas")
    project = Project.load("common_atlas")

    assert project._write_root() == site.resolve()


# -- the loadedness guard ------------------------------------------------


def test_a_shadowed_project_is_not_mistaken_for_the_loaded_one(site, tmp_path,
                                                               monkeypatch):
    """A name is not enough to say "already loaded" once two roots are in play.

    Importing your own `sample1` while the shared `sample1` is the loaded one
    resolves to a different project under an unchanged name, and every guard
    keyed on the name alone would go on serving the other one's table.
    """
    _register(site, "sample1")
    shared_scope = data_model.loaded_scope("sample1")

    _register(tmp_path, "sample1")
    paths.reset()

    assert data_model.loaded_scope("sample1") != shared_scope


def test_one_root_keeps_the_guard_on_the_bare_name(tmp_path, monkeypatch):
    """The short-circuit that keeps this off the tile path for single-user
    installs, which is every install with no shared roots configured."""
    monkeypatch.delenv("PLEXORA_SHARED_PATH", raising=False)
    paths.reset()

    assert data_model.loaded_scope("sample1") == "sample1"


# -- the registry itself -------------------------------------------------


def test_saving_never_copies_shared_entries_into_the_users_registry(site, tmp_path):
    """`save` reads only its target root's config.json. Going through the
    merged view would rewrite every shared project into the user's own registry
    the first time they saved anything.
    """
    _register(site, "common_atlas")
    _register(tmp_path, "my_slide")

    Project.load("my_slide").patch(created_at="2026-01-01").save()

    own = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert set(own) == {"my_slide"}


def test_a_project_built_in_memory_saves_to_the_users_root(site, tmp_path):
    """No home root means the programmatic API's project, which is the user's."""
    csv_path = tmp_path / "cells.csv"
    csv_path.write_text("CellID,X_centroid,Y_centroid\n1,2,3\n", encoding="utf-8")
    Project(name="fresh", dataset=csv_spec(csv_path)).save()

    assert "fresh" in json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert not (site / "config.json").exists()
