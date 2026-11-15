"""What a bug report carries besides the person's words: the recent errors
(services/errors.js) and the diagnostics (services/feedbackDiagnostics.js).

The checks are in `tests/js/feedback_probe.mjs`; this drives it and names every
check, so one quietly deleted fails here rather than passing silently.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "feedback_probe.mjs"

CHECKS = [
    "a URL becomes its script's name, a path <path>, an email <email>, a key <redacted>",
    "reported, uncaught and unhandled failures are kept, newest last, ten at most",
    "the website's Python errors the host page kept are read and scrubbed again",
    "diagnostics: the software, the system, the screen, the kind of page, never its address",
    "the pages it can name: home, settings, a viewer; anything else is other",
    "Windows and Firefox are named from the user agent",
]


@pytest.fixture(scope="module")
def probe():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    return subprocess.run(["node", str(PROBE)], capture_output=True, text=True,
                          encoding="utf-8", cwd=REPO_ROOT, timeout=120)


def test_the_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert f"PASS {line}" in probe.stdout


def test_no_check_was_quietly_dropped(probe):
    assert "all checks passed" in probe.stdout
    assert probe.stdout.count("PASS ") == len(CHECKS)
