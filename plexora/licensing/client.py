"""Talking to the licence service -- the only module here that opens a socket.

Every call is optional. Plexora never needs the service to RUN; it needs it
only to obtain, refresh or release a certificate. A failure is a clear,
actionable `ServerError` when a person asked for the action, and silence when
it is the background refresh (the caller swallows it).

Standard library only (`urllib`): a licensing call is a handful of small JSON
requests a week at most, and pulling an HTTP stack into the import graph for
that would cost every Free user something for nothing.

What is sent is the credential or certificate, the environment BINDING (a hash
of a random secret, see `environment.py`), a coarse platform family, the
scheduler family and the Plexora version. Never a hostname, a username, a MAC,
a path, a project, or anything about data.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from plexora.licensing import environment, store
from plexora.licensing.errors import OfflineRefused, ServerError

#: A person is waiting on these; they can afford a little patience.
INTERACTIVE_TIMEOUT = 15
#: These run behind somebody's work and must never be noticed.
BACKGROUND_TIMEOUT = 5

#: The error codes the service may answer with, mirrored from
#: `licensing/src/http.ts`. A code outside this list is reported as-is.
SERVER_CODES = (
    "invalid_request", "invalid_credential", "credential_revoked", "credential_expired",
    "scope_not_allowed", "license_expired", "license_revoked", "license_suspended",
    "seat_env_limit", "cooldown_active", "rate_limited", "trial_already_issued",
    "trial_machine_limit", "trial_not_available", "forged_certificate",
    "environment_unknown", "environment_mismatch", "not_delegating", "offline_not_allowed",
    "signing_unavailable", "not_found", "internal_error",
)

_SENTENCES = {
    "invalid_credential": "That licence key or token is not recognised.",
    "credential_revoked": "That licence key or token has been revoked.",
    "credential_expired": "That licence token has expired; create a new one in the portal.",
    "scope_not_allowed": "That licence token is not allowed to do this.",
    "license_expired": "This licence has expired.",
    "license_revoked": "This licence has been revoked.",
    "license_suspended": "This licence is suspended; contact whoever manages it.",
    "seat_env_limit": ("This seat already has as many environments as it allows. Remove "
                       "one in the portal (Devices & Environments) or run "
                       "`plexora license deactivate` where it is no longer needed."),
    "cooldown_active": "Environments on this seat were changed recently; try again later.",
    "rate_limited": "Too many requests; try again shortly.",
    "trial_already_issued": "A trial has already been issued to that email address.",
    "trial_machine_limit": "This machine has already had the trials it is allowed.",
    "trial_not_available": "A trial is not available for this account.",
    "forged_certificate": "The licence service did not recognise this certificate.",
    "environment_unknown": "The licence service does not know this environment.",
    "environment_mismatch": "This certificate belongs to a different environment.",
    "offline_not_allowed": "This licence does not include offline certificates.",
    "signing_unavailable": "The licence service cannot issue certificates right now.",
}


def _user_agent() -> str:
    return f"plexora/{environment.plexora_version()} (licensing)"


def _require_service(action: str) -> str:
    if store.offline_only():
        raise OfflineRefused(
            f"{action} needs the licence service, but PLEXORA_LICENSE_OFFLINE is set so "
            f"Plexora makes no licensing network call. Unset it, or install an offline "
            f"licence file with `plexora license install <file>`.")
    base = store.server_url()
    if not base:
        raise ServerError(
            f"{action} needs the licence service, and none is configured for this build "
            f"(set PLEXORA_LICENSE_SERVER).", code="no_server")
    return base


def _post(path: str, body: dict, *, action: str, timeout: float) -> dict:
    base = _require_service(action)
    data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        f"{base}{path}", data=data, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": _user_agent()})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(1024 * 1024)
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read(64 * 1024) if exc.fp is not None else b""
        raise _error_from(exc.code, raw, action) from None
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ServerError(f"Could not reach the licence service ({base}): "
                          f"{type(exc).__name__}. Paid features keep working from the "
                          f"certificate already on this machine, if there is one.",
                          code="unreachable") from None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ServerError(f"{action}: the licence service answered with something that is "
                          f"not JSON.", code="bad_response", status=status) from None
    if not isinstance(payload, dict):
        raise ServerError(f"{action}: unexpected answer from the licence service.",
                          code="bad_response", status=status)
    return payload


def _error_from(status: int, raw: bytes, action: str) -> ServerError:
    """A structured refusal as a ServerError. Anything unparseable is
    `bad_response`, which every caller treats as "stay where you are"."""
    try:
        error = json.loads(raw.decode("utf-8")).get("error") or {}
    except (ValueError, UnicodeDecodeError, AttributeError):
        error = {}
    if not isinstance(error, dict):
        error = {}
    code = error.get("code") if isinstance(error.get("code"), str) else "bad_response"
    message = _SENTENCES.get(code) or error.get("message") or f"HTTP {status}"
    retry_after = error.get("retry_after")
    next_allowed = error.get("next_allowed_at")
    if code == "cooldown_active" and isinstance(next_allowed, (int, float)):
        message += " Next change allowed " + time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(float(next_allowed))) + "."
    return ServerError(f"{action} was refused: {message}", code=code, status=status,
                       detail=error.get("details"),
                       retry_after=retry_after if isinstance(retry_after, int) else None,
                       next_allowed_at=next_allowed if isinstance(next_allowed, (int, float))
                       else None)


def _environment_block(kind: str, name: str | None, *, create: bool = True) -> dict:
    delegation = environment.delegation_keypair(create=create) if kind != "desktop" else None
    return {
        "kind": kind,
        "display_name": (name or "").strip()[:80] or environment.suggested_name(kind),
        "binding": environment.binding(create=create),
        "delegation_pubkey": delegation[1] if delegation else None,
        "platform": environment.platform_label(),
        "scheduler_hint": environment.scheduler_hint(),
        "app_version": environment.plexora_version(),
    }


def activate(credential: str, *, kind: str | None = None, name: str | None = None,
             timeout: float = INTERACTIVE_TIMEOUT) -> dict:
    """Exchange a seat key or licence token for this environment's certificate.

    Idempotent on the server: activating again from the same environment (the
    same binding) returns its existing registration, never a second one.
    Returns `{certificate, environment{id,name,type}, server_time, ...}`.
    """
    chosen = kind if kind in environment.KINDS else environment.suggested_kind()
    body = {"credential": credential.strip(),
            "environment": _environment_block(chosen, name)}
    result = _post("/v1/activate", body, action="Activation", timeout=timeout)
    if not isinstance(result.get("certificate"), str):
        raise ServerError("Activation: the licence service returned no certificate.",
                          code="bad_response")
    return result


def refresh(certificate: str, *, timeout: float = BACKGROUND_TIMEOUT, flags=()) -> dict:
    """`{status: ok|renewed|revoked, certificate?, server_time}` for a cached
    certificate. Raises only for transport problems; callers swallow those.

    `flags` reports what the client noticed -- today only `clock_rollback` --
    for the service's review queue; it never changes the answer."""
    body = {"certificate": certificate, "binding": environment.binding()}
    if flags:
        body["flags"] = [flag for flag in flags if flag in ("clock_rollback",)]
    try:
        return _post("/v1/refresh", body, action="Licence refresh", timeout=timeout)
    except ServerError as exc:
        if exc.code in ("license_revoked", "credential_revoked", "forged_certificate"):
            return {"status": "revoked", "reason": exc.code}
        raise


