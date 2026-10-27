"""The two things a web page can make a browser do to a loopback viewer.

Loopback keeps the viewer's port to the person at this machine -- but that
person's browser is at this machine too, and it will send a cross-site POST to
127.0.0.1 for any page that asks, or talk to the viewer under a rebound name.
plexora._guard refuses both, and these tests pin what passes as much as what
does not: every non-browser client (the MCP server, the askpass helper, the
Flask test client used by every other test in this suite) sends none of the
headers the guard reads, and must be untouched by it.
"""

import pytest

import plexora
from plexora import _guard

#: No route acts on a POST here, so a request the guard lets through is
#: answered by routing (404 or 405) and one it stops is a 403 -- which keeps
#: these tests away from any route that would act.
NOWHERE = "/__no_such_route__"
PASSED = (404, 405)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setitem(plexora.app.config, "PLEXORA_AUTH_TOKEN", "")
    monkeypatch.setitem(plexora.app.config, "PLEXORA_TRUSTED_HOSTS", None)
    monkeypatch.setitem(plexora.app.config, "PLEXORA_ALLOWED_ORIGINS", [])
    return plexora.app.test_client()


def test_a_client_that_sends_no_browser_headers_is_untouched(client):
    assert client.post(NOWHERE).status_code in PASSED


def test_a_write_from_another_site_is_refused(client):
    response = client.post(NOWHERE, headers={"Sec-Fetch-Site": "cross-site",
                                             "Origin": "https://example.org"})
    assert response.status_code == 403
    assert b"own pages" in response.data


def test_a_write_from_another_port_on_this_machine_is_refused(client):
    """Same-site: the port is not part of a site, so a notebook or a dev server
    on localhost:<other port> is same-site with the viewer -- and a stranger."""
    response = client.post(NOWHERE, headers={"Sec-Fetch-Site": "same-site",
                                             "Origin": "http://localhost:8888"})
    assert response.status_code == 403


def test_a_write_from_the_viewers_own_page_passes(client):
    response = client.post(NOWHERE, headers={"Sec-Fetch-Site": "same-origin",
                                             "Origin": "http://localhost"})
    assert response.status_code in PASSED


def test_a_browser_without_fetch_metadata_is_judged_by_origin(client):
    ok = client.post(NOWHERE, headers={"Origin": "http://localhost"})
    refused = client.post(NOWHERE, headers={"Origin": "https://example.org"})
    assert ok.status_code in PASSED and refused.status_code == 403


def test_an_opaque_origin_is_refused(client):
    """A sandboxed frame or a file:// page sends `Origin: null`."""
    assert client.post(NOWHERE, headers={"Origin": "null"}).status_code == 403


def test_an_allowed_origin_passes_behind_a_proxy(client, monkeypatch):
    monkeypatch.setitem(plexora.app.config, "PLEXORA_ALLOWED_ORIGINS",
                        ["https://portal.example.org"])
    response = client.post(NOWHERE, headers={"Sec-Fetch-Site": "cross-site",
                                             "Origin": "https://portal.example.org"})
    assert response.status_code in PASSED


def test_reads_are_never_refused(client):
    response = client.get("/health", headers={"Sec-Fetch-Site": "cross-site",
                                              "Origin": "https://example.org"})
    assert response.status_code == 204


def test_a_get_that_acts_is_judged_as_a_write(client):
    response = client.get("/settings/remotes/_askpass/answer?nonce=x",
                          headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403


@pytest.mark.parametrize("path", [
    "/shutdown",
    "/update/install",
    "/update/restart",
    "/settings/remotes/anything/connect",
    "/desktop/open",
    "/put_file",
])
def test_the_routes_that_change_the_process_are_guarded(client, path):
    response = client.post(path, headers={"Sec-Fetch-Site": "cross-site",
                                          "Origin": "https://example.org"})
    assert response.status_code == 403


def test_a_rebound_name_is_refused_when_the_host_check_is_on(client, monkeypatch):
    monkeypatch.setitem(plexora.app.config, "PLEXORA_TRUSTED_HOSTS", [])
    response = client.get("/health", headers={"Host": "attacker.example:8765"})
    assert response.status_code == 403
    assert b"attacker.example" in response.data


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765",
                                  "[::1]:8765", "plexora.localhost:8765"])
def test_loopback_names_always_pass_the_host_check(client, monkeypatch, host):
    monkeypatch.setitem(plexora.app.config, "PLEXORA_TRUSTED_HOSTS", [])
    assert client.get("/health", headers={"Host": host}).status_code == 204


def test_an_allowed_host_passes(client, monkeypatch):
    monkeypatch.setitem(plexora.app.config, "PLEXORA_TRUSTED_HOSTS", ["lab-box"])
    assert client.get("/health", headers={"Host": "lab-box:8765"}).status_code == 204


def test_no_host_check_when_it_is_off(client):
    """None is what Docker and --bind-node keep: a viewer bound to a network
    address is reached by whatever name the network gives it."""
    assert client.get("/health", headers={"Host": "anything.example"}).status_code == 204


def test_a_token_replaces_the_host_check(client, monkeypatch):
    """A rebound name is another host to the browser, so it never has this
    viewer's cookie; the token check refuses it on its own."""
    monkeypatch.setitem(plexora.app.config, "PLEXORA_TRUSTED_HOSTS", [])
    monkeypatch.setitem(plexora.app.config, "PLEXORA_AUTH_TOKEN", "t0ken")
    response = client.get("/health?token=t0ken", headers={"Host": "portal.example.org"})
    assert response.status_code == 204


@pytest.mark.parametrize("header,name", [
    ("127.0.0.1:8765", "127.0.0.1"), ("LOCALHOST:1", "localhost"),
    ("[::1]:8765", "::1"), ("example.org.", "example.org"), ("::1", "::1"),
])
def test_host_names_are_read_without_port_or_brackets(header, name):
    assert _guard.host_name(header) == name


def test_two_viewers_on_one_machine_keep_separate_cookies():
    assert _guard.auth_cookie_name("127.0.0.1:8765") != _guard.auth_cookie_name("127.0.0.1:8766")
    assert _guard.auth_cookie_name("example.org") == _guard.LEGACY_COOKIE
