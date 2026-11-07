"""The desktop app's "Browser rendering" (plan Phase 6; plexora/browser_rendering.py).

Off by default until the website's GA. With it off -- or anywhere but the
desktop app's server -- `/resource_routing` answers exactly what it always did,
so the page never chooses the `local-browser` routing and the shell grants no
file. With it on, the desktop's answer carries `local`: the project's image and
mask as the page may read them (`node_spec.describe(local=True)`), which the
page checks level by level before it reads a tile.
"""

import pytest

import plexora
from plexora import browser_rendering
from plexora.browser import node_spec

from tests.agent_fixtures import make_synthetic_project

pytest.importorskip("tifffile")


@pytest.fixture
def info(tmp_path):
    return make_synthetic_project(tmp_path)


@pytest.fixture
def client():
    return plexora.app.test_client()


@pytest.fixture
def desktop(monkeypatch):
    monkeypatch.setitem(plexora.app.config, "PLEXORA_DESKTOP", True)


@pytest.fixture
def not_desktop(monkeypatch):
    monkeypatch.setitem(plexora.app.config, "PLEXORA_DESKTOP", False)


# -- the setting ---------------------------------------------------------------

def test_it_is_off_until_someone_turns_it_on():
    assert browser_rendering.DEFAULT == "off"
    assert browser_rendering.mode() == "off"
    assert browser_rendering.set_mode("auto") == "auto"
    assert browser_rendering.mode() == "auto"
    browser_rendering.set_mode("off")
    assert browser_rendering.mode() == "off"
    with pytest.raises(ValueError):
        browser_rendering.set_mode("on")


def test_a_stored_value_it_does_not_know_reads_as_off():
    from plexora import paths

    paths.write_settings({"browser_rendering": "always"})
    assert browser_rendering.mode() == "off"


# -- what the page may read ----------------------------------------------------

def test_formats_are_named_from_the_path():
    assert node_spec.format_of("C:/a/b.ome.tif") == "ome-tiff"
    assert node_spec.format_of("/x/b.OME.TIFF") == "ome-tiff"
    assert node_spec.format_of("/x/b.tif") == "tiff"
    assert node_spec.format_of("/x/b.zarr") == "ome-zarr"
    assert node_spec.format_of("/x/b.zarr/0") == "ome-zarr"
    assert node_spec.format_of("/x/b.svs") == "svs"


def test_only_regular_local_files_are_local(tmp_path):
    here = tmp_path / "image.tif"
    here.write_bytes(b"x")
    assert node_spec.is_local_file(str(here))
    assert not node_spec.is_local_file(str(tmp_path))
    assert not node_spec.is_local_file("relative.tif")
    assert not node_spec.is_local_file(r"\\fileserver\lab\image.tif")
    assert not node_spec.is_local_file("//fileserver/lab/image.tif")
    assert not node_spec.is_local_file("s3://bucket/image.tif")
    assert not node_spec.is_local_file("node://o2/slide")
    assert not node_spec.is_local_file(str(tmp_path / "missing.tif"))


def test_the_desktop_reads_a_local_pyramid_and_mask_as_python_does(info):
    import tifffile

    spec = node_spec.describe(info["name"], local=True)
    assert spec["served"] is True, spec
    assert spec["path"] == info["image_path"] or spec["path"].endswith("image.ome.tif")
    assert spec["format"] == "ome-tiff"
    with tifffile.TiffFile(spec["path"]) as tif:
        levels = [list(level.shape) for level in tif.series[0].levels]
    assert spec["levels"] == levels[:len(spec["levels"])] and spec["levels"]
    labels = spec["labels"]
    assert labels["served"] is True, labels
    assert labels["format"] == "tiff"
    # The website's question about the same project is unchanged: these are
    # not files its page holds.
    website = node_spec.describe(info["name"])
    assert website["served"] is False
    assert website["reason"] == "the file is not one the page holds"


def test_a_file_on_a_share_stays_pythons(info, monkeypatch):
    from plexora.server.models import data_model

    data_model.ensure_loaded(info["name"])
    monkeypatch.setattr(data_model._providers.image, "_path", r"\\fileserver\lab\image.ome.tif")
    spec = node_spec.describe(info["name"], local=True)
    assert spec["served"] is False
    assert spec["reason"] == "not a file on this computer's own disks"


# -- /resource_routing ---------------------------------------------------------

def test_off_the_answer_is_what_it_always_was(info, client, desktop):
    answer = client.get(f"/resource_routing?datasource={info['name']}").get_json()
    assert answer == {"routes": {}}


def test_a_terminal_plexora_never_offers_it_even_with_the_setting_on(info, client, not_desktop):
    browser_rendering.set_mode("auto")
    answer = client.get(f"/resource_routing?datasource={info['name']}").get_json()
    assert answer == {"routes": {}}


def test_on_the_desktop_answer_carries_the_files_the_page_may_read(info, client, desktop):
    browser_rendering.set_mode("auto")
    answer = client.get(f"/resource_routing?datasource={info['name']}").get_json()
    assert answer["routes"] == {}
    local = answer["local"]
    assert local["setting"] == "auto"
    assert local["served"] is True and local["format"] == "ome-tiff"
    assert local["labels"]["served"] is True
    assert local["tile"] == [128, 128]
    # An unknown project is still the plain answer.
    assert client.get("/resource_routing?datasource=nope").get_json() == {"routes": {}}


# -- /desktop/info and Settings > Advanced -------------------------------------

def test_desktop_info_lists_the_active_paths_only_when_on(client, desktop):
    info = client.get("/desktop/info").get_json()
    assert info["browser_rendering"] == "off"
    assert "paths" not in info
    browser_rendering.set_mode("auto")
    info = client.get("/desktop/info").get_json()
    assert info["browser_rendering"] == "auto"
    assert info["paths"]["image_tiles"] == "browser"
    assert info["paths"]["label_tiles"] == "browser"
    assert info["paths"]["tables"] == "server"


def test_desktop_info_says_nothing_of_it_off_the_desktop(client, not_desktop):
    info = client.get("/desktop/info").get_json()
    assert "browser_rendering" not in info and "paths" not in info


def test_the_advanced_settings_read_and_write_the_setting(client, desktop):
    state = client.get("/settings/advanced").get_json()
    assert state["desktop"] is True and state["browser_rendering"] == "off"
    assert state["modes"] == ["auto", "off"] and state["paths"] == {}
    state = client.post("/settings/advanced", json={"browser_rendering": "auto"}).get_json()
    assert state["browser_rendering"] == "auto" and state["paths"]["image_tiles"] == "browser"
    assert browser_rendering.mode() == "auto"
    refused = client.post("/settings/advanced", json={"browser_rendering": "fast"})
    assert refused.status_code == 400
    assert browser_rendering.mode() == "auto"


def test_only_the_desktop_app_can_turn_it_on(client, not_desktop):
    refused = client.post("/settings/advanced", json={"browser_rendering": "auto"})
    assert refused.status_code == 409
    assert browser_rendering.mode() == "off"


def test_the_settings_page_shows_advanced_only_in_the_desktop_app(client, monkeypatch):
    monkeypatch.setitem(plexora.app.config, "PLEXORA_DESKTOP", True)
    page = client.get("/settings").get_data(as_text=True)
    assert 'id="settings_panel_advanced"' in page and 'data-section="advanced"' in page
    monkeypatch.setitem(plexora.app.config, "PLEXORA_DESKTOP", False)
    page = client.get("/settings").get_data(as_text=True)
    assert 'settings_panel_advanced' not in page and 'data-section="advanced"' not in page
