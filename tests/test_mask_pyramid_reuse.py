"""A mask's pyramid built once, and found again on every later open.

A project records which file its mask pyramid was built from and that file's
fingerprint (`segmentationSourceKey`, `<size>-<mtime_ns>`); each open compares
it with the file's fingerprint now, and builds again only when they differ.

Two ways that comparison failed for a file that had not changed:

- On the website every visit mounts the user's files in the worker again, and
  Emscripten's WORKERFS dated a picked folder's files -- and on Firefox every
  pick -- by the time of the mount. So each reload saw a "changed" mask and
  the page built its pyramid again, under a new key, beside the old one.
  client workers/pythonWorker.js `datedFiles` dates each by its own
  `lastModified` (tests/js/workerfs_times_probe.mjs runs it against
  Pyodide's WORKERFS).
- A web page reads a file's time in whole milliseconds, the desktop app in
  nanoseconds: a project the website made and the desktop app opened (they
  share Documents/Plexora/projects) took its mask for a changed one each time,
  and so did the website after the desktop app had written the record. Both
  now agree to the millisecond, as image copies already did
  (`segmentation_pyramid.same_fingerprint`).
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import tifffile

from plexora.server.utils import image_pyramid
from plexora.server.utils import segmentation_pyramid as sp

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "workerfs_times_probe.mjs"
WORKER = REPO_ROOT / "plexora" / "client" / "src" / "js" / "workers" / "pythonWorker.js"

#: A time with nanoseconds past the millisecond, as a desktop disk keeps it.
NS_TIME = 1_762_000_000_123_456_789


def _website_key(path):
    """The fingerprint the website records for `path`: its time in whole
    milliseconds (File.lastModified)."""
    stat = Path(path).stat()
    return f"{stat.st_size}-{stat.st_mtime_ns // 1_000_000 * 1_000_000}"


@pytest.fixture
def built_mask(tmp_path):
    """A flat mask with sub-millisecond time, and its filled label pyramid."""
    labels = np.zeros((256, 320), dtype=np.uint16)
    labels[20:60, 30:90] = 1
    labels[100:180, 150:260] = 2
    source = tmp_path / "cells.tif"
    tifffile.imwrite(source, labels)
    os.utime(source, ns=(NS_TIME, NS_TIME))
    derived = sp.derived_output_path(source, None, mode=sp.MODE_FILLED)
    sp.pyramidize_segmentation_mask(source, derived, overwrite=True, outline=False, tile_size=128)
    return source, derived


# -- the comparison --------------------------------------------------------

def test_one_file_read_by_both_apps_has_one_fingerprint():
    assert sp.same_fingerprint("2048-1762000000123456789", "2048-1762000000123456789")
    # The website's whole milliseconds against the desktop's nanoseconds.
    assert sp.same_fingerprint("2048-1762000000123000000", "2048-1762000000123456789")
    assert sp.same_fingerprint("2048-1762000000123456789", "2048-1762000000123000000")
    # A folder's (an OME-Zarr store's): count, bytes, newest time.
    assert sp.same_fingerprint("12-4096-1762000000123000000", "12-4096-1762000000123999999")


def test_a_changed_file_is_still_a_changed_file():
    assert not sp.same_fingerprint("2048-1762000000123000000", "2049-1762000000123000000")
    assert not sp.same_fingerprint("2048-1762000000123000000", "2048-1762000000125000000")
    assert not sp.same_fingerprint("12-4096-1762000000123000000", "13-4096-1762000000123000000")
    assert not sp.same_fingerprint("12-4096-1762000000123000000", "2048-1762000000123000000")
    # Nothing recorded, or nothing there now.
    assert not sp.same_fingerprint(None, "2048-1762000000123000000")
    assert not sp.same_fingerprint("2048-1762000000123000000", None)


def test_anything_else_is_compared_as_it_is():
    """A remote mask's ETag, a store too large to walk: no time to round."""
    assert sp.same_fingerprint('"9b2cf535f27731c974343645a3985328"', '"9b2cf535f27731c974343645a3985328"')
    assert not sp.same_fingerprint('"9b2cf535f27731c974343645a3985328"', '"9b2cf535f27731c974343645a3985329"')
    assert not sp.same_fingerprint("dir-1762000000123000000", "dir-1762000000123456789")


