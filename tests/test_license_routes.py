"""Settings > License and the page's view of the licence."""

import json
import time

import pytest

import plexora
from plexora import licensing
from plexora.licensing import LICENSING


@pytest.fixture
def client():
    # A plain test client, as the rest of the suite uses: no TESTING flag,
    # which would change error handling for every test after this one.
    with plexora.app.test_client() as test_client:
        yield test_client


def test_status_on_free_says_free_and_carries_no_secrets(client):
    body = client.get("/license/status").get_json()
    info = body["license"]
    assert info["plan"] == "free" and info["state"] == "free" and info["paid"] is False
    assert info["service_configured"] is False
    assert info["portal_url"].startswith("https://account.biocognia")
    assert "certificate" not in json.dumps(body)


def test_status_on_paid(client, paid_license):
    info = client.get("/license/status").get_json()["license"]
    assert info["plan"] == "paid" and info["entitlements"] == ["ai", "mcp"]
    assert paid_license.issue()[:30] not in json.dumps(info)


def test_status_lists_what_each_root_unlocks(client, license_issuer):
    license_issuer.install()  # ["ai"]
    unlocks = {row["entitlement"]: row["granted"]
               for row in client.get("/license/status").get_json()["license"]["unlocks"]}
    assert unlocks == {"ai": True, "mcp": False}


def test_the_page_carries_a_licence_hint_and_nothing_more(client, paid_license):
    html = client.get("/settings").get_data(as_text=True)
    assert 'id="settings_panel_license"' in html
    marker = "window.flaskVariables"
    assert marker in html
    assert "BIOC1." not in html


def test_template_data_licence_is_free_by_default():
    from plexora.server.routes.page_routes import template_data

    with plexora.app.test_request_context("/"):
        assert template_data()["license"] == {"plan": "free", "state": "free", "paid": False,
                                               "entitlements": []}


def test_template_data_survives_a_licensing_failure(monkeypatch):
    from plexora.server.routes.page_routes import template_data

    def boom():
        raise RuntimeError("licensing blew up")

    monkeypatch.setattr(licensing, "peek", boom)
    with plexora.app.test_request_context("/"):
        assert template_data()["license"]["plan"] == "free"


def test_install_an_offline_licence_through_settings(client, license_issuer):
    cert = license_issuer.issue(offline_until=int(time.time()) + 90 * 86400)
    reply = client.post("/settings/license/install",
                        json={"certificate": f"# BioCognia offline licence: Plexora\n{cert}\n"})
    assert reply.status_code == 200, reply.get_json()
    assert reply.get_json()["license"]["state"] == "offline_valid"
    assert licensing.allows("ai:gating")


def test_install_refuses_a_forgery(client, license_issuer):
    cert = license_issuer.issue()
    reply = client.post("/settings/license/install", json={"certificate": cert[:-6] + "AAAAAA"})
    assert reply.status_code == 400
    assert not LICENSING.store.path.exists()


def test_install_refuses_text_with_no_certificate(client):
    assert client.post("/settings/license/install", json={"certificate": "hello"}).status_code == 400


def test_activate_through_settings(client, license_service):
    reply = client.post("/settings/license/activate",
                        json={"credential": "BIOC-AAAA-BBBB", "name": "Bench PC"})
    assert reply.status_code == 200, reply.get_json()
    info = reply.get_json()["license"]
    assert info["paid"] and info["environment"]["name"] == "Bench PC"
    assert "BIOC-AAAA" not in json.dumps(reply.get_json())


def test_activate_refusal_is_a_sentence(client, license_service):
    license_service.script("/v1/activate", 409, {"error": {"code": "device_limit", "message": "x"}})
    reply = client.post("/settings/license/activate", json={"credential": "BIOC-AAAA-BBBB"})
    assert reply.status_code == 409
    assert "Disconnect one in the portal (Devices)" in reply.get_json()["error"]
    assert reply.get_json()["license"]["plan"] == "free"


