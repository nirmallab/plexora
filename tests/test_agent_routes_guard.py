"""Who may reach /agent/v1: loopback without a token; anyone with it."""

import pytest

import plexora


@pytest.fixture
def app():
    previous = plexora.app.config.get("PLEXORA_AUTH_TOKEN")
    yield plexora.app
    plexora.app.config["PLEXORA_AUTH_TOKEN"] = previous


def test_no_token_means_loopback_only(app):
    app.config["PLEXORA_AUTH_TOKEN"] = ""
    client = app.test_client()
    assert client.get("/agent/v1/viewer/sessions").status_code == 200
    remote = client.get("/agent/v1/viewer/sessions",
                        environ_overrides={"REMOTE_ADDR": "10.1.2.3"})
    assert remote.status_code == 403


def test_a_token_is_required_and_then_enough(app):
    app.config["PLEXORA_AUTH_TOKEN"] = "sekrit"
    client = app.test_client()
    assert client.get("/agent/v1/viewer/sessions").status_code == 403
    assert client.get("/agent/v1/viewer/sessions?token=sekrit",
                      environ_overrides={"REMOTE_ADDR": "10.1.2.3"}).status_code == 200
