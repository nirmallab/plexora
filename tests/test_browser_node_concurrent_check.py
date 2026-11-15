"""Two checks of one project at once in the website's browser node
(services/browserNode.js `check`), run in Node with its workers stood in for
(tests/js/browser_node_concurrent_check_probe.mjs).

A frame navigation that lands while the previous check of the same project is
still opening its files (or a mask recheck overlapping a fresh check) ran two
checks side by side. Each started its own worker pools, and the one finishing
last replaced the other's entry without closing it: workers that nothing
could reach, and that not even `forget` let go of."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = Path(__file__).parent / "js" / "browser_node_concurrent_check_probe.mjs"
    done = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_both_checks_are_served(probe):
    assert probe["aloneServed"] and probe["bothServed"], probe


def test_two_checks_at_once_keep_one_projects_workers(probe):
    assert probe["perProject"] > 0 and probe["afterForget"] == 0, probe
    assert probe["aliveForA"] == probe["perProject"], probe


def test_forget_lets_every_worker_go(probe):
    assert probe["aliveAfterForget"] == 0, probe
