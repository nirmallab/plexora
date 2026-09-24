"""Core's own tool: Rotate & Flip, one card for both.

It is a `Plugin` descriptor with no package, offered by the registry beside
whatever is installed, so every surface a plugin tool reaches -- the panel
route, `?tool=`, the card, the requirements gate -- reaches them unchanged.
What this holds:

  * **They exist without any plugin.** A core-only build (`PLEXORA_PLUGINS=""`)
    still turns and mirrors the image.
  * **They are offered in View, not Tools.** The split happens server-side on
    `Plugin.menu`; `describe()` -- which three other tests pin exactly -- does
    not grow a field for it.
  * **They open like any tool, and fetch nothing.** Their scripts are core's,
    already on the page, so the panel route hands the loader no assets.
  * **Boot does not set them up against a panel that is not there.** Their
    definitions are registered on every page; main.js activates a `lazy` one
    only when `?tool=` staged its panel.
"""

import json
import re
from pathlib import Path

import pytest

import plexora
from plexora.api.plugin import Plugin
from plexora.server import core_tools
from plexora.server import plugins as registry

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENT = REPO_ROOT / "plexora" / "client"


class FakeApp:
    def __init__(self):
        self.config = {}
        self.registered = []

    def register_blueprint(self, blueprint, url_prefix):
        self.registered.append((blueprint.name, url_prefix))


@pytest.fixture
def client(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps({"alpha": {"image_kind": "ome_tiff", "dataset": None}}),
        encoding="utf-8",
    )
    return plexora.app.test_client()


def _menu(page: str, marker: str) -> str:
    start = page.index(marker)
    return page[start:page.index("</li>", start)]


def test_a_core_only_build_still_has_both():
    app = FakeApp()
    registry.install(app, [])
    assert registry.installed(app) == []
    assert registry.find(app, "rotate") is core_tools.ROTATE
    names = {p.name for p in registry.tools_for(app, {"image_kind": "ome_tiff"})}
    assert names == {"rotate"}
    assert {p.name for p in registry.ready_tools(app, {"image_kind": "ome_tiff"})} == names


@pytest.mark.parametrize("kind", ["rgb", "blank"])
def test_not_offered_where_there_is_no_view_to_turn(kind):
    app = FakeApp()
    registry.install(app, [])
    assert registry.tools_for(app, {"image_kind": kind}) == []


def test_nothing_else_the_registry_answers_includes_them():
    """Menus of plugin pages, layer sections and asset loading keep reading
    the installed plugins; core's tools have none of those."""
    app = FakeApp()
    registry.install(app, [])
    assert registry.nav_items(app) == []
    assert registry.layer_sections_for(app, {"image_kind": "ome_tiff"}) == []


def test_rotate_and_flip_are_one_tool():
    """They edit one state and answer one question, so they are one menu row
    and one card -- not two cards that fold each other away."""
    assert [tool.name for tool in core_tools.CORE_TOOLS] == ["rotate"]
    assert core_tools.ROTATE.label == "Rotate & Flip"
    app = FakeApp()
    registry.install(app, [])
    assert registry.find(app, "flip") is None


def test_they_are_view_menu_tools_with_nothing_to_fetch():
    for tool in core_tools.CORE_TOOLS:
        assert tool.menu == "view"
        assert tool.scripts == () and tool.styles == ()
        assert tool.blueprint_factory is None
        assert not tool.owns_cell_layer
        assert tool.panels == {"tool_panel_slot": f"tools/{tool.name}_panel.html"}
        assert (CLIENT / "templates" / tool.panels["tool_panel_slot"]).is_file()
        # No shortcut: mod+R reloads the page, and a flip needs no chord.
        assert tool.shortcut == ""


def test_describe_does_not_grow_a_menu_field():
    assert set(core_tools.ROTATE.describe()) == {"name", "label", "icon", "shortcut"}
    assert Plugin(name="x", label="X").menu == "tools"


