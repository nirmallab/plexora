"""A reload of the site in Chrome or Edge keeps the tab's Python (a SharedWorker,
client workers/pythonWorker.js `attach`), and the page it brings is answered
from then on. A write the page before had queued while Python was busy with
something slow (a gate saved behind a channel's stats) must still reach Python
-- services/backend.js: "a write the page made as it left still lands" -- and
only its answer is dropped, since the new page's ids are its own (`pageNow`).

The node probe (tests/js/tab_python_reload_keeps_writes_probe.mjs) runs the
worker's `attach`, `leftBehind` and `drain` with Python stood in for.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests" / "js" / "tab_python_reload_keeps_writes_probe.mjs"


@pytest.fixture(scope="module")
def probe():
    if not shutil.which("node"):
        pytest.skip("needs node")
    done = subprocess.run(["node", str(PROBE)], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_write_was_queued_behind_python_busy_as_the_page_left(probe):
    assert probe["queuedBefore"] == ["PUT /gates/CD3", "GET /stats/other"]


def test_a_write_queued_by_the_page_before_a_reload_still_reaches_python(probe):
    assert "PUT /gates/CD3" in probe["asked"], (
        f"the gate save was dropped at the reload: queue after it {probe['queuedAfter']}, "
        f"Python asked {probe['asked']}")


def test_the_new_page_gets_no_answer_meant_for_the_page_before(probe):
    assert probe["answersToSecond"] == []
