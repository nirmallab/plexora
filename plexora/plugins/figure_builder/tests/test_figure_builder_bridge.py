"""Plugins describe themselves to a figure, and never the other way round.

Three plugins are involved and none of them imports another. They talk through
two DOM events, which means nothing in the Python suite can see whether they
actually agree -- the whole exchange happens in the browser.

The rule the probe holds is the one that is easiest to break by being helpful:
**a panel edit must never rewrite the project's own plugin settings.** ROI's
category visibility and Cell Explorer's palette are persisted preferences, so
restoring them would edit the user's project because they opened a figure panel.
The bridges restore only what is transient and report the rest, and "partial"
has to be a real answer rather than a blanket "ok".

    node tests/js/figure_bridge_probe.mjs
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "figure_bridge_probe.mjs"
PLUGINS_DIR = REPO_ROOT / "plexora" / "plugins"


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


def test_the_bridge_works_in_both_directions(report):
    returncode, data = report
    assert not data["problems"], "\n".join(data["problems"])
    assert returncode == 0


def test_a_contribution_is_state_and_never_a_legend(report):
    """Both bridges used to compute a legend at capture time and Figure Builder
    stored it, so a panel could print a row per phenotype.

    That went. The export re-renders CHANNELS from the source and reproduces no
    overlay at all (see `render.missing_overlays`), so those rows keyed a
    picture the exported figure did not contain -- and whether a figure had them
    depended on which plugins happened to be installed the day it was captured.
    Asserted as an absence, because "a bridge quietly started sending legends
    again" is how this comes back.
    """
    _, data = report
    for name in ("roi", "cell_explorer"):
        assert set(data["contributions"][name]) == {"version", "state"}


def test_restoring_a_panel_does_not_rewrite_the_projects_settings(report):
    """Spec §28, and the reason both bridges report rather than apply: the live
    viewer temporarily HOSTS a panel's scene, and hosting is not owning."""
    _, data = report
    assert data["report"] == {"roi": "partial", "cell_explorer": "partial"}


def test_neither_plugin_imports_the_other_or_figure_builder():
    """The boundary the events exist to preserve. A build with ROI and no Cell
    Explorer -- or with either and no Figure Builder -- has to work, which stops
    being true the moment one file names another plugin's class."""
    offenders = {}
    for plugin in ("roi", "cell_explorer"):
        bridge = next((PLUGINS_DIR / plugin / "static").glob("*FigureBridge.js"), None)
        assert bridge is not None, f"{plugin} ships no figure bridge"
        text = bridge.read_text(encoding="utf-8")
        # Naming another plugin in a STRING is how the events are addressed and
        # is the point; naming one as an identifier is the import this forbids.
        hits = re.findall(r"\b(Roi[A-Z]\w*|FigureBuilder\w*|FigureScene|FigureSchema)\b", text)
        if plugin == "cell_explorer":
            hits = [h for h in hits if not h.startswith("CellExplorer")]
        if hits:
            offenders[plugin] = sorted(set(hits))
    assert not offenders, (
        f"a figure bridge reaches for another plugin's classes: {offenders}. "
        "The bridge is two DOM events precisely so no build combination can "
        "break."
    )


def test_every_bridge_is_declared_by_the_plugin_that_ships_it():
    """A file in static/ that is not in PLUGIN.scripts is a file the browser
    never fetches -- and the failure is invisible, because everything
    server-side is unaffected."""
    from plexora.plugins.cell_explorer import PLUGIN as CELL_EXPLORER
    from plexora.plugins.roi import PLUGIN as ROI

    assert "roiFigureBridge.js" in ROI.scripts
    assert "cellExplorerFigureBridge.js" in CELL_EXPLORER.scripts
