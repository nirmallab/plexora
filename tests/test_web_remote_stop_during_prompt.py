"""Stopping a website connection (or its files sign-in timing out) while the
server asks for a password or Duo answer gives the sign-in up: nothing is sent
to the server as the answer, the stopped connection gets no new question,
and the same server can be connected again at once (no 409).

The node probe (tests/js/web_remote_stop_during_prompt_probe.mjs) runs the
real services/webRemote.js with the SSH library stood in for.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests" / "js" / "web_remote_stop_during_prompt_probe.mjs"


@pytest.fixture(scope="module")
def probe():
    if not shutil.which("node"):
        pytest.skip("needs node")
    done = subprocess.run(["node", str(PROBE)], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_connection_was_asking_when_stopped(probe):
    assert probe["askedBeforeStop"] == "authenticating"
    assert probe["stoppedStatus"] == 200


def test_nothing_is_sent_to_the_server_as_the_answer(probe):
    assert probe["sentToServer"] == []


def test_the_stopped_connection_stays_stopped(probe):
    assert probe["stateAfterStop"] == "exited"
    assert probe["promptAfterStop"] is None


def test_the_server_can_be_connected_again(probe):
    assert probe["reconnectStatus"] == 202
