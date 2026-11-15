"""A save into a picked file Python wrote (services/pythonClient.js `save`,
services/sourceWrites.js) can wait on the user's answer to "Save into <file>?".
The request that wrote the file has its answer wait for that save; every
other request used to wait for it too, so tiles, reads and polls hung behind
an unanswered question. The worker now names the request a save came from
(workers/pythonWorker.js `sourceWrite`), and only that one waits.

The node probe (tests/js/source_write_waits_probe.mjs) runs the real
pythonClient.js with its worker stood in for.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests" / "js" / "source_write_waits_probe.mjs"
WORKER = ROOT / "plexora" / "client" / "src" / "js" / "workers" / "pythonWorker.js"


@pytest.fixture(scope="module")
def probe():
    if not shutil.which("node"):
        pytest.skip("needs node")
    done = subprocess.run(["node", str(PROBE)], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_request_that_wrote_waits_for_its_save(probe):
    assert probe["writerBeforeSave"] is False
    assert probe["writerAfterSave"] is True


def test_another_request_does_not_wait_for_that_save(probe):
    assert probe["otherBeforeSave"] is True


def test_the_worker_names_the_request_a_save_came_from():
    source = WORKER.read_text(encoding="utf-8")
    assert 'kind: "sourceWrite", source, file, request:' in source
