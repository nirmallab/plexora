"""An Update (or Replace) import of a project whose image an earlier bundle
brought keeps that image where the project can open it (server/models/
bundles.py `_apply`): moving the project's folder aside into `.replaced` must
not take the image the earlier import placed in its `media/` with it."""

from pathlib import Path

from plexora import paths
from plexora.api.store import store
from plexora.server.models import bundles, sources
from plexora.server.models.project import Project
from plexora.server.utils import relink

from tests.test_bundles import _another_computer, _members, project  # noqa: F401 -- fixture


def _no_system_index(monkeypatch):
    """`resolve` binds `index=_index` at definition, so patching relink._index
    does not reach the import's search: hand it an empty index here."""
    resolve = relink.resolve
    monkeypatch.setattr(relink, "resolve", lambda *a, **k: resolve(*a, **{**k, "index": lambda name: []}))


def test_an_update_keeps_the_image_an_earlier_bundle_brought(project, tmp_path, monkeypatch):
    first_root = paths.data_root()
    real_key = sources.app_key()
    sent = tmp_path / "sent"
    sent.mkdir()

    # Computer A sends the project with its image.
    one = sent / "one.plexora"
    bundles.export(["slide"], one, include={"images": True})
    pid = _members(one)[1]["projects"][0]["id"]

    # Computer B imports it: the image lands in B's project folder.
    other = _another_computer(tmp_path, monkeypatch)
    _no_system_index(monkeypatch)
    monkeypatch.setattr(sources, "app_key", lambda: "host:OTHER")
    bundles.apply(one)
    brought = Path(Project.load("slide").image.src)
    assert brought.is_file()
    assert paths.project_state_dir("slide") in brought.parents

    # A changes its gates and sends the project again, without images (the default).
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(first_root))
    paths.reset()
    monkeypatch.setattr(sources, "app_key", lambda: real_key)
    store("slide", "gating").put_state(b'{"gates": {"CD3": 150}}')
    two = sent / "two.plexora"
    bundles.export(["slide"], two)
    # A's own copy is on A's disk, which B cannot see (one machine runs the
    # test, and Windows holds the registered image open, so it cannot move).
    on_a = project.parent
    present = relink._present
    monkeypatch.setattr(relink, "_present",
                        lambda path, facts=None: on_a not in Path(path).parents and present(path, facts))

    # B takes the update (its default action).
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(other / "projects"))
    paths.reset()
    monkeypatch.setattr(sources, "app_key", lambda: "host:OTHER")
    assert bundles.preview(two)["projects"][0]["status"] == "update"
    result = bundles.apply(two)["projects"][0]
    assert result["id"] == pid and result["action"] == "update" and result["backup"]
    assert store("slide", "gating").get_state() == b'{"gates": {"CD3": 150}}'

    # The project still names an image it has, and not one in the hidden backup.
    src = Path(Project.load("slide").image.src)
    assert src.is_file() and on_a not in src.parents, \
        f"the project names {src}, which this computer does not have; the image is now only at " \
        f"{sorted(Path(result['backup']).rglob('*.ome.tif'))}"
    assert ".replaced" not in src.parts
    assert result["refs"]["missing"] == 0, result["refs"]
