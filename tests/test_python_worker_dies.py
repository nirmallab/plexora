"""The website's Python worker failing to load (services/pythonClient.js
`spawn`, `start`): its script, or one of the modules it imports, cannot be
fetched or run, so it never answers; the browser fires the worker's `error`
event and nothing more. The page's start() must still settle with an error,
so the loader says "Plexora could not start" (services/shell.js `hostBoot`)
instead of spinning for good. Run in Node over the real module, the browser
and its clock stood in for (tests/js/python_worker_dies_probe.mjs)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = ROOT / "tests" / "js" / "python_worker_dies_probe.mjs"
    done = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_start_settles_with_an_error_when_the_pages_worker_fails_to_load(probe):
    page = probe["page"]
    assert page["made"] is True and page["mode"] == "page" and page["posted"] == ["boot"]
    assert page["settled"] is True, "start() still waits on a worker that failed to load"
    assert page["error"]


def test_start_settles_with_an_error_when_the_tabs_worker_fails_to_load(probe):
    tab = probe["tab"]
    assert tab["made"] is True and tab["mode"] == "tab" and tab["posted"] == ["boot"]
    assert tab["settled"] is True, "start() still waits on a tab's worker that failed to load"
    assert tab["error"]
