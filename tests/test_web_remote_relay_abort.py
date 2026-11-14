"""A relayed request its caller gave up on is cancelled at the node and
forgotten by the page's room (services/webRemote.js `relay`,
services/relayRoom.js `Room.fetch`).

The website's Python times out a node call by aborting its fetch
(plexora/browser/fetch.py `_abortable`); the abort reaches the page as the
signal of the Request handed to PlexoraWebRemote. The probe runs the real
webRemote.js and relayRoom.js against a fake WebSocket, aborts a request the
node already has, and reads the frames the page sent
(tests/js/web_remote_relay_abort_probe.mjs).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_an_aborted_relayed_request_is_cancelled_at_the_node_and_not_sent_again():
    """Aborting the caller's fetch sends the wire's CANCEL for the request's
    stream, ends the page's wait for it, and leaves it out of what the room
    sends again when the node next says hello."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run([node, str(root / "tests" / "js" / "web_remote_relay_abort_probe.mjs")],
                          capture_output=True, text=True, cwd=root, timeout=60)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
