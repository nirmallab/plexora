"""A website tab reloaded while its data node is still starting (a queued job,
an install) waits for the node's hello until the start's own timeout, not
60 s: the node's start command goes on without the page, and a room whose
keys were dropped is one the node joins with nobody listening, holding its
allocation until its idle limit. Once the node has said hello, the record
loses its start deadline and a later reload keeps the short wait.

The node probe (tests/js/web_remote_reattach_starting_probe.mjs) runs the real
services/webRemote.js with the relay room stood in for.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests" / "js" / "web_remote_reattach_starting_probe.mjs"


@pytest.fixture(scope="module")
def probe():
    if not shutil.which("node"):
        pytest.skip("needs node")
    done = subprocess.run(["node", str(PROBE)], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_a_starting_node_is_waited_for_until_its_start_timeout(probe):
    assert probe["startingWait"] >= 28 * 60000
    assert probe["startingKeptAfter60s"] is True


def test_a_connected_node_keeps_the_short_wait(probe):
    assert probe["connectedWait"] == 60000


def test_the_start_deadline_goes_once_the_node_says_hello(probe):
    assert probe["untilAfterHello"] is None
