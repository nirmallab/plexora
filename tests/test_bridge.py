"""The website's request bridge (plexora/browser/bridge.py), under CPython.

In the browser the page hands a request to the worker, which hands it here;
nothing about that path needs Pyodide to be checked, so it is checked with the
app as the desktop builds it.
"""

import json

import pytest

import plexora
from plexora.browser import bridge

ORIGIN = "https://viewer.plexoraapp.com"


@pytest.fixture
def app(monkeypatch):
    app = plexora.app
    monkeypatch.setitem(app.config, "PLEXORA_AUTH_TOKEN", "")
    monkeypatch.setitem(app.config, "PLEXORA_TRUSTED_HOSTS", None)
    monkeypatch.setitem(app.config, "PLEXORA_ALLOWED_ORIGINS", [ORIGIN])
    return app


def _header(answer, name):
    return next((value for key, value in answer["headers"] if key.lower() == name.lower()), None)


def test_a_read_comes_back_as_the_test_client_sees_it(app):
    answer = bridge.handle(app, {"method": "GET", "path": "/projects"}, ORIGIN)
    expected = app.test_client().get("/projects")
    assert answer["status"] == expected.status_code == 200
    assert json.loads(answer["body"]) == expected.get_json()
    assert answer["stream"] is None and answer["wrote"] is False


def test_the_query_and_status_codes_pass_through(app):
    answer = bridge.handle(app, {"method": "GET", "path": "/health"}, ORIGIN)
    assert answer["status"] == 204 and answer["body"] == b""
    missing = bridge.handle(app, {"method": "GET", "path": "/no_such_project_here"}, ORIGIN)
    assert missing["status"] == 404


def test_a_write_from_the_page_is_admitted_and_marked(app):
    body = json.dumps({"prefs": {"hd": True}}).encode()
    answer = bridge.handle(app, {"method": "PUT", "path": "/viewer_prefs", "body": body,
                                 "headers": {"Content-Type": "application/json"}}, ORIGIN)
    assert answer["status"] in (200, 204), answer["body"][:300]
    assert answer["wrote"] is True


def test_a_write_from_another_site_is_refused_as_on_the_desktop(app):
    answer = bridge.handle(app, {"method": "PUT", "path": "/viewer_prefs", "body": b"{}",
                                 "headers": {"Content-Type": "application/json",
                                             "Origin": "https://evil.example"}}, ORIGIN)
    assert answer["status"] == 403


def test_a_long_body_comes_in_parts(app, monkeypatch):
    whole = app.test_client().get("/client/src/js/main.js").data
    assert len(whole) > 3000
    monkeypatch.setattr(bridge, "CHUNK", 1000)
    answer = bridge.handle(app, {"method": "GET", "path": "/client/src/js/main.js"}, ORIGIN)
    assert len(answer["body"]) == 1000 and answer["stream"] is not None
    parts, done = [answer["body"]], False
    while not done:
        more = bridge.read(answer["stream"], 1000)
        parts.append(more["body"])
        done = more["done"]
    assert b"".join(parts) == whole
    assert bridge.read(answer["stream"])["done"] is True


def test_a_stream_the_page_abandons_is_closed(app, monkeypatch):
    monkeypatch.setattr(bridge, "CHUNK", 100)
    answer = bridge.handle(app, {"method": "GET", "path": "/client/src/js/main.js"}, ORIGIN)
    bridge.close(answer["stream"])
    assert bridge.read(answer["stream"]) == {"body": b"", "done": True}


def test_headers_repeat_as_they_were_sent(app):
    answer = bridge.handle(app, {"method": "GET", "path": "/config"}, ORIGIN)
    assert _header(answer, "Content-Type", ) is not None
    assert isinstance(answer["headers"], list) and all(len(pair) == 2 for pair in answer["headers"])
