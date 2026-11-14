"""The website's browser node across a walk of projects in one tab
(services/browserNode.js `project`, `check`, TiffPool), run in Node with its
workers stood in for (tests/js/browser_node_project_walk_probe.mjs).

The host page keeps one PlexoraNode for the tab's life, and every project the
app's frame shows checks its project there (services/shell.js frameBoot:
`revalidate()`, then `project(name)`). Each check of a new project starts the
image's and the mask's worker pools; a project the page has left should let
its workers go, or a dataset walked image by image keeps every image's
workers, their parsed IFDs and their files for as long as the tab is open."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = Path(__file__).parent / "js" / "browser_node_project_walk_probe.mjs"
    done = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_every_project_of_the_walk_opens_in_the_page(probe):
    assert "error" not in probe, probe.get("error")
    assert probe["perProject"] > 0
    assert probe["aliveForLastAtEnd"] == probe["perProject"]


def test_a_project_left_several_pages_ago_holds_no_workers(probe):
    assert probe["aliveForFirstAtEnd"] == 0, probe


def test_live_workers_do_not_grow_with_the_projects_walked(probe):
    # At most two projects' readers (the one shown, and one kept for going
    # back), however many the tab has shown.
    assert max(probe["aliveAfterEach"]) <= 2 * probe["perProject"], probe["aliveAfterEach"]
