"""Load Gates makes the file's gates the panel's gates: drawn, shown and saved.

The checks are in `tests/js/gating_load_gates_file_probe.mjs`, which builds
the real CSVGatingList and GatingSidebarController, wires the real gating
definition's bindEvents on the real SimpleEventHandler, and loads a gates CSV
from another sample the way a confirmed desktop drop does (uploadGatesFrom,
which the panel's Load Gates button shares through applyGates('file')). With
marker B on screen, the cells drawn afterwards must be gated by B alone at
its loaded gate, the panel's slider must show that gate, and the loaded gates
must be saved with the sample. This drives the probe and names every check,
so one quietly deleted fails here rather than passing silently.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "gating_load_gates_file_probe.mjs"

#: Every line the probe prints.
CHECKS = [
    "after Load Gates the cells are gated by the marker on screen alone",
    "after Load Gates the slider shows the loaded gate of the marker on screen",
    "Load Gates saves the loaded gates with the sample",
]


@pytest.fixture(scope="module")
def probe():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    return subprocess.run(["node", str(PROBE)], capture_output=True,
                          text=True, encoding="utf-8", cwd=REPO_ROOT, timeout=120)


def test_the_load_gates_file_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert f"PASS {line}" in probe.stdout, probe.stdout


def test_no_check_was_quietly_dropped(probe):
    assert "all checks passed" in probe.stdout
    assert probe.stdout.count("PASS ") == len(CHECKS)
