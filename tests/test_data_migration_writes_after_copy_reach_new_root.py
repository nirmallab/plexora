"""State saved after a data-root migration has copied a project is not lost
at the restart.

A migration runs in a background thread while the server keeps serving from
the root it started with (settings_routes.py: the new directory is recorded
and takes effect at the next start). Gates, channel lists and every plugin
table are saved through database_model into `paths.project_state_dir`, which
is still the OLD root. So a channel list or gate saved once a project has been
copied lands in the old root, and after the restart -- which reads the new
root -- the project comes back without it.

The correct behaviour is either of two things, and both tests accept either:
the save is refused (the user is told and can redo it after the restart), or
the saved row is there after the restart. What is not acceptable is a save
that reports success and is silently gone.
"""

import json
import time

import pytest

import plexora
from plexora import paths
from plexora.server.models import data_migration, database_model
from plexora.server.models.database_model import ChannelList


@pytest.fixture
def settings_root(tmp_path, monkeypatch):
    """A data root chosen by the settings file, outside the root itself, the
    way tests/test_settings_page.py sets one up (the suite's PLEXORA_DATA_PATH
    is an override the migration route refuses to write under)."""
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


def _make_project(root, name):
    project = root / name
    project.mkdir(parents=True)
    (project / "image.tif").write_text("pixels", encoding="utf-8")
    return project


def _wait_for_job(timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = data_migration.status()
        if state.get("status") != "running":
            return state
        time.sleep(0.02)
    raise AssertionError(f"migration did not finish: {data_migration.status()}")


def _save_or_refuse(name, cells):
    """Save a channel list the way the sidebar does; True if it was accepted."""
    try:
        database_model.save_list(ChannelList, name, cells)
    except Exception:  # noqa: BLE001 - a refusal is one of the correct answers
        return False
    return True


def _restart():
    """What the next start does: resolve the data root afresh from the
    settings file the migration's last step wrote."""
    paths.reset()


def test_a_channel_list_saved_after_a_copy_finished_is_there_after_the_restart(
        client, settings_root, tmp_path):
    _make_project(settings_root, "tonsil")
    target = tmp_path / "new"

    response = client.post("/settings/data",
                           json={"path": str(target), "migrate": "copy"})
    assert response.status_code == 202
    job = _wait_for_job()
    assert job["status"] == "done", job.get("error")
    # The page now shows "Restart to finish"; the viewer is still open and the
    # user keeps working before restarting.
    assert paths.data_root() == settings_root

    accepted = _save_or_refuse("tonsil", b'["CD3", "CD8"]')

    _restart()
    assert paths.data_root() == target
    row = database_model.get(ChannelList, "tonsil")
    if accepted:
        assert row is not None and row.cells == b'["CD3", "CD8"]', (
            "a channel list saved (successfully) after the copy finished is "
            f"missing after the restart; it was written to "
            f"{settings_root / 'tonsil' / 'tonsil.db'} in the old root")


def test_a_channel_list_saved_while_the_copy_runs_is_there_after_the_restart(
        client, settings_root, tmp_path, monkeypatch):
    """Saved after its project was copied but before the job ended: the copy
    in the new root is the snapshot from before the save."""
    _make_project(settings_root, "a_tonsil")
    _make_project(settings_root, "b_spleen")
    target = tmp_path / "new"

    real = data_migration._transfer
    accepted = {}

    def transfer_then_user_saves(source, dest, name, mode):
        real(source, dest, name, mode)
        if name == "a_tonsil":
            # The user gates in the open viewer while b_spleen is copying.
            accepted["a_tonsil"] = _save_or_refuse("a_tonsil", b'["PanCK"]')

    monkeypatch.setattr(data_migration, "_transfer", transfer_then_user_saves)
    response = client.post("/settings/data",
                           json={"path": str(target), "migrate": "copy"})
    assert response.status_code == 202
    job = _wait_for_job()
    assert job["status"] == "done", job.get("error")
    assert "a_tonsil" in accepted

    _restart()
    assert paths.data_root() == target
    row = database_model.get(ChannelList, "a_tonsil")
    if accepted["a_tonsil"]:
        assert row is not None and row.cells == b'["PanCK"]', (
            "a channel list saved (successfully) during the copy, after its "
            "project had been copied, is missing after the restart")
