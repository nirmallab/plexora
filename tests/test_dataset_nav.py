"""Previous and Next, for a sample that is one of several.

The walk order is the dataset's own `projects` order -- the one somebody put
the samples in -- and NOT the Samples page's default sort, which is by "last
opened" and is rewritten by every open, so a walk built on it would reshuffle
underneath the user.

The checks live in tests/js/dataset_nav_probe.mjs and run against the shipped
file; this wrapper runs it and pins its lines.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "dataset_nav_probe.mjs"

#: Every line the probe prints, so a check that is deleted or renamed fails
#: here rather than silently reducing what is covered.
CHECKS = (
    "the middle of a dataset has both neighbours",
    "the walk order is the dataset's own, not alphabetical",
    "the first sample has no Previous",
    "the last sample has no Next",
    "a member this Plexora cannot open is skipped, not offered",
    "a sample in no dataset gets no controls at all",
    "a page with no canvas never mounts one",
    "a /datasets that will not answer is silent",
    "the counter says where in the dataset this sample is",
    "each button names the sample it goes to",
    "at the end the button is disabled, not removed",
    "walking carries the arrangement and then navigates",
    "the open tool rides in the URL so the server renders it",
    "a second click during a navigation is ignored",
    "PageDown walks forward",
    "PageUp walks back",
    "at the end the key does nothing and keeps its scroll",
    "the keys stand down while somebody is typing",
    "and while a dialog owns the window",
    "a modified PageDown is somebody else's shortcut",
    "the dataset is named, so a card can link back to it",
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
def test_each_check_ran(probe, line):
    assert line in probe.stdout


def test_no_check_was_quietly_dropped(probe):
    assert f"{len(CHECKS)} checks passed" in probe.stdout
