"""The Local / Remote switch on every data field, and what it is wired to.

The behaviour is in tests/js/data_location_probe.mjs, run in node below,
because nothing in the Python suite executes client JS. What is left here is
the wiring -- the facts spread across several files that all have to agree
before any of that behaviour is reachable at all:

  page_routes.py     tells the page whether there is a machine to mean
                     "Local" about
  base.html          loads the control, before the fields that mount it
  browsePicker.js    can send a dialog to a node instead of to this server
  settings_routes.py has the relay the control posts a share through

The server halves are tests/test_node_dynamic.py (sharing and unsharing) and
tests/test_browse_routes.py (the two pickers).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

import plexora
from tests.node_harness import node_process  # noqa: F401 - fixture

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "data_location_probe.mjs"
CLIENT = REPO_ROOT / "plexora" / "client"


def source(*parts):
    return CLIENT.joinpath(*parts).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    proc = subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    return proc.stdout


# -- the control -------------------------------------------------------------


def test_each_field_carries_its_own_switch(probe):
    """The architectural point, and the thing that broke: the choice is per
    data input, not per project and not just for the primary image. A project
    is free to keep its image on a cluster, its mask on another one and its
    table here.

    Mounting one field must not be able to cost another. It did: `attach` called
    the caller's `onChange` before returning the handle that handler reaches
    for, the resulting TypeError escaped the loop that mounts all three, and
    the import form shipped offering the choice for the image alone."""
    assert "every data field gets its own switch, independently" in probe, probe
    assert "...mounted in the row, right beside the path box it governs" in probe, probe


def test_the_switch_is_one_letter_wide_and_still_says_what_it_means(probe):
    """It sits INSIDE the field's row, immediately before the path box, on
    forms that already have four of them -- and "This computer | Remote" spent
    more of that row than the box it governed.

    Shrinking a control is only allowed if nothing is lost with the words. The
    meaning moves to the two places it is actually wanted: the aria-label,
    which is what a screen reader reads INSTEAD of the letter, and a tooltip on
    the group for a mouse. The place chip beside it still names the machine,
    which is the part that varies.
    """
    assert "...and it reads L | R" in probe, probe
    assert "...with what each letter means where a screen reader will read it" in probe, probe
    assert "...and where a mouse will find it" in probe, probe


def test_a_desktop_install_is_asked_the_same_question(probe):
    """The switch is on every launch, because there is always somewhere else a
    file could be -- a saved SSH connection, if nothing nearer.

    What changes is what each half MEANS. On a desktop install "This computer"
    is the server's own filesystem, and the box has to post the path exactly as
    typed; hiding that behind a node would break the ordinary local import,
    silently, by asking a machine that is not there."""
    assert "the switch is offered even where Plexora runs on this machine" in probe, probe
    assert "...and This computer means the server's own filesystem" in probe, probe
    assert "...so the box keeps its own name and posts the path as typed" in probe, probe
    assert "...and nothing is asked of any node" in probe, probe


def test_remote_is_a_question_about_which_machine(probe):
    """The point of the whole change: "Remote" is not a mode decided at launch,
    it is a machine chosen while the form is open -- and the field then talks
    to THAT machine's node rather than to whichever one happened to exist."""
    assert "choosing Remote asks which machine, and takes the answer" in probe, probe
    assert "...and clears a path that described a different filesystem" in probe, probe
    assert "...then shares through THAT machine's node" in probe, probe


def test_the_user_reads_a_path_and_the_form_posts_an_address(probe):
    """Nobody should have to look at `node://laptop/cells-7f3a91c2` to know
    they picked ~/study/cells.h5ad -- and two inputs sharing one name would
    post both values, so the name moves rather than being duplicated."""
    assert "the form posts the address..." in probe, probe
    assert "...while the box still shows the path the user picked" in probe, probe
    assert "...so the form field's name moves to a hidden companion" in probe, probe
    assert "...restores the field's own name, so it posts one value" in probe, probe


