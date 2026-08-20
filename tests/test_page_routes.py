"""The viewer page route, and what it does with a URL it does not recognise.

`/<string:datasource>` matches any single path segment, which makes it the last
rule standing between a wrong URL and a 404. It used to answer one by rendering
the empty viewer, so a request for a route that does not exist came back 200
with a full HTML page -- indistinguishable from success to anything that checks
the status code, and HTML to anything expecting JSON.

That matters more now than it did: uninstalling a plugin removes its routes, and
every one of them landed here. Isolated from the real data directory the same
way test_quick_view_routes.py is.
"""

import json

import pytest

import plexora


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(plexora, "data_path", tmp_path)
    monkeypatch.setattr(plexora, "config_json_path", tmp_path / "config.json")
    (tmp_path / "config.json").write_text(
        json.dumps({"real_project": {"image_kind": "ome_tiff", "dataset": None}}),
        encoding="utf-8",
    )
    return plexora.app.test_client()


def test_known_datasource_renders_the_viewer(client):
    response = client.get("/real_project")
    assert response.status_code == 200
    assert b"openseadragon" in response.data.lower()


def test_unknown_datasource_is_a_404(client):
    assert client.get("/no_such_project").status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/get_saved_gating_list",  # a plugin route, once the plugin is uninstalled
        "/save_gating_list",
        "/definitely_not_a_route",
    ],
)
def test_routes_that_do_not_exist_are_not_answered_with_a_page(client, path):
    """The specific regression: these returned 200 text/html, so a caller could
    not tell a missing endpoint from a working one."""
    response = client.get(path)
    assert response.status_code == 404
    assert b"openseadragon" not in response.data.lower()


def test_the_landing_page_still_has_no_datasource(client):
    """`/` legitimately renders the viewer shell with nothing selected. The 404
    above must not have taken that with it."""
    response = client.get("/")
    assert response.status_code == 200
    assert b"openseadragon" in response.data.lower()


# --------------------------------------------------------------------------
# The Tools menu on a project that has no feature table yet
# --------------------------------------------------------------------------

def _config(tmp_path, **entry):
    (tmp_path / "config.json").write_text(
        json.dumps({"proj": {"image_kind": "ome_tiff", **entry}}), encoding="utf-8"
    )
    return plexora.app.test_client()


@pytest.fixture
def no_table(tmp_path, monkeypatch):
    """A real image with no feature data -- the shape of a project registered
    from an image alone."""
    monkeypatch.setattr(plexora, "data_path", tmp_path)
    monkeypatch.setattr(plexora, "config_json_path", tmp_path / "config.json")
    return _config(tmp_path, dataset=None)


def test_tools_menu_is_offered_even_without_a_feature_table(no_table):
    """The regression: gating was filtered out of available_tools whenever the
    project had no feature table, so the Tools dropdown vanished entirely --
    and with it the only route to attaching one.

    Asserted on the rendered page because that is where it broke: base.html
    hides the whole dropdown on an empty available_tools.
    """
    body = no_table.get("/proj").data
    assert b"Thresholding" in body, "Tools menu lost its entry for a table-less project"


def test_opening_a_tool_without_a_table_hands_off_to_the_edit_page(no_table):
    """The no-JavaScript twin of the requirements modal.

    It hands off to the project's own edit page rather than to the import form:
    both are generated from the same requirements, so there is one surface to
    maintain, and `?needs=` lands the user on the specific fields this tool is
    waiting for instead of a blank second import.
    """
    response = no_table.get("/proj/tools/gating")
    assert response.status_code == 302
    assert "/edit_config/proj" in response.headers["Location"]
    assert "needs=gating" in response.headers["Location"]


def test_the_lazy_open_asks_for_what_is_missing_instead_of_navigating(no_table):
    """The modal path. Navigating away to collect a column name would tear down
    and rebuild the whole viewer to answer one question, which is the reason
    the lazy tool-open path exists at all."""
    payload = no_table.get("/proj/tools/gating/panel").get_json()

    assert "redirect" not in payload
    needs = payload["needs"]
    assert needs["tool"] == "gating"
    # A table first; the questions about its columns are unanswerable until it
    # exists, and the server withholds them until then -- optional roles
    # included, which is why only the mask is offered alongside.
    assert [r["key"] for r in needs["missing"]] == ["table"]
    assert [r["key"] for r in needs["optional"]] == ["segmentation"]


def test_a_tool_that_cannot_run_yet_is_not_activated(no_table):
    """Offering it in the menu must not also mean rendering a panel that has no
    data to work with."""
    body = no_table.get("/proj?tool=gating").data
    assert b'data-tool-mount="gating"' not in body


def test_an_rgb_project_offers_no_marker_tools(tmp_path, monkeypatch):
    """Compatibility still filters. No upload gives a flat RGB image channels,
    so this one stays hidden rather than handed off."""
    monkeypatch.setattr(plexora, "data_path", tmp_path)
    monkeypatch.setattr(plexora, "config_json_path", tmp_path / "config.json")
    (tmp_path / "config.json").write_text(
        json.dumps({"proj": {"image_kind": "rgb", "dataset": None}}), encoding="utf-8"
    )
    body = plexora.app.test_client().get("/proj").data
    assert b"Thresholding" not in body
