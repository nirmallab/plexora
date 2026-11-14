"""A figure save that never reached the server is not forgotten by the next one.

FigureDocumentState (static/figureDocumentState.js) applies an edit to the
tab's copy at once and sends it as a PATCH carrying only that commit's
operations. When the request itself fails -- the server restarting, the
website's Python worker stalling, a relay blip -- the status becomes "failed"
and the tab keeps the edit. The next edit's PATCH carries only its own
operations, and once it is stored the status says "Saved" although the server
never received the first edit: it is gone on reload and missing from an export.

The checks are in `tests/js/figure_save_dropped_probe.mjs`: the real
FigureBuilderApi and FigureDocumentState against a stubbed `fetch` that keeps a
server copy of the figure and can drop one request. This drives the probe and
names every check, so one quietly deleted fails here rather than passing.

    node tests/js/figure_save_dropped_probe.mjs
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "figure_save_dropped_probe.mjs"

#: Every line the probe prints.
CHECKS = [
    "two stored saves report Saved and the server holds both panels",
    "a dropped save is reported as not stored",
    "after a dropped save and a stored one, the status does not say Saved "
    "while the server is missing the dropped edit",
    "a dropped add, a move of that panel and a later stored edit do not end "
    "in Saved while the server is missing the dropped panel",
]


@pytest.fixture(scope="module")
def probe():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    return subprocess.run(["node", str(PROBE)], capture_output=True,
                          text=True, cwd=REPO_ROOT, timeout=60)


def test_the_dropped_save_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert f"PASS {line}" in probe.stdout, probe.stdout


def test_no_check_was_quietly_dropped(probe):
    assert "all checks passed" in probe.stdout
    assert probe.stdout.count("PASS ") == len(CHECKS)
