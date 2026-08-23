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

When a stage intentionally changes routes, regenerate with:

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
ADDON_ONLY_IMPORTS = ("anndata", "h5py", "plexora.plugins.cell_explorer",
                      "plexora.plugins.figure_builder",
                      "plexora.plugins.gating", "plexora.plugins.roi")


def _probe(plugins, data_path, tool=None):
    """Boundary description of a fresh interpreter with the given plugins active."""
    env = {
        **os.environ,
        "PLEXORA_PLUGINS": plugins,
        "PLEXORA_PROBE_DATA_PATH": str(data_path),
        "PLEXORA_PROBE_TOOL": tool or (plugins.split(",")[0] if plugins else "gating"),
    }
    result = subprocess.run(
        [sys.executable, "-m", "tests._plugin_boundary_probe"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"probe failed for {plugins!r}:\n{result.stderr}")
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


@pytest.fixture(scope="module")
def roi(tmp_path_factory):
    return _probe("roi", tmp_path_factory.mktemp("roi"))


@pytest.fixture(scope="module")
def cell_explorer(tmp_path_factory):
    return _probe("cell_explorer", tmp_path_factory.mktemp("cell_explorer"))


@pytest.fixture(scope="module")
def figure_builder(tmp_path_factory):
    return _probe("figure_builder", tmp_path_factory.mktemp("figure_builder"))


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
    assert "/plugins/gating/get_saved_gating_list" in gating_routes
    assert "/plugins/gating/save_gating_list" in gating_routes
    # Nine API routes plus the blueprint's own static route: a plugin serves
    # its client assets out of its own directory rather than core's.
    assert len(gating_routes) == 10
    assert "/plugins/gating/static/<path:filename>" in gating_routes


def test_plugin_routes_are_namespaced(gating):
    """Every plugin route sits under its own prefix, so a plugin cannot shadow
    a core route or another plugin's, whatever it names its endpoints."""
    core_paths = {"/health", "/", "/upload_page"}
    for route in gating["routes"]:
        path = route.split(" ", 1)[1]
        if "gat" in path.lower():
            assert path.startswith("/plugins/gating/"), f"un-namespaced plugin route: {path}"
            assert path not in core_paths


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
    # Three mount points -- the sidebar slot, the legacy slot and the workspace
    # split slot -- all stamped with the (empty) active tool. They are static
    # markup, present on every build; what a plugin changes is what gets
    # rendered INTO them, which is what the next assertion covers.
    assert [mount for _, mount in page["tool_mounts"]] == ["", "", ""]


def test_gating_page_mounts_its_panels_and_scripts(gating):
    page = gating["pages"]["viewer_tool"]
    scripts = _asset_paths(page["scripts"])
    assert any(s.endswith("csvGatingList.js") for s in scripts)
    assert any(s.endswith("gatingSidebarController.js") for s in scripts)
    assert page["flask_variables"]["active_tool"] == "gating"
    assert page["flask_variables"]["available_tools"] == [
        {"label": "Thresholding", "name": "gating"}
    ]
    assert [mount for _, mount in page["tool_mounts"]] == ["gating", "gating", "gating"]


def test_core_page_loads_no_plugin_stylesheets(core):
    """index.html used to link gating.css unconditionally, so a core build
    shipped an addon's stylesheet. Stylesheets are now declared by the plugin
    (Plugin.styles) and emitted only when its tool is open."""
    leaked = [p for p in _asset_paths(core["pages"]["viewer_tool"]["styles"]) if "gating" in p.lower()]
    assert leaked == []


def test_plugin_assets_are_cache_busted_by_plugin_version(gating):
    """Asset URLs carry the plugin's own version, so the eager path (base.html)
    and the lazy path (tool_routes) cannot drift apart -- they previously kept
    hand-typed ?v= strings that had to be edited in two places."""
    from plexora.plugins.gating import VERSION

    plugin_assets = [
        u for u in gating["pages"]["viewer_tool"]["scripts"] + gating["pages"]["viewer_tool"]["styles"]
        if "/plugins/gating/static/" in u
    ]
    assert plugin_assets
    assert all(u.endswith(f"?v={VERSION}") for u in plugin_assets), plugin_assets


# --------------------------------------------------------------------------
# The second plugin. Everything above was written while gating was the only
# one, so most of it could have been passing by describing "the plugin" rather
# than "a plugin" -- these are the same assertions asked of a different one.
# --------------------------------------------------------------------------

def test_roi_build_installs_its_routes(roi):
    roi_routes = {r.split(" ", 1)[1] for r in roi["routes"] if "/plugins/roi/" in r}
    assert "/plugins/roi/api/state" in roi_routes
    assert "/plugins/roi/api/operations" in roi_routes
    assert "/plugins/roi/static/<path:filename>" in roi_routes
    for path in roi_routes:
        assert path.startswith("/plugins/roi/"), f"un-namespaced plugin route: {path}"


def test_installing_roi_only_adds_routes(core, roi):
    missing = sorted(set(core["routes"]) - set(roi["routes"]))
    assert missing == [], f"installing roi removed or altered core routes: {missing}"


def test_a_roi_build_does_not_pull_in_gating(roi):
    """Plugins are independent of each other, not just of core. ROI copies
    gating's zarr-writing approach rather than importing it, precisely so a
    build with one and not the other works."""
    assert roi["imported"]["plexora.plugins.gating"] is False


def test_roi_needs_nothing_of_the_project_but_an_image(roi):
    """The claim the whole plugin rests on. The probe's datasource is a CSV
    project, but what matters is that ROI declares no requirements at all -- so
    it is offered, and opens, on a project that is an image and nothing else."""
    from plexora.plugins.roi import PLUGIN

    assert PLUGIN.requires.missing_from({"imageData": []}) == []
    assert PLUGIN.requires.satisfied_by({"imageData": []})
    assert PLUGIN.owns_cell_layer is False
    page = roi["pages"]["viewer_tool"]
    assert page["flask_variables"]["active_tool"] == "roi"
    # ROI declares one panel, so only the sidebar slot is filled -- the legacy
    # mount carries the active tool's name (index.html stamps it on both) but
    # renders nothing.
    assert page["flask_variables"]["active_tool_panels"] == {"tool_panel_slot": "roi/panel.html"}
    assert "roi_panel_section" in page["ids"]


def test_roi_page_loads_only_its_own_assets(roi):
    page = roi["pages"]["viewer_tool"]
    assets = _asset_paths(page["scripts"] + page["styles"])
    assert any(a.endswith("roiSidebarController.js") for a in assets)
    assert [a for a in assets if "gating" in a.lower()] == []


# --------------------------------------------------------------------------
# The third plugin, and the first one whose whole purpose is the cell layer.
# The assertions above are asked of it too; these are the ones only it raises.
# --------------------------------------------------------------------------

def test_cell_explorer_installs_its_routes(cell_explorer):
    routes = {r.split(" ", 1)[1] for r in cell_explorer["routes"]
              if "/plugins/cell_explorer/" in r}
    assert "/plugins/cell_explorer/api/variables" in routes
    assert "/plugins/cell_explorer/api/values" in routes
    assert "/plugins/cell_explorer/api/state" in routes
    assert "/plugins/cell_explorer/static/<path:filename>" in routes
    for path in routes:
        assert path.startswith("/plugins/cell_explorer/"), f"un-namespaced route: {path}"


def test_installing_cell_explorer_only_adds_routes(core, cell_explorer):
    missing = sorted(set(core["routes"]) - set(cell_explorer["routes"]))
    assert missing == [], f"installing cell_explorer removed core routes: {missing}"


def test_a_cell_explorer_build_pulls_in_neither_other_plugin(cell_explorer):
    """Plugins are independent of each other, not just of core. Cell Explorer
    reads metadata through plexora.api exactly as an outside package would, so a
    build with it and neither of the others works."""
    assert cell_explorer["imported"]["plexora.plugins.gating"] is False
    assert cell_explorer["imported"]["plexora.plugins.roi"] is False


def test_cell_explorer_claims_the_cell_layer(cell_explorer):
    """The claim IS the plugin. Without it nothing hands it the layer, and the
    panel works while the image never changes."""
    from plexora.plugins.cell_explorer import PLUGIN

    assert PLUGIN.owns_cell_layer is True


def test_cell_explorer_needs_a_table_but_not_a_mask():
    """The requirement that decides how many projects can use this. Either a
    segmentation mask or x/y coordinates is enough to draw cells, and `Requires`
    cannot express "one or the other" -- so both are optional and the panel
    checks. Demanding the mask would rule out every project that has
    coordinates and no segmentation."""
    from plexora.plugins.cell_explorer import PLUGIN

    needed = {r.key for r in PLUGIN.requires.missing_from({"imageData": []})}
    assert "table" in needed
    assert "segmentation" not in needed


def test_cell_explorer_page_mounts_its_panel_and_scripts(cell_explorer):
    page = cell_explorer["pages"]["viewer_tool"]
    scripts = _asset_paths(page["scripts"])
    assert any(s.endswith("cellExplorerSidebarController.js") for s in scripts)
    assert any(s.endswith("cellExplorerColors.js") for s in scripts)
    assert page["flask_variables"]["active_tool"] == "cell_explorer"
    assert "cell_explorer_panel_section" in page["ids"]


def test_cell_explorer_page_loads_only_its_own_assets(cell_explorer):
    assets = _asset_paths(cell_explorer["pages"]["viewer_tool"]["scripts"]
                          + cell_explorer["pages"]["viewer_tool"]["styles"])
    assert [a for a in assets if "gating" in a.lower()] == []
    assert [a for a in assets if "/roi" in a.lower()] == []


# --------------------------------------------------------------------------
# The fourth plugin, and the first one that is not only a tool. Figure Builder
# has pages of its own and an entry in core's menus, which is a kind of
# extension none of the others exercise.
# --------------------------------------------------------------------------

def test_figure_builder_installs_its_routes(figure_builder):
    routes = {r.split(" ", 1)[1] for r in figure_builder["routes"]
              if "/plugins/figure_builder/" in r}
    assert "/plugins/figure_builder/api/figures" in routes
    assert "/plugins/figure_builder/api/figures/<figure_id>" in routes
    assert "/plugins/figure_builder/figures" in routes
    assert "/plugins/figure_builder/figure/<figure_id>" in routes
    assert "/plugins/figure_builder/static/<path:filename>" in routes
    for path in routes:
        assert path.startswith("/plugins/figure_builder/"), f"un-namespaced route: {path}"


def test_installing_figure_builder_only_adds_routes(core, figure_builder):
    missing = sorted(set(core["routes"]) - set(figure_builder["routes"]))
    assert missing == [], f"installing figure_builder removed core routes: {missing}"


def test_a_figure_builder_build_pulls_in_no_other_plugin(figure_builder):
    assert figure_builder["imported"]["plexora.plugins.gating"] is False
    assert figure_builder["imported"]["plexora.plugins.roi"] is False
    assert figure_builder["imported"]["plexora.plugins.cell_explorer"] is False


def test_figure_builder_needs_nothing_of_the_project_but_an_image():
    """Capturing a field needs the image and nothing else. A table requirement
    would have ruled out every image-only project, which is exactly the kind of
    project a quick figure gets made from."""
    from plexora.plugins.figure_builder import PLUGIN

    assert PLUGIN.requires.missing_from({"imageData": []}) == []
    assert PLUGIN.requires.satisfied_by({"imageData": []})
    assert PLUGIN.owns_cell_layer is False


def test_a_plugin_can_add_an_entry_to_a_core_menu(figure_builder):
    """The extension the library needs. Figure Builder's home is a page rather
    than a tool panel -- a figure spans datasources -- so the Tools menu, which
    is built per project and empty when nothing is open, cannot lead to it."""
    page = figure_builder["pages"]["open_project"]
    assert "nav_figure_builder_figures" in page["ids"]
    menus = {item["menu"] for item in page["flask_variables"]["plugin_nav_items"]}
    assert menus == {"file", "open_project"}


def test_a_core_build_renders_no_plugin_menu_entries(core):
    """The other half: with nothing installed the Open Project page carries no
    tab strip and the File menu gains no items, so this extension costs a
    core-only build nothing at all."""
    page = core["pages"]["open_project"]
    assert page["flask_variables"]["plugin_nav_items"] == []
    assert [i for i in page["ids"] if i.startswith("nav_figure")] == []


def test_figure_builder_page_mounts_its_scripts_and_no_sidebar_panel(figure_builder):
    """The one plugin here with nothing in the tool column.

    Its controls are on the image (figureCaptureDock builds them, because core
    has no slot over the viewer) and on the canvas beside it, so it declares no
    `tool_panel_slot` at all -- which is what stops the sidebar growing an empty
    card with a header, an eye and an X for a panel that does not exist."""
    page = figure_builder["pages"]["viewer_tool"]
    scripts = _asset_paths(page["scripts"])
    assert any(s.endswith("figureSidebarController.js") for s in scripts)
    assert any(s.endswith("figureDocumentState.js") for s in scripts)
    assert any(s.endswith("figureCaptureBoxes.js") for s in scripts)
    assert page["flask_variables"]["active_tool"] == "figure_builder"
    assert "figure_builder_panel_section" not in page["ids"]
    assert "tool_panel_slot" not in page["flask_variables"]["active_tool_panels"]


def test_a_plugin_can_fill_the_second_workspace(figure_builder):
    """The other extension this plugin needed: a slot beside the image for a
    tool that composes rather than inspects. Static markup on every build --
    what a plugin changes is what is rendered into it."""
    core_page = figure_builder["pages"]["viewer"]
    assert "workspace_split_slot" in core_page["ids"]

    page = figure_builder["pages"]["viewer_tool"]
    assert page["flask_variables"]["active_tool_panels"] == {
        "workspace_split_slot": "figure_builder/split_panel.html",
    }
    # The canvas markup really landed in it, rather than the slot merely being
    # named in the descriptor.
    assert "fb_page_surface" in page["ids"]


def test_the_second_workspace_is_empty_without_a_plugin_to_fill_it(core):
    """It costs a core-only build one hidden div and nothing else -- which is
    what makes adding it to a shared template acceptable at all."""
    page = core["pages"]["viewer"]
    assert "workspace_split_slot" in page["ids"]
    assert "fb_page_surface" not in page["ids"]


def test_core_route_inventory_matches_golden(core):
    _check_golden("core", core)


def test_figure_builder_route_inventory_matches_golden(figure_builder):
    _check_golden("figure_builder", figure_builder)


def test_gating_route_inventory_matches_golden(gating):
    _check_golden("gating", gating)


def test_roi_route_inventory_matches_golden(roi):
    _check_golden("roi", roi)


def test_cell_explorer_route_inventory_matches_golden(cell_explorer):
    _check_golden("cell_explorer", cell_explorer)
