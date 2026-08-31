"""One file's bytes, over the node seam.

`/list_dir` gave a machine with no desktop a way to be browsed; these two
routes give it a way to be read from and written to. That is what lets a
plugin's Upload button open a file on a cluster and its Download button put the
result back there -- neither of which the browser can do itself, because it is
on a third machine with no route to either filesystem.

The properties worth a test are the ones a stub would have let through: bytes
that survive the trip unchanged (a CSV export with a NUL in it is still that
file on the far side), a name that cannot walk out of the folder it was given,
an existing file that is refused rather than quietly replaced, and the whole
surface staying behind `--dynamic` -- because between them these routes are
arbitrary read and write on the node's account.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from tests.node_harness import node_process  # noqa: F401 - fixture


def _payload():
    """Bytes chosen to break anything that treats them as text."""
    return b"CellID,CD3\r\n1,0.5\n\x00\xff\xfe binary tail \x80\n" * 64


def _read(node, path, origin=None):
    """The raw response to a read, since what it carries is not JSON."""
    request = urllib.request.Request(
        node.url("/node/v1/read_file"),
        data=json.dumps({"path": str(path)}).encode("utf-8"), method="POST")
    request.add_header("X-Plexora-Node-Token", node.token)
    request.add_header("Content-Type", "application/json")
    if origin:
        request.add_header("Origin", origin)
    return urllib.request.urlopen(request, timeout=60)


# -- reading ----------------------------------------------------------------


def test_a_file_comes_back_byte_for_byte_with_its_name(tmp_path, node_process):
    """The bytes are forwarded to a browser untouched, so a single re-encode
    anywhere on this path is a corrupted download nobody notices until the file
    is opened somewhere else."""
    source = tmp_path / "cells.csv"
    source.write_bytes(_payload())
    node = node_process(dynamic=True)

    with _read(node, source) as response:
        body = response.read()
        name = response.headers.get("X-Plexora-File-Name")
        length = response.headers.get("Content-Length")

    assert body == _payload()
    assert name == "cells.csv"
    assert int(length) == len(_payload())


def test_reading_a_folder_says_so_rather_than_failing_obscurely(
        tmp_path, node_process):
    """A `.zarr` store IS a directory and IS what somebody meant to pick, so
    the refusal has to name the thing rather than read as a bug."""
    node = node_process(dynamic=True)

    with pytest.raises(urllib.error.HTTPError) as raised:
        node.post("/node/v1/read_file", {"path": str(tmp_path)})

    assert raised.value.code == 409
    assert "folder" in raised.value.read().decode("utf-8")


def test_reading_a_missing_file_is_a_sentence_not_a_stack(tmp_path, node_process):
    node = node_process(dynamic=True)

    with pytest.raises(urllib.error.HTTPError) as raised:
        node.post("/node/v1/read_file", {"path": str(tmp_path / "nope.csv")})

    assert raised.value.code == 409
    assert "No such file" in raised.value.read().decode("utf-8")


# -- writing ----------------------------------------------------------------


def test_a_written_file_lands_on_the_node_with_the_bytes_that_were_sent(
        tmp_path, node_process):
    node = node_process(dynamic=True)
    target = tmp_path / "exports"
    target.mkdir()

    answer = node.post_bytes(
        f"/node/v1/write_file?dir={target}&name=gated.csv", _payload())

    assert answer["success"] is True
    assert answer["bytes"] == len(_payload())
    assert (target / "gated.csv").read_bytes() == _payload()


def test_a_second_save_of_the_same_name_is_refused_until_it_is_asked_twice(
        tmp_path, node_process):
    """The refusal carries `exists`, which is the whole difference between a
    dead end and a Replace? question -- and the browser should not have to
    match on a substring to tell them apart."""
    node = node_process(dynamic=True)
    target = tmp_path / "exports"
    target.mkdir()
    (target / "gated.csv").write_bytes(b"last week")

    with pytest.raises(urllib.error.HTTPError) as raised:
        node.post_bytes(f"/node/v1/write_file?dir={target}&name=gated.csv",
                        _payload())
    assert raised.value.code == 409
    assert json.loads(raised.value.read())["exists"] is True
    assert (target / "gated.csv").read_bytes() == b"last week"

    replaced = node.post_bytes(
        f"/node/v1/write_file?dir={target}&name=gated.csv&overwrite=1",
        _payload())
    assert replaced["success"] is True
    assert (target / "gated.csv").read_bytes() == _payload()


@pytest.mark.parametrize("name", ["../escape.csv", "sub/nested.csv", "", "."])
def test_a_name_that_is_not_a_name_is_refused(tmp_path, node_process, name):
    """The directory was picked in a picker that walked the real filesystem;
    the name came out of a text box. Only one of the two is trusted."""
    node = node_process(dynamic=True)

    with pytest.raises(urllib.error.HTTPError) as raised:
        node.post_bytes(
            f"/node/v1/write_file?dir={tmp_path}&name={name}", _payload())

    assert raised.value.code == 409
    assert not (tmp_path.parent / "escape.csv").exists()


def test_a_failed_write_leaves_no_half_file_under_the_real_name(
        tmp_path, node_process):
    """The temp-then-replace is what keeps a dead transfer from leaving a
    truncated file with the right name for somebody's script to pick up."""
    node = node_process(dynamic=True)

    with pytest.raises(urllib.error.HTTPError):
        node.post_bytes(
            f"/node/v1/write_file?dir={tmp_path / 'missing'}&name=x.csv",
            _payload())

    assert list(tmp_path.iterdir()) == []


