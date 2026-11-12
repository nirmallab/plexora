"""Where the website's Python blocks rather than suspends (plexora/server/utils/
no_suspend.py): h5py reading a remote .ims calls back into Python from HDF5's C
code, where a JSPI suspension ends Python ("SuspendError: trying to suspend JS
frames", 0.0.52 on a 349 GB .ims over SFTP). Inside `held()` coroutines run on
the boot's SyncLoop and requests are synchronous; outside, nothing changes.
The browser half is checked in the live worker (docs/internal/WEBSITE_TAB_LIMITS.md);
these pin the decisions."""

import asyncio
import sys
import types

import numpy as np
import pytest

from plexora.browser import boot, fetch
from plexora.server.utils import no_suspend, remote_store
from plexora.server.volume.sources import ims

from tests import test_ims_masks as ims_files
from tests.remote_fixtures import cache_root, http_store  # noqa: F401


def test_held_nests_and_ends():
    assert not no_suspend.active()
    with no_suspend.held():
        with no_suspend.held():
            assert no_suspend.active()
        assert no_suspend.active()
    assert not no_suspend.active()


def test_held_ends_on_an_error():
    with pytest.raises(ValueError):
        with no_suspend.held():
            raise ValueError("a read failed")
    assert not no_suspend.active()


# -- the loop ---------------------------------------------------------------------

@pytest.fixture
def pyodide(monkeypatch):
    """A stand-in `pyodide.webloop` whose loop can only wait by suspending."""
    calls = []

    class WebLoop:
        def run_until_complete(self, future):
            calls.append("suspended")
            if asyncio.iscoroutine(future):
                future.close()
            return "suspended"

    webloop = types.ModuleType("pyodide.webloop")
    webloop.WebLoop = WebLoop
    package = types.ModuleType("pyodide")
    package.webloop = webloop
    monkeypatch.setitem(sys.modules, "pyodide", package)
    monkeypatch.setitem(sys.modules, "pyodide.webloop", webloop)
    return WebLoop, calls


async def _answer():
    await asyncio.sleep(0)
    return "blocked"


def test_with_jspi_a_coroutine_suspends_except_inside_held(pyodide):
    WebLoop, calls = pyodide
    assert boot.blocking_loop(sleep=lambda s: None, jspi=True) is True
    assert WebLoop().run_until_complete(_answer()) == "suspended"
    with no_suspend.held():
        assert WebLoop().run_until_complete(_answer()) == "blocked"
    assert calls == ["suspended"]


def test_without_jspi_every_coroutine_blocks(pyodide):
    WebLoop, calls = pyodide
    assert boot.without_jspi(sleep=lambda s: None) is True
    assert WebLoop().run_until_complete(_answer()) == "blocked"
    assert calls == []


def test_run_to_completion_inside_a_task_sets_the_task_aside():
    """The website's `run` (and any coroutine) calls in from a task of
    Pyodide's loop; Python 3.14 runs one task at a time per thread, so the
    SyncLoop's own would never start."""
    async def outer():
        task = asyncio.current_task()
        value = boot.run_to_completion(_answer(), sleep=lambda s: None)
        return value, asyncio.current_task() is task

    assert asyncio.run(outer()) == ("blocked", True)


def test_a_coroutine_nothing_can_finish_is_said_not_spun_on():
    async def forever():
        await asyncio.Event().wait()

    with pytest.raises(RuntimeError, match="only another event loop"):
        boot.run_to_completion(forever(), sleep=lambda s: None)


# -- the request -------------------------------------------------------------------

def test_a_request_inside_held_is_synchronous(monkeypatch):
    ffi = types.ModuleType("pyodide.ffi")
    ffi.can_run_sync = lambda: True
    ffi.run_sync = lambda promise: promise
    package = types.ModuleType("pyodide")
    package.ffi = ffi
    monkeypatch.setitem(sys.modules, "pyodide", package)
    monkeypatch.setitem(sys.modules, "pyodide.ffi", ffi)
    assert isinstance(fetch._fetcher(), fetch.BrowserFetcher)
    with no_suspend.held():
        assert isinstance(fetch._fetcher(), fetch.XhrFetcher)


# -- a stage --------------------------------------------------------------------------

def test_a_stage_is_not_loaded_inside_held_nor_marked_failed():
    from plexora.browser import stages

    loads = []
    finder = stages.StageFinder(loads.append)
    name = "h5py"
    stage = stages.stage_of(name)
    assert stage not in (None, "base")

    def want():
        return True

    original = stages._wanted
    stages._wanted = want
    try:
        with no_suspend.held():
            assert finder.find_spec(name) is None
        assert loads == [] and stage not in finder.failed
    finally:
        stages._wanted = original


# -- a remote .ims --------------------------------------------------------------------

def test_h5py_reads_a_remote_ims_held_and_plexoras_own_reads_are_not(tmp_path, http_store, monkeypatch):  # noqa: F811
    folder = tmp_path / "served"
    folder.mkdir(exist_ok=True)
    rng = np.random.default_rng(4)
    stack = [("ch0", rng.integers(0, 4000, (6, 96, 128)).astype(np.uint16))]
    ims_files.write_ims(folder / "stack.ims", stack, chunks=(2, 64, 64))
    url = http_store().url("stack.ims")

    seen = []
    read, readinto = remote_store.RemoteFile.read, remote_store.RemoteFile.readinto
    pread_many = remote_store.RemoteFile.pread_many

    def watched_read(self, n=-1):
        seen.append(("h5py", no_suspend.active()))
        return read(self, n)

    def watched_readinto(self, buffer):
        seen.append(("h5py", no_suspend.active()))
        return readinto(self, buffer)

    def watched_pread_many(self, spans, **kwargs):
        seen.append(("plexora", no_suspend.active()))
        return pread_many(self, spans, **kwargs)

    monkeypatch.setattr(remote_store.RemoteFile, "read", watched_read)
    monkeypatch.setattr(remote_store.RemoteFile, "readinto", watched_readinto)
    monkeypatch.setattr(remote_store.RemoteFile, "pread_many", watched_pread_many)
    handle, stream = ims._open_file(url)
    try:
        assert isinstance(stream, remote_store.RemoteFile)
        data = handle["DataSet/ResolutionLevel 0/TimePoint 0/Channel 0/Data"]
        assert data.shape[0] >= 6
    finally:
        ims._close_file(handle, stream)
    h5py_reads = [held for who, held in seen if who == "h5py"]
    assert h5py_reads and all(h5py_reads), seen
    # The first block, outside h5py: where the website can still suspend.
    assert seen[0] == ("plexora", False)
    assert stream.closed
