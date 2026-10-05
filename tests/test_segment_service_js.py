"""Magic select's client (services/segmentService.js): the planner, the token
cache, the one-time setup and its notices, run in node against the shipped
script.

The checks live in tests/js/segment_service_probe.mjs; this pins their lines, so a
check that is deleted or renamed fails here rather than silently reducing
what is covered.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "segment_service_probe.mjs"
SERVICES = REPO_ROOT / "plexora" / "client" / "src" / "js" / "services"
SCRIPTS = [SERVICES / name for name in ("viewSnapshot.js", "segmentService.js")]

CHECKS = (
    "plan answers new, grow, carve, refine, locked, needCategory and needSelection",
    "...a Shift-click inside the selection carves it; far from the session starts anew",
    "strokePoints: a dab is one point, a long line at most eight, evenly along it",
    "Session.addMany is one step: an undo takes back the whole scribble",
    "Session keeps at most MAX_POINTS, dropping the oldest prompts whole",
    "Session adds labelled points, undoes them, knows when it refines, absorbs a box",
    "describeView turns a snapshot into the view and channels a request sends",
    "the first click sends no token, with its own view and channels",
    "a second click inside the crop sends the token and the FIRST click's view",
    "a click outside the encoded crop sends no token",
    "...nor does one after forget(), or on another sample",
    "a refine starting afresh sends the region's outline as mask_geometry",
    "...but not once it can start from the previous mask",
    "a 'model missing' answer starts one install and is reported as setup",
    "...and opens exactly one setup modal, which never names the model",
    "polling while it downloads updates that modal in place",
    "ready: 'Magic select is ready', the click replays once, it closes after 1400 ms",
    "...and nothing replays twice",
    "a click it could not replay is asked for again when ready",
    "'Continue in background' closes the modal and polling does not reopen it",
    "...until magic select is asked for again",
    "a failed install says it could not be set up, with Close and no bar",
    "a failed download keeps the server's reason (URL, HTTP status) out of the modal",
    "a download cancelled meanwhile closes the modal",
    "a 429 is busy",
    "a server error is an error with the server's message",
    "a server without the runtime says once that magic select is not available",
    "?segment=stub answers a closed 24-gon and never fetches",
    "KEY is E",
)

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is not installed")


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_the_scripts_parse(script):
    done = subprocess.run([node, "--check", str(script)], capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 0, done.stderr[-3000:]


def test_the_probe_parses():
    done = subprocess.run([node, "--check", str(PROBE)], capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 0, done.stderr[-3000:]


def test_probe():
    done = subprocess.run([node, str(PROBE)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    lines = [line.strip()[4:] for line in done.stdout.splitlines() if line.strip().startswith("ok  ")]
    assert tuple(lines) == CHECKS
