"""Files on an SSH server, read over SFTP through the chunk cache.

The protocol client against OpenSSH's real `sftp-server`, then the whole path
an image takes: an `sftp://` address registered, its pixels read through the
same cache an `https://` one goes through, and a server that is not connected
saying so at once instead of retrying.
"""

import os
import subprocess
import sys
import time

import numpy as np
import pytest
import tifffile as tf

from plexora import datasource
from plexora.server.models import data_model
from plexora.server.providers.base import RemoteUnreachable, is_remote_locator
from plexora.server.providers.remote import RemoteImageProvider
from plexora.server.utils import remote_store, sftp
from tests.brightfield_fixtures import write_planar_fluorescence
from tests.ngff_fixtures import write_ngff
from tests.remote_fixtures import cache_root  # noqa: F401
from tests.sftp_fixtures import needs_sftp_server, sftp_host, spawn_server  # noqa: F401


# -- addresses ---------------------------------------------------------------


def test_an_sftp_address_is_a_remote_locator():
    assert is_remote_locator("sftp://ajn16@transfer.rc.hms.harvard.edu/n/data1/x.tif")
    assert is_remote_locator("SFTP://host/x.zarr")


def test_the_username_keeps_its_case_and_the_host_does_not():
    url = remote_store.canonical_url("sftp://AJN16@Transfer.RC.HMS.Harvard.EDU//n/data1/x.tif/")
    assert url == "sftp://AJN16@transfer.rc.hms.harvard.edu/n/data1/x.tif"
    assert sftp.target_of(url) == "AJN16@transfer.rc.hms.harvard.edu"


def test_a_path_with_awkward_characters_survives_the_round_trip():
    path = "/n/data1/lab/run 3/50% stack?#1.ome.tif"
    url = sftp.url_for("me@host", path)
    assert " " in url and "?" not in url and "#" not in url
    assert sftp.path_of(remote_store.canonical_url(url)) == path
    assert remote_store.url_name(url) == "50%25 stack%3F%231.ome.tif"


# -- the protocol --------------------------------------------------------------


@needs_sftp_server
def test_stat_list_and_read(tmp_path):
    root = tmp_path.resolve()
    data = os.urandom(700_001)
    (root / "a.bin").write_bytes(data)
    (root / "folder").mkdir()
    (root / ".hidden").write_text("x")
    os.symlink(root / "folder", root / "linked")
    process = spawn_server(root)
    client = sftp.SFTPClient(process, label="local")
    try:
        client.handshake(timeout=10)
        assert client.version == 3
        assert client.realpath(".") == str(root)
        assert client.stat(str(root / "a.bin")).size == 700_001
        found = {name: attrs.is_dir for name, attrs in client.listdir(str(root))}
        # A link to a folder is a folder: on a cluster, group and scratch
        # spaces very often are links, and they must be enterable.
        assert found == {"a.bin": False, "folder": True, "linked": True,
                         ".hidden": False}
        assert client.read_range(str(root / "a.bin"), 5, 600_000) == data[5:600_000]
        assert client.read_all(str(root / "a.bin")) == data
        assert client.read_range(str(root / "a.bin"), 700_000, 900_000) == data[700_000:]
        assert client.read_range(str(root / "a.bin"), 800_000, 900_000) == b""
        with pytest.raises(FileNotFoundError):
            client.stat(str(root / "missing"))
    finally:
        client.close()
        process.wait(timeout=5)


