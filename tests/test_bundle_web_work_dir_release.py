"""The website export's work folder is given back however the export ends.

`POST /projects/export/plan` (server/routes/bundle_routes.py) makes a
`plexora-bundle-*` folder under Python's temporary folder and
`bundles.plan_for_page` writes every database snapshot, entry and capture
into it. On the website that folder is in the tab's memory. Only the manifest
route removes it, which a finished export alone reaches: an export that is
stopped or fails after the plan (services/bundleWeb.js runExport), and a plan
that is refused, must not leave it behind.

The page's side is tests/js/bundle_web_work_dir_probe.mjs; this runs it and
pins its lines. Its tests are skipped without Node.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "bundle_web_work_dir_probe.mjs"

CHECKS = (
    "a stopped export gives Python back its plan's work folder",
    "a failed export gives Python back its plan's work folder",
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


def test_a_refused_plan_leaves_no_work_folder(tmp_path, monkeypatch):
    """A plan for a project that is not there is refused (404), and the work
    folder the route made for it is not left in the temporary folder."""
    import plexora

    temp = tmp_path / "tmp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    client = plexora.app.test_client()
    answer = client.post("/projects/export/plan", json={"ids": ["no-such-project-work-dir"]})
    assert answer.status_code == 404, answer.get_json()
    left = sorted(path.name for path in temp.glob("plexora-bundle-*"))
    assert left == [], f"work folders left behind: {left}"
