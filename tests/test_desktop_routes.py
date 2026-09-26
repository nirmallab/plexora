"""`/desktop/open`, `/desktop/info`, and what desktop mode changes elsewhere."""

import numpy as np
import pytest
import tifffile

import plexora
from plexora import _lifetime
from plexora.server.models import layer_jobs
from plexora.server.routes import page_routes, system_routes

from tests.helpers import use_data_root


@pytest.fixture
def client(tmp_path, monkeypatch):
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    rng = np.random.default_rng(0)
    tifffile.imwrite(source / "slide.ome.tif",
                     rng.integers(0, 4000, (2, 256, 256)).astype(np.uint16))
    layer_jobs.forget()
    yield plexora.app.test_client()
    layer_jobs.forget()


@pytest.fixture
def desktop(monkeypatch):
    monkeypatch.setitem(plexora.app.config, "PLEXORA_DESKTOP", True)


def _slide(tmp_path):
    return str(tmp_path / "source" / "slide.ome.tif")


def test_a_dropped_image_becomes_a_project_and_says_where_to_open_it(client, tmp_path):
    response = client.post("/desktop/open", json={"paths": [_slide(tmp_path)]})
    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["project"] == "slide"
    assert payload["url"].endswith("/slide")
    assert payload["existing"] is False


def test_dropping_it_again_reopens_rather_than_copying(client, tmp_path):
    client.post("/desktop/open", json={"paths": [_slide(tmp_path)]})
    again = client.post("/desktop/open", json={"paths": [_slide(tmp_path)]}).get_json()
    assert again["project"] == "slide"
    assert again["existing"] is True
    assert plexora.get_config_names() == ["slide"]


def test_a_project_folder_opens_that_project(client, tmp_path):
    client.post("/desktop/open", json={"paths": [_slide(tmp_path)]})
    from plexora import paths

    folder = paths.project_dir("slide")
    folder.mkdir(parents=True, exist_ok=True)
    payload = client.post("/desktop/open", json={"paths": [str(folder)]}).get_json()
    assert payload == {"project": "slide", "url": "/slide", "existing": True}


def test_a_missing_path_is_a_400_that_says_which(client, tmp_path):
    response = client.post("/desktop/open",
                           json={"paths": [str(tmp_path / "nope.tif")]})
    assert response.status_code == 400
    assert "nope.tif" in response.get_json()["error"]
    assert response.get_json()["url"].endswith("/open_project")


def test_an_unreadable_file_is_a_400(client, tmp_path):
    junk = tmp_path / "notes.txt"
    junk.write_text("hello", encoding="utf-8")
    response = client.post("/desktop/open", json={"paths": [str(junk)]})
    assert response.status_code == 400


@pytest.mark.parametrize("body", [None, [], {"paths": []}, {"paths": [3]},
                                  {"paths": [""]}, {"other": 1}])
def test_malformed_requests_are_400(client, body):
    response = client.post("/desktop/open", json=body)
    assert response.status_code == 400


def test_info_describes_the_server_without_its_token(client, desktop, monkeypatch):
    monkeypatch.setitem(plexora.app.config, "PLEXORA_AUTH_TOKEN", "")
    payload = client.get("/desktop/info").get_json()
    assert payload["desktop"] is True
    for key in ("version", "python", "executable", "data_root", "settings_path",
                "config_path", "plugins", "tools"):
        assert key in payload
    assert all({"name", "label", "shortcut", "menu"} <= set(tool) for tool in payload["tools"])
    assert "token" not in payload and "port" not in payload


def test_info_says_when_it_is_not_the_desktop_server(client):
    assert client.get("/desktop/info").get_json()["desktop"] is False


def test_the_browse_capability_names_the_shell_only_in_desktop_mode(client, monkeypatch):
    assert "via" not in client.post("/browse_capability", json={}).get_json()
    monkeypatch.setitem(plexora.app.config, "PLEXORA_DESKTOP", True)
    assert client.post("/browse_capability", json={}).get_json()["via"] == "shell"


def test_template_data_exposes_desktop(monkeypatch):
    with plexora.app.test_request_context("/"):
        assert page_routes.template_data()["desktop"] is False
        monkeypatch.setitem(plexora.app.config, "PLEXORA_DESKTOP", True)
        assert page_routes.template_data()["desktop"] is True


def test_quit_answers_first_and_then_stops_gracefully(client, monkeypatch):
    calls = []

    class Timer:
        def __init__(self, delay, function, args=()):
            calls.append((delay, function, args))
            self.daemon = False

        def start(self):
            calls.append("started")

    monkeypatch.setattr(system_routes.threading, "Timer", Timer)
    response = client.post("/shutdown")
    assert response.status_code == 204
    delay, function, _args = calls[0]
    assert function is _lifetime.request_shutdown
    assert 0 < delay < 1
    assert calls[1] == "started"
