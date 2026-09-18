"""Writing zarr on a machine with a real-time scanner on it.

Windows will not rename a file over one that anybody has open, and a file that
was created a moment ago is exactly what Defender, a search indexer or a sync
client has open. Zarr writes every key by renaming a temporary file over the
target, so on Windows a burst of writes to one key -- which is what rewriting a
group's metadata is -- meets ERROR_ACCESS_DENIED for a few milliseconds at a
time. Measured on the machine this was found on, two hundred writes of one
small key were refused 130 times, and every refusal cleared on the next try.

It reached us as forty tests failing across OME-Zarr, SpatialData, AnnData and
ROI with `PermissionError: [WinError 5] ... .partial -> zarr.json`, none of
which had anything wrong with them.

These pin the two halves of the fix: Plexora labels a store in one write rather
than four, and zarr's local store waits out a refusal instead of raising it.
"""

import os

import numpy as np
import pytest
import zarr

from plexora._transient_locks import install_zarr_retry, past_transient_locks


# -- the retry itself --------------------------------------------------------


def test_a_blocked_write_is_waited_out_rather_than_raised():
    """The shape of the real thing: refused, refused, then fine."""
    refusals = [PermissionError(13, "Access is denied"),
                PermissionError(13, "Access is denied")]

    def flaky():
        if refusals:
            raise refusals.pop()
        return "written"

    assert past_transient_locks(flaky, attempts=5, delay=0) == "written"
    assert refusals == []


def test_a_lock_that_is_not_transient_is_still_an_error():
    """A file somebody genuinely cannot write is not something to hide behind
    two seconds of waiting and then swallow -- the caller has to hear about
    it."""
    def denied():
        raise PermissionError(13, "Access is denied")

    with pytest.raises(PermissionError):
        past_transient_locks(denied, attempts=3, delay=0)


def test_only_a_permission_error_is_waited_out():
    """`_put(exclusive=True)` says "this key is already there" with
    FileExistsError, and zarr's group creation branches on it. Waiting that out
    would turn a fast answer into two seconds of nothing and then the same
    answer."""
    attempts = []

    def exists():
        attempts.append(1)
        raise FileExistsError(17, "File exists")

    with pytest.raises(FileExistsError):
        past_transient_locks(exists, attempts=5, delay=0)
    assert len(attempts) == 1


# -- the shim on zarr's local store -----------------------------------------


@pytest.mark.skipif(os.name != "nt",
                    reason="only Windows refuses a rename onto an open file")
def test_zarrs_local_store_is_actually_wrapped():
    """The shim is deliberately quiet -- a zarr that has been rearranged
    underneath us must not turn into an ImportError at startup. That silence is
    only safe if something notices when it stops taking effect, which is this.
    """
    from zarr.storage import _local

    assert getattr(_local._put, "_plexora_retry", False), (
        "zarr.storage._local._put is not the wrapped one: either plexora was "
        "never imported, or zarr moved the function the shim patches")


def test_installing_the_shim_twice_does_not_stack_wrappers():
    """`plexora/__init__` installs it at import. Anything that imports plexora
    twice, or a test that calls install() to be sure, must not end up with the
    retry budget squared."""
    from zarr.storage import _local

    before = getattr(_local, "_put", None)
    install_zarr_retry()

    assert getattr(_local, "_put", None) is before


@pytest.mark.skipif(os.name != "nt",
                    reason="only Windows refuses a rename onto an open file")
def test_writing_one_key_over_and_over_is_never_refused(tmp_path):
    """Straight at the patched function, because that is where the refusal
    lands and the odds of meeting it are what make this reproducible: with the
    retry taken back off, 130 of these 200 writes were denied on the machine
    this was found on, and every one of them cleared on the next attempt.

    Rewriting one key in a burst is not a contrived thing to do -- it is what
    zarr does to `zarr.json` for every attribute a writer assigns.

    On a machine with nothing scanning it, this passes without the retry ever
    being needed. It can go quiet; it cannot go wrong.
    """
    from zarr.core.buffer import cpu
    from zarr.storage import _local

    target = tmp_path / "zarr.json"
    for index in range(200):
        _local._put(target, cpu.Buffer.from_bytes(b'{"written": %d}' % index))

    assert target.read_bytes() == b'{"written": 199}'


def test_a_store_written_to_and_then_labelled_keeps_both(tmp_path):
    """Data and metadata through the same store, in the order every OME-Zarr
    writer uses them: the arrays first, the labels last."""
    group = zarr.open_group(str(tmp_path / "labelled.zarr"), mode="w")
    array = group.create_array("0", shape=(1, 64, 64), dtype="uint16",
                               chunks=(1, 32, 32))
    array[0, 0:32, :] = np.full((32, 64), 7, dtype="uint16")
    group.update_attributes({"plexora_extension": True, "base_levels": 1,
                             "source": "somewhere", "source_key": "abc"})

    reopened = zarr.open_group(str(tmp_path / "labelled.zarr"), mode="r")
    assert dict(reopened.attrs)["base_levels"] == 1
    assert int(np.asarray(reopened["0"][0, 0, 0])) == 7


# -- and the one call site that was making it worse --------------------------


def test_labelling_a_store_is_one_write_not_one_per_key(tmp_path, monkeypatch):
    """`build_extension` used `attrs.update`, which is MutableMapping's and
    assigns a key at a time. Four keys meant four full rewrites of zarr.json --
    the burst above, provoked by us. `Group.update_attributes` is one."""
    from zarr.storage import _local

    original = _local._put
    writes = []

    def counted(path, value, exclusive=False):
        writes.append(str(path))
        return original(path, value, exclusive=exclusive)

    group = zarr.open_group(str(tmp_path / "once.zarr"), mode="w")
    root_metadata = str(tmp_path / "once.zarr" / "zarr.json")

    monkeypatch.setattr(_local, "_put", counted)
    group.update_attributes({"plexora_extension": True, "base_levels": 1,
                             "source": "somewhere", "source_key": "abc"})

    assert writes.count(root_metadata) == 1
