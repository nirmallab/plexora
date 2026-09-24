"""`/view_transform/<datasource>`: where core's Rotate and Flip tools keep the
orientation they set.

The orientation is the image's, not the card's -- closing Rotate leaves the
view turned, and reopening the sample shows it turned -- so it has to survive a
reload, which is what the per-datasource database gives it. Isolated from the
real data directory by the repo-root conftest's PLEXORA_DATA_PATH pin, the same
way test_page_routes.py is.
"""

import json

import pytest

import plexora


@pytest.fixture
def client(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps({"alpha": {"image_kind": "ome_tiff", "dataset": None},
                    "beta": {"image_kind": "ome_tiff", "dataset": None}}),
        encoding="utf-8",
    )
    return plexora.app.test_client()


def test_an_image_never_turned_reads_as_upright(client):
    response = client.get("/view_transform/alpha")
    assert response.status_code == 200
    assert response.get_json() == {"degrees": 0, "flipH": False, "flipV": False}


def test_a_saved_orientation_round_trips(client):
    saved = {"degrees": 90, "flipH": True, "flipV": False}
    response = client.put("/view_transform/alpha", json=saved)
    assert response.status_code == 200
    assert response.get_json() == {"success": True}
    assert client.get("/view_transform/alpha").get_json() == saved


def test_fractional_degrees_survive(client):
    client.put("/view_transform/alpha", json={"degrees": 12.5, "flipH": False, "flipV": True})
    assert client.get("/view_transform/alpha").get_json() == {
        "degrees": 12.5, "flipH": False, "flipV": True}


@pytest.mark.parametrize("sent, stored", [(360, 0), (-90, 270), (450, 90), (720.0, 0)])
def test_degrees_are_normalised_into_one_turn(client, sent, stored):
    client.put("/view_transform/alpha", json={"degrees": sent})
    assert client.get("/view_transform/alpha").get_json()["degrees"] == stored


def test_flips_are_coerced_to_booleans(client):
    client.put("/view_transform/alpha", json={"degrees": 0, "flipH": 1, "flipV": 0})
    assert client.get("/view_transform/alpha").get_json() == {
        "degrees": 0, "flipH": True, "flipV": False}


@pytest.mark.parametrize("body", [
    {"degrees": "ninety"},
    {"degrees": True},
    {"degrees": None},
    [90, True, False],
])
def test_nonsense_is_a_400_and_stores_nothing(client, body):
    client.put("/view_transform/alpha", json={"degrees": 180})
    response = client.put("/view_transform/alpha", json=body)
    assert response.status_code == 400
    assert response.get_json()["success"] is False
    assert client.get("/view_transform/alpha").get_json()["degrees"] == 180


def test_a_non_json_body_is_a_400(client):
    response = client.put("/view_transform/alpha", data="not json",
                          content_type="text/plain")
    assert response.status_code == 400


def test_each_image_keeps_its_own_orientation(client):
    client.put("/view_transform/alpha", json={"degrees": 90, "flipH": True, "flipV": False})
    assert client.get("/view_transform/beta").get_json() == {
        "degrees": 0, "flipH": False, "flipV": False}


def test_an_unknown_datasource_is_a_404(client):
    assert client.get("/view_transform/nope").status_code == 404
    assert client.put("/view_transform/nope", json={"degrees": 0}).status_code == 404
