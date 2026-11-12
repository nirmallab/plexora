"""The website's Python queue when the user leaves a page that still has Python
busy (client workers/pythonWorker.js `drain`, `ahead`, `letIn`, `later`;
services/pythonClient.js `first`, `later`; services/backend.js `untilGone`).

Measured in Chrome on 0.0.53 with a project's viewer whose channel requests
were asked "again later" without end (a load that never finishes) or were slow
(a slow drive): Home took 1.1-4.6 s through the router and 2.6-4.9 s as a
page of its own, and the page's next request 1-4 s more, because every
request the page left had asked waited in front of it, and its retries went
on for good. With the page first, the left page's reads dropped and the
viewer's moved behind, Home took 0.1-0.4 s and the next request what Python
was running at the time.

The node probe (tests/js/python_queue_probe.mjs) runs those pieces of the
worker with Python stood in for.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests" / "js" / "python_queue_probe.mjs"
CLIENT = (ROOT / "plexora" / "client" / "src" / "js" / "services" / "pythonClient.js").read_text(encoding="utf-8")
BACKEND = (ROOT / "plexora" / "client" / "src" / "js" / "services" / "backend.js").read_text(encoding="utf-8")
SHELL = (ROOT / "plexora" / "client" / "src" / "js" / "services" / "shell.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def probe():
    if not shutil.which("node"):
        pytest.skip("needs node")
    done = subprocess.run(["node", str(PROBE)], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_page_asked_for_goes_ahead_of_the_reads_left_waiting(probe):
    # Python answers in microtasks, which let no message in: the page's
    # request is read in between two answers all the same.
    assert probe["first"] == ["/stats/1", "/", "/stats/2", "/stats/3", "/stats/4", "/stats/5", "/stats/6"]


def test_a_page_never_goes_ahead_of_a_write(probe):
    # Drawn before the save, it would show what was true before it.
    assert probe["queuedBehindWrite"] == ["/stats/2", "/save", "/", "/settings", "/stats/4", "/stats/5"]
    assert probe["behindWrite"] == ["/stats/1", "/stats/2", "/save", "/", "/settings", "/stats/4", "/stats/5"]


def test_a_request_dropped_while_python_ran_it_is_not_asked_again(probe):
    assert probe["droppedWhileRunning"] == {"asked": 1, "retrying": 0, "aborted": [1]}
    assert probe["droppedRetry"] == {"waiting": 1, "asked": 1, "retrying": 0, "aborted": [1]}


def test_a_page_shown_over_the_viewer_goes_ahead_of_the_viewers_reads(probe):
    # The viewer's reads wait, not dropped (the viewer is kept to come back
    # to); its write keeps its place.
    assert probe["late"] == ["/stats/2 (late)", "/stats/retry (late)", "/save"]
    assert probe["later"] == ["/stats/1", "/save", "/home-thumb", "/stats/2", "/stats/retry"]


def test_the_pages_say_which_requests_go_first_and_when_reads_are_dropped():
    # The frame's next page and the router's fragment go first; the page kept
    # for the next visit (keepHome) does not.
    assert 'name.toLowerCase() === "x-plexora-fragment"' in CLIENT
    assert '{ headers: { Accept: "text/html" }, first: true }' in SHELL
    keep = SHELL[SHELL.index("function keepHome("):SHELL.index("// -- A kept page replaced")]
    assert "first" not in keep
    # A page that goes drops its reads, never its writes; the viewer covered
    # moves its reads behind.
    assert 'if (!["GET", "HEAD"].includes(request.method)) return python.fetch(request);' in BACKEND
    assert 'global.addEventListener("pagehide", state.gone);' in BACKEND
    assert 'global.addEventListener("plexora:viewer-hidden", state.covered);' in BACKEND
