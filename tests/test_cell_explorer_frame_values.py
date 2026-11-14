"""Cell Explorer colours a time series' frame with that frame's own values.

A time series' object table holds one row per cell per frame, and with a label
volume per frame the same label id is a different cell on the next frame. When
the Depth card steps to another frame, the panel has to read the new frame's
values (`t=`) and build its colours from them; rebuilding the lookup table from
the rows already loaded paints each label with whatever row had that id on the
frame before, and the legend and ROI card agree with the wrong picture.

Driven by tests/js/cell_explorer_frame_values_probe.mjs, which loads the files
named by the plugin's descriptor the way the server sends them.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from plexora.plugins.cell_explorer import PLUGIN

REPO_ROOT = Path(__file__).resolve().parents[1]
PROBE = REPO_ROOT / "tests" / "js" / "cell_explorer_frame_values_probe.mjs"


@pytest.fixture(scope="module")
def report():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, str(PROBE), *PLUGIN.scripts],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
    )
    try:
        return proc.returncode, json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")


def test_the_probe_reached_both_frames(report):
    _, data = report
    assert data["result"] is not None, "\n".join(data["problems"])
    assert data["result"]["handlers"] == 1, "the panel listens for view changes"
    assert data["result"]["frame0"]["cell1"] == [255, 0, 0], "frame 0 colours label 1 Tumor"


def test_stepping_the_frame_reads_the_new_frames_values(report):
    """The values are cached per frame, so a frame already visited is free; a
    new one has to be asked for."""
    _, data = report
    asked = [entry["t"] for entry in data["fetched"]]
    assert "1" in asked, f"values were only asked for t={asked}"


def test_the_new_frame_is_coloured_by_its_own_rows(report):
    """Label 1 is a Tumor cell on frame 0 and a CD8 T cell on frame 1."""
    returncode, data = report
    assert data["result"]["frame1"]["codes"] == [1, 0]
    assert data["result"]["frame1"]["cell1"] == [0, 0, 255]
    assert returncode == 0, "\n".join(data["problems"])
