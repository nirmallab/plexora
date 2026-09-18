"""The plugin's client comes up when loaded the way the server loads it.

Nothing else here runs this plugin's JavaScript. The Python suite renders the
panel's HTML and stops; `node --check` sees syntax only. So the entire client can
be broken -- a file missing from `PLUGIN.scripts`, a constructor that throws the
moment it runs, a registration that never happens -- while every server-side test
passes, and the only symptom is a panel that appears and does nothing.

The file list is read off the descriptor rather than restated here, so what gets
exercised is what the server will actually send.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from plexora.plugins.transcripts import PLUGIN

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "transcripts_boot_probe.mjs"


def _run(scripts):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    proc = subprocess.run(
        [node, str(PROBE), *scripts],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
    )
    try:
        return proc.returncode, json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")


def test_the_declared_scripts_bring_the_client_up():
    returncode, report = _run(PLUGIN.scripts)

    assert returncode == 0, json.dumps(report, indent=2)
    assert [entry["name"] for entry in report["registered"]] == ["transcripts"]
    assert report["controller"] is not None


def test_leaving_a_file_out_is_caught():
    """The failure this probe exists for: a file missing from the tuple loads a
    plugin whose panel renders and does nothing, with every server-side test
    still green."""
    returncode, report = _run([s for s in PLUGIN.scripts if s != "transcriptLayer.js"])

    assert returncode == 1
    assert report["problems"], report


def test_the_panel_template_ships_with_the_plugin():
    template = REPO_ROOT / "plexora" / "plugins" / "transcripts" / "templates" \
        / "transcripts" / "panel.html"

    assert template.is_file()
    assert PLUGIN.panels == {"tool_panel_slot": "transcripts/panel.html"}


def test_every_declared_asset_exists():
    """A declared file that is not there is a 404 in the browser console and a
    plugin that half-loads -- and the descriptor is the only place the two lists
    could disagree."""
    static = REPO_ROOT / "plexora" / "plugins" / "transcripts" / "static"

    for name in (*PLUGIN.scripts, *PLUGIN.styles):
        assert (static / name).is_file(), name


def test_the_plugin_claims_no_cell_layer():
    """It colours points of its own. Claiming the cell layer would evict
    whichever plugin legitimately holds it -- the shader has one range table --
    in exchange for nothing."""
    assert PLUGIN.owns_cell_layer is False


def test_the_plugin_requires_nothing():
    """A transcript layer needs the image and its own points file. Neither a
    feature table nor a segmentation is part of drawing a molecule where it was
    detected, and requiring one would rule out exactly the projects this is for."""
    assert PLUGIN.requires.table is False
    assert PLUGIN.requires.segmentation is False
    assert PLUGIN.requires.optional == ()
