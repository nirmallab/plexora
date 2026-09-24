"""POST /rename_channels: the Image card's "Paste channel names".

The names come from another image's copy rather than a file, so this is
/upload_channels without its front half -- and it must end the same way: the
config renamed, the saved channel list moved with it, and nothing at all
changed when the list is not one this image can take.
"""

import io

import plexora
from tests.test_channel_names_upload import _names_in, _post, _register
from tests.test_channel_rename_state import _save_channels, _saved_names


def _rename(client, names, name="panel_sample"):
    return client.post("/rename_channels", json={"datasource": name, "names": names})


def test_a_complete_list_is_applied_and_handed_back(tmp_path, monkeypatch):
    data_dir = _register(tmp_path, monkeypatch)
    response = _rename(plexora.app.test_client(), ["DAPI", "CD3"])

    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    assert body["names"] == ["DAPI", "CD3"]
    assert body["channel_count"] == 2
    assert _names_in(data_dir) == ["DAPI", "CD3"]


def test_the_saved_channel_list_moves_with_it(tmp_path, monkeypatch):
    _register(tmp_path, monkeypatch)
    before = _names_in(tmp_path / "data")
    _save_channels("panel_sample", [
        {"channel": before[0], "start": 0, "end": 255, "channel_active": True},
        {"channel": before[1], "start": 0, "end": 255, "channel_active": False},
    ])

    response = _rename(plexora.app.test_client(), ["DAPI", "CD3"])

    assert response.status_code == 200
    assert _saved_names("panel_sample") == ["DAPI", "CD3"]


def test_the_wrong_count_changes_nothing_and_says_both_numbers(tmp_path, monkeypatch):
    data_dir = _register(tmp_path, monkeypatch)
    before = _names_in(data_dir)
    response = _rename(plexora.app.test_client(), ["DAPI"])

    assert response.status_code == 400
    body = response.get_json()
    assert body["mismatch"] is True
    assert body["marker_count"] == 1
    assert body["channel_count"] == 2
    assert _names_in(data_dir) == before


def test_a_duplicate_name_is_refused(tmp_path, monkeypatch):
    data_dir = _register(tmp_path, monkeypatch)
    before = _names_in(data_dir)
    response = _rename(plexora.app.test_client(), ["DAPI", "DAPI"])

    assert response.status_code == 400
    assert response.get_json()["success"] is False
    assert _names_in(data_dir) == before


def test_a_blank_name_or_a_non_list_is_refused(tmp_path, monkeypatch):
    data_dir = _register(tmp_path, monkeypatch)
    before = _names_in(data_dir)
    client = plexora.app.test_client()

    assert _rename(client, ["DAPI", "  "]).status_code == 400
    assert _rename(client, "DAPI,CD3").status_code == 400
    assert _rename(client, ["DAPI", 3]).status_code == 400
    assert _names_in(data_dir) == before


def test_an_unknown_datasource_is_422(tmp_path, monkeypatch):
    _register(tmp_path, monkeypatch)
    response = _rename(plexora.app.test_client(), ["DAPI", "CD3"], name="nope")
    assert response.status_code == 422


def test_the_file_route_still_ends_the_same_way(tmp_path, monkeypatch):
    """The two routes share their ending now; the file one must not notice."""
    data_dir = _register(tmp_path, monkeypatch)
    response = _post(plexora.app.test_client(),
                     file=(io.BytesIO(b"marker\nDAPI\nCD3\n"), "panel.csv"))
    assert response.status_code == 200
    assert response.get_json()["channel_count"] == 2
    assert _names_in(data_dir) == ["DAPI", "CD3"]
