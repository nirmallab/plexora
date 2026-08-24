"""Opening a tool must put the same assets on the page whichever way it opens.

There are two ways a tool's panel reaches the browser:

  eager -- `/<datasource>?tool=gating`, where base.html renders
           `data.active_tool_styles` and `data.active_tool_scripts` into <head>.
  lazy  -- the Tools menu, where toolLoader.js fetches
           `/<datasource>/tools/<tool>/panel` and injects what it gets.

The regression: `tool_panel()` returned `fragments`, `scripts` AND `styles` from
the day it was written, and toolLoader.js injected the first two. The plugin's
stylesheet was dropped on the floor on every lazy open.

It stayed invisible for as long as gating's appearance lived in core's
viewer.css, which index.html links unconditionally. Moving those ~150 lines into
the plugin's own gating.css -- where a plugin's CSS belongs, and what
test_plugin_css_boundary.py exists to enforce -- is what made it show: the panel
rendered raw, with the hidden file input appearing as a bare "Choose File"
button. So the two changes were each correct and the bug was in neither; it was
in the client's half of a contract nothing checked.

base.html's own comment claims the eager path "cannot drift out of sync with the
lazy path in tool_routes.py". That was true of the two servers and false of the
client, which is the gap these tests close: the second half asserts the two
payloads are equal instead of asserting it in prose, and the first half runs the
client to see what it does with one.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import plexora
from plexora.server import plugins as plugin_registry

from tests.helpers import ALL_CONFIRMED, csv_spec, entry

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "tool_assets_probe.mjs"
TOOL_LOADER = REPO_ROOT / "plexora" / "client" / "src" / "js" / "views" / "toolLoader.js"


def _run_probe(source=None):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    command = [node, str(PROBE)] + (["--source", str(source)] if source else [])
    proc = subprocess.run(command, capture_output=True, text=True, cwd=REPO_ROOT, timeout=60)
    # The probe reports on stderr so its own diagnostics never mix with output.
    try:
        return proc.returncode, json.loads(proc.stderr)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken probe
        pytest.fail(f"probe produced no report\nstdout:{proc.stdout}\nstderr:{proc.stderr}")


@pytest.fixture(scope="module")
def probe_report():
    return _run_probe()


def test_opening_a_tool_loads_its_stylesheets(probe_report):
    """The regression itself. Asserted on what reached the document rather than
    on the source text, because the payload was always correct and the loader
    always ran without error -- the only observable difference was a <link> that
    was never appended."""
    returncode, report = probe_report
    assert not report["missing"]["styles"], (
        f"stylesheets the server sent that never reached the page: "
        f"{report['missing']['styles']}\n"
        "The panel renders unstyled -- every rule in the plugin's own CSS is "
        "silently absent, including the ones that hide elements."
    )
    assert returncode == 0


def test_opening_a_tool_still_loads_its_scripts(probe_report):
    """The half that already worked, kept under the same probe so a change to
    the asset handling cannot fix one and break the other."""
    _, report = probe_report
    assert not report["missing"]["scripts"]


def test_the_panel_markup_is_still_injected(probe_report):
    """Awaiting the stylesheets happens before the fragments go in; a mistake
    there would leave the slots empty and the tool blank."""
    _, report = probe_report
    assert report["fragments_injected"] == 2


def test_the_probe_can_actually_fail(tmp_path):
    """A probe that passes whatever the loader does is worth nothing. Run it
    against the loader with the stylesheet step removed -- the code exactly as
    it shipped -- and it must report the miss."""
    mutated = tmp_path / "toolLoader.js"
    source = TOOL_LOADER.read_text(encoding="utf-8")
    without_styles = source.replace(
        "await Promise.all((payload.styles || []).map(loadStyle));", ""
    )
    assert without_styles != source, "the line this test mutates has moved or been renamed"
    mutated.write_text(without_styles, encoding="utf-8")

    returncode, report = _run_probe(mutated)

    assert returncode == 1
    assert report["missing"]["styles"], "the probe cannot see a dropped stylesheet"
    # Only the styles go missing: the mutation is the defect and nothing else.
    assert not report["missing"]["scripts"]


# --------------------------------------------------------------------------
# The two paths must send the same thing
# --------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    """A project that satisfies everything gating declares, so opening the tool
    resolves to OPEN rather than a request for the missing pieces."""
    (tmp_path / "config.json").write_text(
        json.dumps({"proj": entry("proj", dataset=csv_spec(
            tmp_path / "cells.csv", markers=["CD3"], metadata=["CellID"]),
            cell_layer="centroids", confirmed=ALL_CONFIRMED)}),
        encoding="utf-8",
    )
    return plexora.app.test_client()


def _gating():
    plugin = plugin_registry.find(plexora.app, "gating")
    if plugin is None:  # pragma: no cover - core-only build
        pytest.skip("gating is not installed")
    return plugin


def test_the_lazy_payload_carries_every_asset_the_plugin_declares(client):
    payload = client.get("/proj/tools/gating/panel").get_json()
    plugin = _gating()

    assert payload["scripts"] == plugin.asset_urls("scripts")
    assert payload["styles"] == plugin.asset_urls("styles")
    assert payload["styles"], "gating declares a stylesheet; the payload dropped it"


def test_the_eager_page_links_the_same_stylesheets_the_lazy_payload_sends(client):
    """Same assets, both routes in. The comparison is the point: either path
    silently shipping less than the other is what the user sees as a tool that
    looks right one way and broken the other."""
    payload = client.get("/proj/tools/gating/panel").get_json()
    page = client.get("/proj?tool=gating").data.decode("utf-8")

    for url in payload["styles"]:
        assert f'<link rel="stylesheet" href="{url}">' in page, (
            f"the lazy path sends {url} but the eager page does not link it"
        )
    for url in payload["scripts"]:
        assert f'<script src="{url}"></script>' in page


def test_a_tool_with_no_sidebar_panel_still_opens_lazily(client):
    """Figure Builder is the one plugin here with nothing in the tool column.

    Its controls are all on the image (figureCaptureDock builds them, because
    core has no slot over the viewer) and its canvas is a page of its own, so it
    declares no panels whatsoever. The lazy path has to cope: `openTool` mounts
    one fragment per slot the payload names and derives the tool's slot list
    from those keys, so the payload must name NOTHING and still deliver the
    scripts. A stray empty `tool_panel_slot` entry here is a card in the sidebar
    with nothing in it.
    """
    plugin = plugin_registry.find(plexora.app, "figure_builder")
    if plugin is None:  # pragma: no cover - core-only build
        pytest.skip("figure_builder is not installed")

    payload = client.get("/proj/tools/figure_builder/panel").get_json()

    assert payload["fragments"] == {}
    # The assets still arrive: the whole tool is JavaScript over the image.
    assert payload["scripts"] == plugin.asset_urls("scripts")
    assert payload["styles"] == plugin.asset_urls("styles")
