"""The Settings page, and the one thing it can currently change.

Two properties are worth more than the rest of this file and are each pinned
twice, from the model side and through the route:

**A change is recorded, never applied to the running process.** `data_root()`
resolves once per interpreter and data_model is holding an open image against
it, so a live repoint would fail as a stack trace from whichever tile read got
there first. The page's whole restart banner rests on the two answers being
allowed to differ.

**The setting is written only after a migration succeeds.** Written first, a
failed copy leaves a config pointing at an empty directory while the projects
sit somewhere the app no longer looks -- which is the one outcome that turns a
mistake into "where has my work gone".

The suite-wide fixture exports PLEXORA_DATA_PATH, which is exactly the
environment override these routes refuse to write under, so almost everything
here runs on `settings_root` instead -- a root chosen the way a real desktop
install chooses one, through the settings file.
"""

import json
import time

import pytest

import plexora
from plexora import paths
from plexora.server.models import data_migration
from plexora.server.routes import settings_routes


@pytest.fixture
def settings_root(tmp_path, monkeypatch):
    """A data root chosen by the settings file rather than the environment.

    The settings file is put OUTSIDE the data root on purpose. In a real
    install it lives in the platform config directory and could never be
    mistaken for a project; conftest puts it inside tmp_path, where
    `migratable()` would list it as one more thing to move and quietly change
    what every count in this file means.
    """
    root = tmp_path / "root"
    root.mkdir()
    store = tmp_path / "settings.json"
    monkeypatch.delenv("PLEXORA_DATA_PATH", raising=False)
    monkeypatch.setattr(paths, "settings_path", lambda: store)
    store.write_text(json.dumps({"data_dir": str(root)}), encoding="utf-8")
    paths.reset()
    data_migration.reset()
    yield root
    paths.reset()
    data_migration.reset()


@pytest.fixture
def client(settings_root):
    return plexora.app.test_client()


def stored_dir():
    return paths.read_settings().get("data_dir")


def make_project(root, name, contents="pixels"):
    """A stand-in for a project directory: a folder with a file inside it, so a
    copy that made the directory and dropped its contents would fail here."""
    project = root / name
    project.mkdir(parents=True)
    (project / "image.tif").write_text(contents, encoding="utf-8")
    return project


