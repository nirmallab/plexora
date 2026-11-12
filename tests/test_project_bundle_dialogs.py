"""The Export and Import dialogs' decisions (views/projectExport.js,
views/projectImport.js): what goes into a `.plexora` file by default, what a
project in one is offered on import, where an import may go, and that every
request goes through `PlexoraBundleTransport` -- the seam the website's own
transport replaces.

The checks live in tests/js/project_bundle_probe.mjs and run against the
shipped files; this wrapper runs it and pins its lines, so a check that is
deleted or renamed fails here rather than silently reducing what is covered.
The dialogs themselves were driven in a real browser against a scratch server
(export, import back under a new name, Make a copy, `?bundle=`).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "project_bundle_probe.mjs"

CHECKS = (
    'sizes are written as Finder writes them',
    'a size nobody knows is left blank',
    'tables, bookmarks and figures start on; images and ready-to-open files start off',
    "the project's own work is always included, and cannot be unticked",
    'the rows on by default are the analysis, the tables and the bookmarks',
    'bookmarks and figures are one choice over two parts',
    'images list every file, the ones that stay where they are included',
    'a part with nothing in it is not offered',
    'files that cannot go are offered disabled, saying where they stay',
    'the total is an upper bound over what is ticked',
    'ready-to-open files left out warn when only this computer has them',
    'and say nothing once they go, or when they are on a shared drive',
    'the include rule is the parts that are on',
    'smaller images start off',
    'smaller images are offered under images once they go, not before',
    "ticked, the images count as the server's estimate of them",
    'a server that cannot make them offers nothing, and counts nothing smaller',
    'and the website never offers them',
    'the include rule names them when they are on',
    'one project is exported under its name',
    'several are counted',
    'a dataset is named',
    'the file is named for the project, made safe',
    'a file kind is told from its role and name',
    'changed on both offers keep both or replace',
    'an update offers update, keep both or skip',
    'one already here offers skip or keep both',
    'a new project has nothing to decide',
    "each status starts at the server's default",
    "the server's own action is kept when it is one on offer",
    'a renamed project says what it is imported as',
    'its images are counted where they are',
    'a smaller copy is counted among the included, and says so',
    'Import counts what the choices bring in',
    "an import goes only to the user's own folders that are there and writable",
    'the default save location comes first',
    'no locations is no choices',
    'not enough space says so, with both numbers',
    'enough space says what it needs',
    'where a file came from is said in one line',
    'a finished project says how it got its name',
    'the server transport is the default',
    'the dialogs run on the desktop and its server',
    'and not on the website, until it brings its own transport',
    "a dropped .plexora opens the Import dialog (the drop router's executor)",
    'the estimate names the projects and the parts',
    'an export posts its destination',
    'a preview names the file and the place',
    'an import of an uploaded file names the upload, not the bytes again',
    "a cancel is a POST, which the website's page can answer too",
    'a browser downloads the file when it is ready',
    'the desktop app asks where the file goes before the export starts',
    'and a cancelled Save dialog starts nothing',
    "the server transport picks its folder before findMissing; the website's picks it itself",
    'Show in folder only where the app can reveal a file',
)


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60
    )


def test_the_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert f"ok - {line}" in probe.stdout.splitlines(), probe.stdout
