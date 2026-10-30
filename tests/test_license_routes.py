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
    assert info["trial_url"] == "", "offline, a trial page could not finish the job"
    assert "certificate" not in json.dumps(body)


def test_status_says_where_pro_is_bought_even_offline(client):
    """The pricing page is a website, not a platform call: a licence bought
    there can still reach this machine as an offline file."""
    from plexora import links

    info = client.get("/license/status").get_json()["license"]
    assert info["pricing_url"] == links.PRICING_URL


def test_settings_offers_the_pricing_page_and_says_where_the_key_goes(client):
    from plexora import links

    page = client.get("/settings").get_data(as_text=True)
    assert 'id="settings_license_buy"' in page
    assert f'href="{links.PRICING_URL}"' in page
    assert "licence key comes by email" in page


def test_settings_license_lead_says_ai_is_invite_only(client):
    """The License lead says one thing: Plexora AI is by invitation, and where
    to ask. What Free and Pro each include is the pricing page's to say."""
    page = client.get("/settings").get_data(as_text=True)
    assert "Plexora AI is currently invite only" in page
    assert 'href="mailto:support@biocognia.com"' in page
    assert "Plexora <strong>Free</strong> is everything" not in page
    assert "External MCP access is off by default" not in page
    assert "everything Free keeps working" not in page


def test_status_on_pro(client, pro_license):
    info = client.get("/license/status").get_json()["license"]
    assert info["plan"] == "pro" and info["entitlements"] == ["ai", "mcp", "plugin:video_studio",
                                                              "transcriptomics", "volume"]
    assert pro_license.issue()[:30] not in json.dumps(info)


def test_status_lists_what_each_root_and_plugin_unlocks(client, license_issuer):
    license_issuer.install()  # ["ai"]
    unlocks = {row["entitlement"]: row["granted"]
               for row in client.get("/license/status").get_json()["license"]["unlocks"]}
    assert unlocks == {"ai": True, "volume": False, "transcriptomics": False, "mcp": False,
                       "plugin:video_studio": False}


def test_the_page_carries_a_licence_hint_and_nothing_more(client, pro_license):
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


def test_refresh_and_deactivate_through_settings(client, license_service, pro_license):
    reply = client.post("/settings/license/refresh", json={})
    assert reply.status_code == 200 and reply.get_json()["outcome"] == "ok"
    reply = client.post("/settings/license/deactivate", json={})
    assert reply.status_code == 200
    assert reply.get_json()["license"]["plan"] == "free"
    assert not LICENSING.store.path.exists()


def test_remove(client, pro_license):
    reply = client.post("/settings/license/remove", json={})
    assert reply.status_code == 200 and reply.get_json()["license"]["plan"] == "free"


def test_changes_are_refused_from_another_machine(client, pro_license):
    """A neighbour who can reach the tiles cannot swap or release a licence."""
    remote = {"REMOTE_ADDR": "10.0.0.8"}
    for path in ("/settings/license/remove", "/settings/license/install",
                 "/settings/license/activate", "/settings/license/deactivate",
                 "/settings/license/connect", "/settings/license/connect/poll"):
        reply = client.post(path, json={}, environ_base=remote)
        assert reply.status_code == 403, path
    assert licensing.current().licensed
    assert client.get("/license/status", environ_base=remote).status_code == 200


ADA = {"email": "ada@lab.example.org", "name": "Ada Lovelace",
       "org": {"name": "Example Lab", "kind": "institution", "id": "org_1"},
       "usr": "usr_1", "server_time": 1}


def _account(monkeypatch, answer=None, error=None):
    """`Licensing.account` as the platform would answer it (biocognia 0.1.2)."""
    def ask():
        if error is not None:
            raise error
        return answer

    monkeypatch.setattr(LICENSING, "account", ask, raising=False)


def test_the_account_passes_four_fields_and_is_never_cached(client, monkeypatch):
    _account(monkeypatch, ADA)
    reply = client.get("/settings/license/account")
    assert reply.status_code == 200
    assert reply.headers["Cache-Control"] == "no-store"
    assert reply.get_json()["account"] == {"email": "ada@lab.example.org", "name": "Ada Lovelace",
                                           "org": {"name": "Example Lab", "kind": "institution"}}
    assert "ada@" not in client.get("/license/status").get_data(as_text=True)


def test_the_account_answers_this_machine_only(client, monkeypatch):
    """A neighbour who can reach the tiles cannot read whose licence it is."""
    _account(monkeypatch, ADA)
    reply = client.get("/settings/license/account", environ_base={"REMOTE_ADDR": "10.0.0.8"})
    assert reply.status_code == 403
    assert "ada@" not in reply.get_data(as_text=True)


def test_an_account_that_cannot_be_asked_says_why(client, monkeypatch):
    from biocognia.errors import LicenseError, ServerError

    _account(monkeypatch, error=LicenseError("Plexora is not connected on this device.", code="not_activated"))
    reply = client.get("/settings/license/account")
    assert reply.status_code == 409 and reply.get_json()["code"] == "not_activated"
    _account(monkeypatch, error=ServerError("Could not reach the BioCognia platform.", code="unreachable"))
    reply = client.get("/settings/license/account")
    assert reply.status_code == 502 and "Could not reach" in reply.get_json()["error"]
    # A BioCognia client older than 0.1.2 has no way to ask.
    monkeypatch.setattr(LICENSING, "account", None, raising=False)
    reply = client.get("/settings/license/account")
    assert reply.status_code == 501 and reply.get_json()["code"] == "client_too_old"
    assert reply.headers["Cache-Control"] == "no-store"


def test_the_account_through_the_biocognia_client(client, license_service, pro_license):
    if not hasattr(LICENSING, "account"):
        pytest.skip("Licensing.account arrives in biocognia 0.1.2")
    reply = client.get("/settings/license/account")
    assert reply.status_code == 200, reply.get_json()
    assert reply.get_json()["account"]["email"] == "ada@lab.example.org"
    sent = license_service.of("/v1/account")[0]["body"]
    assert sent["product"] == "plexora" and sent["certificate"].startswith("BIOC1.")


def test_settings_calls_it_account_and_says_the_email_is_never_saved(client):
    page = client.get("/settings").get_data(as_text=True)
    assert '<h2 class="settings-panel-title">Account</h2>' in page
    assert 'data-section="license"' in page, "links to Settings > License still land here"
    assert 'id="settings_account"' in page and 'id="settings_account_credits"' in page
    assert "never saved on this computer" in page


def test_about_names_the_plan_and_nothing_else(client, pro_license):
    info = client.get("/desktop/info").get_json()
    assert info["plan"] == "Pro"
    assert "lic_" not in json.dumps(info)


def test_install_finds_the_certificate_in_run_together_text(client, license_issuer):
    """A one-line box flattens a pasted file: comments and certificate on one line."""
    cert = license_issuer.issue(offline_until=int(time.time()) + 90 * 86400)
    reply = client.post("/settings/license/install",
                        json={"certificate": f"# BioCognia offline licence # Valid until 2027 {cert}"})
    assert reply.status_code == 200, reply.get_json()
    assert licensing.current().state == "offline_valid"


# -- Connect this device: the device flow --------------------------------------------


def test_with_the_platform_reachable_the_trial_page_is_the_portals(client, license_service):
    info = client.get("/license/status").get_json()["license"]
    assert info["service_configured"] is True
    assert info["trial_url"].endswith("/start?product=plexora")


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
    assert done["license"]["paid"] and done["license"]["state"] == "pro_active"
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
