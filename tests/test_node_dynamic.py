"""A node that can be told about a file after it has started.

Until now everything a node served was named on its command line, which made
"where does this data live" a decision taken before Plexora was open. That is
the wrong moment: the user picking a file on their own laptop does it minutes
into a session, from a form in a browser talking to a viewer on a cluster.

Two properties are load-bearing here and both are about restraint. Runtime
sharing is opt-in (`--dynamic`), because the token holder gains arbitrary file
reads on the node's account. And the manifest a node keeps holds paths and
nothing else -- no project, no roles, no read spec -- so a node still never
becomes a second place where a project is described.
"""

from __future__ import annotations

import json

import numpy as np
import polars as pl
import pytest
import tifffile

from tests.node_harness import node_process  # noqa: F401 - fixture


TOKEN_HEADER = {"X-Plexora-Node-Token": "x"}


def _quiet(*_args, **_kwargs):
    """A node builder's log, silenced -- these tests read responses, not stdout."""


def _table_file(directory, name="cells.csv"):
    directory.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "CellID": [1, 2, 3],
        "X_centroid": [1.0, 2.0, 3.0],
        "Y_centroid": [1.0, 2.0, 3.0],
        "CD3": [0.5, 1.5, 2.5],
    }).write_csv(directory / name)
    return directory / name


def _servable_mask(directory):
    """A label mask that is already a tiled pyramid, so nothing converts."""
    from plexora.server.utils import segmentation_pyramid

    labels = np.zeros((256, 256), dtype=np.uint32)
    labels[40:60, 40:60] = 1
    labels[100:120, 100:120] = 2
    flat = directory / "mask.tif"
    tifffile.imwrite(flat, labels)
    return segmentation_pyramid.pyramidize_segmentation_mask(
        flat, directory / "mask_pyramid.ome.tif", overwrite=True, outline=False)


def _flat_mask(directory):
    """What a segmentation pipeline actually writes: one untiled plane."""
    labels = np.zeros((512, 512), dtype=np.uint32)
    for index in range(1, 5):
        top = index * 60
        labels[top:top + 30, top:top + 30] = index
    path = directory / "flat_mask.tif"
    tifffile.imwrite(path, labels)
    return path


# -- the flag ---------------------------------------------------------------


def test_a_node_with_nothing_to_serve_starts_when_it_is_dynamic(node_process):
    """The empty node is the whole point of the flag.

    `plexora connect` starts one on the user's laptop before any project is
    open, so at that moment there is nothing to name -- and the old refusal
    ("a data node with nothing to serve would answer every request with 404")
    is exactly right for every other case and exactly wrong for this one.
    """
    node = node_process(dynamic=True)
    hello = node.get("/node/v1/hello")

    assert hello["resources"] == []
    assert hello["api_version"] == 1


def test_a_static_node_refuses_to_be_given_more(tmp_path, node_process):
    """403, naming the flag. A bare 403 sends somebody hunting for a wrong
    token when the answer is a flag they did not pass."""
    import urllib.error

    node = node_process(f"table:cells={_table_file(tmp_path)}")
    with pytest.raises(urllib.error.HTTPError) as raised:
        node.post("/node/v1/resources",
                  {"kind": "table", "id": "more", "path": str(tmp_path)})

    assert raised.value.code == 403
    assert "--dynamic" in raised.value.read().decode("utf-8")


# -- sharing a file at runtime ---------------------------------------------


def test_a_table_shared_at_runtime_is_read_like_any_other(tmp_path, node_process):
    from plexora.server.models.project import DataSpec

    path = _table_file(tmp_path)
    node = node_process(dynamic=True)

    added = node.post("/node/v1/resources",
                      {"kind": "table", "id": "cells", "path": str(path)})
    assert added["success"] is True
    assert added["resource"]["state"] == "ready"

    # And it behaves as though it had been on the command line all along: the
    # inspection, the load and the read are the same code either way.
    document = node.get("/node/v1/table/cells/inspect")
    assert document["data_type"] == "csv"

    spec = DataSpec(type="csv", src=str(path))
    loaded = node.post("/node/v1/table/cells/load",
                       {"spec": spec.to_dict(), "reload": True})
    assert loaded["row_count"] == 3


