"""The token that makes a non-loopback bind safe enough to do automatically.

Plexora has never had authentication and does not want any: it binds loopback,
so the operating system is the access control. Open OnDemand breaks that, and
not by choice -- the portal's web host connects to the compute node over the
network, so the viewer has to listen on an address every other account on the
cluster can also reach.

So the rule is narrow on purpose: the guard activates on the PRESENCE OF A
TOKEN, never on the shape of the bind. The Docker image binds 0.0.0.0
deliberately, publishes a port deliberately and shares one server between
people deliberately; it sets no token and must stay exactly as open as it was.

The token is read into `app.config` and consulted per request rather than
deciding whether to register the guard, because `create_app()` runs once per
interpreter (see its docstring) -- so a guard that was not installed at import
could never be installed at all, and these tests could not exist.
"""

import pytest

import plexora
from plexora import AUTH_COOKIE


TOKEN = "s3cret-token"


@pytest.fixture
def guarded(monkeypatch):
    monkeypatch.setitem(plexora.app.config, "PLEXORA_AUTH_TOKEN", TOKEN)
    return plexora.app.test_client()


@pytest.fixture
def unguarded(monkeypatch):
    monkeypatch.setitem(plexora.app.config, "PLEXORA_AUTH_TOKEN", "")
    return plexora.app.test_client()


#: What /health answers when it lets you through -- it deliberately does no
#: work, so there is no body to return.
OK = 204


def test_a_server_with_no_token_is_untouched(unguarded):
    """Every existing way of running Plexora goes through this path."""
    assert unguarded.get("/health").status_code == OK


def test_a_request_without_the_token_is_refused(guarded):
    response = guarded.get("/health")

    assert response.status_code == 403
    assert b"token" in response.data.lower()


def test_the_wrong_token_is_refused(guarded):
    assert guarded.get("/health?token=not-it").status_code == 403


def test_the_token_in_the_url_gets_in(guarded):
    assert guarded.get(f"/health?token={TOKEN}").status_code == OK


def test_nothing_is_exempt_from_the_guard(guarded):
    """Including the health probe. Whoever started this server knows the token
    and passes it; a neighbour on the same node is precisely who is being kept
    out, and an exempt route is a route they can enumerate."""
    for path in ("/", "/config", "/health"):
        assert guarded.get(path).status_code == 403


def test_the_first_request_trades_the_token_for_a_cookie(guarded):
    """So that the hundreds of asset and tile requests the page then makes do
    not each carry the secret in a query string."""
    guarded.get(f"/health?token={TOKEN}")

    assert guarded.get("/health").status_code == OK


def test_the_cookie_is_scoped_to_this_servers_own_mount_path(guarded, monkeypatch):
    """Under OnDemand every job on the cluster is proxied through ONE portal
    origin, so a cookie at "/" would be sent to -- and clobbered by -- every
    other Plexora and every other OnDemand app the user has open."""
    monkeypatch.setitem(plexora.app.config, "PLEXORA_BASE_URL",
                        "/rnode/compute-a-16/8123")

    response = guarded.get(f"/health?token={TOKEN}")

    cookie = response.headers["Set-Cookie"]
    assert "Path=/rnode/compute-a-16/8123" in cookie
    assert "HttpOnly" in cookie


def test_a_rootless_server_still_gets_a_usable_cookie_path(guarded, monkeypatch):
    monkeypatch.setitem(plexora.app.config, "PLEXORA_BASE_URL", "")

    assert "Path=/" in guarded.get(f"/health?token={TOKEN}").headers["Set-Cookie"]


def test_no_cookie_is_handed_out_without_a_valid_token(guarded):
    assert "Set-Cookie" not in guarded.get("/health?token=not-it").headers


def test_an_unguarded_server_sets_no_cookie(unguarded):
    assert "Set-Cookie" not in unguarded.get("/health?token=anything").headers
