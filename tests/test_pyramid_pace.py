"""The website's pyramid builds step back while the page waits on something
(client node/pace.js, workers/pyramidWorker.js, services/browserPyramid.js
`pace`; the signals: services/pythonClient.js and services/browserNode.js
`busy`).

Measured on LSP11385 (Mac, 10 cores, Chrome 157) with the plain mask
`cell.ome.tif`, whose label pyramid the page builds on importing it: the
build on eight hands kept the project's first open waiting -- the viewer drawn
17 s after it was opened (the local server: 4.5 s), Python opening the 66 GB
image in 11 s instead of 1 s and reading the table in 3.8 s instead of 0.6 s.
Paced (one hand while a request has waited on Python more than 50 ms, or
the browser node reads tiles), the viewer was drawn in 4.3 s, the build's
progress shown over it, and the mask drawn when the build ended (23 s instead
of 15 s: the viewer now works beside it).

The node probe (tests/js/pyramid_pace_probe.mjs) stands in the build's worker,
Python's worker and the page's services.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests" / "js" / "pyramid_pace_probe.mjs"
WORKER = (ROOT / "plexora" / "client" / "src" / "js" / "workers" / "pyramidWorker.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def probe():
    if not shutil.which("node"):
        pytest.skip("needs node")
    done = subprocess.run(["node", str(PROBE)], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_a_pacer_holds_work_to_its_limit_in_order(probe):
    assert probe["unlimited"] == {"peak": 6, "answers": [0, 1, 2, 3, 4, 5]}
    assert probe["one"] == {"peak": 1, "order": [0, 1, 2, 3, 4], "activeAfter": 0}


def test_a_pacers_limit_changes_while_work_runs(probe):
    # Raised: what waited starts at once. Lowered: what runs ends.
    assert probe["raised"] == {"before": 1, "raised": 3, "peak": 3}
    assert probe["lowered"] == {"before": 6, "lowered": 0}
    assert probe["failure"] == {"failed": "no", "next": "next", "active": 0}


def test_the_build_worker_paces_its_hands():
    assert 'import { pacer } from "../node/pace.js";' in WORKER
    assert "block: (i, job) => paced(() => call(i, \"block\", job))" in WORKER
    assert "deflate: (bytes) => paced(" in WORKER
    pace = WORKER[WORKER.index('if (data.op === "pace")'):WORKER.index('if (data.op !== "build")')]
    assert "pace.set(data.hands)" in pace


def test_python_is_busy_only_for_what_a_page_waited_on_a_while(probe):
    busy = probe["pythonBusy"]
    assert busy["idle"] is False
    # A quick answer (a status poll's) holds nothing back...
    assert busy["justSent"] is False
    # ...one waited on past 50 ms does, and stops the moment it is answered.
    assert busy["after80"] is True and busy["after80ButAsked200"] is False
    assert busy["answered"] is False
    # The page's own bookkeeping is never waited on.
    assert busy["heapPending"] is False


def test_a_build_steps_back_while_the_page_waits(probe):
    builder = probe["builder"]
    assert builder["build"] == {"op": "build", "workers": 8}
    assert builder["idle"] == []
    assert builder["pythonBusy"] == [1]
    # The build's own job looking in on it is no reason to go faster.
    assert builder["jobLooked"] == [1]
    assert builder["free"] == [1, 8]
    assert builder["nodeBusy"] == [1, 8, 1]


def test_a_request_holding_out_for_the_build_gets_every_hand(probe):
    builder = probe["builder"]
    assert builder["awaited"] == [1, 8, 1, 8]
    assert builder["stillAwaited"] == [1, 8, 1, 8]
    # A second after its last look, the page's waits count again.
    assert builder["awaitedEnded"] == [1, 8, 1, 8, 1]
    # Done: the pacing stops with the worker.
    assert builder["afterDone"] == {"more": 0, "terminated": True}
    assert "worker" not in builder["listed"] and "pacer" not in builder["listed"]
