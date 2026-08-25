"""What the client-side router routes, refuses to route, and keeps alive.

appRouter.js exists so that walking away from a slide and back does not destroy
the OpenSeadragon viewer, its WebGL context, its decoded tiles and its viewport.
The rule it enforces is that the viewer is rebuilt when, and only when, the
PROJECT changes -- and every part of that rule is a decision made in JavaScript
against a click, which nothing on the Python side can see.

So the checks live in tests/js/app_router_probe.mjs, run against the shipped
file. This wrapper runs it and pins its lines, so that a future edit which
quietly drops a check does not read as a passing test.

The server half of the same feature -- that every page can be rendered as a
fragment, and that the fragment is the same page -- is tests/test_app_shell.py.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "app_router_probe.mjs"

#: Every line the probe prints. Two of them carry the whole feature:
#: "fetches nothing at all" is what makes the return instantaneous, and "a link
#: to a different project" is what stops it being a lie.
CHECKS = (
    "a page link routes and never touches the viewer",
    "coming back to the viewer fetches nothing at all",
    "the viewer announces going and coming back",
    "a link to a different project is left to the browser",
    "a modified click is the browser's, not the router's",
    "a Tools row, a download and a new tab are all left alone",
    "a script the document already ran is not run again",
    "the page's controllers are mounted, and the last page's dropped",
    "a fragment that will not load becomes a real navigation",
    "a redirect to another project is handed to the browser",
    "a redirect elsewhere pushes where the content came from",
    "returning with ?tool= opens it, and only if it is shut",
    "a page's stylesheet is disabled on the way out, not removed",
    "back and forward route without pushing a new entry",
    "a burst of Back presses lands where the last one asked",
    "a document with no viewer stands down but still navigates",
)


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60
    )


def test_the_router_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert line in probe.stdout


def test_no_check_was_quietly_dropped(probe):
    assert f"{len(CHECKS)} checks passed" in probe.stdout
