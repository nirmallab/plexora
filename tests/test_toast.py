"""The bottom-right notice: something happened, and nothing is waiting on you.

Core had no such thing. The navbar chip says whether the app is busy or broken,
the resource banner exists to offer a FIX, and a dialog asks a question -- what
was left over is a statement about something already done, which is what this
is for.

The checks live in tests/js/toast_probe.mjs (with a controllable clock, so the
twenty-second timeout and the hover pause are testable) and run against the
shipped file; this wrapper runs it and pins its lines.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "toast_probe.mjs"

#: Every line the probe prints, so a check that is deleted or renamed fails
#: here rather than silently reducing what is covered.
CHECKS = (
    "a notice renders its title",
    "with no title there is nothing to say and nothing is drawn",
    "the note and the list are drawn when given",
    "the host is a polite live region",
    "it goes by itself after twenty seconds",
    "hovering stops the clock",
    "and leaving starts it again, in full",
    "keyboard focus holds it too",
    "the dismiss button takes it away",
    "the caller can take back one it raised",
    "a second notice replaces the first rather than stacking",
    "dismissing twice is not an error and removes nothing twice",
    "a timeout of 0 means it stays until dismissed",
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


def test_the_toast_is_on_every_page():
    """Its CSS is in main.css, which base.html loads everywhere, and a notice
    that only existed on the viewer would send every other page back to
    `window.alert`. Same terms as confirmDialog.js beside it."""
    html = (REPO_ROOT / "plexora" / "client" / "templates" / "base.html").read_text(
        encoding="utf-8")
    assert "services/toast.js" in html
