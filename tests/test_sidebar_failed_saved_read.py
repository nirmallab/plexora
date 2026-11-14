"""A saved-state read that failed is not a sample with nothing saved.

At boot the channel sidebar reads the project's saved channel list and each
sidebar module's saved state (Thresholding: the saved gates). A sample with
nothing saved answers `[]`, and the sidebar then writes its default guess so
the project has a starting point.

`DataLayer.getSavedChannelList` and `GatingApi.getSavedGatingList` swallow
their errors, so a read that failed -- a 502 page from a proxy, a JSON 500, a
dropped tunnel -- resolves to nothing. Taking that for "nothing saved" writes
the default channel list over the saved one, and the default gate list over
the saved gates with no `base` (the failed read never learned the revision),
which the server stores whole (`model.save_gating_list`). The sample's gates
are gone and nothing on screen says so.

The checks are in `tests/js/sidebar_failed_saved_read_probe.mjs`: the real
ViewerSidebar, DataLayer, GatingApi and GatingSidebarController against a
stubbed `fetch` that records what would reach the server. This drives the
probe and names every check, so one quietly deleted fails here rather than
passing silently.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "sidebar_failed_saved_read_probe.mjs"

#: Every line the probe prints.
CHECKS = [
    "a sample with nothing saved gets the default channel list written",
    "a sample with nothing saved gets the default gate list written",
    "a sample with saved channels and gates is not written at boot",
    "a 502 page from the proxy on the saved channel list writes no channel list over it",
    "a 502 page from the proxy on the saved gates writes no gate list over them",
    "a JSON 500 on the saved channel list writes no channel list over it",
    "a JSON 500 on the saved gates writes no gate list over them",
    "a dropped connection on the saved channel list writes no channel list over it",
    "a dropped connection on the saved gates writes no gate list over them",
    "a tool opened mid-session whose saved read failed writes no gate list",
]


@pytest.fixture(scope="module")
def probe():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    return subprocess.run(["node", str(PROBE)], capture_output=True,
                          text=True, cwd=REPO_ROOT, timeout=120)


def test_the_failed_saved_read_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert f"PASS {line}" in probe.stdout, probe.stdout


def test_no_check_was_quietly_dropped(probe):
    assert "all checks passed" in probe.stdout
    assert probe.stdout.count("PASS ") == len(CHECKS)
