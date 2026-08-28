"""Every Browse button reaches a filter the picker knows about.

The bug this file exists for: the Data field on the import page asks for the
"data" filter, which native_dialog.py defines and browse_routes.py's hand-typed
allowlist did not. The route answered 400, browsePicker's fetch threw, and
attachBrowseButton had no error path -- so the button rendered normally, did
nothing at all when clicked, and said nothing about why.

The route is not exercised end to end here: browse_for_path opens a real OS
dialog and blocks until a human dismisses it. What is worth pinning is the
contract on either side of it -- that the names the templates ask for exist,
and that an unknown one is refused rather than silently widened.
"""

import re
from pathlib import Path

import pytest

import plexora
from plexora.server.utils import native_dialog

TEMPLATES = Path(plexora.__file__).parent / "client" / "templates"

#: Both halves of a Browse button: the filter and the mode it opens in.
_FILTER = re.compile(r'data-browse-filter="([^"]+)"')
_MODE = re.compile(r'data-browse-mode="([^"]+)"')


def _attributes(pattern):
    """(template name, value) for every occurrence across the templates."""
    found = []
    for path in TEMPLATES.rglob("*.html"):
        found.extend((path.name, value)
                     for value in pattern.findall(path.read_text(encoding="utf-8")))
    return found


@pytest.fixture
def client(monkeypatch, tmp_path):
    return plexora.app.test_client()


def test_every_filter_a_template_asks_for_exists():
    """A filter name is typed in three places -- the template, the picker's
    table and the route's guard -- and only this notices when they diverge."""
    asked = _attributes(_FILTER)
    assert asked, "no Browse buttons found; this test is no longer testing anything"

    unknown = sorted({value for _, value in asked} - native_dialog.FILTER_NAMES)
    assert not unknown, (
        f"templates ask for filter(s) {unknown} that native_dialog.py does not define"
    )


def test_every_mode_a_template_asks_for_is_one_the_picker_supports():
    modes = {value for _, value in _attributes(_MODE)}
    assert modes <= {"file", "directory"}


def test_the_data_field_offers_both_a_file_and_a_directory_picker():
    """A .csv is a file and a .zarr store is a directory, and one input takes
    both. Neither picker can select the other's kind, which is why there are
    two buttons rather than one."""
    upload = (TEMPLATES / "upload.html").read_text(encoding="utf-8")
    data_field = upload.split('id="data_file"', 1)[1].split("</div>", 2)[0]

    assert 'data-browse-mode="file"' in data_field
    assert 'data-browse-mode="directory"' in data_field


def test_an_unknown_filter_is_refused(client):
    response = client.post("/browse_path", json={"mode": "file", "filter": "nonsense"})

    assert response.status_code == 400
    assert "nonsense" in response.get_json()["error"]


def test_an_unknown_mode_is_refused(client):
    assert client.post("/browse_path", json={"mode": "sideways"}).status_code == 400


def test_a_known_filter_reaches_the_picker(client, monkeypatch):
    """Guards the route's own plumbing without opening a dialog: what matters
    is that "data" gets through to browse_for_path at all."""
    seen = {}

    def _fake(mode, file_filter, **kwargs):
        seen.update(mode=mode, file_filter=file_filter)
        return "/picked/cells.csv"

    monkeypatch.setattr(native_dialog, "available", lambda: True)
    monkeypatch.setattr("plexora.server.routes.browse_routes.browse_for_path", _fake)

    response = client.post("/browse_path", json={"mode": "file", "filter": "data"})

    assert response.get_json()["path"] == "/picked/cells.csv"
    assert seen == {"mode": "file", "file_filter": "data"}


# -- the machine with no desktop -------------------------------------------


def test_a_machine_with_no_desktop_offers_the_listing_instead(client, monkeypatch):
    """A compute node has no display, which used to make Browse simply refuse
    -- so the only way to name a file on the cluster was to know its path.

    The refusal is structured rather than prose so the button can act on it.
    """
    monkeypatch.setattr(native_dialog, "available", lambda: False)

    answer = client.post("/browse_path", json={"mode": "file", "filter": "data"})

    assert answer.status_code == 400
    assert answer.get_json()["fallback"] == "list"


def test_the_listing_puts_folders_first_and_hands_back_no_bytes(client, tmp_path):
    """A .zarr store is a directory and the single Data input takes one, so
    both kinds have to be equally easy to reach."""
    (tmp_path / "zebra.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (tmp_path / "store.zarr").mkdir()
    (tmp_path / ".hidden").write_text("", encoding="utf-8")

    answer = client.post("/list_dir", json={"path": str(tmp_path)}).get_json()

    assert [e["name"] for e in answer["entries"]] == ["store.zarr", "zebra.csv"]
    assert answer["entries"][0]["is_dir"] is True
    assert answer["entries"][1]["size"] == len("a,b\n1,2\n")
    # Names, sizes and kinds. Never content -- this is a picker, not a reader.
    assert set(answer["entries"][0]) == {"name", "is_dir", "size"}
    # And a way back up, so the picker is navigable rather than a dead end.
    assert answer["parent"] == str(tmp_path.parent)


def test_the_listing_refuses_something_that_is_not_a_folder(client, tmp_path):
    path = tmp_path / "cells.csv"
    path.write_text("a\n", encoding="utf-8")

    answer = client.post("/list_dir", json={"path": str(path)})
    assert answer.status_code == 400
    assert "Not a folder" in answer.get_json()["error"]


def test_the_listing_starts_at_home_when_asked_for_nowhere(client):
    from pathlib import Path as _Path

    answer = client.post("/list_dir", json={}).get_json()
    assert answer["path"] == str(_Path.home().resolve())


# -- browsing the other machine --------------------------------------------


def test_browse_can_be_relayed_to_a_node(client, monkeypatch):
    """The dialog opens where the desktop is.

    On the layout this exists for, "here" is a compute node with no display and
    the node is the laptop the user is looking at -- so the request is relayed
    rather than made by the browser, which has neither the address nor the
    token.
    """
    from plexora import nodes as node_api

    seen = {}

    def _fake(node, mode="file", file_filter="any"):
        seen.update(node=node, mode=mode, file_filter=file_filter)
        return "/Users/me/study/cells.h5ad"

    monkeypatch.setattr(node_api, "browse_on_node", _fake)

    answer = client.post("/browse_path", json={
        "mode": "file", "filter": "data", "node": "laptop"})

    assert answer.get_json()["path"] == "/Users/me/study/cells.h5ad"
    assert seen == {"node": "laptop", "mode": "file", "file_filter": "data"}


def test_relaying_to_a_node_that_is_not_registered_names_it(client):
    answer = client.post("/browse_path", json={"mode": "file", "node": "ghost"})

    assert answer.status_code == 400
    assert "ghost" in answer.get_json()["error"]
