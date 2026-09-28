"""The browser half of licensing, run in node against the shipped scripts.

The checks live in tests/js/license_probe.mjs; this pins their lines, so a
check that is deleted or renamed fails here rather than silently reducing what
is covered.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "license_probe.mjs"

CHECKS = (
    "loading paidFeature.js fetches nothing, stores nothing, shows nothing",
    "the page hint is hierarchical and false on Free",
    "the modal names the feature, says Free keeps working, and offers a trial on Free",
    "a lapsed licence says so",
    "Start a Trial opens the portal's trial page in the browser",
    "Enter License goes to Settings > License",
    "with no licence service, a trial explains itself instead of opening nothing",
    "the badge says Paid",
    "a locked tool opens the Paid modal and mounts nothing",
    "a quiet (restore) load of a locked tool opens nothing",
)

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is not installed")


def test_probe():
    done = subprocess.run([node, str(PROBE)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    lines = [line.strip()[4:] for line in done.stdout.splitlines() if line.strip().startswith("ok  ")]
    assert tuple(lines) == CHECKS


@pytest.mark.parametrize("source", ["services/paidFeature.js", "views/settingsPage.js",
                                    "views/toolLoader.js", "views/helpMenu.js"])
def test_syntax(source):
    path = REPO_ROOT / "plexora" / "client" / "src" / "js" / source
    done = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


def test_no_licence_secret_is_ever_in_browser_code():
    """The page is told plan, state and grants. Nothing in the client may
    reach for a certificate, a seat key or a token, or keep one in storage."""
    for path in (REPO_ROOT / "plexora" / "client" / "src" / "js").rglob("*.js"):
        text = path.read_text(encoding="utf-8")
        if "PlexoraPaid" not in text and "license" not in text.lower():
            continue
        assert "PLEXORA1." not in text, path
        assert "localStorage.setItem(\"license" not in text, path
        assert "sessionStorage.setItem(\"license" not in text, path