def test_a_stored_value_is_not_reinterpreted(probe):
    """A field that arrives with a value carries a stored answer -- a server
    path or a node address. Reading it as a path on the laptop would break a
    project that was working."""
    assert "a field that arrives with a value is left describing the server" in probe, probe
    assert "...with its own name still on it, so it posts what it always did" in probe, probe


def test_nothing_is_submittable_until_the_other_machine_has_the_file(probe):
    assert "a mask still converting is not something to submit" in probe, probe
    assert "...and the field says so rather than sitting silent" in probe, probe
    assert "once it lands the form is free again" in probe, probe
    assert "a path that is not on that machine blocks the form, saying why" in probe, probe


def test_a_share_is_taken_back_when_the_field_moves_on(probe):
    """Or a node accumulates every path somebody browsed past on the way to
    the one they meant."""
    assert "switching to the server takes the share back" in probe, probe
    assert "...and clears a path that described the other machine" in probe, probe


def test_a_csv_can_be_sent_rather_than_named(probe):
    """The one thing a browser can do unaided. Worth having even when a node
    IS attached: a CSV is copied into the project directory on import anyway,
    so an uploaded one outlives the session, which nothing reached through a
    tunnel does."""
    assert "a CSV can be sent from the browser rather than named" in probe, probe
    assert "...and what the form posts is a path on the server, not an address" in probe, probe
    assert "...while the box shows the name the user picked" in probe, probe


def test_a_session_with_no_node_still_gets_an_honest_answer(probe):
    """Started by hand over ssh, or through an Open OnDemand portal: the server
    is still not the user's machine, so the question is still real. What is
    left of the answer is a CSV upload -- and for an image or a mask, a
    sentence naming what would make them possible."""
    assert "a session with no node still asks the question" in probe, probe
    assert "...because a CSV can still be sent" in probe, probe
    assert "...and the path box stops pretending to take a path" in probe, probe
    assert "a mask cannot be sent, so it says what would make it possible" in probe, probe
    assert "...and offers no upload it could not honour" in probe, probe


# -- what the browser is allowed to send -------------------------------------


@pytest.fixture
def client():
    return plexora.app.test_client()


def test_a_csv_lands_somewhere_the_import_can_read(client, plexora_data_root):
    import io

    from plexora.server.routes import import_routes

    answer = client.post("/upload_data_file", data={
        "file": (io.BytesIO(b"CellID,X,Y\n1,2,3\n"), "cells.csv"),
    }, content_type="multipart/form-data").get_json()

    assert answer["ok"] is True
    staged = Path(answer["path"])
    assert staged.read_text(encoding="utf-8").startswith("CellID")
    # Staged under the uploads root, which is what lets the import clean it up
    # afterwards and the sweep clean it up if nothing ever does.
    assert staged.parent.parent == import_routes._uploads_root()


def test_only_a_table_may_come_this_way(client, plexora_data_root):
    """An .h5ad is read where it lies and is routinely tens of gigabytes.
    Uploading one would be moving the very data this design exists to leave
    where it is -- and the refusal has to say what to do instead."""
    import io

    answer = client.post("/upload_data_file", data={
        "file": (io.BytesIO(b"\x89HDF\r\n\x1a\n"), "cells.h5ad"),
    }, content_type="multipart/form-data")

    assert answer.status_code == 400
    error = answer.get_json()["error"]
    assert "read where they lie" in error and "data node" in error


def test_an_imported_upload_does_not_sit_on_the_disk(client, plexora_data_root,
                                                     tmp_path):
    """A CSV is copied into the project directory, so the staged copy has done
    its job the moment that lands."""
    import io

    from plexora.server.routes import import_routes

    staged = Path(client.post("/upload_data_file", data={
        "file": (io.BytesIO(b"CellID,X,Y\n1,2,3\n"), "cells.csv"),
    }, content_type="multipart/form-data").get_json()["path"])

    import_routes._copy_into_project("demo", staged)
    assert not staged.exists()
    assert not staged.parent.exists()


