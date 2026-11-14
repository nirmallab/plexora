"""Leaving the figure canvas takes its window listeners with it.

The canvas is a routed page: appRouter.js mounts it over the viewer without a
document load, and the back arrow puts the viewer back through
PlexoraPage.unmount(), which runs FigureWorkspace.destroy(). The window outlives
the workspace, so anything destroy() leaves on it stays for the rest of the
session and piles up with every visit -- and the hold-Space-to-pan listener
cancels Space everywhere, which is the key that presses a focused button.

The probe builds a workspace with setup(), destroys it, and counts what is left
on a window that really keeps its listeners. The script list is read off the
descriptor, as in test_figure_builder_boot.py.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from plexora.plugins.figure_builder import PLUGIN

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "figure_workspace_teardown_probe.mjs"


@pytest.fixture(scope="module")
def report():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, str(PROBE), *PLUGIN.scripts],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
    )
    try:
        return json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")


def _load_problems(data):
    return [p for p in data["problems"] if "failed to load" in p or "threw" in p]


def test_the_probe_builds_and_destroys_a_workspace(report):
    assert not _load_problems(report), "\n".join(report["problems"])
    assert report["addedBySetup"], "setup() put nothing on the window to check"


def test_destroy_removes_every_window_listener_setup_added(report):
    """The space-pan keydown/keyup/blur listeners included: left behind, each
    one pins the whole workspace -- canvas, document, undo history -- and every
    visit to the canvas adds another set."""
    assert not _load_problems(report), "\n".join(report["problems"])
    assert report["leftAfterDestroy"] == []


def test_space_is_not_cancelled_once_the_canvas_has_gone(report):
    """Space on a focused button presses it. After the canvas is unmounted
    nothing of the canvas may still be cancelling that keystroke."""
    assert not _load_problems(report), "\n".join(report["problems"])
    assert report["spacePreventedAfterDestroy"] is False


def test_destroy_unregisters_the_page_as_a_drop_zone(report):
    """The desktop's native file drop zone registered for the page is removed
    with it, rather than kept in core's registry for a detached element."""
    assert not _load_problems(report), "\n".join(report["problems"])
    assert report["droppedZones"] == 1
    assert report["detachedZones"] == report["droppedZones"]
