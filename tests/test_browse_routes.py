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
from plexora.server.routes import browse_routes
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
    assert modes <= {"file", "directory", "any"}


def test_every_path_field_asks_with_one_button_that_takes_either_kind():
    """A .csv is a file and a .zarr store is a directory, and one input takes
    both -- so each field asks once, in mode "any".

    There used to be two buttons per field, File... and Store..., because
    neither single-kind dialog can return the other's kind. That made the user
    classify their own file before they were allowed to point at it, and on the
    image field it was the difference between an OME-TIFF and an OME-Zarr --
    which is a fact about the format, not a decision anybody wants to make.

    The import form those fields lived on is gone; the edit page still has
    them, and the Import Sample dialog answers the same question differently --
    its two halves are pressed by the user, so each passes its OWN kind and
    "any" never arises there (see tests/test_file_explorer.py).
    """
    edit_page = (TEMPLATES / "project_edit.html").read_text(encoding="utf-8")

    modes = _MODE.findall(edit_page)

    # Segmentation Mask and Data -- and nothing single-kind left behind.
    assert modes and set(modes) == {"any"}


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


# -- one dialog that takes either kind --------------------------------------


def test_the_hybrid_mode_reaches_the_picker(client, monkeypatch):
    """What every path field now asks for. Same plumbing check as above: no
    dialog is opened, only that "any" survives the route's guards."""
    seen = {}

    def _fake(mode, file_filter, **kwargs):
        seen.update(mode=mode, file_filter=file_filter)
        return "/picked/study.zarr"

    monkeypatch.setattr(native_dialog, "available", lambda: True)
    monkeypatch.setattr(native_dialog, "hybrid_available", lambda: True)
    monkeypatch.setattr("plexora.server.routes.browse_routes.browse_for_path", _fake)

    response = client.post("/browse_path", json={"mode": "any", "filter": "image"})

    assert response.get_json()["path"] == "/picked/study.zarr"
    assert seen == {"mode": "any", "file_filter": "image"}


def test_a_machine_with_no_hybrid_dialog_is_asked_which_kind(client, monkeypatch):
    """Only macOS has an OS dialog that takes a file OR a folder. A Windows or
    Linux desktop still HAS a native dialog -- two of them, one per kind -- so
    the answer there is to ask which, not to substitute the in-app listing for
    a system file browser the machine was perfectly able to open.

    Which is why this is "kinds" and the no-desktop refusal below is "list":
    answering both the same way is what silently took the native picker away
    from every non-Mac desktop when mode "any" arrived.
    """
    monkeypatch.setattr(native_dialog, "available", lambda: True)
    monkeypatch.setattr(native_dialog, "hybrid_available", lambda: False)

    answer = client.post("/browse_path", json={"mode": "any", "filter": "image"})

    assert answer.status_code == 400
    assert answer.get_json()["fallback"] == "kinds"


@pytest.mark.parametrize("mode", ["file", "directory"])
def test_the_kind_the_menu_picks_opens_a_real_dialog(client, monkeypatch, mode):
    """The other half of that contract: what the split menu re-posts is an
    ordinary single-kind request, which the machine that just refused "any"
    answers with its own native panel. If either of these were refused too, the
    menu would be a dead end rather than a route to the system browser."""
    monkeypatch.setattr(native_dialog, "available", lambda: True)
    monkeypatch.setattr(native_dialog, "hybrid_available", lambda: False)
    monkeypatch.setattr("plexora.server.routes.browse_routes.browse_for_path",
                        lambda mode, file_filter: f"/picked/by-{mode}")

    answer = client.post("/browse_path", json={"mode": mode, "filter": "image"})

    assert answer.status_code == 200
    assert answer.get_json()["path"] == f"/picked/by-{mode}"


def test_the_hybrid_dialog_answers_with_a_path_or_a_cancel(monkeypatch):
    """The JXA script's half of the contract, which is the same one the Tk
    script keeps: the last line of stdout is JSON, and a cancel is a null path
    rather than a non-zero exit. Not run against a real panel -- that blocks
    until a human dismisses it."""
    class _Result:
        returncode = 0
        stderr = ""

        def __init__(self, stdout):
            self.stdout = stdout

    monkeypatch.setattr(native_dialog, "hybrid_available", lambda: True)

    monkeypatch.setattr(native_dialog.subprocess, "run",
                        lambda *a, **k: _Result('{"path": "/x/store.zarr"}\n'))
    assert native_dialog.browse_for_path(mode="any") == "/x/store.zarr"

    monkeypatch.setattr(native_dialog.subprocess, "run",
                        lambda *a, **k: _Result('{"path": null}\n'))
    assert native_dialog.browse_for_path(mode="any") is None


