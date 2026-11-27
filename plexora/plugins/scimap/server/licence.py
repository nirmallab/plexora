"""Scimap Pro's own licence, read and changed without importing Scimap Pro.

This is not Plexora's licence. Scimap Pro is a separate product with its own
seat, and Plexora's `Plugin.entitlement` cannot name another product's
licence -- so the plugin is Free in Plexora and Scimap Pro's own gates apply
to every run, exactly as they would in a Jupyter notebook: Starter runs up to
100,000 cells on one worker with watermarked plots, a seat lifts that.

Both products keep their certificate in the same BioCognia store, and
`biocognia.Licensing` reads it for any product given that product's manifest.
So the state is read with Scimap Pro's `_license/manifest.json` (located with
`find_spec`, never imported), in well under 100 ms, and an activation here is
seen by Scimap Pro the next time it checks -- `refresh_library()` makes that
immediate when it is already loaded.

Credentials pass through these functions and are never stored by this plugin,
logged, or written into a notebook or an export. The certificate itself is
never sent to the page: `state()` returns what the page shows and nothing else.
"""

from __future__ import annotations

import functools
import re
import sys
import threading

from plexora.plugins.scimap.server import install

#: Where Scimap Pro is bought and where its licensing is explained. Scimap Pro's
#: own constants (`scimappro._license._errors`), restated because reading them
#: needs the import this module exists to avoid. A test pins them together.
PURCHASE_URL = "https://pro.scimap.xyz/pricing"
DOCS_URL = "https://pro.scimap.xyz/licensing"
TRIAL_URL = "https://account.biocognia.com/start?product=scimappro"

#: Starter's limits when the certificate cannot be read at all.
STARTER_LIMITS = {"max_cells": 100_000, "max_workers": 1}