def test_sharing_the_same_file_twice_hands_back_the_same_resource(
        tmp_path, node_process):
    """A resource id is derived from the file's own path, so a reopened
    project, a retried request and a second browser tab all ask for exactly
    what this node already serves. A refusal there is one the caller can
    neither act on nor tell apart from a real clash."""
    path = _table_file(tmp_path)
    node = node_process(dynamic=True)
    payload = {"kind": "table", "id": "cells", "path": str(path)}

    first = node.post("/node/v1/resources", payload)
    second = node.post("/node/v1/resources", payload)

    assert first["resource"]["id"] == second["resource"]["id"] == "cells"
    assert len(node.get("/node/v1/hello")["resources"]) == 1


def test_a_different_file_under_a_taken_id_is_refused(tmp_path, node_process):
    import urllib.error

    node = node_process(dynamic=True)
    node.post("/node/v1/resources", {
        "kind": "table", "id": "cells", "path": str(_table_file(tmp_path))})

    with pytest.raises(urllib.error.HTTPError) as raised:
        node.post("/node/v1/resources", {
            "kind": "table", "id": "cells",
            "path": str(_table_file(tmp_path / "other"))})
    assert raised.value.code == 409


def test_a_file_that_is_not_there_is_refused_with_the_path(tmp_path, node_process):
    import urllib.error

    node = node_process(dynamic=True)
    with pytest.raises(urllib.error.HTTPError) as raised:
        node.post("/node/v1/resources", {
            "kind": "table", "id": "cells", "path": str(tmp_path / "nope.csv")})

    assert raised.value.code == 409
    assert "nope.csv" in raised.value.read().decode("utf-8")


def test_a_shared_resource_can_be_taken_back(tmp_path, node_process):
    path = _table_file(tmp_path)
    node = node_process(dynamic=True)
    node.post("/node/v1/resources",
              {"kind": "table", "id": "cells", "path": str(path)})

    answer = node.delete("/node/v1/resources/cells")
    assert answer == {"success": True, "removed": True}
    assert node.get("/node/v1/hello")["resources"] == []
    # The node was pointed at somebody's file and never given permission to
    # delete it.
    assert path.exists()


# -- a mask that has to be converted first ---------------------------------


def test_a_mask_shared_at_runtime_converts_and_then_serves(tmp_path, node_process):
    """The share returns immediately and the conversion happens behind it.

    A whole-slide mask takes minutes to pyramidize. Doing that inside the
    request would mean a browser waiting on a socket with nothing to show for
    it -- so the node answers `preparing` and the caller polls.
    """
    import time

    flat = _flat_mask(tmp_path)
    node = node_process(dynamic=True)

    added = node.post("/node/v1/resources",
                      {"kind": "segmentation", "id": "mask", "path": str(flat)})
    assert added["resource"]["state"] in ("preparing", "ready")

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        state = node.get("/node/v1/resources/mask/status")["resource"]
        if state["state"] != "preparing":
            break
        time.sleep(0.2)
    assert state["state"] == "ready", state.get("error")
    # And what it now serves is a label pyramid, reported as such -- the
    # primary records `segmentationMode` from this and draws the wrong picture,
    # with no error, if it is missing.
    assert state["mask_mode"] == "filled"

    tile = node.get("/node/v1/seg/mask/tile/0/0_0", raw=True)
    assert tile[:8] == b"\x89PNG\r\n\x1a\n"


def test_a_tile_is_refused_while_a_mask_is_still_converting(tmp_path):
    """A half-written pyramid serves a picture rather than an error, which is
    the failure this state exists to make impossible."""
    from plexora.server.node import resources as node_resources
    from plexora.server.node.app import create_node_app

    mask = _servable_mask(tmp_path)
    app = create_node_app([f"segmentation:mask={mask}"], token="x", log=_quiet)
    app.config["PLEXORA_NODE_RESOURCES"].get("mask").state = (
        node_resources.PREPARING)

    answer = app.test_client().get("/node/v1/seg/mask/tile/0/0_0",
                                   headers=TOKEN_HEADER)
    assert answer.status_code == 409
    assert "still being prepared" in answer.get_json()["error"]


def test_sharing_a_prepared_mask_again_does_not_restart_the_conversion(tmp_path):
    """Once a mask is servable, re-sharing it must not put it back into
    `preparing` -- the caller would poll for work that is not going to happen,
    and a project reopened in a later session shares every one of its files
    again by construction."""
    import time

    from plexora.server.node.app import create_node_app

    mask = _servable_mask(tmp_path)
    app = create_node_app([], token="x", dynamic=True, log=_quiet)
    client = app.test_client()
    payload = {"kind": "segmentation", "id": "mask", "path": str(mask)}

    client.post("/node/v1/resources", json=payload, headers=TOKEN_HEADER)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        state = client.get("/node/v1/resources/mask/status",
                           headers=TOKEN_HEADER).get_json()["resource"]
        if state["state"] != "preparing":
            break
        time.sleep(0.05)
    assert state["state"] == "ready", state.get("error")

    again = client.post("/node/v1/resources", json=payload, headers=TOKEN_HEADER)
    assert again.get_json()["resource"]["state"] == "ready"


