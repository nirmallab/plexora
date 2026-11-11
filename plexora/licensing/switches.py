"""What the BioCognia admin holds back from every Plexora licence, and whether Plexora offers its trial.

Staff set both on admin.biocognia.com (Licensing › Features): a switch per
licensed feature, and one for the trial. The platform already leaves a
feature that is off out of every certificate it signs, so no gate here needs
this -- `licensing.allows` follows the certificate. This is for what Plexora
*says*: Settings and `plexora license status` list what Pro includes, and a
feature held back from everyone (Plexora 3D and Video Studio, until they are
released) is left out of that list rather than shown to a paying licence as
"Not included"; and a trial that is not offered is not offered in Settings.

Read from the platform's public `GET /v1/features`:

    {"products": {"plexora": {"off": ["plugin:video_studio", "volume"], "trial": true}}, ...}

at most once an hour (MAX_AGE), and kept in the data root so a machine that
cannot reach the platform uses the last answer it had; having never had one,
nothing is held back and the trial is offered, which is what Plexora said
before the switches existed. Asking takes at most TIMEOUT, and a failure is
not asked again for RETRY. Nothing is asked with BIOCOGNIA_OFFLINE set, with
no platform configured, or with PLEXORA_FEATURES_URL=off (the test suite).
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

#: How long an answer is used before it is asked again.
MAX_AGE = 3600
#: How long asking may take before the last answer is used instead.
TIMEOUT = 2.0
#: After a failure, how long before asking again.
RETRY = 600
FILENAME = "licence_switches.json"
DEFAULT = {"off": [], "trial": True}

_memo = {"at": 0.0, "value": None}


def _path():
    from plexora import paths

    return paths.data_root() / FILENAME


def _url() -> str:
    """Where to ask, or "" for not at all."""
    from biocognia import store

    override = (os.environ.get("PLEXORA_FEATURES_URL") or "").strip()
    if override == "off" or store.offline_only():
        return ""
    if override:
        return override
    base = store.server_url()
    return f"{base}/v1/features" if base else ""


def _clean(value) -> dict:
    """Plexora's entry, well formed: anything unreadable reads as nothing held back and the trial offered."""
    from plexora.licensing import PRODUCT

    entry = value.get(PRODUCT.id) if isinstance(value, dict) else None
    entry = entry if isinstance(entry, dict) else {}
    off = entry.get("off")
    off = sorted({str(name) for name in off if isinstance(name, str) and name}) if isinstance(off, list) else []
    return {"off": off, "trial": entry.get("trial") is not False}


def _saved():
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
        return raw.get("products"), float(raw.get("at") or 0)
    except (OSError, ValueError, AttributeError):
        return None, 0.0


def _fetch(url: str):
    from plexora import __version__

    # A named client: Cloudflare refuses Python-urllib's default user agent.
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": f"Plexora/{__version__}",
                                                   "BioCognia-Client": f"plexora/{__version__}"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read() or b"{}").get("products")


def current(*, network: bool = True) -> dict:
    """`{"off": [...], "trial": bool}`: the features held back from every Plexora licence, and whether a trial is
    offered. `network=False` answers from what is already known (memo, then the data root), never asking."""
    now = time.time()
    if _memo["value"] is not None and (now - _memo["at"] < MAX_AGE or not network):
        return _memo["value"]
    saved, at = _saved()
    if saved is not None and (now - at < MAX_AGE or not network):
        _memo.update(at=at, value=_clean(saved))
        return _memo["value"]
    url = _url() if network else ""
    fallback = _clean(saved) if saved is not None else dict(DEFAULT)
    if not url:
        _memo.update(at=now, value=fallback)
        return fallback
    try:
        products = _fetch(url)
        if not isinstance(products, dict):
            raise ValueError("no products in the answer")
    except Exception:  # noqa: BLE001 - offline, refused, malformed: the last answer stands
        _memo.update(at=now - MAX_AGE + RETRY, value=fallback)
        return fallback
    value = _clean(products)
    _memo.update(at=now, value=value)
    try:
        _path().write_text(json.dumps({"at": now, "products": products}), encoding="utf-8")
    except OSError:
        pass
    return value


def held(*, network: bool = True) -> tuple[str, ...]:
    """The entitlements no Plexora licence is given now (`volume`, `plugin:video_studio`, ...)."""
    return tuple(current(network=network)["off"])


def held_by(entitlement, *, network: bool = False) -> str:
    """The feature held back from every licence that `entitlement` is, or is under ("volume" for
    "volume:surfaces"), or "". From what is known already unless `network`: a refusal never waits on the
    platform."""
    text = str(entitlement or "")
    return next((name for name in held(network=network) if text == name or text.startswith(name + ":")), "")


def is_held(entitlement, *, network: bool = False) -> bool:
    return bool(held_by(entitlement, network=network))


def trial_offered() -> bool:
    return bool(current()["trial"])


def reset_for_tests() -> None:
    _memo.update(at=0.0, value=None)
