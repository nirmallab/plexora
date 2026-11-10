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
import shutil
import subprocess
import time
from pathlib import Path

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


def test_on_the_website_data_and_updates_say_what_applies_there(client, monkeypatch):
    """The website keeps projects in the browser and is always current: its
    Data panel has no folder to read or move and its Updates panel no check,
    so neither asks a route that only the desktop answers (each was a 501
    and a red "That did not work")."""
    monkeypatch.setenv("PLEXORA_BACKEND", "browser")
    body = client.get("/settings").get_data(as_text=True)
    assert 'id="settings_web_storage"' in body
    assert 'id="settings_current_path"' not in body and 'id="settings_new_path"' not in body
    assert 'id="settings_updates_check"' not in body
    assert f"Plexora {plexora.__version__}" in body
    for section in settings_routes.SECTIONS:
        assert f'id="settings_panel_{section["id"]}"' in body
    assert settings_routes.WEBSITE_BLURBS["data"] in body

    monkeypatch.delenv("PLEXORA_BACKEND")
    body = client.get("/settings").get_data(as_text=True)
    assert 'id="settings_web_storage"' not in body
    assert 'id="settings_current_path"' in body and 'id="settings_updates_check"' in body


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


def test_a_directory_adopted_from_a_connection_can_still_be_changed_here(
        tmp_path, monkeypatch):
    """A suggestion is adopted INTO the settings file, not over it.

    So the page sees an ordinary recorded directory with no override standing
    over it, and the user can still move it. The refusal above is for
    PLEXORA_DATA_PATH, which provably cannot be changed from here -- and
    applying it to a suggested directory would leave somebody connected over
    ssh unable to correct the very path the connection had just chosen.
    """
    store = tmp_path / "settings.json"
    monkeypatch.delenv("PLEXORA_DATA_PATH", raising=False)
    monkeypatch.setattr(paths, "settings_path", lambda: store)
    monkeypatch.setenv("PLEXORA_DATA_PATH_DEFAULT", str(tmp_path / "suggested"))
    paths.reset()
    data_migration.reset()
    client = plexora.app.test_client()

    state = client.get("/settings/data").get_json()
    assert state["in_use"] == str((tmp_path / "suggested").resolve())
    assert "suggested" in state["rule"]
    assert state["env_override"] == ""
    assert state["pending"] == "", "adopting records it, so nothing is pending"

    response = client.post(
        "/settings/data",
        json={"path": str(tmp_path / "corrected"), "migrate": "none"})

    assert response.status_code == 200
    assert stored_dir() == str((tmp_path / "corrected").resolve())
    paths.reset()


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


# -- Locations (settingsPage.js LocationsSection, WebLocations) -------------
#
# What each row of Settings > Data's locations offers, and what a press
# sends, run in node against the shipped settingsPage.js
# (tests/js/settings_locations_probe.mjs). The routes themselves are
# tests/test_settings_locations.py's.

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCATIONS_PROBE = REPO_ROOT / "tests" / "js" / "settings_locations_probe.mjs"

