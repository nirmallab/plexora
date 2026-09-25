"""Settings > Web data: the chunk cache's budget, usage and address book.

Named "Web data" because "Remote servers" is already a section and the two are
unrelated: that one runs Plexora elsewhere, this one reads bytes from
elsewhere into this Plexora.
"""

import pytest

from plexora import app, paths
from plexora.server.utils import remote_store
from tests.ngff_fixtures import write_ngff
from tests.remote_fixtures import cache_root, http_store  # noqa: F401


@pytest.fixture
def client(cache_root):
    with app.test_client() as test_client:
        yield test_client


def test_the_section_is_on_the_page(client):
    from plexora.server.routes.settings_routes import SECTIONS

    assert any(s["id"] == "webdata" and s["label"] == "Web data" for s in SECTIONS)
    page = client.get("/settings")
    assert page.status_code == 200


def test_usage_shape(client):
    payload = client.get("/settings/webdata").get_json()
    for key in ("used_bytes", "pinned_bytes", "budget_bytes", "cache_dir",
                "env_override", "stores", "options", "support"):
        assert key in payload
    assert payload["budget_bytes"] == paths.REMOTE_CACHE_DEFAULT_BYTES
    assert payload["support"]["https"] is True


def test_budget_is_validated_and_applied_live(client):
    assert client.post("/settings/webdata", json={"budget_gb": 0.5}).status_code == 400
    assert client.post("/settings/webdata", json={"budget_gb": "lots"}).status_code == 400
    answer = client.post("/settings/webdata", json={"budget_gb": 3})
    assert answer.status_code == 200
    assert answer.get_json()["budget_bytes"] == 3 * 1024 ** 3
    assert paths.read_settings()["remote_cache_bytes"] == 3 * 1024 ** 3
    assert remote_store.cache_index().budget == 3 * 1024 ** 3


def test_the_environment_overrides_the_setting(client, monkeypatch):
    monkeypatch.setenv(paths.ENV_REMOTE_CACHE_BYTES, str(2 * 1024 ** 3))
    payload = client.get("/settings/webdata").get_json()
    assert payload["env_override"] is True
    assert payload["budget_bytes"] == 2 * 1024 ** 3
    assert client.post("/settings/webdata", json={"budget_gb": 5}).status_code == 409


def test_stores_are_listed_with_their_projects_and_can_be_cleared(
        tmp_path, client, http_store):
    from plexora import datasource

    write_ngff(tmp_path / "served" / "slide.ome.zarr", shape=(1, 64, 64), levels=1,
               version="0.5")
    server = http_store()
    url = server.url("slide.ome.zarr")
    datasource.register_image_datasource(name="web", image=url)

    payload = client.get("/settings/webdata").get_json()
    [row] = payload["stores"]
    assert row["url"] == url and row["projects"] == ["web"] and row["bytes"] > 0

    cleared = client.post("/settings/webdata/clear", json={}).get_json()
    assert cleared["used_bytes"] == 0


def test_pin_route_refuses_over_budget_with_the_numbers(tmp_path, client, http_store,
                                                          monkeypatch):
    write_ngff(tmp_path / "served" / "slide.ome.zarr", shape=(1, 256, 256), levels=1,
               version="0.5")
    server = http_store()
    monkeypatch.setenv(paths.ENV_REMOTE_CACHE_BYTES, "1000")
    answer = client.post("/settings/webdata/pin", json={"source": server.url("slide.ome.zarr")})
    assert answer.status_code == 409
    assert answer.get_json()["estimate"] > 1000
    assert client.post("/settings/webdata/pin", json={"source": "/local"}).status_code == 400


def test_address_book_routes_drop_secrets(client):
    saved = client.post("/settings/webdata/options", json={
        "prefix": "s3://idr", "endpoint_url": "https://uk1s3.embassy.ebi.ac.uk",
        "anon": True, "secret": "nope"}).get_json()
    assert saved["ok"] and saved["stored"]["prefix"] == "s3://idr/"
    assert "secret" not in saved["stored"]
    listed = client.get("/settings/webdata/options").get_json()["options"]
    assert listed == [{"prefix": "s3://idr/", "endpoint_url": "https://uk1s3.embassy.ebi.ac.uk",
                       "anon": True}]
    assert client.post("/settings/webdata/options",
                       json={"prefix": "not-a-url"}).status_code == 400
    removed = client.delete("/settings/webdata/options", json={"prefix": "s3://idr/"})
    assert removed.get_json()["ok"] and removed.get_json()["options"] == []


def test_check_path_existence_answers_without_the_network(client):
    answer = client.post("/check_path_existence",
                         json={"path": "https://nowhere.invalid/x.zarr"}).get_json()
    assert answer == {"exists": True, "remote": True}


def test_cli_sets_the_cache_budget(capsys):
    from plexora import cli

    assert cli.main(["config", "set", "remote-cache-gb", "0.2"]) == 2
    assert cli.main(["config", "set", "remote-cache-gb", "4"]) in (0, None)
    assert paths.read_settings()["remote_cache_bytes"] == 4 * 1024 ** 3
    assert paths.remote_cache_budget() == 4 * 1024 ** 3


def test_migration_leaves_the_cache_behind(tmp_path):
    from plexora.server.models import data_migration

    (tmp_path / paths.REMOTE_CACHE_DIRNAME).mkdir(exist_ok=True)
    (tmp_path / "demo").mkdir()
    names = data_migration.migratable(tmp_path)
    assert "demo" in names and paths.REMOTE_CACHE_DIRNAME not in names