def test_the_hybrid_dialog_refuses_rather_than_opening_a_file_only_one(monkeypatch):
    """A caller that never asked `hybrid_available` must not silently get a
    file-only panel: the user would be left clicking at a .zarr store that
    would not highlight, with nothing saying why."""
    monkeypatch.setattr(native_dialog, "hybrid_available", lambda: False)

    with pytest.raises(RuntimeError):
        native_dialog.browse_for_path(mode="any")


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
    # Bytes, not text. `write_text` translates newlines, so this file is 10
    # bytes on Windows and 8 on Linux -- and the listing was reporting the
    # right number both times while the figure it was compared against was
    # only right on one of them.
    body = b"a,b\n1,2\n"
    (tmp_path / "zebra.csv").write_bytes(body)
    (tmp_path / "store.zarr").mkdir()
    (tmp_path / ".hidden").write_bytes(b"")

    answer = client.post("/list_dir", json={"path": str(tmp_path)}).get_json()

    assert [e["name"] for e in answer["entries"]] == ["store.zarr", "zebra.csv"]
    assert answer["entries"][0]["is_dir"] is True
    assert answer["entries"][1]["size"] == len(body)
    # Names, sizes, dates, kinds -- and the path, which is the picker's whole
    # way of navigating without doing path arithmetic in a browser that has no
    # idea whether the far side joins with "/" or "\". Never content: this is
    # a picker, not a reader.
    assert set(answer["entries"][0]) == {"name", "is_dir", "size", "path",
                                         "mtime", "kind", "store"}
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


def test_the_capability_is_answerable_before_anything_is_clicked(client, monkeypatch):
    """The Browse control is a different control depending on the answer -- one
    button where a dialog takes both kinds, the File/Folder pair where two
    single-kind dialogs are all there is. That decision is made while the form
    is drawn, which is far too early to learn it from a refusal."""
    monkeypatch.setattr(native_dialog, "available", lambda: True)
    monkeypatch.setattr(native_dialog, "hybrid_available", lambda: False)

    assert client.post("/browse_capability", json={}).get_json() == {
        "dialogs": "kinds"}

    monkeypatch.setattr(native_dialog, "hybrid_available", lambda: True)
    assert client.post("/browse_capability", json={}).get_json() == {
        "dialogs": "hybrid"}

    monkeypatch.setattr(native_dialog, "available", lambda: False)
    monkeypatch.setattr(native_dialog, "hybrid_available", lambda: False)
    assert client.post("/browse_capability", json={}).get_json() == {
        "dialogs": "none"}


def test_asking_the_capability_opens_nothing(client, monkeypatch):
    """It is asked on mount, by every Browse control on the form at once. A
    version of this that reached `browse_for_path` would put a file dialog on
    screen for a page that had merely been opened."""
    def _explode(*args, **kwargs):
        raise AssertionError("browse_capability must not open a dialog")

    monkeypatch.setattr("plexora.server.routes.browse_routes.browse_for_path",
                        _explode)

    assert client.post("/browse_capability", json={}).status_code == 200


def test_the_capability_of_a_node_is_the_node_s_own(client, monkeypatch):
    from plexora import nodes as node_api

    monkeypatch.setattr(node_api, "dialogs_on_node", lambda name: "hybrid")
    assert client.post("/browse_capability",
                       json={"node": "laptop"}).get_json() == {"dialogs": "hybrid"}

    # A node too old to say, or not answering just now.
    monkeypatch.setattr(node_api, "dialogs_on_node", lambda name: None)
    assert client.post("/browse_capability",
                       json={"node": "laptop"}).get_json() == {"dialogs": "none"}


