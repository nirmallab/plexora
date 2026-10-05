"""Resolve the licence once, lazily, and keep it fresh only when it must be.

Three rules drive everything here:

* Nothing happens at import. The state is resolved the first time something
  asks -- an entitled capability, the Settings page, `plexora license status`
  -- and a Free capability never asks (see `guards.check_capability`).
* Resolution fails OPEN, to Free. A damaged file, an unknown key, a server
  that is down: each is a state with a reason, never an exception, and never
  anything that stops Plexora starting, opening data, rendering or exporting.
* The network is touched only on purpose. A cached certificate is refreshed
  in the background only when its last check is more than a week old (or it
  is close to running out), never in a subprocess, never under
  `PLEXORA_LICENSE_OFFLINE`, and never at all without a certificate -- which
  means never for a Free user. The one exception is `plexora mcp serve`, which
  calls `refresh_now` at start and every 15 minutes (`plexora/mcp/server.py`),
  so a revocation or a grant change reaches an outside agent within minutes;
  the same hard stops apply to it.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import replace

from plexora.licensing import certificate, delegation, environment, store
from plexora.licensing.entitlements import normalize
from plexora.licensing.errors import LicenseError, ServerError
from plexora.licensing.models import LicenseState, free_state

log = logging.getLogger("plexora.licensing")

#: How stale a certificate's last server check may get before a refresh.
REFRESH_STALE_SECONDS = 7 * 86400
#: A certificate this close to its expiry is worth asking about sooner: the
#: server renews one inside this window.
RENEW_WINDOW_SECONDS = 21 * 86400
#: However due a refresh is, one that failed is not retried sooner than this.
RETRY_SECONDS = 6 * 3600
#: A clock this far behind what this installation has already seen is not
#: drift, it is a clock that was set back.
ROLLBACK_TOLERANCE = 86400
#: How long a failed token exchange is remembered before trying again.
TOKEN_RETRY_SECONDS = 300

_lock = threading.RLock()
_state: LicenseState | None = None
_resolve_again_at: float | None = None
_clock_floor = 0.0
_heartbeat_started = False
_said: set[str] = set()

#: The wall clock. A module attribute so tests can move time.
_clock = time.time


def now() -> float:
    """Wall-clock time, never earlier than the latest this install has seen.

    Winding the clock back is the cheapest way to outlive an expiry date;
    comparing against the high-water mark turns it into a no-op. Advisory, not
    tamper-proof: the mark is a number in a file the user owns.
    """
    return max(_clock(), _clock_floor)


def say_once(key: str, message: str, level: int = logging.WARNING) -> None:
    with _lock:
        if key in _said:
            return
        _said.add(key)
    log.log(level, message)


def current(*, network: bool = True) -> LicenseState:
    """The licence, resolving it on first use.

    Expiry is re-applied on every call, not only at resolution, so a server
    that has been up for a month drops into grace, and then to Free, at the
    moment it should. With `network=False` a licence token that has not been
    exchanged yet reads as Free without the exchange being attempted, and that
    answer is not remembered -- the next caller that allows the network tries.
    """
    global _state, _resolve_again_at
    state = _state
    if state is None or (_resolve_again_at is not None and _clock() >= _resolve_again_at):
        with _lock:
            if _state is None or (_resolve_again_at is not None
                                  and _clock() >= _resolve_again_at):
                resolved, complete = _resolve(network=network)
                if not complete:
                    return _apply_expiry(resolved)
                _state = resolved
                _maybe_start_heartbeat(resolved)
            state = _state
    return _apply_expiry(state)


def peek() -> LicenseState:
    """`current` without any network call: for page renders and status lines."""
    return current(network=False)


def reset() -> None:
    """Forget everything resolved. The next `current()` starts again."""
    global _state, _resolve_again_at, _clock_floor, _heartbeat_started
    with _lock:
        _state = None
        _resolve_again_at = None
        _clock_floor = 0.0
        _heartbeat_started = False
        _said.clear()


def reload(*, network: bool = True) -> LicenseState:
    reset()
    return current(network=network)


def set_state(state: LicenseState) -> None:
    global _state, _resolve_again_at
    with _lock:
        _state = state
        _resolve_again_at = None


def advance_clock_floor(moment) -> None:
    """Record that `moment` has genuinely happened (the server said so)."""
    global _clock_floor
    try:
        value = float(moment)
    except (TypeError, ValueError):
        return
    with _lock:
        if value > _clock_floor:
            _clock_floor = value


# -- resolution ---------------------------------------------------------------


def _resolve(*, network: bool) -> tuple[LicenseState, bool]:
    global _resolve_again_at
    _resolve_again_at = None
    record = store.read_license()
    _load_clock_floor(record)

    job = store.env_job_cert()
    if job:
        return _state_for_job(job), True

    token = store.env_token()
    if token:
        return _state_for_token(token, record, network=network)

    path = store.env_file()
    if path:
        try:
            cert = store.read_license_file(path)
        except (OSError, ValueError) as exc:
            say_once("license-file", f"PLEXORA_LICENSE_FILE points at {path}, which could "
                                     f"not be read as a licence file ({exc}). Continuing "
                                     f"on Free.")
            return free_state("unreadable_file", source="file", state="invalid"), True
        return state_for_certificate(cert, source="file"), True

    cert = record.get("certificate")
    if cert:
        return _state_for_record(record), True
    return free_state(), True


def _load_clock_floor(record: dict) -> None:
    global _clock_floor
    try:
        hwm = float(record.get("hwm") or 0.0)
    except (TypeError, ValueError):
        hwm = 0.0
    _clock_floor = max(_clock_floor, hwm)
    wall = _clock()
    # Advance the mark at most hourly, and only where there is a licence file
    # to keep it in: a Free user gets no file written, ever.
    if record.get("certificate") and wall > hwm + 3600:
        try:
            store.update_license(hwm=int(wall))
        except OSError:
            pass


def _rolled_back() -> bool:
    return _clock() < _clock_floor - ROLLBACK_TOLERANCE


def _state_for_record(record: dict) -> LicenseState:
    state = state_for_certificate(record["certificate"], source="cache", record=record)
    if record.get("revoked") and state.state != "invalid":
        return state.with_state("revoked", "revoked")
    return state


def state_for_certificate(cert: str, *, source: str, record: dict | None = None,
                          keys: dict | None = None, bound: bool = True) -> LicenseState:
    """Verify `cert` and turn it into a state (before expiry is applied)."""
    try:
        payload = certificate.verify(cert, now=now(), keys=keys)
    except LicenseError as exc:
        say_once(f"certificate:{source}", f"{exc} Continuing on Free.")
        return free_state(exc.code, source=source, state="invalid")
    return _state_for_payload(payload, cert, source=source, record=record, bound=bound)


def _state_for_payload(payload: dict, cert: str, *, source: str, record: dict | None,
                       bound: bool) -> LicenseState:
    if payload.get("plan") != "paid":
        return free_state("unknown_plan", source=source, state="invalid")
    binding = payload.get("env_binding")
    if bound and binding:
        if environment.binding() != binding:
            say_once("environment-mismatch",
                     "This licence certificate was issued for a different registered "
                     "environment. Run `plexora license activate` here, or install "
                     "the certificate issued for this environment. Continuing on Free.")
            return free_state("environment_mismatch", source=source, state="invalid")

    def _num(name):
        value = payload.get(name)
        return float(value) if isinstance(value, (int, float)) else None

    expires = _num("expires_at")
    offline_until = _num("offline_until")
    if offline_until is not None:
        expires = offline_until if expires is None else min(expires, offline_until)
    grace_days = payload.get("grace_days")
    grace = float(grace_days) * 86400 if isinstance(grace_days, (int, float)) and grace_days > 0 else 0.0
    trial = payload.get("trial") is True
    base = "trial" if trial else "offline_valid" if offline_until is not None else "paid_active"
    granted = normalize(payload.get("entitlements"))
    record = record or {}
    env_record = record.get("environment") if isinstance(record.get("environment"), dict) else {}
    last_validated = record.get("last_validated")
    return LicenseState(
        state=base, reason="valid", plan="paid", entitlements=granted, granted=granted,
        source=source, trial=trial,
        use_class=payload.get("use_class") if isinstance(payload.get("use_class"), str) else None,
        license_id=payload.get("license_id"), account_id=payload.get("account_id"),
        seat_id=payload.get("seat_id"), cert_id=payload.get("cert_id"),
        environment_id=payload.get("environment_id"),
        environment_type=payload.get("environment_type"),
        environment_name=env_record.get("name"),
        issued_at=_num("issued_at"), expires_at=expires,
        license_expires_at=_num("license_expires_at"),
        grace_until=(expires + grace) if expires is not None else None,
        offline_until=offline_until,
        last_validated=float(last_validated) if isinstance(last_validated, (int, float)) else None,
        clock_rollback=_rolled_back(),
        certificate=cert,
    )


def _state_for_job(text: str) -> LicenseState:
    """`PLEXORA_LICENSE_JOB_CERT`: a job certificate minted locally by a
    registered cluster (PLEXORAD1), or one the service signed (`/v1/delegate`,
    a PLEXORA1 certificate of type `job`). Neither is bound to an environment
    secret -- a job has none -- so both are short-lived by construction, and a
    PLEXORA1 certificate of any OTHER type is refused here: an environment's
    own certificate must not escape its binding by being put in this variable.
    """
    try:
        if certificate.looks_like(text):
            payload = certificate.verify(text, now=now())
            issued = payload.get("issued_at") or 0
            expires = payload.get("expires_at") or 0
            if payload.get("environment_type") != "job" or \
                    expires - issued > delegation.MAX_LIFETIME_SECONDS \
                    + certificate.MAX_CLOCK_SKEW_SECONDS:
                raise LicenseError("PLEXORA_LICENSE_JOB_CERT holds an environment "
                                   "certificate, not a job certificate",
                                   code="environment_mismatch")
            payload = {**payload, "grace_days": 0}
        else:
            payload = delegation.verify(text, now=now())
    except LicenseError as exc:
        say_once("job-cert", f"PLEXORA_LICENSE_JOB_CERT: {exc} Continuing on Free.")
        return free_state(exc.code, source="job", state="invalid")
    return _state_for_payload(payload, text, source="job", record=None, bound=False)


def credential_hint(credential: str) -> str:
    """A short, non-reversible tag for which credential a cached cert came from."""
    return hashlib.sha256(credential.strip().encode("utf-8")).hexdigest()[:16]


def _state_for_token(token: str, record: dict, *, network: bool) -> tuple[LicenseState, bool]:
    """A licence token (`PLEXORA_LICENSE_TOKEN`), exchanged once and cached.

    A certificate already obtained with this same token is used as it is --
    which on a cluster, where license.json sits in `$HOME`, means the first job
    exchanges the token and every later job on every node just reads the file.
    """
    global _resolve_again_at
    hint = credential_hint(token)
    if record.get("certificate") and record.get("credential_hint") == hint:
        cached = _state_for_record(record)
        if cached.state not in ("invalid", "expired"):
            return replace(cached, source="token"), True
    if store.offline_only():
        say_once("token-offline", "PLEXORA_LICENSE_TOKEN is set but PLEXORA_LICENSE_OFFLINE "
                                  "forbids network calls, so it cannot be exchanged. "
                                  "Continuing on Free; install a licence file instead.")
        return free_state("offline_refused", source="token"), True
    if not network:
        return free_state("token_not_exchanged", source="token"), False
    if not store.server_url():
        say_once("token-no-server", "PLEXORA_LICENSE_TOKEN is set but no licence service is "
                                    "configured (PLEXORA_LICENSE_SERVER). Continuing on Free.")
        return free_state("token_not_exchanged", source="token"), True

    from plexora.licensing import client

    try:
        result = client.activate(token)
    except ServerError as exc:
        say_once("token-exchange", f"PLEXORA_LICENSE_TOKEN could not be exchanged: {exc} "
                                   f"Continuing on Free.")
        if exc.code in ("unreachable", "bad_response", "rate_limited"):
            _resolve_again_at = _clock() + TOKEN_RETRY_SECONDS
        if exc.code in ("credential_revoked", "license_revoked"):
            return free_state("revoked", source="token", state="revoked"), True
        if exc.code == "license_expired":
            return free_state("expired", source="token", state="expired"), True
        return free_state("token_not_exchanged", source="token"), True
    except LicenseError as exc:
        say_once("token-exchange", f"{exc} Continuing on Free.")
        return free_state("token_not_exchanged", source="token"), True
    saved = save_activation(result, credential=token, source="token")
    state = _state_for_record(saved)
    return replace(state, source="token"), True


def save_activation(result: dict, *, credential: str, source: str) -> dict:
    """Persist what `/v1/activate` returned. Returns the record written (or
    that would have been, when the config directory is read-only)."""
    moment = int(_clock())
    server_time = result.get("server_time")
    advance_clock_floor(server_time)
    record = {
        "certificate": result["certificate"],
        "source": source,
        "credential_hint": credential_hint(credential),
        "installed_at": moment,
        "last_validated": moment,
        "last_attempt": moment,
        "hwm": int(max(moment, float(server_time or 0))),
        "server": store.server_url(),
        "environment": result.get("environment") or {},
        "revoked": None,
    }
    try:
        store.write_license(record)
    except OSError as exc:
        say_once("license-write", f"The licence could not be saved ({exc}); it works for "
                                  f"this process only.")
    return record


def install_certificate(cert: str, *, source: str = "install",
                        environment_info: dict | None = None) -> LicenseState:
    """Verify and save a certificate someone installed by hand."""
    state = state_for_certificate(cert, source="cache")
    if state.state == "invalid":
        raise LicenseError(_invalid_sentence(state.reason), code=state.reason)
    moment = int(_clock())
    store.write_license({
        "certificate": cert.strip(), "source": source, "credential_hint": None,
        "installed_at": moment, "last_validated": None, "last_attempt": None,
        "hwm": int(max(moment, _clock_floor)), "server": store.server_url(),
        "environment": environment_info or {}, "revoked": None,
    })
    return reload(network=False)


def _invalid_sentence(reason: str) -> str:
    return {
        "environment_mismatch": "This certificate was issued for a different environment.",
        "unknown_key": "This certificate was signed with a key this Plexora does not trust.",
        "bad_signature": "This certificate has been altered and does not verify.",
        "wrong_product": "This is not a Plexora certificate.",
        "unsupported_version": "This certificate needs a newer Plexora.",
        "future_dated": "This certificate is dated in the future; check the clock.",
        "unknown_plan": "This certificate names a plan this Plexora does not know.",
    }.get(reason, "This is not a valid Plexora certificate.")


# -- expiry ---------------------------------------------------------------------


def _apply_expiry(state: LicenseState) -> LicenseState:
    """Move a paid state to grace or expired when its time has come.

    On every check, so the common case -- a licence that is simply valid -- is
    one comparison returning the same object.
    """
    global _state
    if not state.paid or state.expires_at is None:
        return state
    moment = now()
    if moment <= state.expires_at:
        return state
    if state.grace_until is not None and moment <= state.grace_until:
        if state.state == "grace":
            return state
        moved = state.with_state("grace", "in_grace")
        say_once("grace", f"Your Plexora licence has expired. Paid features keep working "
                          f"for {moved.days_left(moment)} more day(s); renew to avoid an "
                          f"interruption. Everything Free is unaffected.")
    else:
        moved = state.with_state("expired", "expired")
        say_once("expired", "Your Plexora licence has expired; Paid features are off. "
                            "Everything Free, and everything you made with Paid features, "
                            "is unaffected.", logging.INFO)
    with _lock:
        if _state is state:
            _state = moved
    return moved


# -- the heartbeat ----------------------------------------------------------------


def _due(state: LicenseState, record: dict) -> bool:
    wall = _clock()
    last_attempt = record.get("last_attempt")
    if isinstance(last_attempt, (int, float)) and 0 <= wall - last_attempt < RETRY_SECONDS:
        return False
    if state.clock_rollback:
        return True
    last = state.last_validated
    if last is None:
        return True
    age = wall - last
    if age > REFRESH_STALE_SECONDS or age < -ROLLBACK_TOLERANCE:
        return True
    if state.expires_at is not None and state.expires_at - now() < RENEW_WINDOW_SECONDS \
            and age > 86400:
        return True
    return state.state in ("grace", "expired") and age > 86400


def refreshable(state: LicenseState) -> str | None:
    """Why `state` is never refreshed from the service, or None when it may be.

    The hard stops shared by the lazy heartbeat and `refresh_now`: no
    certificate (which means Free), one that did not come from the cache or a
    token, one already invalid or revoked, an offline licence file, the offline
    and no-heartbeat switches, no service URL, and a child process."""
    if state.source not in ("cache", "token") or not state.certificate:
        return "no_certificate"
    if state.state in ("invalid", "revoked"):
        return state.state
    if state.offline_until is not None:
        return "offline_licence"
    if store.offline_only() or store.heartbeat_disabled():
        return "disabled"
    if not store.server_url():
        return "no_server"
    import multiprocessing

    if multiprocessing.parent_process() is not None:
        return "subprocess"
    return None


def _maybe_start_heartbeat(state: LicenseState) -> None:
    """Refresh a cached certificate in the background, if it is worth it."""
    global _heartbeat_started
    if _heartbeat_started or refreshable(state) is not None:
        return
    record = store.read_license()
    if not _due(state, record):
        return
    _heartbeat_started = True
    threading.Thread(target=heartbeat, args=(state,), name="plexora-license",
                     daemon=True).start()


def heartbeat(state: LicenseState, *, timeout: float | None = None, via: str | None = None) -> str:
    """Refresh the cached certificate once. Every failure is silent: the
    certificate already in hand stands. Returns what happened, for tests."""
    from plexora.licensing import client

    try:
        store.update_license(last_attempt=int(_clock()))
    except OSError:
        pass
    options = {"via": via} if via else {}
    if timeout is not None:
        options["timeout"] = timeout
    try:
        result = client.refresh(state.certificate,
                                flags=("clock_rollback",) if state.clock_rollback else (),
                                **options)
    except Exception:  # noqa: BLE001 - fail open; the certificate stands
        return "failed"
    return apply_refresh(result, state)


def refresh_now(*, reason: str, timeout: float | None = None) -> str:
    """Ask the service about the cached certificate now, whatever its age.

    For a long-lived process that must notice a revocation or a grant change
    within minutes: the MCP server, at start and on its recheck interval. The
    hard stops of `refreshable` still apply (`"skipped"`), so a Free machine,
    an offline licence or `PLEXORA_LICENSE_OFFLINE` never touches the network.
    Marks the lazy heartbeat as done, so this process does not also start one.
    Returns what `heartbeat` returned, or `"skipped"`."""
    global _heartbeat_started
    with _lock:
        _heartbeat_started = True
    state = current()
    why = refreshable(state)
    if why is not None:
        log.debug("licence refresh (%s) skipped: %s", reason, why)
        return "skipped"
    outcome = heartbeat(state, timeout=timeout, via="mcp")
    log.debug("licence refresh (%s): %s", reason, outcome)
    return outcome


def apply_refresh(result: dict, state: LicenseState) -> str:
    status = (result or {}).get("status")
    server_time = (result or {}).get("server_time")
    advance_clock_floor(server_time)
    moment = int(_clock())
    hwm = int(max(moment, float(server_time or 0)))
    try:
        if status == "revoked":
            store.update_license(revoked={"at": moment, "reason": result.get("reason")},
                                 last_validated=moment, hwm=hwm)
            set_state(state.with_state("revoked", "revoked"))
            say_once("revoked", "This Plexora licence has been revoked; Paid features are "
                                "off. Everything Free is unaffected.")
            return "revoked"
        if status == "renewed" and result.get("certificate"):
            fresh = state_for_certificate(result["certificate"], source=state.source)
            if fresh.state == "invalid":
                return "rejected"
            record = store.update_license(certificate=result["certificate"],
                                          last_validated=moment, hwm=hwm, revoked=None)
            env_record = ((record or {}).get("environment") or {}) if record else {}
            set_state(replace(fresh, last_validated=float(moment),
                              environment_name=env_record.get("name")
                              or state.environment_name))
            return "renewed"
        if status == "ok":
            store.update_license(last_validated=moment, hwm=hwm)
            set_state(replace(state, last_validated=float(moment), clock_rollback=False))
            return "ok"
    except OSError:
        return "failed"
    return "ignored"
