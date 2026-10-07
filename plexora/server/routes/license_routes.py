"""The local end of licensing: what the Settings page and the paid-feature
modal ask, and the few things they can change.

- `GET  /license/status` -- plan, state, grants, dates; whether the BioCognia
  platform is reachable by configuration; where the portal and the trial page
  are. Never the certificate, never a code, never a token. No network: a page
  load must not wait on the platform.
- `POST /settings/license/connect` `{name?, cluster?, trial?}` -- Connect this
  device: start the device flow. Answers `{code, verify_url, interval,
  expires_in}`; the page shows the code and the link and polls.
- `POST /settings/license/connect/poll` `{code}` -- `{pending: true}` until the
  person approves at account.biocognia.com, then the certificates are saved
  and the status comes back.
- `POST /settings/license/activate` `{credential, name?, cluster?}` -- an
  activation code minted in the portal (`BIOC-...`) or a `BIOCT1_` token,
  exchanged for this device's certificate.
- `POST /settings/license/install` `{certificate}` -- an offline licence file's
  text, or a bare `BIOC1` certificate.
- `POST /settings/license/refresh` -- check with the platform now.
- `POST /settings/license/deactivate` -- release this device, then remove.
- `POST /settings/license/remove` -- remove the local licence (no network).

Sign-in, seats and payment happen only at account.biocognia.com; Plexora
holds no account and never sees a password or an email address here.

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

_CERTIFICATE = re.compile(r"BIOC1\.[A-Za-z0-9]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
#: A device-flow or portal code as the platform prints it (`BIOC-7F3K-9Q2M`).
_CODE = re.compile(r"^BIOC-[A-Z0-9]{4}(?:-[A-Z0-9]{4}){1,4}$")

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
    from biocognia import store

    from plexora import licensing
    from plexora.licensing import LICENSING, PRODUCT, manifest

    try:
        info = licensing.describe(network=False)
    except Exception:
        # Licensing never breaks a page: an unexpected failure reads as Free,
        # which is what it would resolve to.
        from biocognia.models import free_state

        info = licensing.describe(state=free_state(PRODUCT.id, PRODUCT.free_limits,
                                                   "unreadable_file", state="invalid"))
    # What each root grants on this licence ("External MCP access: Not
    # included"), so Settings lists add-ons a licence lacks as well as has.
    info["unlocks"] = manifest.unlocks(info.get("entitlements"), paid=bool(info.get("paid")))
    info["service_configured"] = bool(store.server_url())
    info["offline_only"] = store.offline_only()
    info["portal_url"] = store.portal_url()
    # The portal's trial page, or "" where the page could not finish the job:
    # offline, or a build with no platform to connect to afterwards.
    info["trial_url"] = (store.portal_url(f"start?product={PRODUCT.id}")
                         if info["service_configured"] and not info["offline_only"] else "")
    info["license_dir"] = str(LICENSING.store.path.parent)
    return info


def _refusal(exc, status=400):
    return jsonify(success=False, error=str(exc), code=getattr(exc, "code", "license_error"),
                   license=status_payload()), status


def _kind(body):
    return "cluster" if body.get("cluster") else None


def _name(body):
    return str(body.get("name") or "").strip()[:80] or None


@license_bp.route("/license/status", methods=["GET"])
def license_status():
    return jsonify(success=True, license=status_payload())


@license_bp.route("/settings/license/connect", methods=["POST"])
def license_connect():
    """Connect this device: a short code the person approves in the portal."""
    from plexora.licensing import LICENSING, LicenseError

    body = _body()
    try:
        started = LICENSING.client.activate_start(kind=_kind(body), name=_name(body),
                                                  trial=bool(body.get("trial")))
    except LicenseError as exc:
        return _refusal(exc)
    from biocognia import store

    return jsonify(success=True, code=started["code"],
                   verify_url=started.get("verify_url") or store.portal_url("activate"),
                   interval=max(1, int(started.get("interval") or 5)),
                   expires_in=int(started.get("expires_in") or 900))


@license_bp.route("/settings/license/connect/poll", methods=["POST"])
def license_connect_poll():
    from plexora.licensing import LICENSING, LicenseError

    code = str(_body().get("code") or "").strip().upper()
    if not _CODE.match(code):
        return jsonify(success=False, error="That is not a connection code."), 400
    try:
        answer = LICENSING.client.activate_poll(code)
    except LicenseError as exc:
        return _refusal(exc, 410 if getattr(exc, "code", "") in ("expired", "denied") else 400)
    if answer.get("status") == "pending":
        return jsonify(success=True, pending=True, retry_after=answer.get("retry_after"))
    LICENSING.save_certificates(answer, credential=None, source="activation")
    LICENSING.reload(network=False)
    return jsonify(success=True, pending=False, license=status_payload())


@license_bp.route("/settings/license/activate", methods=["POST"])
def license_activate():
    from plexora.licensing import LICENSING, LicenseError

    body = _body()
    credential = str(body.get("credential") or "").strip()
    if not credential or len(credential) > MAX_CREDENTIAL:
        return jsonify(success=False,
                       error="Enter an activation code from account.biocognia.com (BIOC-...) "
                             "or a BIOCT1_ token."), 400
    try:
        LICENSING.activate(credential, kind=_kind(body), name=_name(body))
    except LicenseError as exc:
        return _refusal(exc, 409 if getattr(exc, "status", None) == 409 else 400)
    return jsonify(success=True, license=status_payload())


@license_bp.route("/settings/license/install", methods=["POST"])
def license_install():
    from plexora.licensing import LICENSING, LicenseError

    text = str(_body().get("certificate") or "")
    if not text.strip() or len(text) > MAX_CERTIFICATE_TEXT:
        return jsonify(success=False, error="Paste the licence file's contents."), 400
    # The certificate line, found anywhere: a pasted file keeps its comment
    # lines, and a one-line box may have run them together.
    found = _CERTIFICATE.search(text)
    cert = found.group(0) if found else None
    if cert is None:
        return jsonify(success=False, error="That does not contain a licence certificate."), 400
    try:
        LICENSING.install_certificate(cert, source="install")
    except LicenseError as exc:
        return _refusal(exc)
    return jsonify(success=True, license=status_payload())


@license_bp.route("/settings/license/refresh", methods=["POST"])
def license_refresh():
    from plexora.licensing import LICENSING, LicenseError

    current = LICENSING.reload(network=False)
    if not current.certificate or current.source not in ("cache", "token"):
        return jsonify(success=False, error="There is no activated licence here to check."), 400
    # Asked for by a person, so neither the heartbeat switch nor the weekly
    # schedule applies -- only BIOCOGNIA_OFFLINE, which the client enforces.
    try:
        result = LICENSING.client.refresh(current.certificate, timeout=15)
    except LicenseError as exc:
        return _refusal(exc)
    outcome = LICENSING.apply_refresh(result, current)
    return jsonify(success=True, outcome=outcome, license=status_payload())


@license_bp.route("/settings/license/deactivate", methods=["POST"])
def license_deactivate():
    from plexora.licensing import LICENSING, LicenseError

    current = LICENSING.reload(network=False)
    if not current.certificate or current.source != "cache":
        return jsonify(success=False, error="There is no activated licence here to deactivate."), 400
    try:
        result = LICENSING.deactivate()
    except LicenseError as exc:
        return _refusal(exc, 429 if getattr(exc, "code", "") == "cooldown_active" else 400)
    return jsonify(success=True, next_allowed_at=result.get("next_allowed_at"), license=status_payload())


@license_bp.route("/settings/license/remove", methods=["POST"])
def license_remove():
    from plexora.licensing import LICENSING

    LICENSING.remove()
    return jsonify(success=True, license=status_payload())