def test_a_laptop_node_is_asked_which_kind_rather_than_handed_the_listing(
        client, monkeypatch):
    """The node in this arrangement IS the user's laptop -- the one machine
    that reliably has a screen. Its refusal of mode "any" means "my dialogs do
    one kind at a time", not "I have no desktop", and reading it as the latter
    offered the in-app listing to a machine sitting in front of a person with
    Explorer on it."""
    from plexora import nodes as node_api

    def _refuse(node, mode="file", file_filter="any"):
        raise RuntimeError("this machine's file dialog cannot take a folder")

    monkeypatch.setattr(node_api, "browse_on_node", _refuse)
    monkeypatch.setattr(node_api, "dialogs_on_node",
                        lambda name: native_dialog.KINDS)

    answer = client.post("/browse_path", json={
        "mode": "any", "filter": "image", "node": "laptop"})

    assert answer.status_code == 400
    assert answer.get_json()["fallback"] == "kinds"


@pytest.mark.parametrize("says", [native_dialog.NONE, None])
def test_a_node_with_no_desktop_still_gets_the_listing(client, monkeypatch, says):
    """Both halves of the compatibility story. NONE is a real compute node, and
    None is a node too old to carry the field at all or unreachable in the
    moment -- and a missing answer has to mean what every node meant before the
    field existed, or adding it would have been a breaking change."""
    from plexora import nodes as node_api

    def _refuse(node, mode="file", file_filter="any"):
        raise RuntimeError("this machine has no desktop")

    monkeypatch.setattr(node_api, "browse_on_node", _refuse)
    monkeypatch.setattr(node_api, "dialogs_on_node", lambda name: says)

    answer = client.post("/browse_path", json={
        "mode": "any", "filter": "image", "node": "cluster"})

    assert answer.status_code == 400
    assert answer.get_json()["fallback"] == "list"


def test_only_the_ambiguous_refusal_costs_a_second_question(client, monkeypatch):
    """A refused "file" or "directory" is refused because the machine cannot
    show a dialog at all -- there is nothing to disambiguate, so nothing is
    asked. The probe is an extra round trip and it earns its place only on the
    one refusal that is genuinely two different answers."""
    from plexora import nodes as node_api

    asked = []

    def _refuse(node, mode="file", file_filter="any"):
        raise RuntimeError("this machine has no desktop")

    monkeypatch.setattr(node_api, "browse_on_node", _refuse)
    monkeypatch.setattr(node_api, "dialogs_on_node",
                        lambda name: asked.append(name) or native_dialog.KINDS)

    answer = client.post("/browse_path", json={
        "mode": "file", "filter": "image", "node": "laptop"})

    assert answer.get_json()["fallback"] == "list"
    assert asked == []


def test_a_node_that_cannot_be_reached_is_not_asked_a_second_question(
        client, monkeypatch):
    """502 is the unreachable node, and it stays that way. Probing one that
    just failed to answer would only be a second thing to wait for."""
    from plexora import nodes as node_api
    from plexora.server.providers.base import ResourceUnavailable

    asked = []

    def _gone(node, mode="file", file_filter="any"):
        raise ResourceUnavailable("laptop is not reachable")

    monkeypatch.setattr(node_api, "browse_on_node", _gone)
    monkeypatch.setattr(node_api, "dialogs_on_node",
                        lambda name: asked.append(name) or native_dialog.KINDS)

    answer = client.post("/browse_path", json={
        "mode": "any", "filter": "image", "node": "laptop"})

    assert answer.status_code == 502
    assert answer.get_json()["fallback"] == "list"
    assert asked == []


def test_the_probe_never_turns_a_refusal_into_a_crash(client, monkeypatch):
    """`dialogs_on_node` is asked while already handling a failure. If it threw
    -- an unregistered name, a socket that died mid-handshake -- it would
    replace a refusal the button can act on with a 500 nobody can."""
    from plexora import nodes as node_api

    answer = node_api.dialogs_on_node("no-such-node-anywhere")

    assert answer is None


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

    # `drives` is a fact about the machine (its drive letters, on Windows),
    # not something remembered.
    answer.pop("drives", None)
    assert answer == {"last_dir": "", "recent": [], "pinned": [],
                      "view": browse_routes.DEFAULT_VIEW}


