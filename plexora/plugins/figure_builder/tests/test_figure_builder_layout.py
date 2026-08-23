"""Page arithmetic: the part that decides what a figure looks like.

None of it runs anywhere else in this suite -- pytest renders HTML and stops,
and `node --check` sees syntax only -- so each of these mistakes would ship
green and produce a figure that is quietly, expensively wrong:

* a corner resize that does not keep the aspect ratio squashes the tissue in a
  panel, which is a scientific error wearing a layout error's clothes;
* "distribute" as equal CENTRES rather than equal GAPS looks right only when
  every panel is the same size, which for a figure of mixed crops is almost
  never;
* a snap threshold in millimetres is unusably sticky zoomed in and inert zoomed
  out;
* labels in capture order give a 3x2 grid numbered by when each field happened
  to be found.

    node tests/js/figure_layout_probe.mjs
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "figure_layout_probe.mjs"


@pytest.fixture(scope="module")
def report():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    proc = subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60)
    try:
        return proc.returncode, json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")


def test_the_page_arithmetic_is_right(report):
    returncode, data = report
    assert not data["problems"], "\n".join(data["problems"])
    assert returncode == 0


def test_the_probe_actually_committed_something(report):
    """A probe whose fixture silently does nothing passes every assertion in
    it."""
    _, data = report
    assert data["commits"] >= 1