def test_an_abandoned_upload_is_swept(client, plexora_data_root):
    import io
    import os
    import time

    from plexora.server.routes import import_routes

    staged = Path(client.post("/upload_data_file", data={
        "file": (io.BytesIO(b"a\n"), "old.csv"),
    }, content_type="multipart/form-data").get_json()["path"])

    stale = time.time() - import_routes.UPLOAD_KEEP_SECONDS - 60
    os.utime(staged.parent, (stale, stale))
    import_routes._sweep_uploads()

    assert not staged.parent.exists()


# -- the wiring around it ----------------------------------------------------


def test_the_page_says_whether_there_is_a_second_machine():
    """Two facts, both from the server, because the browser can work out
    neither. `client_node` names the node on the user's own machine -- nothing
    in a page can tell that from a node beside the viewer. `server_is_remote`
    says whether this process is on the browser's machine at all, which is what
    decides whether "This computer" is a path or a node."""
    from plexora.server.routes.page_routes import template_data

    with plexora.app.test_request_context():
        data = template_data()
    assert "client_node" in data
    assert data["server_is_remote"] is False


def test_a_notebook_session_knows_the_server_is_not_the_users_machine():
    from plexora.server.routes.page_routes import server_is_remote

    plexora.app.config["PLEXORA_NOTEBOOK_MODE"] = True
    try:
        assert server_is_remote() is True
    finally:
        plexora.app.config["PLEXORA_NOTEBOOK_MODE"] = False


def test_every_machine_a_file_could_be_on_is_listed(client, plexora_data_root):
    """One list behind the Remote option, and a saved connection appears in it
    without having been connected -- the whole point is that connecting is what
    choosing it does."""
    from plexora.server.models import remotes as remote_store

    remote_store.save(remote_store.Remote(name="hpc", target="me@login.edu"))
    answer = client.get("/data_places").get_json()

    hpc = next(place for place in answer["places"] if place["id"] == "hpc")
    assert hpc["detail"] == "me@login.edu"
    # Not connected, and that is a state rather than an error: pressing Connect
    # in the picker is what opens it.
    assert hpc["state"] == "idle" and hpc["node"] is None
    # No "server" entry: Plexora is on this machine, so it is not somewhere
    # else and offering it as a destination would be offering This computer
    # twice under two names.
    assert not any(place["id"] == "server" for place in answer["places"])


def test_a_place_says_what_connecting_to_it_will_cost(client, plexora_data_root):
    """Both of these are invisible from the picker and neither is free: a
    scheduler turns Connect from seconds into a wait in a queue, and a profile
    that already has a viewer connection gets a second, separate one."""
    from plexora.server.models import remote_sessions
    from plexora.server.models import remotes as remote_store

    remote_store.save(remote_store.Remote(name="hpc", target="me@login.edu",
                                          srun="-p interactive"))
    remote_store.save(remote_store.Remote(name="desk", target="me@workstation"))
    places = {p["id"]: p for p in client.get("/data_places").get_json()["places"]}

    assert places["hpc"]["queued"] is True
    # `None` and `""` are different answers here: "no scheduler" is not the
    # same as "the scheduler, with no arguments".
    assert places["desk"]["queued"] is False
    assert places["hpc"]["viewer_state"] is None
    assert remote_sessions.get("hpc") is None


def test_the_control_is_loaded_before_the_fields_that_mount_it():
    base = source("templates", "base.html")
    assert "services/dataLocation.js" in base
    assert base.index("services/dataLocation.js") < base.index("views/dataSourceField.js")
    # And the picker before the control, which calls into it the moment
    # somebody presses Remote.
    assert base.index("services/placePicker.js") < base.index("services/dataLocation.js")


def test_a_browse_button_can_be_sent_to_the_other_machine():
    """Without this the Local option could name a file and never let anyone
    find one: the server's own dialog opens on the server's screen."""
    picker = source("src", "js", "services", "browsePicker.js")
    # The name travels in the request body, so /browse_path knows whose dialog
    # to open...
    assert "node: node" in picker
    # ...and it is resolved at click time, because the switch can be flipped
    # long after the button was wired.
    assert 'typeof node === "function" ? node() : node' in picker


