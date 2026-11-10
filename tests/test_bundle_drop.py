"""A `.plexora` dropped on the desktop app's window (server/routes/
bundle_routes.py's drop sniffer).

The drop router asks the server what dropped files are before it does
anything with them (`POST /drop/inspect`). A project file is not a table or
an image: left to the import engine, it was refused as a kind it does not
know. The sniffer claims it by its suffix and names the `import_bundle`
action, which the page's Import dialog (views/projectImport.js) carries out
-- a dialog that asks before anything is added, so the action runs at once
wherever the file lands.
"""

import pytest

from plexora.server.models import drop_plan
from plexora.server.routes import bundle_routes


@pytest.fixture
def client():
    import plexora

    return plexora.app.test_client()


def test_a_project_file_is_claimed_by_its_suffix():
    found = bundle_routes._dropped_bundle("/Volumes/lab/exports/NLU.PLEXORA", {"open": None, "target": "home"})
    assert found["kind"] == "plexora_bundle"
    assert found["label"] == "NLU.PLEXORA"
    assert found["action"] == {"kind": "import_bundle", "status": "auto", "message": "Import NLU.PLEXORA"}


@pytest.mark.parametrize("path", ["/data/cells.csv", "/data/slide.ome.tif", "/data/plexora", "C:\\x\\a.plexora.zip"])
def test_anything_else_is_left_to_the_engine(path):
    assert bundle_routes._dropped_bundle(path, {"open": None, "target": "home"}) is None


def test_the_sniffer_is_registered_once():
    assert drop_plan._SNIFFERS.count(bundle_routes._dropped_bundle) == 1


@pytest.mark.parametrize("target", ["home", "viewer"])
def test_a_dropped_project_file_plans_the_import_dialog(client, tmp_path, target):
    """Wherever it lands -- the Samples page or an open sample -- the plan
    is one action, run without a confirmation: the dialog is the question."""
    bundle = tmp_path / "exports" / "lsp11385_2.plexora"
    bundle.parent.mkdir()
    bundle.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    plan = client.post("/drop/inspect", json={"paths": [str(bundle)], "target": target}).get_json()
    assert [item["kind"] for item in plan["items"]] == ["plexora_bundle"]
    assert not plan["unrecognised"]
    [action] = plan["actions"]
    assert action["kind"] == "import_bundle"
    assert action["status"] == "auto"
    assert action["paths"] == [str(bundle)]


def test_the_page_carries_the_action_out():
    """The action's kind names an executor the page registers; nothing else
    in core would run it."""
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "plexora" / "client" / "src" / "js" / "views"
              / "projectImport.js").read_text(encoding="utf-8")
    assert 'registerExecutor?.("import_bundle"' in source
