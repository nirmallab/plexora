"""The licence state machine: eight states, one precedence order, and a
failure mode that is always Free."""

import os
import stat
import sys
import time

import pytest

from plexora import licensing
from plexora.licensing import environment, models, state, store


@pytest.fixture
def clock(monkeypatch):
    """Move the licensing clock without touching the real one."""
    class Clock:
        def __init__(self):
            self.t = time.time()

        def __call__(self):
            return self.t

        def advance(self, seconds):
            self.t += seconds

    fake = Clock()
    monkeypatch.setattr(state, "_clock", fake)
    return fake


# -- behaviour table ------------------------------------------------------------

@pytest.mark.parametrize("name", models.STATES)
def test_behaviour_table(name):
    """Free runs in every state; Paid runs in exactly the four paid ones."""
    st = models.LicenseState(state=name, granted=("ai",), entitlements=("ai",)
                             if name in models.PAID_STATES else ())
    assert st.allows(None) and st.allows("free")
    assert st.allows("ai:gating") is (name in {"trial", "paid_active", "offline_valid",
                                               "grace"})
    assert st.paid is (name in models.PAID_STATES)


def test_with_state_keeps_plan_and_grants_consistent():
    st = models.LicenseState(state="paid_active", plan="paid", granted=("ai",),
                             entitlements=("ai",))
    expired = st.with_state("expired", "expired")
    assert expired.plan == "free" and expired.entitlements == () and expired.granted == ("ai",)
    back = expired.with_state("grace", "in_grace")
    assert back.plan == "paid" and back.entitlements == ("ai",)


# -- resolution -----------------------------------------------------------------

def test_no_licence_is_free_and_writes_nothing(tmp_path):
    st = licensing.current()
    assert (st.state, st.reason, st.plan, st.entitlements) == ("free", "no_license", "free", ())
    assert not licensing.allows("ai")
    assert licensing.allows(None)
    assert not store.license_dir().exists(), "a Free user must get no files written"


def test_paid_licence(paid_license):
    st = licensing.current()
    assert st.state == "paid_active" and st.plan == "paid"
    assert licensing.allows("ai:gating:session")
    assert licensing.allows("ai:evidence")
    assert not licensing.allows("plugin:something_else")


def test_narrow_grants_are_honoured(license_issuer):
    license_issuer.install(license_issuer.issue(entitlements=["ai:evidence"]))
    assert licensing.allows("ai:evidence")
    assert not licensing.allows("ai:gating")
    assert not licensing.allows("ai")


def test_empty_grants_unlock_nothing(license_issuer):
    """Missing limits never mean unlimited."""
    license_issuer.install(license_issuer.issue(entitlements=[]))
    assert licensing.current().state == "paid_active"
    assert not licensing.allows("ai")
    license_issuer.install(license_issuer.sign(
        {k: v for k, v in license_issuer.payload().items() if k != "entitlements"}))
    assert not licensing.allows("ai")


def test_trial(license_issuer):
    license_issuer.install(license_issuer.issue(trial=True, grace_days=0))
    st = licensing.current()
    assert st.state == "trial" and st.trial and licensing.allows("ai")


def test_offline_valid(license_issuer, tmp_path, monkeypatch):
    now = int(time.time())
    cert = license_issuer.issue(offline_until=now + 180 * 86400)
    path = tmp_path / "lab.plexora"
    path.write_text(f"# Plexora offline licence for Test Lab\n# until 2027\n{cert}\n")
    monkeypatch.setenv(store.ENV_FILE, str(path))
    st = licensing.current()
    assert st.state == "offline_valid" and st.source == "file"
    assert licensing.allows("ai")


def test_offline_until_caps_expiry(license_issuer, clock):
    now = int(clock())
    license_issuer.install(license_issuer.issue(expires_at=now + 90 * 86400,
                                                offline_until=now + 10 * 86400, grace_days=0))
    clock.advance(11 * 86400)
    assert licensing.current().state == "expired"


def test_grace_then_expired(license_issuer, clock):
    now = int(clock())
    license_issuer.install(license_issuer.issue(expires_at=now + 86400, grace_days=14))
    assert licensing.current().state == "paid_active"
    clock.advance(2 * 86400)
    st = licensing.current()
    assert st.state == "grace" and licensing.allows("ai")
    assert st.days_left(state.now()) == 13
    clock.advance(14 * 86400)
    st = licensing.current()
    assert st.state == "expired" and not licensing.allows("ai")
    assert st.granted == ("ai",), "what it used to unlock is remembered for the message"