def test_the_drives_are_this_machines_and_only_this_machines(client, monkeypatch):
    """The listing is a Windows machine's Browse for "a file or a folder", and
    the home folder sits on one drive of several. Named, never opened: a
    mapped drive that is not answering costs nothing until somebody clicks it.
    A node's record carries none -- its drives are not this machine's."""
    import os
    import sys

    from plexora.server.routes import browse_routes

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "listdrives", lambda: ["D:\\", "C:\\"], raising=False)
    assert browse_routes._drives() == ["C:\\", "D:\\"]
    assert client.get("/picker_prefs").get_json()["drives"] == ["C:\\", "D:\\"]
    assert client.get("/picker_prefs?node=hpc").get_json()["drives"] == []
    monkeypatch.setattr(sys, "platform", "darwin")
    assert browse_routes._drives() == []


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


def test_pins_sent_at_the_same_moment_are_all_kept():
    """A pin and the explorer closing (or a view change) land together. Each
    request used to read the file, merge, and write it back with no lock and
    one temporary name per process, so the later write threw the pin away --
    or both failed outright -- while the star stayed drawn. On Windows this
    lost nearly every change of eight at once."""
    import threading

    pins = [f"/n/scratch/aj/study{index}" for index in range(8)]
    barrier = threading.Barrier(len(pins))
    statuses = []

    def pin(path):
        with plexora.app.test_client() as each:
            barrier.wait()
            statuses.append(each.post("/picker_prefs",
                                      json={"node": "hpc", "pin": path}).status_code)

    threads = [threading.Thread(target=pin, args=(path,)) for path in pins]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert statuses == [200] * len(pins)
    kept = plexora.app.test_client().get("/picker_prefs?node=hpc").get_json()["pinned"]
    assert sorted(kept) == sorted(pins)


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

    answer = client.get("/picker_prefs").get_json()
    answer.pop("drives", None)
    assert answer == {"last_dir": "", "recent": [], "pinned": ["/ok"],
                      "view": browse_routes.DEFAULT_VIEW}


def test_remembering_a_place_leaves_the_rest_of_the_settings_alone(client):
    """It shares settings.json with the data directory, which is the one
    setting a user cannot afford to have quietly rewritten."""
    from plexora import paths

    paths.write_settings({"data_dir": "/somewhere/chosen"})

    client.post("/picker_prefs", json={"pin": "/n/scratch/aj"})

    assert paths.read_settings()["data_dir"] == "/somewhere/chosen"


# -- the explorer: one listing, compressed, with the new fields ----------------


def test_a_big_listing_is_compressed_for_a_browser_that_takes_it(client, tmp_path):
    """50,000 entries are ~8 MB of JSON and ~1 MB gzipped, and the slow link is
    often the tunnel between this server and the browser."""
    import gzip
    import json

    for index in range(1500):
        (tmp_path / f"sample-{index:05d}-with-a-long-descriptive-name.ome.tif").write_bytes(b"")

    plain = client.post("/list_dir", json={"path": str(tmp_path)})
    squeezed = client.post("/list_dir", json={"path": str(tmp_path)},
                           headers={"Accept-Encoding": "gzip, deflate"})

    assert plain.status_code == 200 and "Content-Encoding" not in plain.headers
    assert squeezed.headers["Content-Encoding"] == "gzip"
    answer = json.loads(gzip.decompress(squeezed.data))
    assert answer == plain.get_json()
    assert answer["total"] == 1500 and answer["partial"] is False
    # Plain JSON either way, with no envelope: the explorer reads it as is.
    assert "success" not in answer


def test_the_listing_says_what_the_open_folder_is(client, tmp_path):
    store = tmp_path / "cells.zarr"
    store.mkdir()
    (store / ".zgroup").write_text("{}")

    answer = client.post("/list_dir", json={"path": str(store)}).get_json()

    assert answer["dataset"] == {"type": "zarr", "label": "Zarr store",
                                 "licensed": True}
    assert answer["platform"] in ("macos", "linux", "windows")


def test_a_relayed_listing_keeps_the_new_fields(client, monkeypatch):
    from plexora import nodes as node_api

    def _fake(node, path="", show_hidden=False):
        return {"path": "/n/scratch", "parent": "/n", "crumbs": [],
                "entries": [], "truncated": False, "total": 0, "partial": False,
                "dataset": None, "platform": "linux"}

    monkeypatch.setattr(node_api, "list_dir_on_node", _fake)

    answer = client.post("/list_dir", json={"node": "hpc", "path": "/n/scratch"})

    assert answer.status_code == 200
    assert answer.get_json()["platform"] == "linux"


