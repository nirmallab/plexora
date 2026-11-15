"""Walking a dataset leaves the page: what has to hold around that.

A Previous / Next / strip pick (views/datasetNav.js go()) is a full page
navigation. The plane, the rotation and the viewer preferences each save on a
400 ms debounce, and a pending timer dies with the page -- so an edit made just
before N is pressed must be sent before the page goes. And a walk that did not
happen (the user answered Stay at a leave prompt, or the page came back from
the back/forward cache) must leave Previous and Next working on that page.

The checks live in tests/js/dataset_walk_leave_probe.mjs and run against the
shipped files; this wrapper runs it and pins its lines.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "dataset_walk_leave_probe.mjs"

#: Every line the probe prints, so a check that is deleted or renamed fails
#: here rather than silently reducing what is covered.
CHECKS = (
    "a walk saves the plane, rotation and viewer prefs that were still waiting",
    "a page left any other way (a link, Back, closing the tab) still sends what was waiting",
    "a walk the browser did not take (Stay at the leave prompt) leaves Next working",
    "a page back from the back/forward cache can walk again",
)


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60
    )


def test_the_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_passed(probe, line):
    assert f"  ok  {line}" in probe.stdout, f"{probe.stdout}\n{probe.stderr}"


def test_no_check_was_quietly_dropped(probe):
    assert f"{len(CHECKS)} checks passed" in probe.stdout
