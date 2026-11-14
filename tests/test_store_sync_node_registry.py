"""The website's node registry stays in the browser that made it (client
workers/storeSync.js, `open(handle, {shared: true})`).

The website's Python keeps its data root, /data, in the workspace's projects
folder on the computer, and writes `nodes.json` there: each data node it
reaches through the relay, with the node's bearer token, at a room only this
browser holds the key to. A node registry is never a shared root's
(plexora/server/models/nodes.py), so the store neither writes it into the
shared folder nor reads one another browser left there; otherwise two browsers
on one workspace take each other's relay nodes off the map. The checks run in
node against the shipped file (tests/js/store_sync_node_registry_probe.mjs).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "store_sync_node_registry_probe.mjs"

#: Every line the probe prints, so a check that is deleted or renamed fails
#: here rather than silently reducing what is covered.
CHECKS = (
    "the node registry and its bearer token are not written into the shared folder",
    "another browser does not read this browser's node registry in",
    "another browser's registry never replaces this browser's: its relay node stays registered",
    "the project registry is still shared",
)


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return subprocess.run([node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60)


def test_a_shared_folder_never_carries_the_node_registry(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"
    assert "store sync node registry probe OK" in probe.stdout


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert line in probe.stdout
