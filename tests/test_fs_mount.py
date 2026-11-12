"""A remote .ims given to h5py as a path where a mounter is set (plexora/server/
utils/fs_mount.py): on the website its file-object driver stops at 4 GiB in
32-bit WebAssembly ("attempting I/O in temporary file space" on the 349 GB
Imaris file). The website's mounter is checked in the worker; here a stand-in
copies the remote file to disk, and the pins are: the path is used, the
stream is still what Plexora's own chunk reads use, and closing releases it."""

import numpy as np
import pytest

from plexora.server.utils import fs_mount, remote_store
from plexora.server.volume.sources import ims, open_volume

from tests import test_ims_masks as ims_files
from tests.remote_fixtures import cache_root, http_store  # noqa: F401


@pytest.fixture
def mounter(tmp_path):
    made, released = [], []

    def mount(stream, name):
        path = tmp_path / f"mounted-{len(made)}-{name}"
        path.write_bytes(stream.pread(0, stream.size))
        made.append((str(path), stream))
        return str(path)

    fs_mount.set_mounter(mount, released.append)
    yield made, released
    fs_mount.set_mounter(None, None)


def test_without_a_mounter_there_is_no_path():
    fs_mount.set_mounter(None, None)
    assert fs_mount.path_for(object(), "x.ims") is None


def test_a_remote_ims_opens_by_path_and_reads_the_same(tmp_path, http_store, mounter):  # noqa: F811
    made, released = mounter
    folder = tmp_path / "served"
    folder.mkdir(exist_ok=True)
    rng = np.random.default_rng(5)
    stack = [(f"ch{c}", rng.integers(0, 4000, (6, 96, 128)).astype(np.uint16)) for c in range(2)]
    local_path = ims_files.write_ims(folder / "stack.ims", stack, chunks=(2, 64, 64))
    url = http_store().url("stack.ims")

    remote = open_volume(url)
    local = ims.open_ims_volume(local_path)
    try:
        assert [name for _path, _stream in made for name in [_path]] and made[0][0].endswith("stack.ims")
        assert isinstance(made[0][1], remote_store.RemoteFile)
        rows, cols = slice(5, 90), slice(7, 120)
        np.testing.assert_array_equal(remote.read_block(0, 0, 1, 1, 5, rows, cols),
                                      local.read_block(0, 0, 1, 1, 5, rows, cols))
    finally:
        remote.close()
        local.close()
    assert released == [path for path, _stream in made]


def test_release_is_once(mounter):
    made, released = mounter

    class Stream:
        size = 4

        def pread(self, offset, n):
            return b"abcd"[offset:offset + n]

    stream = Stream()
    path = fs_mount.path_for(stream, "f.ims")
    fs_mount.release(stream)
    fs_mount.release(stream)
    assert released == [path]


def test_a_host_without_a_worker_has_no_mounter(monkeypatch):
    """The bundle's import check boots Python in Node, where `js` has no
    `self`: the boot must go on without a mounter, not fail."""
    import sys
    import types

    from plexora.browser import remote_mount

    class NoSelf:
        def __getattr__(self, name):
            raise AttributeError(name)

    monkeypatch.setitem(sys.modules, "js", NoSelf())
    assert remote_mount.available() is False
    worker = types.SimpleNamespace(plexoraMountReader=lambda *a: None)
    monkeypatch.setitem(sys.modules, "js", types.SimpleNamespace(self=worker))
    assert remote_mount.available() is True