def test_zero_grace_expires_immediately(license_issuer, clock):
    now = int(clock())
    license_issuer.install(license_issuer.issue(expires_at=now + 10, grace_days=0))
    clock.advance(11)
    assert licensing.current().state == "expired"


# -- the dates a person is shown --------------------------------------------------

DAY = 86400


def _validity():
    return licensing.describe()["validity"]


def test_the_date_shown_is_the_licences_not_the_certificates(license_issuer, clock):
    now = int(clock())
    license_issuer.install(license_issuer.issue(expires_at=now + 90 * DAY,
                                                license_expires_at=now + 365 * DAY))
    assert _validity() == {"until": now + 365 * DAY, "ended": False, "renew_by": None, "renew": None}


def test_a_certificate_left_unrenewed_asks_to_connect_before_it_runs_out(license_issuer, clock):
    now = int(clock())
    license_issuer.install(license_issuer.issue(expires_at=now + 90 * DAY,
                                                license_expires_at=now + 365 * DAY))
    clock.advance(70 * DAY)  # 20 days left: renewing quietly, nothing to say yet
    assert _validity()["renew"] is None
    clock.advance(10 * DAY)  # 10 days left and still not renewed: this machine is offline
    assert _validity() == {"until": now + 365 * DAY, "ended": False,
                           "renew_by": now + 90 * DAY, "renew": "online"}


def test_grace_on_a_running_licence_is_a_renewal_deadline(license_issuer, clock):
    now = int(clock())
    license_issuer.install(license_issuer.issue(expires_at=now + 90 * DAY, grace_days=14,
                                                license_expires_at=now + 365 * DAY))
    clock.advance(91 * DAY)
    assert licensing.current().state == "grace"
    assert _validity() == {"until": now + 365 * DAY, "ended": False,
                           "renew_by": now + 104 * DAY, "renew": "online"}
    clock.advance(14 * DAY)
    assert licensing.current().state == "expired"
    assert _validity() == {"until": now + 365 * DAY, "ended": False, "renew_by": None,
                           "renew": "online"}, "paused, not over: renewing brings it back"


def test_a_licence_that_ended_says_ended(license_issuer, clock):
    now = int(clock())
    license_issuer.install(license_issuer.issue(expires_at=now + 10 * DAY, grace_days=14,
                                                license_expires_at=now + 10 * DAY))
    clock.advance(11 * DAY)
    assert licensing.current().state == "grace"
    assert _validity() == {"until": now + 10 * DAY, "ended": True, "renew_by": None, "renew": None}


def test_an_offline_file_that_ends_before_the_licence_says_so(license_issuer, clock, tmp_path,
                                                              monkeypatch):
    now = int(clock())
    cert = license_issuer.issue(expires_at=now + 180 * DAY, offline_until=now + 180 * DAY,
                                license_expires_at=now + 365 * DAY)
    path = tmp_path / "lab.plexora"
    path.write_text(cert)
    monkeypatch.setenv(store.ENV_FILE, str(path))
    assert _validity() == {"until": now + 365 * DAY, "ended": False,
                           "renew_by": now + 180 * DAY, "renew": "file"}


def test_a_certificate_from_before_the_licence_date_shows_its_own(license_issuer, clock):
    now = int(clock())
    payload = license_issuer.payload(expires_at=now + 90 * DAY)
    del payload["license_expires_at"]
    license_issuer.install(license_issuer.sign(payload))
    assert _validity() == {"until": now + 90 * DAY, "ended": False, "renew_by": None, "renew": None}


def test_a_revoked_licence_shows_no_date(license_issuer):
    license_issuer.install(revoked={"at": int(time.time())})
    assert licensing.current().state == "revoked"
    assert _validity()["until"] is None


def test_revoked_flag(license_issuer):
    license_issuer.install(revoked={"at": int(time.time()), "reason": "license_revoked"})
    st = licensing.current()
    assert st.state == "revoked" and not licensing.allows("ai")


@pytest.mark.parametrize("damage", ["", "{", "[]", '{"certificate": 5}',
                                    '{"certificate": "PLEXORA1.x.y.z"}', "\x00\xff"])
def test_damaged_licence_file_fails_open(damage, license_issuer):
    path = store.license_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(damage.encode("latin-1"))
    st = licensing.current()
    assert not st.paid
    assert st.state in ("free", "invalid")
    assert licensing.allows(None)


def test_tampered_cached_certificate_is_invalid(license_issuer):
    cert = license_issuer.issue()
    prefix, kid, body, sig = cert.split(".")
    license_issuer.install(".".join([prefix, kid, body, sig[:-4] + "AAAA"]))
    st = licensing.current()
    assert st.state == "invalid" and st.reason == "bad_signature"


