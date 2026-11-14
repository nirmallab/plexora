"""A launched or carried channel's auto-level is never written to the project.

`plexora.view(channels=[...])` and a Prev/Next walk through a dataset open a
sample showing channels it did not save, and the design rests on none of that
being written back (tests/test_launch_state.py). A channel arriving without a
range auto-levels in two passes -- the stats hint, then the mixture fit about
a second later -- after init() has already let go of `_restoring`. The slot's
`autoSilent` flag has to keep BOTH passes from scheduling a save, or the fit's
landing writes the carried arrangement as the new sample's own channels.

The probe is tests/js/launch_autolevel_silent_probe.mjs (the real
ViewerSidebar levelling and save debounce, with the fetches stubbed).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "launch_autolevel_silent_probe.mjs"

CHECKS = (
    "a carried channel is levelled by both passes (hint, then fit)",
    "a carried or launched channel's auto-level is never saved to the project",
    "a channel the user picked is saved once its fit lands",
)


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return subprocess.run([node, str(PROBE)], capture_output=True, text=True, encoding="utf-8",
                          cwd=REPO_ROOT, timeout=60)


def test_the_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_passes(probe, line):
    assert f"PASS {line}" in probe.stdout, probe.stdout


def test_no_check_was_quietly_dropped(probe):
    assert probe.stdout.count("PASS ") + probe.stdout.count("FAIL ") == len(CHECKS), probe.stdout
