"""The licensing client against a stand-in licence service."""

import json
import time

import pytest

from plexora import licensing
from plexora.licensing import client, environment, state, store
from plexora.licensing.cli import run as run_cli
from plexora.licensing.errors import OfflineRefused, ServerError


def _cli(argv):
    lines = []
    return run_cli(argv, log=lines.append), "\n".join(lines)


def test_activate_a_seat_key(license_service):
    code, out = _cli(["activate", "PLEX-AAAA-BBBB-CCCC-DDDD", "--name", "My laptop"])
    assert code == 0, out
    request = license_service.of("/v1/activate")[0]["json"]
    assert request["credential"] == "PLEX-AAAA-BBBB-CCCC-DDDD"
    env = request["environment"]
    assert env["kind"] == "desktop" and env["display_name"] == "My laptop"
    assert env["binding"] == environment.binding()
    assert env["delegation_pubkey"] is None
    assert set(env) == {"kind", "display_name", "binding", "delegation_pubkey", "platform",
                        "scheduler_hint", "app_version"}
    st = licensing.current()
    assert st.paid and st.environment_name == "My laptop"
    assert store.read_license()["credential_hint"] == state.credential_hint(
        "PLEX-AAAA-BBBB-CCCC-DDDD")


def test_nothing_identifying_is_sent(license_service):
    import getpass
    import socket

    _cli(["activate", "PLEX-AAAA-BBBB-CCCC-DDDD"])
    sent = json.dumps(license_service.requests[0]["json"])
    assert socket.gethostname() not in sent
    # A username short enough to occur by chance in a hash says nothing.
    if len(getpass.getuser()) >= 5:
        assert getpass.getuser() not in sent
    assert environment.secret() not in sent, "the secret itself never leaves"


def test_cluster_activation_sends_a_delegation_key(license_service):
    code, out = _cli(["environment", "register", "--cluster", "--name", "O2",
                      "--credential", "PLXT1_" + "b" * 43])
    assert code == 0, out
    env = license_service.of("/v1/activate")[0]["json"]["environment"]
    assert env["kind"] == "cluster" and env["delegation_pubkey"]
    assert "one registration" in out
    assert licensing.current().environment_type == "cluster"


def test_refusal_is_a_readable_sentence(license_service):
    license_service.script("/v1/activate", 409, {"error": {
        "code": "seat_env_limit", "message": "limit", "details": {"allowed": 2}}})
    with pytest.raises(ServerError) as info:
        client.activate("PLEX-AAAA-BBBB-CCCC-DDDD")
    assert info.value.code == "seat_env_limit"
    assert "Devices & Environments" in str(info.value)
    assert info.value.detail == {"allowed": 2}


def test_cooldown_names_when(license_service):
    later = time.time() + 30 * 3600
    license_service.script("/v1/deactivate", 429, {"error": {
        "code": "cooldown_active", "message": "wait", "next_allowed_at": later}})
    with pytest.raises(ServerError) as info:
        client.deactivate("PLEXORA1.x.y.z")
    assert info.value.next_allowed_at == later
    assert "Next change allowed" in str(info.value)


def test_unparseable_error_is_bad_response(license_service):
    license_service.script("/v1/activate", 500, b"<html>oops</html>")
    with pytest.raises(ServerError) as info:
        client.activate("PLEX-AAAA-BBBB-CCCC-DDDD")
    assert info.value.code == "bad_response"


def test_non_json_success_is_bad_response(license_service):
    license_service.script("/v1/refresh", 200, b"not json")
    with pytest.raises(ServerError) as info:
        client.refresh("PLEXORA1.x.y.z")
    assert info.value.code == "bad_response"


def test_refresh_maps_revocation_errors_to_revoked(license_service):
    license_service.script("/v1/refresh", 403, {"error": {"code": "license_revoked",
                                                          "message": "gone"}})
    assert client.refresh("PLEXORA1.x.y.z")["status"] == "revoked"


def test_refresh_names_its_client_only_when_asked(license_service):
    client.refresh("PLEXORA1.x.y.z")
    client.refresh("PLEXORA1.x.y.z", via="mcp")
    first, second = (request["json"] for request in license_service.of("/v1/refresh"))
    assert "client" not in first
    assert second["client"] == "mcp"


def test_unreachable(monkeypatch):
    monkeypatch.delenv(store.ENV_OFFLINE)
    monkeypatch.setenv(store.ENV_SERVER, "http://127.0.0.1:9")
    with pytest.raises(ServerError) as info:
        client.refresh("PLEXORA1.x.y.z", timeout=1)
    assert info.value.code == "unreachable"