def test_activate_under_offline_says_so(client):
    reply = client.post("/settings/license/activate", json={"credential": "BIOC-AAAA-BBBB"})
    assert reply.status_code == 400
    assert "BIOCOGNIA_OFFLINE" in reply.get_json()["error"]
    reply = client.post("/settings/license/connect", json={})
    assert reply.status_code == 400
    assert "BIOCOGNIA_OFFLINE" in reply.get_json()["error"]


def test_refresh_and_deactivate_through_settings(client, license_service, paid_license):
    reply = client.post("/settings/license/refresh", json={})
    assert reply.status_code == 200 and reply.get_json()["outcome"] == "ok"
    reply = client.post("/settings/license/deactivate", json={})
    assert reply.status_code == 200
    assert reply.get_json()["license"]["plan"] == "free"
    assert not LICENSING.store.path.exists()


def test_remove(client, paid_license):
    reply = client.post("/settings/license/remove", json={})
    assert reply.status_code == 200 and reply.get_json()["license"]["plan"] == "free"


def test_changes_are_refused_from_another_machine(client, paid_license):
    """A neighbour who can reach the tiles cannot swap or release a licence."""
    remote = {"REMOTE_ADDR": "10.0.0.8"}
    for path in ("/settings/license/remove", "/settings/license/install",
                 "/settings/license/activate", "/settings/license/deactivate",
                 "/settings/license/connect", "/settings/license/connect/poll"):
        reply = client.post(path, json={}, environ_base=remote)
        assert reply.status_code == 403, path
    assert licensing.current().licensed
    assert client.get("/license/status", environ_base=remote).status_code == 200


def test_about_names_the_plan_and_nothing_else(client, paid_license):
    info = client.get("/desktop/info").get_json()
    assert info["plan"] == "Paid"
    assert "lic_" not in json.dumps(info)


def test_install_finds_the_certificate_in_run_together_text(client, license_issuer):
    """A one-line box flattens a pasted file: comments and certificate on one line."""
    cert = license_issuer.issue(offline_until=int(time.time()) + 90 * 86400)
    reply = client.post("/settings/license/install",
                        json={"certificate": f"# BioCognia offline licence # Valid until 2027 {cert}"})
    assert reply.status_code == 200, reply.get_json()
    assert licensing.current().state == "offline_valid"


# -- Connect this device: the device flow --------------------------------------------


def test_connect_shows_a_code_and_the_link_then_fills_in_on_approval(client, license_service):
    started = client.post("/settings/license/connect", json={"name": "Bench PC"}).get_json()
    assert started["code"] == "BIOC-TEST-0001"
    assert started["verify_url"] == "https://account.biocognia.test/activate"
    assert started["interval"] >= 1 and started["expires_in"] > 0
    body = license_service.of("/v1/activate/start")[0]["body"]
    assert body["product"] == "plexora" and body["environment"]["display_name"] == "Bench PC"
    assert len(body["environment"]["binding"]) == 64

    pending = client.post("/settings/license/connect/poll", json={"code": started["code"]})
    assert pending.status_code == 200 and pending.get_json()["pending"] is True
    assert "license" not in pending.get_json()

    done = client.post("/settings/license/connect/poll", json={"code": started["code"]}).get_json()
    assert done["pending"] is False
    assert done["license"]["paid"] and done["license"]["state"] == "paid_active"
    assert done["license"]["environment"]["name"] == "Bench PC"
    assert licensing.allows("ai:gating")
    assert "BIOC1." not in json.dumps(done)


def test_a_declined_connection_says_so(client, license_service):
    started = client.post("/settings/license/connect", json={}).get_json()
    license_service.script("/v1/activate/poll", 403, {"error": {"code": "denied", "message": "no"}})
    reply = client.post("/settings/license/connect/poll", json={"code": started["code"]})
    assert reply.status_code == 410
    assert "declined" in reply.get_json()["error"]
    assert reply.get_json()["license"]["plan"] == "free"


def test_poll_refuses_what_is_not_a_code(client, license_service):
    reply = client.post("/settings/license/connect/poll", json={"code": "PLEX-AAAA"})
    assert reply.status_code == 400
    assert license_service.of("/v1/activate/poll") == []
