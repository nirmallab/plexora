"""The local end of licensing: what the Settings page and the paid-feature
modal ask, and the few things they can change.

- `GET  /license/status` -- plan, state, grants, dates; whether a licence
  service is configured; where the trial and portal pages are. Never the
  certificate, never a key, never a token. No network: a page load must not
  wait on the licence service.
- `POST /settings/license/activate` `{credential, name?, cluster?}` -- a seat
  key or licence token, exchanged online for this environment's certificate.
- `POST /settings/license/install` `{certificate}` -- an offline licence file's
  text, or a bare certificate.
- `POST /settings/license/refresh` -- check with the service now.
- `POST /settings/license/deactivate` -- release this environment, then remove.
- `POST /settings/license/remove` -- remove the local licence (no network).

Every mutation answers this machine only, unless the server has an auth
token, exactly like the agent control plane: a neighbour on the network must
not be able to install, swap or release somebody's licence because the tiles
happen to be reachable.

Nothing here is needed for anything Free, and nothing Free calls it.
"""

from __future__ import annotations

import ipaddress
import re

from flask import Blueprint, current_app, jsonify, request

license_bp = Blueprint("license", __name__)

_CERTIFICATE = re.compile(r"PLEXORA1\.[A-Za-z0-9]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")

#: What the credential field accepts, as a sanity bound before any parsing.
MAX_CREDENTIAL = 128
MAX_CERTIFICATE_TEXT = 64 * 1024


def _local_or_token():
    if current_app.config.get("PLEXORA_AUTH_TOKEN"):
        return None
    try:
        if ipaddress.ip_address(request.remote_addr or "").is_loopback:
            return None
    except ValueError:
        pass
    return jsonify(success=False, error="licence changes are accepted from this machine only"), 403


@license_bp.before_request
def _guard():
    if request.method != "GET":
        return _local_or_token()
    return None


def _body():
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def status_payload() -> dict:
    """The status every licence surface shows. Safe to render into a page."""
    from plexora import licensing
    from plexora.licensing import client, store

    try:
        info = licensing.describe(network=False)
    except Exception:
        # Licensing never breaks a page: an unexpected failure reads as Free,
        # which is what it would resolve to.
        from plexora.licensing.models import free_state

        info = free_state("unreadable_file", state="invalid").describe()
    info["service_configured"] = bool(store.server_url())
    info["offline_only"] = store.offline_only()
    info["portal_url"] = store.portal_url()
    info["trial_url"] = client.trial_url() if store.server_url() else ""
    info["license_dir"] = str(store.license_dir())
    return info


def _refusal(exc, status=400):
    return jsonify(success=False, error=str(exc), code=getattr(exc, "code", "license_error"),
                   license=status_payload()), status


@license_bp.route("/license/status", methods=["GET"])
def license_status():
    return jsonify(success=True, license=status_payload())


@license_bp.route("/settings/license/activate", methods=["POST"])
def license_activate():
    from plexora.licensing import client, state
    from plexora.licensing.errors import LicenseError

    body = _body()
    credential = str(body.get("credential") or "").strip()
    if not credential or len(credential) > MAX_CREDENTIAL:
        return jsonify(success=False, error="Enter a seat key (PLEX-...) or a licence token."), 400
    name = str(body.get("name") or "").strip()[:80] or None
    kind = "cluster" if body.get("cluster") else None
    try:
        result = client.activate(credential, kind=kind, name=name)
        state.save_activation(result, credential=credential, source="activation")
        state.reload(network=False)
    except LicenseError as exc:
        return _refusal(exc, 409 if getattr(exc, "status", None) == 409 else 400)
    return jsonify(success=True, license=status_payload())


@license_bp.route("/settings/license/install", methods=["POST"])
def license_install():
    from plexora.licensing import state
    from plexora.licensing.errors import LicenseError

    text = str(_body().get("certificate") or "")
    if not text.strip() or len(text) > MAX_CERTIFICATE_TEXT:
        return jsonify(success=False, error="Paste the licence file's contents."), 400
    # The certificate line, found anywhere: a pasted file keeps its comment
    # lines, and a one-line box may have run them together.
    found = _CERTIFICATE.search(text)
    cert = found.group(0) if found else None
    if cert is None:
        return jsonify(success=False, error="That does not contain a Plexora licence certificate."), 400
    try:
        state.install_certificate(cert)
    except LicenseError as exc:
        return _refusal(exc)
    return jsonify(success=True, license=status_payload())


@license_bp.route("/settings/license/refresh", methods=["POST"])
def license_refresh():
    from plexora.licensing import client, state
    from plexora.licensing.errors import LicenseError

    current = state.reload(network=False)
    if not current.certificate or current.source not in ("cache", "token"):
        return jsonify(success=False, error="There is no activated licence here to check."), 400
    try:
        result = client.refresh(current.certificate, timeout=client.INTERACTIVE_TIMEOUT)
    except LicenseError as exc:
        return _refusal(exc)
    outcome = state.apply_refresh(result, current)
    return jsonify(success=True, outcome=outcome, license=status_payload())


@license_bp.route("/settings/license/deactivate", methods=["POST"])
def license_deactivate():
    from plexora.licensing import client, state, store
    from plexora.licensing.errors import LicenseError

    current = state.reload(network=False)
    if not current.certificate or current.source != "cache":
        return jsonify(success=False, error="There is no activated licence here to deactivate."), 400
    try:
        result = client.deactivate(current.certificate)
    except LicenseError as exc:
        return _refusal(exc, 429 if getattr(exc, "code", "") == "cooldown_active" else 400)
    store.clear_license()
    state.reset()
    return jsonify(success=True, next_allowed_at=result.get("next_allowed_at"), license=status_payload())


@license_bp.route("/settings/license/remove", methods=["POST"])
def license_remove():
    from plexora.licensing import state, store

    store.clear_license()
    state.reset()
    return jsonify(success=True, license=status_payload())