# -- what a node was serving last time -------------------------------------


def test_a_restarted_node_serves_what_it_was_serving(tmp_path, node_process):
    """The whole reopen-a-project promise on the laptop side.

    The session ends, the tunnel dies, the node exits. Next time `plexora
    connect` runs, the node comes back with the same id and finds the same
    files under the same resource ids -- so a project whose binding says
    `node://…/cells_ab12` reads again with nobody pointing at anything.
    """
    path = _table_file(tmp_path)
    manifest = tmp_path / "manifest.json"

    first = node_process(dynamic=True, manifest=manifest)
    first.post("/node/v1/resources",
               {"kind": "table", "id": "cells", "path": str(path)})
    first.stop()

    assert manifest.exists()
    recorded = json.loads(manifest.read_text("utf-8"))["resources"]
    # Paths and ids, and nothing about any project. A node that recorded a read
    # spec would be a second place a project is described.
    assert recorded == [{"kind": "table", "id": "cells", "path": str(path)}]

    second = node_process(dynamic=True, manifest=manifest)
    assert [r["id"] for r in second.get("/node/v1/hello")["resources"]] == ["cells"]


def test_a_manifest_entry_whose_file_is_gone_is_skipped(tmp_path, node_process):
    """A manifest describes what was shared in some earlier session, and by now
    the user may perfectly reasonably have moved a file. Refusing to start over
    one of them would strand every other file -- at the exact moment somebody
    is trying to reopen their work."""
    path = _table_file(tmp_path)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"resources": [
        {"kind": "table", "id": "cells", "path": str(path)},
        {"kind": "table", "id": "ghost", "path": str(tmp_path / "nope.csv")},
    ]}), encoding="utf-8")

    node = node_process(dynamic=True, manifest=manifest)
    assert [r["id"] for r in node.get("/node/v1/hello")["resources"]] == ["cells"]


# -- the viewer relaying a share on the browser's behalf -------------------
#
# The browser cannot POST to the node itself. It has no token for a write, and
# on the layout this exists for the node is on the user's own machine while the
# page came from a cluster -- so the viewer, which knows the address and the
# token, does it.


@pytest.fixture
def client():
    from plexora import app

    return app.test_client()


def _registered_client_node(client, node, name="laptop"):
    """Record a running node as the one on the browser's own machine."""
    answer = client.post("/settings/nodes", json={
        "name": name, "endpoint": node.endpoint, "token": node.token,
        "role": "client"})
    assert answer.status_code == 200, answer.get_json()
    return answer.get_json()["node"]


def test_the_viewer_relays_a_share_and_hands_back_a_locator(
        client, tmp_path, node_process):
    path = _table_file(tmp_path)
    node = node_process(dynamic=True)
    _registered_client_node(client, node)

    answer = client.post("/nodes/laptop/resources",
                         json={"kind": "table", "path": str(path)})
    assert answer.status_code == 200, answer.get_json()
    resource = answer.get_json()["resource"]

    # What the form field then holds. The same vocabulary the import form
    # already takes, so nothing downstream learns a new shape.
    assert resource["locator"] == f"node://laptop/{resource['id']}"
    assert resource["state"] == "ready"
    # And the node really is serving it.
    assert [r["id"] for r in node.get("/node/v1/hello")["resources"]] == [
        resource["id"]]


def test_a_resource_id_is_the_same_next_session(tmp_path):
    """Nothing is exchanged between sessions to reconcile ids.

    A project records one in its binding; a node records it in its manifest.
    They meet again only because both were computed from the same path -- which
    is the whole of "reopen the project and it just works".
    """
    from plexora.nodes import resource_id_for

    path = tmp_path / "study" / "cells.h5ad"
    assert resource_id_for(path) == resource_id_for(str(path))
    # Readable, so a message about the resource says something to a person...
    assert resource_id_for(path).startswith("cells-")
    # ...and distinct per directory, so sharing two files of the same name does
    # not silently serve one of them twice.
    assert resource_id_for(path) != resource_id_for(tmp_path / "other" / "cells.h5ad")