def test_offline_refused_before_any_socket(monkeypatch):
    monkeypatch.setenv(store.ENV_SERVER, "http://127.0.0.1:9")
    with pytest.raises(OfflineRefused):
        client.activate("PLEX-AAAA-BBBB-CCCC-DDDD")


def test_no_service_configured(monkeypatch):
    monkeypatch.delenv(store.ENV_OFFLINE)
    with pytest.raises(ServerError) as info:
        client.activate("PLEX-AAAA-BBBB-CCCC-DDDD")
    assert info.value.code == "no_server"


def test_token_is_exchanged_once_then_read_from_the_cache(license_service, monkeypatch):
    monkeypatch.setenv(store.ENV_TOKEN, "PLXT1_" + "c" * 43)
    assert licensing.allows("ai:gating")
    assert licensing.current().source == "token"
    assert len(license_service.of("/v1/activate")) == 1
    licensing.reset_for_tests()          # a second job on another node
    assert licensing.allows("ai:gating")
    assert len(license_service.of("/v1/activate")) == 1, "no second registration"


def test_token_exchange_failure_is_free_and_retried_later(license_service, monkeypatch):
    monkeypatch.setenv(store.ENV_TOKEN, "PLXT1_" + "d" * 43)
    license_service.script("/v1/activate", 503, {"error": {"code": "rate_limited",
                                                           "message": "slow down"}})
    assert not licensing.allows("ai")
    assert state._resolve_again_at is not None
    assert not licensing.allows("ai")
    assert len(license_service.of("/v1/activate")) == 1, "not retried on every call"
    monkeypatch.setattr(state, "_clock", lambda: time.time() + state.TOKEN_RETRY_SECONDS + 1)
    assert licensing.allows("ai")
    assert len(license_service.of("/v1/activate")) == 2


def test_revoked_token_reads_as_revoked(license_service, monkeypatch):
    monkeypatch.setenv(store.ENV_TOKEN, "PLXT1_" + "e" * 43)
    license_service.script("/v1/activate", 403, {"error": {"code": "credential_revoked",
                                                           "message": "revoked"}})
    assert licensing.current().state == "revoked"


def test_refresh_command(license_service, paid_license):
    code, out = _cli(["refresh"])
    assert code == 0 and "Refresh: ok" in out
    request = license_service.of("/v1/refresh")[0]["json"]
    assert request["binding"] == environment.binding()
    assert store.read_license()["last_validated"] >= int(time.time()) - 5


def test_deactivate_command(license_service, paid_license):
    code, out = _cli(["deactivate"])
    assert code == 0, out
    assert not store.license_path().exists()
    assert not licensing.current().paid


def test_heartbeat_runs_once_when_stale(license_service, license_issuer, monkeypatch):
    import threading

    monkeypatch.delenv(store.ENV_NO_HEARTBEAT)
    license_issuer.install(last_validated=time.time() - 10 * 86400)
    assert licensing.current().paid
    for thread in threading.enumerate():
        if thread.name == "plexora-license":
            thread.join(5)
    assert len(license_service.of("/v1/refresh")) == 1
    assert store.read_license()["last_validated"] >= int(time.time()) - 5
    licensing.reset_for_tests()
    licensing.current()
    assert len(license_service.of("/v1/refresh")) == 1, "fresh now, so not due again"


def test_trial_by_email(license_service):
    code, out = _cli(["trial", "--email", "someone@example.org"])
    assert code == 0 and "Check your email." in out
    body = license_service.of("/v1/trial/start")[0]["json"]
    assert body["email"] == "someone@example.org"
    assert body["fingerprint"] == environment.machine_basis()
    assert len(body["fingerprint"]) == 64


def test_trial_url_carries_only_a_hash(license_service):
    url = client.trial_url()
    assert url.startswith(license_service.url + "/portal/trial?fp=")
    assert url.endswith(environment.machine_basis())


def test_a_rolled_back_clock_is_reported_on_refresh(license_service, license_issuer, monkeypatch):
    now = time.time()
    license_issuer.install(hwm=int(now + 40 * 86400))   # this install has seen a later time
    st = licensing.current()
    assert st.clock_rollback
    state.heartbeat(st)
    assert license_service.of("/v1/refresh")[0]["json"]["flags"] == ["clock_rollback"]


def test_an_ordinary_refresh_reports_nothing(license_service, paid_license):
    state.heartbeat(licensing.current())
    assert "flags" not in license_service.of("/v1/refresh")[0]["json"]
