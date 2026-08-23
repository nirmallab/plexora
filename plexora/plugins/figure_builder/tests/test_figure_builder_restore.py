"""A captured panel comes back as the view that produced it.

This is the claim the whole plugin rests on -- a panel is a reproducible viewer
scene, not a screenshot -- and it is the one nothing else in the suite can
check: restoring runs entirely in the browser, against a viewer and a sidebar
the Python tests never construct.

The second half is the guarantee that makes re-editing safe to offer at all.
Loading a panel's channels into the live viewer goes through the ordinary
setters, and those setters autosave -- so without the suspension seam, merely
LOOKING at a figure panel would permanently overwrite the channel settings the
user had chosen for that project, with nothing on screen to say so. That seam
lives in core (viewerSidebar.js), which is why it is asserted from both sides:
behaviourally through the probe, and structurally here.

    node tests/js/figure_restore_probe.mjs
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "figure_restore_probe.mjs"
VIEWER_SIDEBAR = REPO_ROOT / "plexora" / "client" / "src" / "js" / "views" / "viewerSidebar.js"


@pytest.fixture(scope="module")
def report():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    proc = subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60)
    try:
        return proc.returncode, json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")


def test_a_captured_scene_round_trips(report):
    returncode, data = report
    assert not data["problems"], "\n".join(data["problems"])
    assert returncode == 0


def test_the_project_channel_list_is_never_written_while_editing(report):
    """The restore drives the same setters a user's click drives, and those
    setters autosave. Every attempt has to land while persistence is
    suspended."""
    _, data = report
    assert data["saveAttempts"], "the fixture never attempted a save, so nothing was tested"
    assert all(attempt["suspended"] > 0 for attempt in data["saveAttempts"])


def test_the_restore_reports_what_it_could_not_do(report):
    """A panel captured with a plugin that is not open still shows its image and
    its channels. Saying which part is absent is more useful than refusing the
    whole restore -- and far more useful than restoring something plausible."""
    _, data = report
    assert data["restoreReport"]["plugins"] == {"roi": "ok", "cell_explorer": "skipped"}
    assert data["restoreReport"]["viewport"] is True


def test_the_restore_bridge_is_an_event_not_a_call(report):
    """Figure Builder never imports a plugin. The same rule ROI and Cell
    Explorer already follow between themselves, for the same reason: a build
    with one and not the other has to work."""
    _, data = report
    assert "plexora:figure-restore-state" in data["events"]


def test_core_carries_the_persistence_seam():
    """The structural half. The probe models this guard; this asserts the real
    one is still there, because a refactor that dropped it would leave the
    probe passing against a fixture and the product silently overwriting the
    user's channels."""
    source = VIEWER_SIDEBAR.read_text(encoding="utf-8")
    assert "suspendPersistence()" in source
    assert "resumePersistence()" in source
    assert "if (this._restoring || this._persistenceSuspended) return;" in source, (
        "scheduleSaveChannels no longer honours the suspension seam. Loading a "
        "figure panel into the viewer would overwrite the project's own saved "
        "channels."
    )
