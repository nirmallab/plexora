"""The bin layer's client-side arithmetic, actually run.

Everything the browser decides about a Visium HD layer is a tile query
string and a placement, and each way of getting one wrong draws a plausible
picture rather than an error: a missing `color=` is a grey plane, `bin=` in
microns pools four times too coarse, an undivided transform puts the slide at
four times its size. The probe (tests/js/visium_hd_layer_probe.mjs) says
which checks and why; this runs it, and `node --check`s the three scripts,
which the Python suite otherwise never loads.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "visium_hd_layer_probe.mjs"
STATIC = REPO_ROOT / "plexora" / "plugins" / "visium_hd" / "static"
SCRIPTS = ("visiumHdApi.js", "binLayer.js", "spotLayer.js",
           "visiumHdSidebarController.js")


def _node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return node


def test_the_client_arithmetic_holds():
    proc = subprocess.run([_node(), str(PROBE)], capture_output=True, text=True,
                          cwd=REPO_ROOT, timeout=60)

    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize("name", SCRIPTS)
def test_the_script_parses(name):
    proc = subprocess.run([_node(), "--check", str(STATIC / name)],
                          capture_output=True, text=True, timeout=60)

    assert proc.returncode == 0, proc.stderr


def test_the_descriptor_ships_every_script_it_names():
    from plexora.plugins.visium_hd import PLUGIN

    for name in (*PLUGIN.scripts, *PLUGIN.styles):
        assert (STATIC / name).is_file(), name
