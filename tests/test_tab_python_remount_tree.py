"""A reload of the site in Chrome or Edge keeps the tab's Python (a SharedWorker,
client workers/pythonWorker.js) and every mount the page before made. The new
page's `restore` (services/pickedFiles.js) mounts each picked folder, slide,
DICOM and NDPIS set again through the worker's `mountTree`; a mount over a
mount point still there is Emscripten's EBUSY, and every such pick was left
behind Reconnect. `mountTree` now replaces the mount, as `mount` does.

The node probe (tests/js/tab_python_remount_tree_probe.mjs) runs `mountTree`
against a stand-in FS that refuses as Emscripten's does.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests" / "js" / "tab_python_remount_tree_probe.mjs"


@pytest.fixture(scope="module")
def probe():
    if not shutil.which("node"):
        pytest.skip("needs node")
    done = subprocess.run(["node", str(PROBE)], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_a_folder_mounted_by_the_page_before_mounts_again(probe):
    assert probe["error"] is None, f"the second mount failed: {probe['error']}"
    assert probe["again"] == 2


def test_the_new_page_files_replace_the_old_ones(probe):
    assert probe["blobs"] == ["set/a.tif", "set/b.tif"]