def test_a_health_probe_without_the_token_is_refused_rather_than_ignored(
        tmp_path, node_process):
    """Every route on a node is guarded, `/health` included -- and that makes
    an unauthenticated probe a 403, not a slow answer.

    This is what `connect._wait_for_health` polls while a node starts. Sending
    no token made it wait out its whole deadline against a node that was up and
    serving, then report that the node had never answered. Four minutes, on a
    process that was healthy the entire time.
    """
    import urllib.error
    import urllib.request

    node = node_process(f"table:cells={_table_file(tmp_path)}")

    with pytest.raises(urllib.error.HTTPError) as raised:
        urllib.request.urlopen(node.url("/node/v1/health"), timeout=10)
    assert raised.value.code == 403

    # And with it, the answer the poll is actually waiting for.
    assert node.get("/node/v1/health")["ok"] is True


def test_connect_sends_the_header_this_node_checks(tmp_path, node_process):
    """connect.py keeps its own copy of the header name -- it is
    standalone-loadable and cannot import the node package. Pinned here, where
    both spellings are in the same room."""
    import urllib.request

    from plexora import connect
    from plexora.server.node.api import TOKEN_HEADER

    assert connect.NODE_TOKEN_HEADER == TOKEN_HEADER

    node = node_process(f"table:cells={_table_file(tmp_path)}")
    request = urllib.request.Request(
        node.url("/node/v1/health"),
        headers={connect.NODE_TOKEN_HEADER: node.token})
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 200


def test_a_node_can_be_browsed_by_listing_it(tmp_path, node_process):
    """The only way to browse a cluster. A native dialog needs a desktop and a
    compute node has none, so without a listing "Remote" could offer nothing
    but a box to type a remembered path into -- and the paths on a cluster are
    exactly the ones nobody remembers."""
    _table_file(tmp_path / "study")
    (tmp_path / "study" / "runs").mkdir()
    node = node_process(dynamic=True)

    found = node.post("/node/v1/list_dir", {"path": str(tmp_path / "study")})

    assert found["path"] == str(tmp_path / "study")
    names = [entry["name"] for entry in found["entries"]]
    # Folders first, because a .zarr store IS a folder and the Data field takes
    # one -- burying directories under files would hide half the answers.
    assert names == ["runs", "cells.csv"]
    assert found["entries"][0]["is_dir"] is True
    # Each row carries its own full path, and the trail back to the root. Built
    # on the machine that owns the filesystem because it is the only one that
    # knows how paths are spelled there -- a browser on a Mac listing a Windows
    # node would join `C:\data` and `runs` with a slash.
    assert found["entries"][0]["path"] == str(tmp_path / "study" / "runs")
    assert found["crumbs"][-1]["label"] == "study"


def test_a_node_shows_hidden_files_only_when_asked(tmp_path, node_process):
    _table_file(tmp_path / "study")
    (tmp_path / "study" / ".snakemake").mkdir()
    node = node_process(dynamic=True)

    plain = node.post("/node/v1/list_dir", {"path": str(tmp_path / "study")})
    asked = node.post("/node/v1/list_dir", {"path": str(tmp_path / "study"),
                                            "show_hidden": True})

    assert [e["name"] for e in plain["entries"]] == ["cells.csv"]
    assert [e["name"] for e in asked["entries"]] == [".snakemake", "cells.csv"]


def test_a_static_node_will_not_be_walked(tmp_path, node_process):
    """Listing is behind --dynamic for the same reason sharing is: it lets the
    token holder walk the account's filesystem. A node started with a fixed
    list of files was never offering that."""
    import urllib.error

    node = node_process(f"table:cells={_table_file(tmp_path)}")
    with pytest.raises(urllib.error.HTTPError) as raised:
        node.post("/node/v1/list_dir", {"path": str(tmp_path)})

    assert raised.value.code == 403
    assert "--dynamic" in raised.value.read().decode("utf-8")


