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


def test_a_desktop_install_never_sees_a_switch_it_cannot_use(probe):
    """The whole control rests on there being a second machine. Without one,
    "Local" and "Remote" name the same computer and the choice is noise."""
    assert "without a client node nothing is rendered at all" in probe, probe
    assert "...and available() says so before anyone tries" in probe, probe


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
    """`client_node` is the one fact the control reads to decide whether to
    render at all, and it comes from the server -- the browser cannot tell a
    node beside the viewer from one on its own machine."""
    from plexora.server.routes.page_routes import template_data

    with plexora.app.test_request_context():
        assert "client_node" in template_data()


def test_the_control_is_loaded_before_the_fields_that_mount_it():
    base = source("templates", "base.html")
    assert "services/dataLocation.js" in base
    assert base.index("services/dataLocation.js") < base.index("views/dataSourceField.js")


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
