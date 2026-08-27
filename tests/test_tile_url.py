"""The URL every tile is fetched from, and the routing that decides it.

Two probes, in node, against the shipped client source. `getTileUrl` is called
once per tile per channel per viewport step, and it now has two things to put
in a query string rather than one -- the HD flag, written as a bare "?q=hd",
and a node's token. Getting the join wrong produces a URL with two '?' in it,
which fetches successfully, returns the wrong thing, and looks like nothing at
all went wrong.

`PlexoraRouting` is the other half: it decides whether a tile is fetched from
this server or from the machine holding it, and the property that matters most
is the one about projects that have never heard of a data node -- they must
come out the far side with no probe, no storage and no change.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(probe_name):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, str(REPO_ROOT / "tests" / "js" / probe_name)],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def test_a_tile_url_has_exactly_one_query_string():
    assert "tile URL probe OK" in _run("tile_url_probe.mjs")


def test_routing_costs_an_ordinary_project_nothing():
    assert "resource routing probe OK" in _run("resource_routing_probe.mjs")
