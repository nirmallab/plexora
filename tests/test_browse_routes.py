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

import os
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
    # Names, sizes, kinds -- and the path, which is the picker's whole way of
    # navigating without doing path arithmetic in a browser that has no idea
    # whether the far side joins with "/" or "\". Never content: this is a
    # picker, not a reader.
    assert set(answer["entries"][0]) == {"name", "is_dir", "size", "path"}
    assert answer["entries"][0]["path"] == str(tmp_path / "store.zarr")
    # And a way back up, so the picker is navigable rather than a dead end.
    assert answer["parent"] == str(tmp_path.parent)


def test_the_listing_refuses_something_that_is_not_a_folder(client, tmp_path):
    """A path naming a file is not a refusal any more -- it opens the folder
    that holds it. What is left is a path that names nothing at all."""
    answer = client.post("/list_dir", json={"path": str(tmp_path / "gone" / "away")})
    assert answer.status_code == 400
    assert "Not a folder" in answer.get_json()["error"]


def test_a_file_path_opens_the_folder_that_holds_it(client, tmp_path):
    """Both callers hand /list_dir whatever was in the text box, and that is as
    often a file as a folder -- a field being corrected already holds one. It
    is tolerated in dir_listing rather than in this route so that the node's
    own /node/v1/list_dir gets the same behaviour for free."""
    (tmp_path / "cells.csv").write_text("a\n", encoding="utf-8")

    answer = client.post("/list_dir", json={"path": str(tmp_path / "cells.csv")})

    assert answer.status_code == 200
    assert answer.get_json()["path"] == str(tmp_path)


def test_the_listing_carries_a_clickable_trail_back_to_the_root(client, tmp_path):
    """Built here, on the machine that owns the filesystem, for the same reason
    each entry's path is: the label of "/" is the empty string and so is the
    label of "C:\\", and only the side holding the Path object knows that."""
    (tmp_path / "study").mkdir()

    answer = client.post("/list_dir", json={"path": str(tmp_path / "study")}).get_json()

    crumbs = answer["crumbs"]
    assert crumbs[-1] == {"label": "study", "path": str(tmp_path / "study")}
    assert crumbs[-2] == {"label": tmp_path.name, "path": str(tmp_path)}
    # The first crumb is the root, which has no name of its own.
    assert crumbs[0]["path"] == str(Path(tmp_path.anchor))
    assert crumbs[0]["label"] == str(Path(tmp_path.anchor))
    # Every crumb is somewhere the picker can actually go.
    assert all(client.post("/list_dir", json={"path": c["path"]}).status_code == 200
               for c in crumbs)


def test_dotfiles_stay_hidden_until_they_are_asked_for(client, tmp_path):
    """Noise in a picker for scientific data, and skipped while scanning rather
    than filtered afterwards -- so a .snakemake directory cannot eat the
    2000-entry limit of the folder somebody is actually looking at."""
    (tmp_path / "cells.csv").write_text("a\n", encoding="utf-8")
    (tmp_path / ".hidden").write_text("", encoding="utf-8")

    plain = client.post("/list_dir", json={"path": str(tmp_path)}).get_json()
    asked = client.post("/list_dir", json={"path": str(tmp_path),
                                           "show_hidden": True}).get_json()

    assert [e["name"] for e in plain["entries"]] == ["cells.csv"]
    assert [e["name"] for e in asked["entries"]] == [".hidden", "cells.csv"]


def test_the_whole_directory_is_sorted_before_the_limit_cuts_it():
    """The bug: the scan stopped at the limit and sorted what it had, so the
    2000 entries a user was shown were an arbitrary slice of readdir order --
    which on a scratch mount is no order at all. Their file could be absent
    from a listing of a directory it is in, with no way to tell.

    Called directly rather than through the route: 2001 files is not a fixture
    anybody wants, and the limit is the argument being tested."""
    import tempfile

    from plexora.server.utils import dir_listing

    with tempfile.TemporaryDirectory() as raw:
        folder = Path(raw)
        for name in ("b.csv", "c.csv", "a.csv"):
            (folder / name).write_text("", encoding="utf-8")

        found = dir_listing.listing(str(folder), limit=2)

    assert [e["name"] for e in found["entries"]] == ["a.csv", "b.csv"]
    assert found["truncated"] is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root can read anything")
def test_a_folder_that_cannot_be_read_says_so_plainly(client, tmp_path):
    """On a cluster this is not a bug to report but a fact about the account:
    /n/groups holds a hundred directories the user cannot enter, and they need
    to read the sentence and move on."""
    shut = tmp_path / "locked"
    shut.mkdir()
    shut.chmod(0o000)
    try:
        answer = client.post("/list_dir", json={"path": str(shut)})

        assert answer.status_code == 400
        assert answer.get_json()["error"] == f"Permission denied: {shut}"
    finally:
        # Or pytest's own tmp_path cleanup fails, in a later test.
        shut.chmod(0o755)


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


def test_a_relayed_listing_is_asked_for_hidden_files_too(client, monkeypatch):
    """The toggle is a question put to whichever machine owns the filesystem,
    not a filter applied here -- there is nothing here to filter."""
    from plexora import nodes as node_api

    seen = {}

    def _fake(node, path="", show_hidden=False):
        seen.update(node=node, path=path, show_hidden=show_hidden)
        return {"path": path, "parent": None, "crumbs": [], "entries": [],
                "truncated": False}

    monkeypatch.setattr(node_api, "list_dir_on_node", _fake)

    client.post("/list_dir", json={"node": "hpc", "path": "/scratch",
                                   "show_hidden": True})

    assert seen == {"node": "hpc", "path": "/scratch", "show_hidden": True}