def deactivate(certificate: str, *, timeout: float = INTERACTIVE_TIMEOUT) -> dict:
    """Release this environment's registration on its seat."""
    body = {"certificate": certificate, "binding": environment.binding()}
    return _post("/v1/deactivate", body, action="Deactivation", timeout=timeout)


def delegate(certificate: str, *, ttl_hours: int = 48,
             timeout: float = INTERACTIVE_TIMEOUT) -> dict:
    """A server-signed job certificate, for a primary that cannot mint one
    itself. Read-only on the server; never a registration."""
    body = {"certificate": certificate, "binding": environment.binding(),
            "ttl_hours": int(ttl_hours)}
    return _post("/v1/delegate", body, action="Job licence", timeout=timeout)


def start_trial(email: str, *, timeout: float = INTERACTIVE_TIMEOUT) -> dict:
    """Ask for a 30-day trial; the seat key arrives by email."""
    body = {"email": email.strip(), "fingerprint": environment.machine_basis(),
            "app_version": environment.plexora_version()}
    return _post("/v1/trial/start", body, action="Trial request", timeout=timeout)


def trial_url() -> str:
    """The portal's trial page, carrying this machine's trial fingerprint (a
    hash) so the page can make the one-trial-per-machine check."""
    page = store.portal_url("trial")
    if not page:
        return ""
    return f"{page}?fp={environment.machine_basis()}"