# -- the explorer: metadata a partial listing left out ---------------------------


def test_stat_entries_fills_in_what_a_partial_listing_left_out(client, tmp_path):
    (tmp_path / "cells.csv").write_bytes(b"12345")

    answer = client.post("/stat_entries", json={
        "path": str(tmp_path), "names": ["cells.csv", "gone.csv"]})

    assert answer.status_code == 200
    found = answer.get_json()
    assert found["path"] == str(tmp_path)
    assert set(found["entries"]) == {"cells.csv"}
    assert found["entries"]["cells.csv"]["size"] == 5


@pytest.mark.parametrize("body", [
    {"names": "cells.csv"},
    {"names": ["../etc"]},
    {"names": [f"f{index}" for index in range(501)]},
])
def test_stat_entries_refuses_anything_but_a_short_list_of_names(
        client, tmp_path, body):
    answer = client.post("/stat_entries", json={"path": str(tmp_path), **body})
    assert answer.status_code == 400
    assert answer.get_json()["error"]


def test_stat_entries_is_relayed_and_an_old_node_is_unsupported(client, monkeypatch):
    """A node older than the route sent whole listings, so nothing is missing:
    `unsupported`, a 200, and no entries -- never an error bar."""
    from plexora import nodes as node_api

    seen = {}

    def _fake(node, path, names):
        seen.update(node=node, path=path, names=names)
        return {"path": path, "entries": {"a.csv": {"size": 1, "mtime": 2,
                                                    "kind": "file", "is_dir": False}}}

    monkeypatch.setattr(node_api, "stat_entries_on_node", _fake)
    answer = client.post("/stat_entries", json={
        "node": "hpc", "path": "/n/scratch", "names": ["a.csv"]}).get_json()
    assert seen == {"node": "hpc", "path": "/n/scratch", "names": ["a.csv"]}
    assert answer["entries"]["a.csv"]["size"] == 1

    monkeypatch.setattr(node_api, "stat_entries_on_node", lambda *a, **k: None)
    answer = client.post("/stat_entries", json={
        "node": "hpc", "path": "/n/scratch", "names": ["a.csv"]})
    assert answer.status_code == 200
    assert answer.get_json() == {"unsupported": True, "path": "/n/scratch",
                                 "entries": {}}


@pytest.mark.parametrize("route, body", [
    ("/stat_entries", {"names": ["a.csv"]}),
    ("/search_dir", {"query": "a"}),
])
@pytest.mark.parametrize("raised, status", [
    (lambda: KeyError("no node named 'ghost'; known nodes: hpc"), 400),
    ("unavailable", 503),
    ("resource", 400),
    (lambda: RuntimeError("connection reset"), 502),
])
def test_every_explorer_relay_tells_the_failures_apart(
        client, monkeypatch, route, body, raised, status):
    from plexora import nodes as node_api
    from plexora.server.providers.base import ResourceError, ResourceUnavailable

    makers = {"unavailable": lambda: ResourceUnavailable("hpc is not answering"),
              "resource": lambda: ResourceError("Not a folder: /scartch")}
    make = makers.get(raised, raised)

    def _fake(*args, **kwargs):
        raise make()

    monkeypatch.setattr(node_api, "stat_entries_on_node", _fake)
    monkeypatch.setattr(node_api, "search_on_node", _fake)

    answer = client.post(route, json={"node": "hpc", "path": "/x", **body})

    assert answer.status_code == status
    assert not answer.get_json()["error"].startswith("'")


def test_a_files_only_server_answers_the_new_routes_as_unsupported(client):
    base = "sftp://nobody@nowhere.example"

    stats = client.post("/stat_entries", json={"node": base, "names": ["a"]})
    assert stats.status_code == 200 and stats.get_json()["unsupported"] is True

    found = client.post("/search_dir", json={"node": base, "query": "a"})
    assert found.status_code == 200
    assert found.get_json() == {"unsupported": True, "path": "", "query": "a",
                                "matches": [], "visited": 0, "stopped": None}

    places = client.get(f"/fs_places?node={base}")
    assert places.status_code == 200
    assert places.get_json()["unsupported"] is True
    assert places.get_json()["hostname"] == "nobody@nowhere.example"
    assert places.get_json()["cloud"] == []


