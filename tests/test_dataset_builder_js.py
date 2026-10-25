"""The New Dataset dialog in the browser: syntax, wiring, and its controller.

The controller -- requests, polling, the order replies land in -- is run in
node by tests/js/dataset_builder_probe.mjs; this file runs it and checks that
the page loads the dialog and that the Samples page and the home page reach it.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CLIENT = REPO_ROOT / "plexora" / "client"
PROBE = REPO_ROOT / "tests" / "js" / "dataset_builder_probe.mjs"

CHECKS = (
    "a scan polls until it is no longer pending, then stops",
    "a folder on a data node names the node, and so does an added folder",
    "a start that is stopped lets go of the scan it started",
    "quick corrections go as one request, and a late answer is dropped",
    "a refused correction puts back what the server last agreed",
    "create posts the overrides and polls the build to its end",
    "a taken name is reported, not built",
    "closing stops a scan but leaves a build running",
    "the edits keep one placement per file",
    "names and paths are escaped in the table",
)


@pytest.fixture(scope="module")
def node():
    found = shutil.which("node")
    if found is None:
        pytest.skip("node is not installed")
    return found


@pytest.mark.parametrize("source", ["views/datasetBuilder.js", "views/openProjectPage.js",
                                    "views/quickViewLanding.js"])
def test_syntax(node, source):
    done = subprocess.run([node, "--check", str(CLIENT / "src" / "js" / source)],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


def test_controller_probe(node):
    done = subprocess.run([node, str(PROBE)], capture_output=True, text=True,
                          cwd=REPO_ROOT, timeout=120)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    lines = [line.strip()[4:] for line in done.stdout.splitlines()
             if line.strip().startswith("ok  ")]
    assert tuple(lines) == CHECKS


def test_every_page_loads_the_dialog_after_import_sample():
    base = (CLIENT / "templates" / "base.html").read_text(encoding="utf-8")
    sample = base.index("views/importSample.js?v=")
    builder = re.search(r"views/datasetBuilder\.js\?v=[\w.-]+", base)
    assert builder and builder.start() > sample


def test_its_styles_are_on_every_page():
    css = (CLIENT / "src" / "css" / "main.css").read_text(encoding="utf-8")
    for selector in (".plx-dsb ", ".plx-dsb-table", ".plx-dsb-row", ".plx-dsb-chip.is-matched",
                     ".plx-dsb-urow", ".plx-dsb-assign"):
        assert selector in css, selector
    tokens = (CLIENT / "src" / "css" / "tokens.css").read_text(encoding="utf-8")
    assert "--accent-success-soft" in tokens


def test_new_dataset_and_a_cohort_folder_open_it():
    page = (CLIENT / "src" / "js" / "views" / "openProjectPage.js").read_text(encoding="utf-8")
    assert "PlexoraDatasetBuilder.open(" in page
    assert re.search(r"open: \(id\) =>", page), "the page lets a finished build land in its folder"
    landing = (CLIENT / "src" / "js" / "views" / "quickViewLanding.js").read_text(encoding="utf-8")
    assert "PlexoraDatasetBuilder.open({root: path})" in landing


def test_a_folder_on_a_server_goes_to_new_dataset_too():
    """A cohort on a cluster is the usual case. A files-only server's folder
    is read as an `sftp://` address like any other cohort folder; a node
    cannot share a folder at all, so a folder there opens New Dataset before
    the share is attempted -- from the box and from the browser alike."""
    landing = (CLIENT / "src" / "js" / "views" / "quickViewLanding.js").read_text(encoding="utf-8")
    cohort = landing[landing.index("function cohortFolder(path, proposal)"):]
    cohort = cohort[:cohort.index("\n    }\n")]
    assert "!/^sftp:\\/\\//i.test(path)" in cohort
    assert "PlexoraDatasetBuilder.open({root: path, node})" in landing
    for gesture in ("function go()", "function take(path)"):
        body = landing[landing.index(gesture):]
        assert body.index("nodeFolder(") < body.index("checkable()"), gesture
    dialog = (CLIENT / "src" / "js" / "views" / "datasetBuilder.js").read_text(encoding="utf-8")
    # Browsing and typing both happen on the chosen machine.
    assert "node: browseNode()," in dialog
    assert "PlexoraLocators.sftpUrl(files.base, text, files.home)" in dialog


def test_every_icon_is_a_font_awesome_7_name():
    """A dead icon name draws nothing and says nothing."""
    source = (CLIENT / "src" / "js" / "views" / "datasetBuilder.js").read_text(encoding="utf-8")
    names = set(re.findall(r"\bfa-([a-z0-9-]+)", source)) - {"solid", "regular"}
    svgs = CLIENT / "node_modules" / "@fortawesome" / "fontawesome-free" / "svgs"
    if not svgs.exists():
        pytest.skip("Font Awesome is not installed here")
    dead = sorted(name for name in names
                  if not any((svgs / style / f"{name}.svg").exists()
                             for style in ("solid", "regular", "brands")))
    assert not dead, dead