#: Every line the probe prints, so a check that is deleted or renamed fails
#: here rather than silently reducing what is covered.
LOCATION_CHECKS = (
    'each location is a row',
    'a row carries its path, its kind and its project count',
    'a workspace row shows the folder picked, not its projects folder',
    'the default location is marked, and only it',
    'the head names where new projects are saved',
    'a location not mounted says so, and is not counted',
    'the default row offers no Make default',
    'Make default is greyed, with the reason, for the older nested folder',
    'for a shared folder',
    'and for a folder that is not there',
    'and is offered plainly for a writable folder of your own',
    'Remove is offered for added workspaces only',
    'Tidy up is offered for the older nested folder only',
    'and is greyed while Documents/Plexora holds projects',
    'pressing a greyed offer says why, in the card, and sends nothing',
    "Make default posts that location's id",
    'and the answer is drawn: the new default marked, the head moved',
    'and says so in a sentence',
    'a refusal is shown as the route wrote it, under the list',
    'Remove sends a DELETE for that location and says nothing in it changed',
    'with nothing in Documents/Plexora yet, Tidy up is offered',
    'Tidy up asks first, naming each folder that moves and where to',
    "with the dialog's X as its only way out",
    'and dismissed, it sends nothing',
    "confirmed, Tidy up posts the older folder's id",
    'and says what moved where',
    'Add an existing Plexora workspace opens the field',
    'a refused folder is said under the field, as the route wrote it',
    'Enter adds the typed folder, and the answer says what was found',
    'with PLEXORA_LIBRARY_PATH set the card says so',
    'and nothing can be added, removed or tidied up',
    'while where new projects go can still be chosen',
    "the website's default folder is the first row, marked default, with its count",
    'an added folder Python has mounted shows its count',
    'a folder the browser has not allowed this visit offers Reconnect',
    'the default folder offers neither Make default nor Remove',
    'Add a folder… and Set up again are offered',
    'Make default asks the store, with no reload, then remounts the folders',
    'Remove forgets the folder, then remounts, and says nothing in it changed',
    'Set up again opens the onboarding',
    "Add a folder… goes straight to the store's folder dialog",
    'with this browser as the default, its row says so and offers a folder',
    "and Make default brings this browser's projects along into that folder",
    'a default folder not opened this visit says so',
    'and reconnects in place, with no reload',
    "a store's refusal is shown in plain words",
)


@pytest.fixture(scope="module")
def locations_probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return subprocess.run([node, str(LOCATIONS_PROBE)], capture_output=True, text=True,
                          cwd=REPO_ROOT, timeout=60)


def test_the_locations_probe_passes(locations_probe):
    assert locations_probe.returncode == 0, f"{locations_probe.stdout}\n{locations_probe.stderr}"


@pytest.mark.parametrize("line", LOCATION_CHECKS)
def test_each_locations_check_ran(locations_probe, line):
    assert f"ok  {line}" in locations_probe.stdout


def test_no_locations_check_was_quietly_dropped(locations_probe):
    assert f"{len(LOCATION_CHECKS)} checks passed" in locations_probe.stdout


def test_the_desktop_data_panel_lists_locations_above_the_data_directory(client):
    """The wider question first: the data directory is one of the locations.
    Shared directories are rows of the list now, not a card of their own."""
    body = client.get("/settings").get_data(as_text=True)
    assert 'id="settings_locations"' in body
    assert body.index('id="settings_locations"') < body.index('id="settings_data_current"')
    for element_id in ("settings_locations_list", "settings_location_open", "settings_location_form",
                       "settings_location_path", "settings_location_add", "settings_location_error"):
        assert f'id="{element_id}"' in body, element_id
    assert "Add an existing Plexora workspace" in body
    assert 'id="settings_shared_card"' not in body


def test_the_website_data_panel_draws_its_folders_as_locations(client, monkeypatch):
    """The website's "Kept in" card is its default location's row now, with
    Add a folder and Set up again; the desktop's list is not drawn there."""
    monkeypatch.setenv("PLEXORA_BACKEND", "browser")
    body = client.get("/settings").get_data(as_text=True)
    for element_id in ("settings_web_place", "settings_web_locations", "settings_web_add_folder",
                       "settings_web_setup_again"):
        assert f'id="{element_id}"' in body, element_id
    assert 'id="settings_locations"' not in body
    for gone in ("settings_web_to_folder", "settings_web_to_browser", "settings_web_place_name"):
        assert gone not in body, gone


def test_the_locations_styles_live_in_main_css():
    """The project cards carry the location chip too, and the app shell turns
    a page's own stylesheet off when it leaves: the block is main.css's, and
    every part the scripts toggle with `hidden` is held to it there."""
    main = (REPO_ROOT / "plexora" / "client" / "src" / "css" / "main.css").read_text(encoding="utf-8")
    for selector in (".plx-locations-row", ".plx-locations-default", ".plx-locations-chip",
                     ".plx-locations-add[hidden]", ".plx-locations-error[hidden]",
                     ".open-project-location[hidden]"):
        assert selector in main, selector