# -- the explorer: places ---------------------------------------------------------


_PLACE_KEYS = {"platform", "hostname", "home", "known", "volumes", "cloud",
               "project", "last_dir", "recent", "pinned", "view"}


def test_places_for_this_machine_come_with_what_is_remembered(client, monkeypatch):
    from plexora.server.utils import fs_places

    dropbox = {"label": "Dropbox (Personal)", "path": "/Users/jane/Dropbox (Personal)",
               "provider": "dropbox"}
    monkeypatch.setattr(fs_places, "_usage_many", lambda paths: {})
    monkeypatch.setattr(fs_places, "cloud_folders", lambda *args: [dropbox])
    client.post("/picker_prefs", json={"pin": "/data/study"})

    answer = client.get("/fs_places").get_json()

    assert set(answer) == _PLACE_KEYS
    assert answer["cloud"] == [dropbox]
    assert answer["pinned"] == ["/data/study"]
    assert answer["view"] == browse_routes.DEFAULT_VIEW
    assert answer["home"] == str(Path.home())
    assert any(volume["path"] == "/" or volume["path"].endswith("\\")
               for volume in answer["volumes"])


def test_places_relayed_from_a_node_carry_that_machines_record(client, monkeypatch):
    from plexora import nodes as node_api

    monkeypatch.setattr(node_api, "fs_places_on_node", lambda node: {
        "platform": "linux", "hostname": "login01", "home": "/home/aj",
        "known": [], "volumes": [{"label": "scratch", "path": "/n/scratch",
                                  "kind": "network", "fstype": "lustre",
                                  "total": None, "free": None}],
        "cloud": [{"label": "OneDrive", "path": "/home/aj/OneDrive",
                   "provider": "onedrive"}],
        "project": [{"label": "$SCRATCH", "path": "/n/scratch/users/a/aj"}]})
    client.post("/picker_prefs", json={"node": "hpc", "pin": "/n/groups/lab"})

    answer = client.get("/fs_places?node=hpc").get_json()

    assert set(answer) == _PLACE_KEYS
    assert answer["hostname"] == "login01"
    assert answer["volumes"][0]["path"] == "/n/scratch"
    assert answer["cloud"][0]["path"] == "/home/aj/OneDrive"
    assert answer["pinned"] == ["/n/groups/lab"]
    # The laptop's own pins are not the cluster's.
    assert client.get("/fs_places").get_json()["pinned"] == []


@pytest.mark.parametrize("failure", [
    lambda: __import__("plexora.server.providers.base", fromlist=["x"]
                       ).ResourceUnavailable("hpc is not answering"),
    lambda: __import__("plexora.server.providers.base", fromlist=["x"]
                       ).ResourceError("this node was started without --dynamic"),
    lambda: KeyError("no node named 'hpc'"),
])
def test_places_of_a_machine_that_is_down_still_draw_its_pins(
        client, monkeypatch, failure):
    """Always a 200: the pins and recent folders are this server's to give, and
    a sidebar that goes blank because a laptop shut its lid is no sidebar."""
    from plexora import nodes as node_api

    def _down(node):
        raise failure()

    monkeypatch.setattr(node_api, "fs_places_on_node", _down)
    monkeypatch.setitem(node_api._platforms, "hpc", "linux")
    client.post("/picker_prefs", json={"node": "hpc", "pin": "/n/groups/lab"})

    answer = client.get("/fs_places?node=hpc")

    assert answer.status_code == 200
    found = answer.get_json()
    assert found["unavailable"] and not found["unavailable"].startswith("'")
    assert found["pinned"] == ["/n/groups/lab"]
    assert found["hostname"] == "hpc" and found["platform"] == "linux"
    assert found["volumes"] == [] and found["known"] == []
    assert found["cloud"] == []
    assert "unsupported" not in found


def test_places_of_a_node_too_old_to_say_are_unsupported(client, monkeypatch):
    from plexora import nodes as node_api

    monkeypatch.setattr(node_api, "fs_places_on_node", lambda node: None)

    found = client.get("/fs_places?node=old").get_json()

    assert found["unsupported"] is True
    assert found["platform"] == ""
    assert found["home"] == "" and found["volumes"] == []
    assert found["cloud"] == []
    assert "unavailable" not in found


