"""Work done on the website while the workspace folder was not opened is
never lost on Reconnect, and such a visit is not served copies older than the
folder's (client services/browserStore.js; audit lead flow-website-3).

Adoption and Reconnect set aside the browser's copies of the projects the
workspace holds (OPFS plexora/set-aside/<time>), so a visit that does not open
the folder starts without them. A project both sides hold that a visit changed
is kept twice on Reconnect: the folder's as it is, this visit's copied by
Python as "<name> (browser copy, <date>)"; `reconnect` returns the copies and
the app frame says so (views/webOnboarding.js `noticeCopies`). The checks are
in tests/js/browser_store_reconnect_edits_probe.mjs; this wrapper runs it and
pins its lines. The copy's own rows, paths and id are
tests/test_bundles.py `test_a_copy_under_a_name_of_its_own`.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "browser_store_reconnect_edits_probe.mjs"

#: Every line the probe prints, so a check that is deleted or renamed fails
#: here rather than silently reducing what is covered.
CHECKS = (
    "visit 1: the browser's project is copied into the workspace on adoption",
    "after adoption the browser's copy is set aside, not left to be served",
    "a visit that does not open the folder starts without the adopted copies",
    "visit 2, folder not granted: an older version's left-over copies are what Python boots on",
    "Reconnect does not drop this visit's edit without a word",
    "both versions are kept: the folder's as it was, this visit's under a name of its own",
    "the copy is made by Python (its rows, paths and id its own), not by copying files",
    "a project only looked at is not copied; what the folder holds is set aside",
    "the copies are left for the app's notice, and told once",
    "a copy that cannot be made stops the Reconnect: nothing moved, nothing set aside",
)


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return subprocess.run([node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60)


def test_reconnect_keeps_or_reports_edits_made_while_the_folder_was_closed(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"
    assert "browser store reconnect edits probe OK" in probe.stdout


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert f"  ok  {line}" in probe.stdout, probe.stdout