@pytest.mark.parametrize("raised, status", [
    (lambda: KeyError("no node named 'ghost'; known nodes: hpc"), 400),
    ("unavailable", 503),
    ("resource", 400),
    (lambda: RuntimeError("connection reset"), 502),
])
def test_a_relayed_listing_tells_the_failures_apart(client, monkeypatch,
                                                    raised, status):
    """"That folder does not exist" and "the node is not answering" are
    different sentences to read and different things to do next. Collapsing
    both into 502 put a gateway error in the picker's error bar for an ordinary
    typo, which is not something a user can act on."""
    from plexora import nodes as node_api
    from plexora.server.providers.base import ResourceError, ResourceUnavailable

    makers = {"unavailable": lambda: ResourceUnavailable("hpc is not answering"),
              "resource": lambda: ResourceError("Not a folder: /scartch")}
    make = makers.get(raised, raised)

    def _fake(node, path="", show_hidden=False):
        raise make()

    monkeypatch.setattr(node_api, "list_dir_on_node", _fake)

    answer = client.post("/list_dir", json={"node": "hpc", "path": "/x"})

    assert answer.status_code == status
    assert answer.get_json()["error"]
    # Never the quotes Python puts round a KeyError's argument.
    assert not answer.get_json()["error"].startswith("'")


# -- where the picker was standing last time --------------------------------


def test_a_machine_nobody_has_browsed_yet_remembers_nothing(client):
    answer = client.get("/picker_prefs").get_json()

    assert answer == {"last_dir": "", "recent": [], "pinned": []}


def test_choosing_a_file_records_the_folder_it_was_in(client):
    """One write per pick, carrying both facts -- not one per step through the
    tree. This is a file on disk."""
    client.post("/picker_prefs", json={"last_dir": "/n/scratch/aj",
                                       "add_recent": "/n/scratch/aj"})

    answer = client.get("/picker_prefs").get_json()

    assert answer["last_dir"] == "/n/scratch/aj"
    assert answer["recent"] == ["/n/scratch/aj"]


def test_recent_folders_are_newest_first_and_each_only_once(client):
    """Choosing three files out of the same folder is one place, not three
    lines of the same name."""
    from plexora.server.routes.browse_routes import RECENT_LIMIT

    for index in range(RECENT_LIMIT + 3):
        client.post("/picker_prefs", json={"add_recent": f"/data/run{index}"})
    client.post("/picker_prefs", json={"add_recent": "/data/run2"})

    recent = client.get("/picker_prefs").get_json()["recent"]

    assert len(recent) == RECENT_LIMIT
    assert recent[0] == "/data/run2"
    assert len(set(recent)) == len(recent)
    # The oldest fell off the end rather than the newest being refused.
    assert "/data/run0" not in recent


def test_a_pinned_folder_survives_and_can_be_let_go(client):
    client.post("/picker_prefs", json={"pin": "/n/groups/lab/2024-03-scans"})
    client.post("/picker_prefs", json={"pin": "/n/groups/lab/2024-03-scans"})

    assert client.get("/picker_prefs").get_json()["pinned"] == [
        "/n/groups/lab/2024-03-scans"]

    client.post("/picker_prefs", json={"unpin": "/n/groups/lab/2024-03-scans"})
    # And un-pinning something that was never pinned is not an error.
    client.post("/picker_prefs", json={"unpin": "/somewhere/else"})

    assert client.get("/picker_prefs").get_json()["pinned"] == []


def test_each_machine_is_remembered_separately(client):
    """The point of keying this by node: /n/scratch/aj means nothing on the
    laptop, and the same user browses both in one session."""
    client.post("/picker_prefs", json={"last_dir": "/Users/aj/study"})
    client.post("/picker_prefs", json={"node": "hpc", "last_dir": "/n/scratch/aj"})

    assert client.get("/picker_prefs").get_json()["last_dir"] == "/Users/aj/study"
    assert client.get("/picker_prefs?node=hpc").get_json()["last_dir"] == "/n/scratch/aj"
    assert client.get("/picker_prefs?node=laptop").get_json()["last_dir"] == ""


def test_a_path_that_is_not_a_string_is_refused_rather_than_coerced(client):
    """`str(None)` written into the recent list as "None" would sit there being
    clicked forever."""
    answer = client.post("/picker_prefs", json={"add_recent": 7})

    assert answer.status_code == 400
    assert "add_recent" in answer.get_json()["error"]
    assert client.get("/picker_prefs").get_json()["recent"] == []


def test_a_damaged_settings_file_costs_the_recent_list_and_nothing_else(client):
    """`read_settings` promises a dict and nothing about what is in it -- the
    file is user-editable, and a picker that will not open because somebody
    hand-edited their preferences is a much worse failure than an empty
    sidebar."""
    from plexora import paths

    paths.write_settings({"path_picker": {"places": {"": {
        "last_dir": 7, "recent": "not a list", "pinned": ["/ok", 3, None]}}}})

    assert client.get("/picker_prefs").get_json() == {
        "last_dir": "", "recent": [], "pinned": ["/ok"]}


def test_remembering_a_place_leaves_the_rest_of_the_settings_alone(client):
    """It shares settings.json with the data directory, which is the one
    setting a user cannot afford to have quietly rewritten."""
    from plexora import paths

    paths.write_settings({"data_dir": "/somewhere/chosen"})

    client.post("/picker_prefs", json={"pin": "/n/scratch/aj"})

    assert paths.read_settings()["data_dir"] == "/somewhere/chosen"
