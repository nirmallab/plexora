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
        json.dumps({"real_project": {"image_kind": "ome_tiff", "featureData": []}}),
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