@needs_sftp_server
def test_short_reads_are_followed_up_not_taken_for_the_end(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    data = os.urandom(50_000)
    (root / "a.bin").write_bytes(data)
    process = spawn_server(root)
    client = sftp.SFTPClient(process, label="local")
    try:
        client.handshake(timeout=10)
        # Ask for more per request than a server will hand back in one reply,
        # by lying about what it allows: every reply is then short.
        client.max_read = 1000
        assert client.read_range(str(root / "a.bin"), 0, 50_000) == data
    finally:
        client.close()
        process.wait(timeout=5)


def _fake_process(code):
    return subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def test_text_where_the_reply_should_be_names_the_shell_startup_file():
    process = _fake_process(
        "import sys; sys.stdout.write('Welcome to the transfer cluster\\n');"
        "sys.stdout.flush(); sys.stdin.read()")
    client = sftp.SFTPClient(process, label="host")
    try:
        with pytest.raises(sftp.SFTPError) as caught:
            client.handshake(timeout=10)
        assert caught.value.diagnosed
        assert "Welcome to the transfer cluster" in str(caught.value)
        assert ".bashrc" in str(caught.value)
    finally:
        client.close()
        process.kill()


def test_a_login_that_fails_quotes_what_ssh_said():
    process = _fake_process("import sys; sys.exit(255)")
    client = sftp.SFTPClient(
        process, label="host",
        describe_exit=lambda: ["", "Permission denied (publickey,keyboard-interactive)."])
    with pytest.raises(ConnectionError) as caught:
        client.handshake(timeout=10)
    assert "Permission denied (publickey,keyboard-interactive)." in str(caught.value)
    client.close()


@needs_sftp_server
def test_a_dropped_connection_fails_waiting_calls_instead_of_hanging(tmp_path):
    process = spawn_server(tmp_path)
    client = sftp.SFTPClient(process, label="local", call_timeout=10)
    client.handshake(timeout=10)
    process.kill()
    process.wait(timeout=5)
    started = time.monotonic()
    with pytest.raises(ConnectionError):
        client.stat(str(tmp_path))
    assert time.monotonic() - started < 5
    assert not client.alive
    client.close()


# -- through the chunk cache ---------------------------------------------------


def test_an_unconnected_server_says_so_at_once(cache_root):  # noqa: F811
    url = "sftp://nobody@nowhere.example/n/data1/slide.ome.zarr"
    store, _ = remote_store.open_store(url)
    started = time.monotonic()
    with pytest.raises(RemoteUnreachable) as caught:
        store.read("zarr.json")
    assert time.monotonic() - started < 1
    assert "Not connected to nobody@nowhere.example" in str(caught.value)
    result = remote_store.probe(url, fresh=True)
    assert result.status == "offline" and "Not connected" in result.detail


def test_an_ome_zarr_store_registers_and_serves_its_pixels(sftp_host):
    host = sftp_host()
    write_ngff(host.root / "slide.ome.zarr", shape=(2, 300, 300), levels=2,
               labels=["DNA", "CD3"], version="0.5")
    url = host.url("slide.ome.zarr")

    entry = datasource.register_image_datasource(name="sftpzarr", image=url)

    assert entry["channelFile"] == url
    assert entry["image_kind"] == "ome_zarr"
    assert [c["name"] for c in entry["imageData"]] == ["DNA", "CD3"]
    data_model.load_datasource("sftpzarr", reload=True)
    assert isinstance(data_model._providers.image, RemoteImageProvider)
    key = entry["imageData"][1]["src"].rstrip("/").rsplit("/", 1)[-1]
    encoded, mimetype = data_model.encode_tile("sftpzarr", key, 1, "0_0.png", "default")
    assert encoded and mimetype == "image/webp"


def test_a_tiff_registers_and_reads_the_local_pixels(sftp_host):
    host = sftp_host()
    local = host.root / "panel.ome.tif"
    write_planar_fluorescence(local)
    url = host.url("panel.ome.tif")

    entry = datasource.register_image_datasource(name="sftptiff", image=url)

    assert entry["image_kind"] == "ome_tiff"
    assert [c["name"] for c in entry["imageData"]] == ["DAPI", "CD3", "Ki67"]
    data_model.load_datasource("sftptiff", reload=True)
    with tf.TiffFile(str(local)) as tiff:
        expected = tiff.series[0].asarray()
    pyramid = data_model.channels
    assert np.array_equal(np.asarray(pyramid[0][1, 0:200, 50:250]),
                          expected[1, 0:200, 50:250])


def test_cached_tiles_still_draw_after_a_disconnect(sftp_host):
    host = sftp_host()
    local = host.root / "panel.ome.tif"
    write_planar_fluorescence(local)
    url = host.url("panel.ome.tif")
    size = local.stat().st_size
    assert size > 256 * 1024
    first = remote_store.open_file(url).pread(0, 4096)
    assert first == local.read_bytes()[:4096]
    host.disconnect()
    again = remote_store.open_file(url)
    assert again.pread(0, 4096) == first
    with pytest.raises(RemoteUnreachable) as caught:
        again.pread(size - 10, 10)
    assert "Not connected" in str(caught.value)


def test_a_mask_picked_beside_its_image_becomes_the_segmentation(sftp_host):
    """An MCMICRO run's mask is `cell.ome.tif`. Picked with its image off a
    files-only server, it is the sample's segmentation, not a second image
    layer -- found by its pixels, as on this computer."""
    import plexora
    from plexora.server.models.project import Project

    host = sftp_host()
    write_planar_fluorescence(host.root / "slide.ome.tif")
    labels = np.zeros((512, 640), np.uint32)
    labels[40:120, 50:200] = 7
    labels[300:420, 260:480] = 12
    tf.imwrite(host.root / "cell.ome.tif", labels, tile=(256, 256), ome=True)
    image, mask = host.url("slide.ome.tif"), host.url("cell.ome.tif")
    client = plexora.app.test_client()

    proposal = client.post("/import/inspect", json={"paths": [image, mask]}).get_json()
    [sample] = proposal["samples"]
    assert [(layer["src"], layer["role"]) for layer in sample["layers"]] == [
        (image, "image"), (mask, "mask")]

    answer = client.post("/import/sample", json={"paths": [image, mask], "name": "mcmicro"})
    assert answer.status_code == 200, answer.get_data(as_text=True)
    assert Project.load("mcmicro").segmentation.source == mask


# -- browsing --------------------------------------------------------------------


def test_listing_hands_back_whole_addresses(sftp_host):
    host = sftp_host()
    (host.root / "images").mkdir()
    (host.root / "images" / "b.ome.tif").write_bytes(b"x" * 10)
    (host.root / "a.txt").write_text("hello")
    (host.root / ".dot").write_text("")

    home = sftp.listing(host.base)
    assert home["path"] == host.url()
    assert [e["name"] for e in home["entries"]] == ["images", "a.txt"]
    images = home["entries"][0]
    assert images["is_dir"] and images["path"] == host.url("images")
    assert home["entries"][1]["size"] == 5
    assert home["crumbs"][0] == {"label": "/", "path": f"{host.base}/"}
    assert home["crumbs"][-1]["path"] == host.url()

    inside = sftp.listing(host.base, images["path"])
    assert inside["parent"] == host.url()
    assert inside["entries"][0]["path"] == host.url("images/b.ome.tif")
    # A file names its folder; a relative path starts at home.
    assert sftp.listing(host.base, host.url("images/b.ome.tif"))["path"] == host.url("images")
    assert sftp.listing(host.base, "images")["path"] == host.url("images")
    assert ".dot" in [e["name"] for e in
                      sftp.listing(host.base, show_hidden=True)["entries"]]


def test_listing_an_unconnected_server_raises_not_connected():
    with pytest.raises(sftp.SFTPNotConnected):
        sftp.listing("sftp://nobody@nowhere.example")
