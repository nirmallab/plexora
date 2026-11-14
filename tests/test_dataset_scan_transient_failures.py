"""A scan that could not read a file this time reads it again next time.

The inspection cache is keyed on a file's version -- (path, size, mtime) -- so
a rescan reads only what changed. That is right for an answer about the FILE
("not a TIFF", "needs imagecodecs"). It is wrong for an answer about the
CONNECTION: a node that timed out, a bucket that was throttled or private
until its access was set up in Settings > Web data, a file another program had
open. None of those changes the file's version, so a cached failure would
leave the same samples "unrecognised" on every rescan until the server
restarts. Pinned here: after such a failure, the next scan asks again.
"""

from plexora import nodes as node_api
from plexora.server.models import dataset_scan, import_proposal
from tests import dataset_fixtures as fx

import pytest


@pytest.fixture(autouse=True)
def _forget_scans():
    dataset_scan.forget()
    yield
    dataset_scan.forget()


def _remote_inventory(place, path, root):
    inventory = dataset_scan.Inventory(place=place)
    inventory.add(dataset_scan._new_entry(path, root, kind="raster", size=4096,
                                          mtime_ns=1_700_000_000_000_000_000,
                                          place=place))
    return inventory


def _only(inventory):
    [entry] = inventory.entries.values()
    return entry


def test_a_file_that_raised_while_read_is_read_again_by_the_next_scan(tmp_path,
                                                                     monkeypatch):
    """A header read that raised (here: the file was held open by another
    program) is not the file's answer; the next scan reads it again."""
    root = tmp_path / "cohort"
    fx.image(root / "A.ome.tiff")
    real = dataset_scan._inspect_one
    calls = []

    def flaky(entry, place=None):
        calls.append(entry.name)
        if len(calls) == 1:
            raise PermissionError(32, "The process cannot access the file because "
                                      "it is being used by another process")
        return real(entry, place)

    monkeypatch.setattr(dataset_scan, "_inspect_one", flaky)
    first = _only(dataset_scan.scan([root]))
    assert first.role is None and "being used by another process" in first.reason

    second = _only(dataset_scan.scan([root]))
    assert len(calls) == 2, "the failed read was replayed from the cache"
    assert second.role == "image"


def test_a_node_that_timed_out_is_asked_again_by_the_next_scan(monkeypatch):
    """A data node that did not answer in time says nothing about the file;
    once it answers, the rescan finds the image."""
    calls = []

    def detect(node, path):
        calls.append(path)
        if len(calls) == 1:
            raise TimeoutError("o2 did not answer within 30 s")
        return {"kind": "image", "mask": False,
                "geometry": {"width": 256, "height": 256, "numChannels": 3}}

    monkeypatch.setattr(node_api, "detect_on_node", detect)
    place = ("node", "o2")
    path, root = "/n/data/cohort/S1.ome.tiff", "/n/data/cohort"

    first = _only(dataset_scan.inspect(_remote_inventory(place, path, root)))
    assert first.role is None and "did not answer" in first.reason

    second = _only(dataset_scan.inspect(_remote_inventory(place, path, root)))
    assert len(calls) == 2, "the node's timeout was replayed from the cache"
    assert second.role == "image"


def test_a_bucket_read_before_its_access_was_set_up_is_read_again(tmp_path,
                                                                  monkeypatch):
    """A private bucket is 'inaccessible' until its access is set up in
    Settings > Web data -- which the row itself tells the user to do. The
    rescan after they do must read the file, not repeat the old sentence."""
    local = fx.image(tmp_path / "S1.ome.tiff")
    url = "s3://private-cohort/data/S1.ome.tiff"
    calls = []

    def detect(target, answers, declared=None):
        calls.append(target)
        if len(calls) == 1:
            return [], [], None, [
                "S1.ome.tiff: access denied (403). A private bucket needs its "
                "access set up in Settings > Web data."]
        return import_proposal._detect_file(local, answers)

    monkeypatch.setattr(import_proposal, "_detect_remote", detect)
    place = ("web", "s3://private-cohort")
    root = "s3://private-cohort/data"

    first = _only(dataset_scan.inspect(_remote_inventory(place, url, root)))
    assert first.role is None and "Settings > Web data" in first.reason

    second = _only(dataset_scan.inspect(_remote_inventory(place, url, root)))
    assert len(calls) == 2, "the bucket's refusal was replayed from the cache"
    assert second.role == "image"