def wait_for_job(timeout=10.0):
    """Block until the migration thread finishes, and return its final status.

    Polled rather than joined: `start` deliberately does not hand back the
    thread, because nothing in the app has anything to do with one.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = data_migration.status()
        if state.get("status") not in ("running",):
            return state
        time.sleep(0.02)
    raise AssertionError(f"migration did not finish: {data_migration.status()}")


# -- the page ------------------------------------------------------------


def test_the_page_renders_a_rail_entry_for_every_declared_section(client):
    """SECTIONS is the only list; the rail is generated from it. A section
    added to the tuple and forgotten in the template would render a tab that
    switches to a panel that does not exist."""
    body = client.get("/settings").get_data(as_text=True)
    # The opening tag, not the class prefix: `settings-tab-icon` and
    # `settings-tab-text` are inside every button and would each count again.
    assert body.count('<button type="button" class="settings-tab') == \
        len(settings_routes.SECTIONS)
    for section in settings_routes.SECTIONS:
        assert f'data-section="{section["id"]}"' in body
        assert section["label"] in body
        # Both halves: the rail button and the panel it reveals.
        assert f'id="settings_panel_{section["id"]}"' in body


def test_the_settings_link_is_on_every_page(client):
    """base.html's File menu, so the page is reachable from the viewer and the
    project picker alike rather than only by typing the URL."""
    assert 'id="nav_settings"' in client.get("/").get_data(as_text=True)


# -- reading the current state -------------------------------------------


def test_the_state_names_the_running_root_and_the_rule_that_chose_it(client, settings_root):
    state = client.get("/settings/data").get_json()
    assert state["in_use"] == str(settings_root)
    assert "settings.json" in state["rule"]
    assert state["pending"] == ""
    assert state["env_override"] == ""


def test_the_state_counts_what_is_in_the_root(client, settings_root):
    make_project(settings_root, "tonsil")
    make_project(settings_root, "spleen")
    assert client.get("/settings/data").get_json()["entry_count"] == 2


# -- changing the directory ----------------------------------------------


def test_a_change_is_recorded_and_the_running_process_stays_put(client, settings_root, tmp_path):
    """The load-bearing behaviour: the settings file moves, `data_root()` does
    not, and the state says both so the page can ask for a restart."""
    target = tmp_path / "elsewhere"
    before = paths.data_root()

    state = client.post("/settings/data",
                        json={"path": str(target), "migrate": "none"}).get_json()

    assert stored_dir() == str(target)
    assert paths.data_root() == before, "the live process must not be repointed"
    assert state["in_use"] == str(before)
    assert state["pending"] == str(target)


def test_an_environment_override_is_refused_rather_than_quietly_ignored(
        client, settings_root, tmp_path, monkeypatch):
    """PLEXORA_DATA_PATH beats the settings file (paths._candidate_data_root),
    so writing the preference would produce a user who restarts, lands in the
    same directory, and has nothing on screen explaining why."""
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(settings_root))
    paths.reset()

    response = client.post("/settings/data",
                           json={"path": str(tmp_path / "elsewhere"), "migrate": "none"})

    assert response.status_code == 409
    assert "PLEXORA_DATA_PATH" in response.get_json()["error"]
    assert stored_dir() == str(settings_root), "nothing may be written"
    assert client.get("/settings/data").get_json()["env_override"] == "PLEXORA_DATA_PATH"


def test_an_empty_path_is_rejected(client):
    assert client.post("/settings/data", json={"path": "   "}).status_code == 400
    assert client.post("/settings/data/check", json={}).status_code == 400


def test_an_unknown_migration_mode_is_rejected(client, tmp_path):
    response = client.post("/settings/data",
                           json={"path": str(tmp_path / "x"), "migrate": "sync"})
    assert response.status_code == 400


# -- the preview ---------------------------------------------------------


def test_checking_a_directory_does_not_create_it(client, tmp_path):
    """`paths.is_writable` mkdirs what it probes, which is right for a root the
    app is committing to and wrong here: a typo in the box would leave a real
    directory behind, and the "does not exist yet" line drawn from the same
    response would already be false."""
    target = tmp_path / "typo" / "data"

    result = client.post("/settings/data/check", json={"path": str(target)}).get_json()

    assert not target.exists(), "a preview must not touch the filesystem"
    assert result["exists"] is False
    assert result["writable"] is True, "it is creatable, which is what matters"


def test_the_preview_reports_what_would_move(client, settings_root, tmp_path):
    make_project(settings_root, "tonsil")
    make_project(settings_root, "spleen")

    result = client.post("/settings/data/check",
                         json={"path": str(tmp_path / "new")}).get_json()

    assert result["entries"] == ["spleen", "tonsil"]
    assert result["can_migrate"] is True
    assert result["collisions"] == []


def test_the_preview_names_collisions_and_refuses_the_whole_migration(
        client, settings_root, tmp_path):
    """All-or-nothing on purpose. Merging two project registries has a right
    answer only the user knows, and moving the non-colliding half would leave
    their projects split across two roots with nothing saying so."""
    make_project(settings_root, "tonsil")
    make_project(settings_root, "spleen")
    target = tmp_path / "new"
    make_project(target, "tonsil", contents="a different tonsil")

    result = client.post("/settings/data/check", json={"path": str(target)}).get_json()

    assert result["collisions"] == ["tonsil"]
    assert result["can_migrate"] is False
    # The one that did not collide is still refused.
    assert "spleen" in result["entries"]


def test_a_refused_migration_moves_nothing(client, settings_root, tmp_path):
    make_project(settings_root, "tonsil")
    target = tmp_path / "new"
    make_project(target, "tonsil", contents="theirs")

    response = client.post("/settings/data",
                           json={"path": str(target), "migrate": "move"})

    assert response.status_code == 409
    assert (settings_root / "tonsil" / "image.tif").read_text() == "pixels"
    assert (target / "tonsil" / "image.tif").read_text() == "theirs"
    assert stored_dir() == str(settings_root)


@pytest.mark.parametrize("nesting", ["inside", "contains"])
def test_nesting_either_way_is_refused(client, settings_root, nesting):
    """Target inside source is a tree copied into itself. Source inside target
    leaves the old root behind as an empty directory sitting in the new one,
    where the project listing would offer it as a project."""
    make_project(settings_root, "tonsil")
    target = (settings_root / "nested") if nesting == "inside" else settings_root.parent

    result = client.post("/settings/data/check", json={"path": str(target)}).get_json()

    assert result["can_migrate"] is False
    assert result["problems"], "a refusal has to say why"


# -- migrating -----------------------------------------------------------


def test_a_move_takes_the_projects_and_then_records_the_directory(
        client, settings_root, tmp_path):
    make_project(settings_root, "tonsil")
    make_project(settings_root, "spleen")
    (settings_root / "config.json").write_text("{}", encoding="utf-8")
    target = tmp_path / "new"

    response = client.post("/settings/data",
                           json={"path": str(target), "migrate": "move"})
    assert response.status_code == 202

    job = wait_for_job()
    assert job["status"] == "done", job.get("error")
    assert sorted(job["migrated"]) == ["config.json", "spleen", "tonsil"]
    assert (target / "tonsil" / "image.tif").read_text() == "pixels"
    assert not (settings_root / "tonsil").exists()
    assert stored_dir() == str(target)


def test_a_copy_leaves_the_originals_where_they_are(client, settings_root, tmp_path):
    make_project(settings_root, "tonsil")
    target = tmp_path / "new"

    client.post("/settings/data", json={"path": str(target), "migrate": "copy"})
    assert wait_for_job()["status"] == "done"

    assert (target / "tonsil" / "image.tif").read_text() == "pixels"
    assert (settings_root / "tonsil" / "image.tif").read_text() == "pixels"
    assert stored_dir() == str(target)


def test_the_directory_is_not_recorded_when_the_migration_fails(
        client, settings_root, tmp_path, monkeypatch):
    """The ordering that matters. A half-finished copy leaves the pointer on
    the root that still holds the data, so the recovery is "try again" rather
    than "find my projects"."""
    make_project(settings_root, "tonsil")
    target = tmp_path / "new"

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(data_migration, "_transfer", explode)

    client.post("/settings/data", json={"path": str(target), "migrate": "move"})
    job = wait_for_job()

    assert job["status"] == "error"
    assert "disk full" in job["error"]
    assert stored_dir() == str(settings_root), "the pointer must not have moved"
    assert (settings_root / "tonsil" / "image.tif").read_text() == "pixels"


def test_a_failure_stops_rather_than_carrying_on(client, settings_root, tmp_path):
    """Entries after the failure are left untouched, and the report names the
    ones that got through -- a partially migrated root nobody can describe is
    worse than one that stopped at a known point."""
    for name in ("a_first", "b_second", "c_third"):
        make_project(settings_root, name)
    target = tmp_path / "new"

    real = data_migration._transfer

    def fail_on_second(source, dest, name, mode):
        if name == "b_second":
            raise OSError("nope")
        return real(source, dest, name, mode)

    data_migration._transfer = fail_on_second
    try:
        client.post("/settings/data", json={"path": str(target), "migrate": "move"})
        job = wait_for_job()
    finally:
        data_migration._transfer = real

    assert job["status"] == "error"
    assert job["migrated"] == ["a_first"]
    assert (settings_root / "c_third").exists(), "later entries are untouched"


def test_only_one_migration_runs_at_a_time(client, settings_root, tmp_path):
    make_project(settings_root, "tonsil")
    data_migration._job.update(status="running")
    try:
        response = client.post("/settings/data",
                               json={"path": str(tmp_path / "new"), "migrate": "move"})
    finally:
        data_migration.reset()
    assert response.status_code == 409


def test_write_probes_are_not_carried_into_the_new_root(client, settings_root, tmp_path):
    """A crashed process can leave one behind, and it is the one file in a data
    root that means something and would be wrong somewhere else."""
    make_project(settings_root, "tonsil")
    (settings_root / f"{paths._PROBE_PREFIX}.999.1").write_text("", encoding="utf-8")
    target = tmp_path / "new"

    client.post("/settings/data", json={"path": str(target), "migrate": "move"})
    assert wait_for_job()["status"] == "done"

    assert not list(target.glob(f"{paths._PROBE_PREFIX}*"))
