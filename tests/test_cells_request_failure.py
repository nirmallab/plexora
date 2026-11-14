"""One failed /get_all_cells is not kept for the rest of the page.

`NumericData.loadCells` caches the page's one cells request: picking, the
legacy centroids, the gating classifier and Cell Explorer's ROI mapping all
read it. `DataLayer.getAllCells` swallowed a network error (an empty table was
cached) and never looked at the status (an error page's bytes became a cached
RangeError, or, when their length was a multiple of 4, cell ids). Nothing but
a cell-keying change cleared the cache, so a server that recovered a moment
later was never asked again.

Run in node against the real dataLayer.js, numericData.js and
datasetContext.js (tests/js/cells_request_failure_probe.mjs).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "cells_request_failure_probe.mjs"


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60
    )
    assert "cells request failure" in proc.stdout + proc.stderr, f"{proc.stdout}\n{proc.stderr}"
    return proc.stdout


def _passed(probe, name):
    assert f"PASS {name}" in probe, probe


def test_a_network_error_is_retried_rather_than_cached_as_no_cells(probe):
    _passed(probe, "a network error is not reported as a project with no cells")
    _passed(probe, "after a network error the next loadCells asks the server again")
    _passed(probe, "...and gets the cells once the server answers")


def test_an_http_error_is_retried_rather_than_cached_as_a_rejection(probe):
    _passed(probe, "an HTTP 500 makes loadCells fail rather than resolve")
    _passed(probe, "after an HTTP 500 the next loadCells asks the server again")
    _passed(probe, "...and gets the cells instead of the cached failure")


def test_an_error_body_is_never_read_as_cell_ids(probe):
    _passed(probe, "a 503 body is never read as cell ids")
    _passed(probe, "after a 503 the next loadCells asks the server again")


def test_a_successful_load_is_still_fetched_once(probe):
    _passed(probe, "a successful load is still cached: two loads, one request")