def test_certificate_for_another_environment_is_invalid(license_issuer):
    license_issuer.install(license_issuer.issue(env_binding="0" * 64))
    st = licensing.current()
    assert st.state == "invalid" and st.reason == "environment_mismatch"


def test_copied_license_json_without_the_environment_is_invalid(license_issuer):
    license_issuer.install()
    environment.forget()
    licensing.reset_for_tests()
    assert licensing.current().reason == "environment_mismatch"


def test_unbound_certificate_needs_no_environment(license_issuer):
    license_issuer.install(license_issuer.issue(env_binding=None))
    environment.forget()
    licensing.reset_for_tests()
    assert licensing.current().paid


def test_unknown_plan_is_invalid(license_issuer):
    license_issuer.install(license_issuer.issue(plan="enterprise"))
    assert licensing.current().reason == "unknown_plan"


def test_precedence_env_file_beats_cache(license_issuer, tmp_path, monkeypatch):
    license_issuer.install(license_issuer.issue(entitlements=["ai:evidence"]))
    file_cert = license_issuer.issue(entitlements=["ai"], offline_until=int(time.time()) + 86400)
    path = tmp_path / "x.plexora"
    path.write_text(file_cert)
    monkeypatch.setenv(store.ENV_FILE, str(path))
    assert licensing.current().source == "file"
    assert licensing.allows("ai:gating")


def test_unreadable_env_file_is_invalid_not_fatal(monkeypatch, tmp_path):
    monkeypatch.setenv(store.ENV_FILE, str(tmp_path / "missing.plexora"))
    st = licensing.current()
    assert st.state == "invalid" and st.reason == "unreadable_file"


def test_token_without_network_does_not_stick(monkeypatch):
    """peek() must never make the exchange; and its Free answer is not cached."""
    monkeypatch.setenv(store.ENV_TOKEN, "PLXT1_" + "a" * 43)
    monkeypatch.delenv(store.ENV_OFFLINE)
    monkeypatch.setenv(store.ENV_SERVER, "http://127.0.0.1:9")
    st = licensing.peek()
    assert st.reason == "token_not_exchanged"
    assert state._state is None


def test_token_under_offline_is_free(monkeypatch):
    monkeypatch.setenv(store.ENV_TOKEN, "PLXT1_" + "a" * 43)
    st = licensing.current()
    assert st.reason == "offline_refused" and not st.paid


def test_token_with_no_service_configured(monkeypatch):
    monkeypatch.setenv(store.ENV_TOKEN, "PLXT1_" + "a" * 43)
    monkeypatch.delenv(store.ENV_OFFLINE)
    assert licensing.current().reason == "token_not_exchanged"


def test_offline_mode_never_opens_a_socket(paid_license, monkeypatch, clock):
    """PLEXORA_LICENSE_OFFLINE means no licensing network, even when due."""
    import socket

    def refuse(*args, **kwargs):
        raise AssertionError("a socket was opened under PLEXORA_LICENSE_OFFLINE")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.delenv(store.ENV_NO_HEARTBEAT)
    monkeypatch.setenv(store.ENV_SERVER, "http://127.0.0.1:9")
    clock.advance(30 * 86400)
    licensing.reset_for_tests()
    assert licensing.current().paid
    assert not [t for t in __import__("threading").enumerate() if t.name == "plexora-license"]


# -- the clock ------------------------------------------------------------------

def test_clock_rollback_cannot_revive_an_expired_licence(license_issuer, clock):
    now = int(clock())
    license_issuer.install(license_issuer.issue(expires_at=now + 86400, grace_days=0))
    clock.advance(2 * 86400)
    assert licensing.current().state == "expired"      # this advances the mark
    clock.advance(-30 * 86400)                          # wind the clock back a month
    licensing.reset_for_tests()
    st = licensing.current()
    assert st.state == "expired"
    assert st.clock_rollback


def test_high_water_mark_is_advanced_at_resolution(license_issuer, clock):
    license_issuer.install()
    before = store.read_license()["hwm"]
    clock.advance(2 * 3600)
    licensing.reset_for_tests()
    licensing.current()
    assert store.read_license()["hwm"] > before


def test_server_time_advances_the_floor(paid_license, clock):
    st = licensing.current()
    ahead = clock() + 5 * 86400
    assert state.apply_refresh({"status": "ok", "server_time": ahead}, st) == "ok"
    assert state.now() >= ahead
    assert store.read_license()["hwm"] >= int(ahead)


# -- refresh outcomes -------------------------------------------------------------

