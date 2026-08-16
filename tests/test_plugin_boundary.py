"""The core/plugin boundary, asserted rather than hoped for.

This is the regression net the plugin extraction is built on top of. It pins
three things that every later stage must preserve:

1. A core build installs no addon routes and does not pay an addon's import
   cost (anndata/h5py stay out of sys.modules).
2. Installing a plugin is purely additive -- it may not remove, rename or
   otherwise disturb a single core route.
3. The full route inventory of both builds matches a checked-in golden file,
   so an accidental route change anywhere shows up as a diff.

Each probe runs in a SUBPROCESS. That is not incidental: plexora.create_app()
is single-shot per interpreter (its route modules register via import side
effects, so a second call yields an app with 1 route), and "was this module
imported?" is a process-global question. See tests/_plugin_boundary_probe.py.

When a stage intentionally changes routes -- S4a moves gating under
/plugins/gating -- regenerate with:

    PLEXORA_UPDATE_GOLDEN=1 pytest tests/test_plugin_boundary.py

and review the resulting diff as part of that stage's change.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = Path(__file__).resolve().parent / "golden"

# Dependencies that exist ONLY to serve an addon. sklearn/scipy are deliberately
# absent from this list: core's auto-contrast GMM imports them, so they are
# present in every build and asserting on them would be asserting a falsehood.
ADDON_ONLY_IMPORTS = ("anndata", "h5py", "plexora.server.modules.gating")


def _probe(active_module, data_path):
    """Boundary description of a fresh interpreter with the given module active."""
    env = {
        **os.environ,
        "PLEXORA_ACTIVE_MODULE": active_module,
        "PLEXORA_PROBE_DATA_PATH": str(data_path),
    }
    result = subprocess.run(
        [sys.executable, "-m", "tests._plugin_boundary_probe"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"probe failed for {active_module!r}:\n{result.stderr}")
    return json.loads(result.stdout)


def _check_golden(name, observed):
    path = GOLDEN_DIR / f"boundary_{name}.json"
    if os.environ.get("PLEXORA_UPDATE_GOLDEN"):
        GOLDEN_DIR.mkdir(exist_ok=True)
        path.write_text(json.dumps(observed, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        pytest.skip(f"regenerated {path.name}")
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert observed["routes"] == expected["routes"]
    assert observed["imported"] == expected["imported"]
    assert observed["pages"] == expected["pages"]


@pytest.fixture(scope="module")
def core(tmp_path_factory):
    return _probe("", tmp_path_factory.mktemp("core"))


@pytest.fixture(scope="module")
def gating(tmp_path_factory):
    return _probe("gating", tmp_path_factory.mktemp("gating"))


def test_core_build_installs_no_gating_routes(core):
    assert [r for r in core["routes"] if "gat" in r.lower()] == []


def test_core_build_does_not_import_addon_dependencies(core):
    still_imported = [m for m in ADDON_ONLY_IMPORTS if core["imported"][m]]
    assert still_imported == [], (
        f"core build imported addon-only modules: {still_imported}. "
        "The registry's lazy loaders exist precisely to prevent this."
    )


def test_core_build_still_serves_the_viewer(core):
    # Guards against the deceptive failure where a hollow app (1 route) passes
    # every "no gating" assertion above while serving nothing at all.
    assert core["route_count"] > 40


def test_gating_build_installs_its_routes(gating):
    gating_routes = {r.split(" ", 1)[1] for r in gating["routes"] if "gat" in r.lower()}
    assert "/get_saved_gating_list" in gating_routes
    assert "/save_gating_list" in gating_routes
    assert len(gating_routes) == 9


def test_installing_a_plugin_only_adds_routes(core, gating):
    """A plugin is additive. Every core route must survive its installation
    byte-identically -- same methods, same rule, nothing shadowed or renamed."""
    missing = sorted(set(core["routes"]) - set(gating["routes"]))
    assert missing == [], f"installing gating removed or altered core routes: {missing}"


def _asset_paths(urls):
    """Asset URLs minus their ?v= cache-bust suffix. The suffixes are hand-typed
    strings that happen to contain feature names ("?v=20260815_gating_fixes" is
    on dataLayer.js, a core file), so matching on them would report coupling
    that isn't there."""
    return [u.split("?", 1)[0] for u in urls]


def test_core_page_loads_no_plugin_scripts(core):
    page = core["pages"]["viewer_tool"]
    leaked = [p for p in _asset_paths(page["scripts"]) if "gating" in p.lower()]
    assert leaked == [], f"core viewer page pulled in plugin scripts: {leaked}"


def test_core_page_reports_no_active_tool(core):
    page = core["pages"]["viewer_tool"]
    # ?tool=gating was requested but nothing is installed, so the server must
    # refuse to activate it rather than rendering an inert panel.
    assert page["flask_variables"]["active_tool"] == ""
    assert page["flask_variables"]["available_tools"] == []
    assert [mount for _, mount in page["tool_mounts"]] == ["", ""]


def test_gating_page_mounts_its_panels_and_scripts(gating):
    page = gating["pages"]["viewer_tool"]
    scripts = _asset_paths(page["scripts"])
    assert any(s.endswith("csvGatingList.js") for s in scripts)
    assert any(s.endswith("gatingSidebarController.js") for s in scripts)
    assert page["flask_variables"]["active_tool"] == "gating"
    assert page["flask_variables"]["available_tools"] == [
        {"label": "Thresholding", "name": "gating"}
    ]
    assert [mount for _, mount in page["tool_mounts"]] == ["gating", "gating"]


@pytest.mark.xfail(
    reason="Known leak: index.html links gating.css unconditionally. Fixed in S6 "
    "when gating's assets move under the plugin; flip to a plain assert then.",
    strict=True,
)
def test_core_page_loads_no_plugin_stylesheets(core):
    leaked = [p for p in _asset_paths(core["pages"]["viewer_tool"]["styles"]) if "gating" in p.lower()]
    assert leaked == []


def test_core_route_inventory_matches_golden(core):
    _check_golden("core", core)


def test_gating_route_inventory_matches_golden(gating):
    _check_golden("gating", gating)
