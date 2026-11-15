"""The website's import keeps a Stop pressed while it is starting.

services/bundleWeb.js runImport: until `POST /projects/import` answers, the
page's job has no Python job to cancel. A Stop in that window was acknowledged
and then ignored, so the import ran to completion (audit lead sc-bundles-10).

The page's side is tests/js/bundle_web_import_stop_probe.mjs; this runs it and
pins its lines. Its tests are skipped without Node.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "bundle_web_import_stop_probe.mjs"

CHECKS = (
    "a Stop pressed while the import starts stops Python's import",
)


def _node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return node


@pytest.fixture(scope="module")
def probe():
    return subprocess.run([_node(), str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=120)


def test_the_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert f"ok - {line}" in probe.stdout.replace(f"not ok - {line}", "")


def test_no_check_was_quietly_dropped(probe):
    assert f"{len(CHECKS)} checks passed" in probe.stdout
