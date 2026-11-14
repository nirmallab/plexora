"""The page's end of a relay room (services/relayRoom.js) puts a node's
multi-frame answer back together in the order the node sent it.

Each frame is decrypted with WebCrypto, which does not promise to finish two
decryptions in the order they were started: Firefox and Node finish a small
end frame before the 1 MiB chunk ahead of it. The probe runs the real
relayRoom.js against a fake WebSocket and holds each decrypt's result until it
lets them finish in a chosen order (tests/js/relay_room_order_probe.mjs).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_a_multi_frame_answer_comes_back_whole_and_in_order_whatever_order_its_frames_decrypt_in():
    """A 1 MiB chunk is not dropped when the end frame behind it decrypts
    first, two full chunks do not swap, and a head that decrypts late does not
    wipe a chunk already in."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run([node, str(root / "tests" / "js" / "relay_room_order_probe.mjs")],
                          capture_output=True, text=True, cwd=root, timeout=60)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