# -- the explorer: search -----------------------------------------------------------


def test_search_finds_names_under_a_folder(client, tmp_path):
    (tmp_path / "study" / "run1").mkdir(parents=True)
    (tmp_path / "study" / "run1" / "LSP11385.ome.tif").write_bytes(b"")
    (tmp_path / "LSP11385-notes.txt").write_text("")

    answer = client.post("/search_dir", json={"path": str(tmp_path),
                                              "query": "lsp11385"})

    assert answer.status_code == 200
    found = answer.get_json()
    assert [match["name"] for match in found["matches"]] == [
        "LSP11385-notes.txt", "LSP11385.ome.tif"]
    assert found["matches"][1]["depth"] == 3
    assert found["stopped"] is None and found["visited"] == 3


def test_search_bounds_are_clamped_not_refused(client, monkeypatch):
    from plexora.server.utils import dir_search

    seen = {}

    def _fake(raw, query, **bounds):
        seen.update(bounds)
        return {"path": raw, "query": query, "matches": [], "visited": 0,
                "stopped": None}

    monkeypatch.setattr(dir_search, "search", _fake)
    client.post("/search_dir", json={"query": "a", "limit": 10_000,
                                     "budget_ms": 60_000, "max_depth": 0})

    assert seen["limit"] == dir_search.SEARCH_LIMIT
    assert seen["budget_ms"] == dir_search.SEARCH_BUDGET_MS
    assert seen["max_depth"] == 1


@pytest.mark.parametrize("body", [
    {"query": ""}, {"query": "   "}, {"query": "x" * 257}, {"query": 7},
    {"query": "a", "limit": "lots"}, {"query": "a", "budget_ms": True},
])
def test_search_refuses_a_query_or_bound_it_cannot_use(client, tmp_path, body):
    answer = client.post("/search_dir", json={"path": str(tmp_path), **body})
    assert answer.status_code == 400
    assert answer.get_json()["error"]


def test_a_third_search_at_once_is_told_to_wait(client, tmp_path, monkeypatch):
    import threading

    from plexora.server.utils import dir_search

    slots = threading.BoundedSemaphore(2)
    slots.acquire()
    slots.acquire()
    monkeypatch.setattr(dir_search, "_slots", slots)

    answer = client.post("/search_dir", json={"path": str(tmp_path), "query": "a"})

    assert answer.status_code == 429
    assert answer.get_json()["error"]


def test_search_is_relayed_clamped_and_an_old_node_is_unsupported(
        client, monkeypatch):
    from plexora import nodes as node_api
    from plexora.server.utils import dir_search

    seen = {}

    def _fake(node, path, query, **bounds):
        seen.update(node=node, path=path, query=query, **bounds)
        return {"path": path, "query": query, "matches": [], "visited": 4,
                "stopped": "budget"}

    monkeypatch.setattr(node_api, "search_on_node", _fake)
    answer = client.post("/search_dir", json={
        "node": "hpc", "path": "/n/scratch", "query": " LSP ",
        "limit": 9999, "show_hidden": True}).get_json()
    assert answer["stopped"] == "budget"
    assert seen == {"node": "hpc", "path": "/n/scratch", "query": "LSP",
                    "show_hidden": True, "limit": dir_search.SEARCH_LIMIT,
                    "budget_ms": dir_search.SEARCH_BUDGET_MS,
                    "max_depth": dir_search.SEARCH_MAX_DEPTH}

    def _busy(*args, **kwargs):
        raise dir_search.SearchBusy("busy over there")

    monkeypatch.setattr(node_api, "search_on_node", _busy)
    assert client.post("/search_dir", json={
        "node": "hpc", "query": "a"}).status_code == 429

    monkeypatch.setattr(node_api, "search_on_node", lambda *a, **k: None)
    answer = client.post("/search_dir", json={"node": "hpc", "path": "/x",
                                              "query": "a"})
    assert answer.status_code == 200
    assert answer.get_json() == {"unsupported": True, "path": "/x", "query": "a",
                                 "matches": [], "visited": 0, "stopped": None}