_CERTIFICATE = re.compile(r"BIOC1\.[A-Za-z0-9]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_CODE = re.compile(r"^BIOC-[A-Z0-9]{4}(?:-[A-Z0-9]{4}){1,4}$")
MAX_CREDENTIAL = 128
MAX_CERTIFICATE_TEXT = 64 * 1024

_lock = threading.Lock()


class LicenceError(ValueError):
    """A refusal with a sentence the page can show as it is."""

    def __init__(self, message, code="license_error"):
        super().__init__(message)
        self.code = code


@functools.lru_cache(maxsize=1)
def _licensing():
    """`biocognia.Licensing` for Scimap Pro, or None when it is not installed."""
    root = install.package_dir()
    if root is None:
        return None
    manifest = root / "_license" / "manifest.json"
    if not manifest.is_file():
        return None
    from biocognia import Licensing, Product

    return Licensing(Product.from_manifest(manifest))


def _tier(current) -> str:
    """One word for the page: licensed, trial, grace, starter, expired, invalid."""
    if current is None:
        return "starter"
    if getattr(current, "licensed", False):
        if getattr(current, "trial", False):
            return "trial"
        if str(getattr(current, "state", "")) == "grace":
            return "grace"
        return "licensed"
    reason = str(getattr(current, "reason", "") or "")
    if reason in ("expired", "license_expired", "seat_ended", "grace_ended"):
        return "expired"
    if reason in ("invalid", "unreadable_file", "bad_signature", "wrong_device",
                  "clock_rollback", "revoked"):
        return "invalid"
    if reason in ("unverifiable", "offline_expired"):
        return "unverifiable"
    return "starter"


def state() -> dict:
    """What the page shows about Scimap Pro's licence. Never raises, never
    touches the network, never includes the certificate or a key."""
    lic = _licensing()
    base = {"purchase_url": PURCHASE_URL, "docs_url": DOCS_URL, "trial_url": TRIAL_URL}
    if lic is None:
        return {**base, "tier": "starter", "licensed": False, "limits": dict(STARTER_LIMITS),
                "features": [], "expires_at": None, "plan": None, "watermark": True}
    try:
        current = lic.current(network=False)
    except Exception:  # noqa: BLE001 -- licensing never breaks a page
        current = None
    tier = _tier(current)
    limits = dict(getattr(current, "limits", None) or STARTER_LIMITS)
    features = sorted(getattr(current, "entitlements", ()) or ())
    return {
        **base,
        "tier": tier,
        "licensed": tier in ("licensed", "trial", "grace"),
        "limits": {"max_cells": limits.get("max_cells"), "max_workers": limits.get("max_workers")},
        "features": features,
        "expires_at": getattr(current, "license_expires_at", None) or getattr(current, "expires_at", None),
        "grace_until": getattr(current, "grace_until", None) if tier == "grace" else None,
        "plan": getattr(current, "plan", None),
        "watermark": "nowatermark" not in features,
    }


def max_cells():
    """The cell cap this licence enforces, or None for none."""
    return state()["limits"].get("max_cells")


def max_workers():
    return state()["limits"].get("max_workers")


def allows(feature: str) -> bool:
    return feature in state()["features"]


# -- changing it ---------------------------------------------------------


def _required():
    lic = _licensing()
    if lic is None:
        raise LicenceError("Scimap Pro is not installed here, so there is no licence to add.",
                           "not_installed")
    return lic


def refresh_library():
    """Make an already-imported Scimap Pro see a licence changed here, with no
    restart. Not importing it is fine: it reads the store when it loads."""
    module = sys.modules.get("scimappro._license")
    if module is None:
        return
    licensing = getattr(module, "LICENSING", None)
    reload = getattr(licensing, "reload", None)
    if callable(reload):
        try:
            reload(network=False)
        except Exception:  # noqa: BLE001 -- the next check reads the store anyway
            pass


def _wrap(call):
    from biocognia import LicenseError as BiocogniaError

    try:
        with _lock:
            call()
    except BiocogniaError as exc:
        raise LicenceError(str(exc), getattr(exc, "code", None) or "license_error") from exc
    refresh_library()
    return state()


def activate(credential: str) -> dict:
    """Exchange a licence key, an activation code or a BIOCT1_ token."""
    credential = str(credential or "").strip()
    if not credential or len(credential) > MAX_CREDENTIAL:
        raise LicenceError("Enter the licence key from your account's Licenses page "
                           "(BIOC-XXXX-XXXX-XXXX-XXXX).", "invalid_input")
    lic = _required()
    return _wrap(lambda: lic.activate(credential))


def install_file(text: str) -> dict:
    """Install a licence file's certificate (an offline computer)."""
    text = str(text or "")
    if not text.strip() or len(text) > MAX_CERTIFICATE_TEXT:
        raise LicenceError("Choose the .bioc licence file.", "invalid_input")
    found = _CERTIFICATE.search(text)
    if found is None:
        raise LicenceError("That file does not contain a Scimap Pro licence.", "invalid_input")
    lic = _required()
    return _wrap(lambda: lic.install_certificate(found.group(0), source="install"))


def connect_start() -> dict:
    """Start the browser sign-in: a short code the person approves."""
    from biocognia import LicenseError as BiocogniaError
    from biocognia import store

    lic = _required()
    try:
        started = lic.client.activate_start(kind=None, name=None, trial=False)
    except BiocogniaError as exc:
        raise LicenceError(str(exc), getattr(exc, "code", None) or "license_error") from exc
    return {"code": started["code"],
            "verify_url": started.get("verify_url") or store.portal_url("activate"),
            "interval": max(1, int(started.get("interval") or 5)),
            "expires_in": int(started.get("expires_in") or 900)}


def connect_poll(code: str) -> dict:
    from biocognia import LicenseError as BiocogniaError

    code = str(code or "").strip().upper()
    if not _CODE.match(code):
        raise LicenceError("That is not a connection code.", "invalid_input")
    lic = _required()
    try:
        answer = lic.client.activate_poll(code)
    except BiocogniaError as exc:
        raise LicenceError(str(exc), getattr(exc, "code", None) or "license_error") from exc
    if answer.get("status") == "pending":
        return {"pending": True, "retry_after": answer.get("retry_after")}

    def save():
        lic.save_certificates(answer, credential=None, source="activation")
        lic.reload(network=False)

    return {"pending": False, "license": _wrap(save)}
