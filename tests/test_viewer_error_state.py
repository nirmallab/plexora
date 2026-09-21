"""Why the canvas is empty, said on the canvas.

An image that cannot be read used to be a blank rectangle: the server raised,
every tile came back 500, and the reason was in a terminal nobody was watching.
Three causes -- moved, unreadable, not an image -- want three different
sentences, and the node-unreachable case is deliberately NOT one of them,
because it already has a banner that can offer to reconnect the machine.

The checks live in tests/js/viewer_error_state_probe.mjs and run against the
shipped file; this wrapper runs it and pins its lines.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "viewer_error_state_probe.mjs"

#: Every line the probe prints, so a check that is deleted or renamed fails
#: here rather than silently reducing what is covered.
CHECKS = (
    "a missing file says the file is not there",
    "an unreadable file says it is a permission problem",
    "a corrupt file says the bytes are the problem",
    "the three sentences are all different",
    "an unreachable node is left to the banner that can fix it",
    "a healthy image draws nothing",
    "an unknown status draws nothing rather than guessing",
    "the file is named, because somebody has to go and find it",
    "the server's own line is shown, so two failures read differently",
    "it offers the edit page, where the image field is",
    "and the way back to this sample's own dataset when it has one",
    "falling back to the whole list when it is in none",
    "it announces itself",
    "showing twice leaves one card, not two",
    "hiding takes it off the page and off the record",
    "a page with no canvas is left alone",
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