def test_the_viewer_relays_a_listing_to_the_machine_that_has_the_files(
        client, tmp_path, node_process):
    """The browser has neither the node's address nor its token, so the walk
    goes through here -- exactly as a share does."""
    _table_file(tmp_path / "study")
    node = node_process(dynamic=True)
    _registered_client_node(client, node, name="hpc")

    answer = client.post("/list_dir", json={"node": "hpc",
                                            "path": str(tmp_path / "study")})

    assert answer.status_code == 200, answer.get_json()
    assert [e["name"] for e in answer.get_json()["entries"]] == ["cells.csv"]
    # Relayed whole. The keys are copied out by name on the way through, so a
    # field the picker learns to draw is silently dropped until it is added to
    # that list -- which is exactly what happened to `crumbs`.
    assert answer.get_json()["crumbs"][-1]["label"] == "study"
    assert answer.get_json()["entries"][0]["path"] == str(
        tmp_path / "study" / "cells.csv")


def test_the_relay_carries_the_hidden_files_question_across(
        client, tmp_path, node_process):
    """The toggle is a question put to whichever machine owns the filesystem.
    Answering it here would mean filtering a listing the far side had already
    truncated, which is not the same answer."""
    _table_file(tmp_path / "study")
    (tmp_path / "study" / ".snakemake").mkdir()
    node = node_process(dynamic=True)
    _registered_client_node(client, node, name="hpc")

    asked = client.post("/list_dir", json={"node": "hpc", "show_hidden": True,
                                           "path": str(tmp_path / "study")})

    assert asked.status_code == 200, asked.get_json()
    assert [e["name"] for e in asked.get_json()["entries"]] == [".snakemake",
                                                               "cells.csv"]


def test_the_relay_names_a_node_it_does_not_know(client):
    answer = client.post("/nodes/ghost/resources",
                         json={"kind": "table", "path": "/tmp/cells.csv"})
    assert answer.status_code == 400
    message = answer.get_json()["error"]
    assert "ghost" in message and "known nodes" in message


def test_the_relay_reports_a_file_that_is_not_there(client, tmp_path, node_process):
    node = node_process(dynamic=True)
    _registered_client_node(client, node)

    answer = client.post("/nodes/laptop/resources",
                         json={"kind": "table", "path": str(tmp_path / "nope.csv")})
    assert answer.status_code == 400
    assert "nope.csv" in answer.get_json()["error"]


def test_pointing_a_field_somewhere_else_takes_the_share_back(
        client, tmp_path, node_process):
    """Or a node accumulates every path a user browsed past on the way to the
    one they meant."""
    node = node_process(dynamic=True)
    _registered_client_node(client, node)
    shared = client.post("/nodes/laptop/resources", json={
        "kind": "table", "path": str(_table_file(tmp_path))}).get_json()["resource"]

    status = client.get(f"/nodes/laptop/resources/{shared['id']}/status")
    assert status.get_json()["resource"]["state"] == "ready"

    removed = client.delete(f"/nodes/laptop/resources/{shared['id']}")
    assert removed.status_code == 200, removed.get_json()
    assert node.get("/node/v1/hello")["resources"] == []


def test_the_page_context_names_the_node_on_the_browser_s_machine(
        client, tmp_path, node_process):
    """What every data form reads to decide whether to offer Local at all."""
    from plexora import app
    from plexora.server.routes.page_routes import template_data

    def named():
        with app.test_request_context():
            return template_data()["client_node"]

    node = node_process(dynamic=True)
    assert named() == "", "nothing registered yet"

    # A node registered by hand is a machine somewhere else, which is precisely
    # what "the browser's machine" does not mean.
    client.post("/settings/nodes", json={
        "name": "hpc", "endpoint": node.endpoint, "token": node.token})
    assert named() == ""

    _registered_client_node(client, node, name="laptop")
    assert named() == "laptop"


def test_a_manifest_does_not_override_the_command_line(tmp_path, node_process):
    """What an operator typed just now beats what a file remembers."""
    wanted = _table_file(tmp_path, "wanted.csv")
    stale = _table_file(tmp_path, "stale.csv")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"resources": [
        {"kind": "table", "id": "cells", "path": str(stale)},
    ]}), encoding="utf-8")

    node = node_process(f"table:cells={wanted}", dynamic=True, manifest=manifest)
    document = node.get("/node/v1/table/cells/inspect")

    assert [r["id"] for r in node.get("/node/v1/hello")["resources"]] == ["cells"]
    assert document["data_type"] == "csv"
    # The manifest is rewritten from what is actually served, so the stale
    # entry does not come back to haunt the next launch.
    node.post("/node/v1/resources",
              {"kind": "table", "id": "second", "path": str(stale)})
    recorded = {e["id"]: e["path"]
                for e in json.loads(manifest.read_text("utf-8"))["resources"]}
    assert recorded["cells"] == str(wanted)