def test_image_copies_and_masks_agree_on_what_the_same_file_is():
    for recorded, now in (("2048-1762000000123000000", "2048-1762000000123456789"),
                          ("2048-1762000000123000000", "2048-1762000000125000000"),
                          ('"etag"', '"etag"')):
        assert image_pyramid._same_source(recorded, now) == sp.same_fingerprint(recorded, now)


# -- a project the other app recorded ----------------------------------------

def test_a_mask_the_website_recorded_is_current_on_the_desktop(built_mask):
    from plexora.server.models import data_model

    source, derived = built_mask
    entry = {"segmentation": str(derived), "segmentationSource": str(source),
             "segmentationSourceKey": _website_key(source), "segmentationMode": sp.MODE_FILLED,
             "segmentation_status": "ready"}

    assert data_model._segmentation_mapping_is_current(entry)
    _changed, pending = data_model.refresh_segmentation_mapping(entry, "from-the-website")
    assert pending is None
    assert entry["segmentation"] == str(derived)
    assert entry["segmentation_status"] == "ready"


def test_a_desktop_record_is_current_on_the_website(built_mask, tmp_path):
    """The other way round: the record holds nanoseconds, the website reads
    the same file in whole milliseconds. Modelled here by a copy of the mask
    dated to the millisecond, with the desktop's key."""
    from plexora.server.models import data_model

    source, derived = built_mask
    desktop_key = sp.source_fingerprint(source)
    on_the_website = tmp_path / "picked" / "cells.tif"
    on_the_website.parent.mkdir()
    shutil.copy2(source, on_the_website)
    whole_ms = NS_TIME // 1_000_000 * 1_000_000
    os.utime(on_the_website, ns=(whole_ms, whole_ms))
    entry = {"segmentation": str(derived), "segmentationSource": str(on_the_website),
             "segmentationSourceKey": desktop_key, "segmentationMode": sp.MODE_FILLED,
             "segmentation_status": "ready"}

    assert data_model._segmentation_mapping_is_current(entry)


def test_a_mask_changed_since_is_built_again(built_mask):
    from plexora.server.models import data_model

    source, derived = built_mask
    entry = {"segmentation": str(derived), "segmentationSource": str(source),
             "segmentationSourceKey": _website_key(source), "segmentationMode": sp.MODE_FILLED,
             "segmentation_status": "ready"}
    later = NS_TIME + 5_000_000_000
    labels = tifffile.imread(source)
    labels[200:240, 10:50] = 3
    tifffile.imwrite(source, labels)
    os.utime(source, ns=(later, later))

    assert not data_model._segmentation_mapping_is_current(entry)
    _changed, pending = data_model.refresh_segmentation_mapping(entry, "changed")
    assert pending == str(source)
    assert entry["segmentation_status"] == "pending"


# -- the website's worker ----------------------------------------------------

def test_the_worker_dates_a_mounted_file_by_its_own_time():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    done = subprocess.run([node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    assert "workerfs times probe OK" in done.stdout


def test_every_mount_in_the_worker_is_dated():
    """A mount that went round `datedFiles` would date its files by the visit
    again: picks (`mount`), folders (`mountTree`) and a saved file
    (`swapPicked`) all go through it."""
    source = WORKER.read_text(encoding="utf-8")
    mounts = re.findall(r"FS\.mount\(([^,]+),", source)
    assert len(mounts) >= 3
    assert all(first.strip() == "datedFiles(py.FS.filesystems.WORKERFS)" for first in mounts), mounts