# -- the explorer: pin order and the view --------------------------------------------


def test_pins_can_be_reordered_without_losing_or_reviving_any(client):
    for path in ("/a", "/b", "/c"):
        client.post("/picker_prefs", json={"pin": path})

    answer = client.post("/picker_prefs", json={
        "pinned_order": ["/c", "/gone", "/a", "/c"]}).get_json()

    # Named and pinned first, in the order given and once each; a pin the order
    # forgot keeps its place after them; a path that is not pinned is ignored.
    assert answer["pinned"] == ["/c", "/a", "/b"]
    assert client.get("/picker_prefs").get_json()["pinned"] == ["/c", "/a", "/b"]


def test_a_pin_order_that_is_not_a_list_of_paths_is_refused(client):
    answer = client.post("/picker_prefs", json={"pinned_order": "/a"})
    assert answer.status_code == 400
    assert "pinned_order" in answer.get_json()["error"]


def test_the_view_is_one_record_for_every_machine(client):
    """A sort order is about the person reading, not the disk being read."""
    answer = client.post("/picker_prefs", json={"node": "hpc", "view": {
        "sort_by": "mtime", "sort_dir": "desc"}}).get_json()

    assert answer["view"]["sort_by"] == "mtime"
    for query in ("", "?node=hpc", "?node=laptop"):
        assert client.get(f"/picker_prefs{query}").get_json()["view"] == answer["view"]
        assert client.get(f"/fs_places{query}").get_json()["view"] == answer["view"]


def test_the_view_is_merged_key_by_key_and_column_by_column(client):
    client.post("/picker_prefs", json={"view": {"layout": "grid",
                                                "columns": {"size": 120}}})
    answer = client.post("/picker_prefs", json={"view": {
        "dirs_first": False, "columns": {"mtime": 200},
        "sidebar": {"collapsed": ["recent", "nonsense", "favorites"]},
        "colour": "teal"}}).get_json()

    view = answer["view"]
    assert view["layout"] == "grid" and view["dirs_first"] is False
    assert view["columns"] == {"mtime": 200, "kind": 120, "size": 120}
    # Known sections only, in the order they are drawn.
    assert view["sidebar"] == {"collapsed": ["favorites", "recent"]}
    # An unknown key is dropped, not stored.
    assert "colour" not in view
    from plexora import paths
    assert "colour" not in paths.read_settings()["path_picker"]["view"]


def test_a_column_width_is_clamped_not_refused(client):
    view = client.post("/picker_prefs", json={"view": {"columns": {
        "mtime": 5, "kind": 99999, "size": 81.6}}}).get_json()["view"]
    assert view["columns"] == {"mtime": 40, "kind": 2000, "size": 82}


@pytest.mark.parametrize("view, key", [
    ("list", "view"),
    ({"sort_by": "colour"}, "view.sort_by"),
    ({"sort_dir": "up"}, "view.sort_dir"),
    ({"layout": "tiles"}, "view.layout"),
    ({"dirs_first": "yes"}, "view.dirs_first"),
    ({"show_hidden": 1}, "view.show_hidden"),
    ({"columns": [100]}, "view.columns"),
    ({"columns": {"size": "wide"}}, "view.columns.size"),
    ({"sidebar": {"collapsed": "recent"}}, "view.sidebar.collapsed"),
])
def test_a_view_of_the_wrong_type_is_refused_naming_the_key(client, view, key):
    answer = client.post("/picker_prefs", json={"view": view})

    assert answer.status_code == 400
    assert answer.get_json()["error"].startswith(key)
    assert client.get("/picker_prefs").get_json()["view"] == browse_routes.DEFAULT_VIEW


def test_a_damaged_stored_view_reads_as_the_default(client):
    from plexora import paths

    paths.write_settings({"path_picker": {"view": {
        "sort_by": "colour", "dirs_first": "yes", "columns": {"size": "wide"},
        "layout": "grid"}}})
    view = client.get("/picker_prefs").get_json()["view"]
    assert view == {**browse_routes.DEFAULT_VIEW, "layout": "grid"}

    paths.write_settings({"path_picker": {"view": "not a view"}})
    assert client.get("/picker_prefs").get_json()["view"] == browse_routes.DEFAULT_VIEW
