"""A connection that fails in the background comes back to show the failure.

"Continue in background" (and Escape) hide a connection that is still opening,
and the background watch brings the dialog back when there is "a failure to
read" (services/connectionModal.js, module docstring). Bringing it back must
not POST /settings/remotes/<name>/connect again: the server starts a fresh
session over a failed one, which is a second ssh login (another password or Duo
push), a second scheduler job or a second Google Cloud VM start that nobody
asked for, and its "connecting" replaces the failure the dialog came back to
show. Only Connect and Try again start an attempt.

The behaviour is in tests/js/connection_modal_background_failure_probe.mjs,
run in node below.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "connection_modal_background_failure_probe.mjs"

CHECKS = (
    "the connection is watched in the background",
    "a failure in the background brings the dialog back",
    "bringing it back to show the failure does not start a new attempt",
    "...and the failure is what it shows, with Try again",
    "Try again is what starts a new attempt",
    "leaving with a question on screen leaves an Answer notice",
    "Answer on a connection that has since failed does not start a new attempt",
)


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60
    )
    return proc


def test_a_background_failure_is_shown_not_retried(probe):
    """The dialog the background watch brings back for a failure shows that
    failure with Try again, and starts no second connection of its own -- nor
    does the notice's Answer button on a connection that has since failed."""
    for line in CHECKS:
        assert f"ok  {line}" in probe.stdout, f"{probe.stdout}\n{probe.stderr}"
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"
