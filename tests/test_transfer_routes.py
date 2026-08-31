"""The primary as a relay for one file's bytes, in either direction.

`/list_dir` already lets a browser walk a cluster's filesystem from a laptop.
What it cannot do is act on what was found: the path that comes back names a
machine the browser has no route to, no address for and no token. These two
routes close that -- a plugin's Upload button gets the file it named, and its
Save button gets somewhere on the far side to put the result.

The properties worth a test are the seam's, not the filesystem's (that is
`test_file_transfer_node.py`'s job): that bytes survive both hops unchanged,
that "" still means this server's own disk the way it does for `/list_dir`,
that the exists-refusal keeps its flag across the relay rather than arriving as
a generic 502, and that a node which is not answering reads as a 503 rather
than as a bad request.
"""

from __future__ import annotations

import io

import pytest

from tests.node_harness import node_process, register  # noqa: F401 - fixture


PAYLOAD = b"CellID,CD3\r\n1,0.5\n\x00\xff\xfe binary tail \x80\n" * 128


@pytest.fixture
def client():
    from plexora import app

    return app.test_client()


def _upload(**form):
    """The multipart body `/put_file` takes, with the file part last."""
    fields = dict(form)
    fields["file"] = (io.BytesIO(PAYLOAD), fields.pop("filename", "export.csv"))
    return fields


# -- fetching ---------------------------------------------------------------


def test_a_file_on_this_server_comes_back_whole(client, tmp_path):
    """`node: ""` means this machine, the same as it does for /list_dir -- and
    the "server" place the browser is offered carries no node name at all."""
    source = tmp_path / "cells.csv"
    source.write_bytes(PAYLOAD)

    answer = client.post("/fetch_file", json={"node": "", "path": str(source)})

    assert answer.status_code == 200
    assert answer.data == PAYLOAD
    assert answer.headers["X-Plexora-File-Name"] == "cells.csv"


def test_a_file_on_a_node_comes_back_whole(client, tmp_path, node_process):
    """Two hops -- node to primary to browser -- and a single re-encode
    anywhere on the way is a corrupt download nobody notices until the file is
    opened somewhere else."""
    source = tmp_path / "cells.csv"
    source.write_bytes(PAYLOAD)
    register("hpc", node_process(dynamic=True))

    answer = client.post("/fetch_file", json={"node": "hpc", "path": str(source)})

    assert answer.status_code == 200
    assert answer.data == PAYLOAD
    assert answer.headers["X-Plexora-File-Name"] == "cells.csv"


def test_a_missing_file_on_a_node_is_a_bad_request_not_a_gateway_error(
        client, tmp_path, node_process):
    """A typo and an unreachable machine are different sentences to read and
    different things to do next -- the same split `_list_dir_on_node` makes."""
    register("hpc", node_process(dynamic=True))

    answer = client.post("/fetch_file",
                         json={"node": "hpc", "path": str(tmp_path / "nope.csv")})

    assert answer.status_code == 400
    assert "No such file" in answer.get_json()["error"]


def test_an_unknown_node_is_refused_before_any_socket(client, tmp_path):
    answer = client.post("/fetch_file", json={"node": "ghost", "path": "/tmp/x"})

    assert answer.status_code == 400


def test_a_node_that_has_stopped_answering_is_a_503(client, tmp_path, node_process):
    """503 rather than 400, because "come back later" and "you asked for
    something that is not there" are answered differently by the caller."""
    source = tmp_path / "cells.csv"
    source.write_bytes(PAYLOAD)
    node = node_process(dynamic=True)
    register("hpc", node)
    node.stop()

    answer = client.post("/fetch_file", json={"node": "hpc", "path": str(source)})

    assert answer.status_code == 503


# -- putting ----------------------------------------------------------------


def test_a_file_saved_here_lands_where_it_was_asked_to(client, tmp_path):
    answer = client.post("/put_file", data=_upload(
        node="", dir=str(tmp_path), name="gated.csv"),
        content_type="multipart/form-data")

    assert answer.status_code == 200
    assert (tmp_path / "gated.csv").read_bytes() == PAYLOAD


def test_a_file_saved_on_a_node_lands_on_the_node(client, tmp_path, node_process):
    register("hpc", node_process(dynamic=True))
    target = tmp_path / "exports"
    target.mkdir()

    answer = client.post("/put_file", data=_upload(
        node="hpc", dir=str(target), name="gated.csv"),
        content_type="multipart/form-data")

    assert answer.status_code == 200
    assert answer.get_json()["bytes"] == len(PAYLOAD)
    assert (target / "gated.csv").read_bytes() == PAYLOAD


@pytest.mark.parametrize("node", ["", "hpc"])
def test_an_existing_file_keeps_its_exists_flag_across_the_relay(
        client, tmp_path, node_process, node):
    """The flag is the whole difference between a dead end and a Replace?
    question, and it has to survive the hop that turns a node's 409 into
    ours -- otherwise the browser is matching on a message to find it."""
    if node:
        register("hpc", node_process(dynamic=True))
    (tmp_path / "gated.csv").write_bytes(b"last week")

    refused = client.post("/put_file", data=_upload(
        node=node, dir=str(tmp_path), name="gated.csv"),
        content_type="multipart/form-data")

    assert refused.status_code == 409
    assert refused.get_json()["exists"] is True
    assert (tmp_path / "gated.csv").read_bytes() == b"last week"

    replaced = client.post("/put_file", data=_upload(
        node=node, dir=str(tmp_path), name="gated.csv", overwrite="1"),
        content_type="multipart/form-data")

    assert replaced.status_code == 200
    assert (tmp_path / "gated.csv").read_bytes() == PAYLOAD


def test_a_name_that_walks_out_of_the_folder_is_refused_on_either_machine(
        client, tmp_path, node_process):
    register("hpc", node_process(dynamic=True))
    inside = tmp_path / "inside"
    inside.mkdir()

    for node in ("", "hpc"):
        answer = client.post("/put_file", data=_upload(
            node=node, dir=str(inside), name="../escape.csv"),
            content_type="multipart/form-data")
        assert answer.status_code == 409

    assert not (tmp_path / "escape.csv").exists()


def test_a_put_with_no_file_part_says_so(client, tmp_path):
    answer = client.post("/put_file", data={"node": "", "dir": str(tmp_path),
                                            "name": "x.csv"},
                         content_type="multipart/form-data")

    assert answer.status_code == 400
