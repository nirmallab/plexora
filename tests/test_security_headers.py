"""The response headers every viewer page and API reply carries."""

import pytest

import plexora


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setitem(plexora.app.config, "PLEXORA_AUTH_TOKEN", "")
    monkeypatch.setitem(plexora.app.config, "PLEXORA_TRUSTED_HOSTS", None)
    monkeypatch.setitem(plexora.app.config, "PLEXORA_NOTEBOOK_MODE", False)
    monkeypatch.setitem(plexora.app.config, "PLEXORA_ALLOW_FRAMING", False)
    return plexora.app.test_client()


def test_every_response_says_nosniff_and_keeps_its_referrer_to_itself(client):
    for path in ("/health", "/__no_such_route__"):
        headers = client.get(path).headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Referrer-Policy"] == "same-origin"


def test_pages_refuse_to_be_framed_outside_notebook_mode(client):
    assert client.get("/health").headers["X-Frame-Options"] == "SAMEORIGIN"


@pytest.mark.parametrize("flag", ["PLEXORA_NOTEBOOK_MODE", "PLEXORA_ALLOW_FRAMING"])
def test_notebook_mode_leaves_framing_to_the_notebook(client, monkeypatch, flag):
    """A sidecar is always framed by the notebook's own, different, origin."""
    monkeypatch.setitem(plexora.app.config, flag, True)
    assert "X-Frame-Options" not in client.get("/health").headers


def test_resource_routing_is_never_cached(client):
    """Its reply carries the data nodes' tokens."""
    response = client.get("/resource_routing")
    assert response.headers["Cache-Control"] == "no-store"


def test_a_token_cookie_is_named_for_the_port_it_was_set_on(client, monkeypatch):
    monkeypatch.setitem(plexora.app.config, "PLEXORA_AUTH_TOKEN", "t0ken")
    response = client.get("/health?token=t0ken", headers={"Host": "127.0.0.1:8765"})
    cookie = response.headers["Set-Cookie"]
    assert cookie.startswith("plexora_auth_8765=t0ken")
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie


def test_the_token_cookie_lives_as_long_as_the_launcher_says(client, monkeypatch):
    monkeypatch.setitem(plexora.app.config, "PLEXORA_AUTH_TOKEN", "t0ken")
    monkeypatch.setitem(plexora.app.config, "PLEXORA_AUTH_COOKIE_MAX_AGE", 3600)
    cookie = client.get("/health?token=t0ken").headers["Set-Cookie"]
    assert "Max-Age=3600" in cookie


def test_a_cookie_from_before_the_per_port_name_still_gets_in(client, monkeypatch):
    monkeypatch.setitem(plexora.app.config, "PLEXORA_AUTH_TOKEN", "t0ken")
    client.set_cookie("plexora_auth", "t0ken")
    assert client.get("/health").status_code == 204


def test_a_client_may_send_the_token_as_a_header(client, monkeypatch):
    monkeypatch.setitem(plexora.app.config, "PLEXORA_AUTH_TOKEN", "t0ken")
    response = client.get("/health", headers={"X-Plexora-Token": "t0ken"})
    assert response.status_code == 204
    assert "Set-Cookie" not in response.headers