def test_refresh_revoked(paid_license):
    st = licensing.current()
    assert state.apply_refresh({"status": "revoked", "server_time": time.time()}, st) \
        == "revoked"
    assert licensing.current().state == "revoked"
    licensing.reset_for_tests()
    assert licensing.current().state == "revoked", "revocation survives a restart"


def test_refresh_renewed(paid_license, clock):
    st = licensing.current()
    fresh = paid_license.issue(cert_id="crt_new", expires_at=int(clock()) + 200 * 86400)
    assert state.apply_refresh({"status": "renewed", "certificate": fresh,
                                "server_time": clock()}, st) == "renewed"
    assert licensing.current().cert_id == "crt_new"
    assert store.read_license()["certificate"] == fresh


def test_refresh_with_a_forged_renewal_keeps_the_old(paid_license):
    st = licensing.current()
    assert state.apply_refresh({"status": "renewed", "certificate": "PLEXORA1.x.e30.AA"},
                               st) == "rejected"
    assert licensing.current().cert_id == "crt_test0001"


def test_refresh_nonsense_is_ignored(paid_license):
    st = licensing.current()
    assert state.apply_refresh({"status": "maybe"}, st) == "ignored"
    assert state.apply_refresh(None, st) == "ignored"
    assert licensing.current().paid


# -- when the heartbeat is due ------------------------------------------------------

def _st(**kw):
    base = dict(state="paid_active", source="cache", certificate="x",
                expires_at=time.time() + 60 * 86400, last_validated=time.time())
    base.update(kw)
    return models.LicenseState(**base)


def test_due_rules(clock):
    now = clock()
    assert not state._due(_st(last_validated=now - 86400), {})
    assert state._due(_st(last_validated=now - 8 * 86400), {})
    assert state._due(_st(last_validated=None), {})
    assert state._due(_st(last_validated=now + 3 * 86400), {}), "clock went backwards"
    assert state._due(_st(last_validated=now - 2 * 86400, expires_at=now + 5 * 86400), {})
    assert not state._due(_st(last_validated=now - 8 * 86400), {"last_attempt": now - 60})


def test_heartbeat_does_not_start_for_free_or_offline_or_subprocess(monkeypatch, paid_license):
    monkeypatch.delenv(store.ENV_NO_HEARTBEAT)
    started = []
    monkeypatch.setattr(state.threading, "Thread",
                        lambda *a, **k: started.append(k) or type("T", (), {"start": lambda s: None})())
    state._maybe_start_heartbeat(models.free_state())
    state._maybe_start_heartbeat(_st(offline_until=time.time() + 1))
    assert not started  # OFFLINE is still set by the suite
    monkeypatch.delenv(store.ENV_OFFLINE)
    monkeypatch.setenv(store.ENV_SERVER, "http://127.0.0.1:9")
    state._maybe_start_heartbeat(_st(last_validated=time.time() - 30 * 86400))
    assert len(started) == 1


# -- the files ----------------------------------------------------------------------

@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX modes")
def test_files_are_private(paid_license):
    for path in (store.license_path(), store.environment_path()):
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600, (path, oct(mode))
    assert stat.S_IMODE(os.stat(store.license_dir()).st_mode) == 0o700


def test_licence_lives_outside_the_data_root_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv(store.ENV_DIR)
    from plexora import paths

    assert paths.data_root() not in store.license_dir().parents


def test_environment_secret_is_created_once(tmp_path):
    first = environment.secret(create=True)
    assert environment.secret(create=True) == first
    assert environment.binding() == environment.binding_of(first)
    assert len(environment.binding()) == 64


def test_environment_secret_race_has_one_winner():
    """Two jobs on two nodes creating the secret at once end with ONE identity."""
    import threading

    results, barrier = [], threading.Barrier(8)

    def create():
        barrier.wait()
        results.append(environment.secret(create=True))

    threads = [threading.Thread(target=create) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(results)) == 1
    assert environment.secret() == results[0]


def test_describe_never_exposes_the_certificate(paid_license):
    info = licensing.describe()
    text = repr(info) + repr(licensing.current())
    assert paid_license.issue()[:40] not in text
    assert "certificate" not in info
    assert "account_id" not in info
    assert info["plan"] == "paid" and info["days_left"] >= 89


def test_importing_licensing_loads_no_crypto():
    import subprocess

    code = ("import sys; import plexora.licensing, plexora.licensing.manifest; "
            "print('cryptography' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={**os.environ, "PLEXORA_LICENSE_OFFLINE": "1"})
    assert out.stdout.strip().splitlines()[-1] == "False", out.stderr