def test_a_browsed_path_is_announced_the_way_a_typed_one_is(probe):
    """Assigning `input.value` from code fires no events at all, so the button
    dispatches them by hand -- and `change` has to be among them. It is the one
    the location switch waits for, because sharing a file with another machine
    is not something to do per keystroke.

    Sending only `input` and `keyup` meant browsing to a file on a cluster
    filled the box and shared nothing. The form then posted an empty locator
    and the import refused it, naming a path that was on screen the whole
    time."""
    assert 'new Event("change"' in source(
        "src", "js", "services", "browsePicker.js")
    assert "a path that arrived from Browse is shared like a typed one" in probe, probe
    assert "...so the form has an address to post, not an empty box" in probe, probe


def test_a_node_with_no_desktop_is_not_a_gateway_failure(client, monkeypatch):
    """Every cluster is one, so this is the ordinary answer rather than a
    breakage -- and `fallback` is what turns it into the listing picker instead
    of a 502 in the console that nobody can act on.

    Driven by rebinding the relay rather than by a real node: a node started
    here runs on a Mac, which HAS a desktop, so the branch under test is
    unreachable from a subprocess -- and reaching for it would pop a file
    dialog on whoever is running the suite.
    """
    from plexora import nodes as node_api
    from plexora.server.providers.base import ResourceError, ResourceUnavailable

    def refuses(*_args, **_kwargs):
        raise ResourceError("this machine has no desktop to open a file dialog on")

    monkeypatch.setattr(node_api, "browse_on_node", refuses)
    answer = client.post("/browse_path", json={"node": "hpc", "mode": "file",
                                               "filter": "image"})

    # The node answered; it said no. That is not a bad gateway.
    assert answer.status_code == 400
    assert answer.get_json()["fallback"] == "list"

    def unreachable(*_args, **_kwargs):
        raise ResourceUnavailable("data node 'hpc' did not answer")

    monkeypatch.setattr(node_api, "browse_on_node", unreachable)
    answer = client.post("/browse_path", json={"node": "hpc", "mode": "file",
                                               "filter": "image"})

    # A node that could not be reached at all IS one, and it is a different
    # problem with a different fix.
    assert answer.status_code == 502
    assert answer.get_json()["fallback"] == "list"


def test_every_surface_that_takes_a_data_file_offers_the_choice():
    """Three surfaces ask "which file?" and all three have to offer it, or the
    answer depends on which page somebody happened to be on."""
    for module in ("services/importFormValidation.js", "views/dataSourceField.js",
                   "views/projectEdit.js", "views/requirementsModal.js"):
        text = source("src", "js", *module.split("/"))
        assert "PlexoraDataLocation" in text, module


def test_the_image_is_offered_the_choice_only_where_it_can_still_move():
    """Where the primary image lives is fixed once the project exists --
    coordinates, ROIs and figures are all in its pixel space. So the switch is
    on the import form and nowhere else."""
    assert "'image_file', 'image'" in source(
        "src", "js", "services", "importFormValidation.js")
    edit = source("src", "js", "views", "projectEdit.js")
    assert "kind: \"segmentation\"" in edit
    assert "kind: \"image\"" not in edit


def test_how_many_machines_there_are_decides_how_the_question_is_asked(probe):
    """A list of one is not a choice, and a list of none is not a list.

    Asking the same way in all three cases is what made Remote a dialog to be
    dismissed before anything could happen: on a laptop with one cluster
    saved, the picker existed to be clicked through, every time, on every
    field. The shortcut has to be undoable, though -- the place chip always
    opens the list, whatever the count.
    """
    assert "with one machine reachable, flipping to Remote just takes it" in probe, probe
    assert "...and the chip says which machine that was" in probe, probe
    assert "...but the chip still opens the list, so it can be changed" in probe, probe


def test_a_field_with_nowhere_to_go_opens_the_connection_dialog(probe):
    """A machine that is SAVED but not connected is not reachable, and
    adopting it would put the field on a filesystem it cannot read a path on
    -- which is the state the switch exists to avoid. What is needed then is
    not a picker but a connection."""
    assert "with nothing connected, Remote opens the connection dialog" in probe, probe
    assert "...and the machine it opened is the one the field adopts" in probe, probe
    assert "...and backing out of it leaves the field on This computer" in probe, probe