def test_the_panel_route_serves_the_fragment_and_no_assets(client):
    response = client.get("/alpha/tools/rotate/panel")
    assert response.status_code == 200
    payload = response.get_json()
    fragment = payload["fragments"]["tool_panel_slot"]
    assert "rotate_panel_section" in fragment
    assert "flip_horizontal_button" in fragment and "flip_vertical_button" in fragment
    assert payload["scripts"] == [] and payload["styles"] == []


def test_a_tool_link_opens_the_card_on_load(client):
    page = client.get("/alpha?tool=rotate").get_data(as_text=True)
    assert 'data-tool-mount="rotate"' in page
    assert 'id="rotate_panel_section"' in page
    assert 'id="flip_horizontal_button"' in page


def test_they_are_listed_in_view_and_not_in_tools(client):
    page = client.get("/alpha").get_data(as_text=True)
    view = _menu(page, '<div class="dropdown-menu view-menu"')
    assert 'data-tool="rotate"' in view
    assert 'data-tool="flip"' not in view
    assert "Rotate &amp; Flip" in view or "Rotate & Flip" in view
    if 'id="navbarToolsDropdown"' in page:
        tools = _menu(page, 'id="navbarToolsDropdown"')
        assert 'data-tool="rotate"' not in tools and 'data-tool="flip"' not in tools
    # Their scripts are always on the viewer page.
    assert "js/services/viewTransform.js?v=" in page
    assert "js/views/viewTransformTools.js?v=" in page


def test_boot_activates_a_lazy_definition_only_when_staged():
    main = (CLIENT / "src" / "js" / "main.js").read_text(encoding="utf-8")
    block = main[main.index("const pluginDefs = "):]
    block = block[:block.index(";") + 1]
    assert "!definition.lazy" in block
    assert '[data-tool-mount="${definition.name}"]' in block


def test_boot_adopts_the_saved_orientation_before_any_channel():
    main = (CLIENT / "src" / "js" / "main.js").read_text(encoding="utf-8")
    assert re.search(r"Promise\.all\(\[\s*d3\.json\(", main), "fetched beside /config"
    adopt = main.index("seaDragonViewer.viewTransform?.adopt(savedViewTransform)")
    assert main.index("new ImageViewer(config") < adopt < main.index("seaDragonViewer.init(")


def test_a_card_that_draws_nothing_gets_no_eye():
    loader = (CLIENT / "src" / "js" / "views" / "toolLoader.js").read_text(encoding="utf-8")
    body = loader[loader.index("function buildCard(toolName, mount) {"):]
    body = body[:body.index("\n    }\n")]
    assert "?.hasLayer === false" in body
    assert "onToggle: drawsNothing" in body


def test_the_osd_rotate_and_flip_keys_are_vetoed():
    """OSD's own r/R/f turned the view unsaved, behind the cards' back."""
    viewer = (CLIENT / "src" / "js" / "views" / "imageViewer.js").read_text(encoding="utf-8")
    handler = viewer[viewer.index('this.viewer.addHandler("canvas-key"'):]
    handler = handler[:handler.index("});")]
    assert "code === 82" in handler and "code === 70" in handler
    assert "preventDefaultAction = true" in handler


def test_the_scale_bar_stops_clamping_to_a_turned_corner():
    """The bar keeps inside the image by clamping to OSD's bottom-right image
    corner, which OSD computes turned but not mirrored. On a turned view that
    walked the bar to the top of the screen, so a transform pins it to the
    viewer's corner and upright keeps today's placement."""
    viewer = (CLIENT / "src" / "js" / "views" / "imageViewer.js").read_text(encoding="utf-8")
    hook = viewer[viewer.index("this.viewTransform?.subscribe((state) => {"):]
    hook = hook[:hook.index("});")]
    assert "stayInsideImage: PlexoraViewTransform.isIdentity(state)" in hook