# -- the flag ---------------------------------------------------------------


def test_neither_route_exists_on_a_node_without_dynamic(tmp_path, node_process):
    """Between them these are arbitrary read and write on the node's account,
    which is a larger grant than `/browse` and `/list_dir` and behind the same
    flag for a stronger version of the same reason."""
    source = tmp_path / "cells.csv"
    source.write_bytes(_payload())
    node = node_process(f"table:cells={source}")

    for call in (
        lambda: node.post("/node/v1/read_file", {"path": str(source)}),
        lambda: node.post_bytes(
            f"/node/v1/write_file?dir={tmp_path}&name=out.csv", b"x"),
    ):
        with pytest.raises(urllib.error.HTTPError) as raised:
            call()
        assert raised.value.code == 403
        assert "--dynamic" in raised.value.read().decode("utf-8")


def test_the_wrong_token_reads_nothing(tmp_path, node_process):
    source = tmp_path / "cells.csv"
    source.write_bytes(_payload())
    node = node_process(dynamic=True)

    request = urllib.request.Request(
        node.url("/node/v1/read_file"), data=b"{}", method="POST")
    request.add_header("X-Plexora-Node-Token", "not-the-token")
    request.add_header("Content-Type", "application/json")
    with pytest.raises(urllib.error.HTTPError) as raised:
        urllib.request.urlopen(request, timeout=30)

    assert raised.value.code == 403


# -- the node that is older than the route ----------------------------------


def test_a_node_too_old_for_the_route_says_so_rather_than_pasting_a_404_page(
        tmp_path, node_process):
    """The first thing anybody meets when this ships.

    A data node is a Plexora installed on the OTHER machine -- on a cluster,
    whichever one is on the module path there -- so it is routinely older than
    the one asking. `api_version` cannot catch it: that marks incompatible wire
    SHAPES and is deliberately not bumped when an endpoint is added, so a node
    without the route answers with Flask's own 404 page.

    Which used to be pasted, tags and all, into whatever dialog had asked --
    "does not serve that resource: <!doctype html> <title>404 Not Found</title>
    ..." -- naming neither the machine, nor its version, nor the one thing to
    do about it.
    """
    from plexora.server.models.nodes import Node
    from plexora.server.providers import http
    from plexora.server.providers.base import ResourceError

    started = node_process(dynamic=True)
    node = Node(name="HMS-O2", endpoint=started.endpoint, token=started.token,
                plexora_version="0.0.4")

    with pytest.raises(ResourceError) as raised:
        http.request(node, "POST", "/node/v1/read_file_from_the_future",
                     body={"path": str(tmp_path)})

    said = str(raised.value)
    assert "<" not in said, said
    assert "HMS-O2" in said
    assert "/node/v1/read_file_from_the_future" in said
    assert "0.0.4" in said
    assert "pip install --upgrade plexora" in said


def test_a_node_that_means_its_own_404_still_says_what_it_meant(
        tmp_path, node_process):
    """The other 404, and the reason the two are told apart by the body rather
    than by the status. `/resources/<id>` answers its own "no such resource" as
    JSON, and that sentence names the resource -- replacing it with a guess
    about versions would be a worse message, not a better one."""
    from plexora.server.models.nodes import Node
    from plexora.server.providers import http
    from plexora.server.providers.base import ResourceError

    started = node_process(dynamic=True)
    node = Node(name="HMS-O2", endpoint=started.endpoint, token=started.token,
                plexora_version="0.0.4")

    with pytest.raises(ResourceError) as raised:
        http.request(node, "GET", "/node/v1/resources/no-such-thing/status")

    said = str(raised.value)
    assert "no-such-thing" in said, said
    assert "too old" not in said
    # The node's own words, not the JSON document they arrived in.
    assert "success" not in said and "{" not in said


def test_the_file_name_header_is_exposed_to_a_browser(tmp_path, node_process):
    """A browser talking to a node directly can read the body without this and
    would build every File it fetched under the name `download`."""
    source = tmp_path / "cells.csv"
    source.write_bytes(_payload())
    node = node_process(dynamic=True, allow_origins=("http://localhost:8000",))

    with _read(node, source, origin="http://localhost:8000") as response:
        exposed = response.headers.get("Access-Control-Expose-Headers") or ""

    assert "X-Plexora-File-Name" in exposed
