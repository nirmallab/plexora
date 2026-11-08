"""Whether connecting to a server needs a licence here: the admin's switch.

Two switches in the BioCognia admin (admin.biocognia.com, Landing & sales ›
Plexora features), one for the website and one for local sessions (the
desktop app and pip `plexora`), each "free" or "licensed". Both are free
until someone changes them. They are read from the platform's public
`GET /v1/features` (docs/internal/WEB_REMOTE_RELAY.md, "Licence switch"):

    {"remote_servers": {"website": "free", "local": "free"}}

Read when somebody connects, at most once an hour, and kept in the data root
so a machine that cannot reach the platform uses the last answer it had --
and, having never had one, the default: free. A switch only ever withholds
connecting from an install with no valid licence; it never ends a connection
that is already open.

On the website the relay checks the same switch itself (relay/), so a page
cannot talk its way past this one.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

#: The platform's public read of the switches.
FEATURES_URL = "https://api.biocognia.com/v1/features"
#: How long an answer is used before it is asked again.
MAX_AGE = 3600
#: How long asking may take before the last answer is used instead.
TIMEOUT = 3.0
FILENAME = "feature_switches.json"
DEFAULT = {"website": "free", "local": "free"}

_memo = {"at": 0.0, "value": None}


def _path():
    from plexora import paths

    return paths.data_root() / FILENAME


def _saved():
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
        return raw.get("remote_servers"), float(raw.get("at") or 0)
    except (OSError, ValueError, AttributeError):
        return None, 0.0


def _fetch():
    url = os.environ.get("PLEXORA_FEATURES_URL") or FEATURES_URL
    if url == "off":  # never asked (the test suite): free
        return dict(DEFAULT)
    with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}),
                                timeout=TIMEOUT) as response:
        return json.loads(response.read() or b"{}").get("remote_servers")


def _clean(value):
    value = value if isinstance(value, dict) else {}
    return {key: ("licensed" if value.get(key) == "licensed" else "free") for key in DEFAULT}


def switches() -> dict:
    """`{"website": …, "local": …}`, each "free" or "licensed"."""
    now = time.time()
    if _memo["value"] is not None and now - _memo["at"] < MAX_AGE:
        return _memo["value"]
    saved, at = _saved()
    if saved is not None and now - at < MAX_AGE:
        _memo.update(at=at, value=_clean(saved))
        return _memo["value"]
    try:
        fetched = _clean(_fetch())
    except Exception:  # noqa: BLE001 - offline, refused, malformed: the last answer stands
        value = _clean(saved) if saved is not None else dict(DEFAULT)
        # Asked again in a minute rather than on every connect while down.
        _memo.update(at=now - MAX_AGE + 60, value=value)
        return value
    _memo.update(at=now, value=fetched)
    try:
        _path().write_text(json.dumps({"at": now, "remote_servers": fetched}), encoding="utf-8")
    except OSError:
        pass
    return fetched


def where() -> str:
    from plexora.server.models import web_remote

    return "website" if web_remote.active() else "local"


def _state():
    from plexora import licensing

    try:
        return licensing.peek()
    except Exception:  # noqa: BLE001 - an unreadable licence is no licence
        return None


#: What a refused connect says, in the app and in a terminal.
SENTENCE = "Connecting to servers needs a Plexora licence."


def needs_licence() -> bool:
    """Whether the switch here says licensed and this install holds none."""
    if switches().get(where()) != "licensed":
        return False
    state = _state()
    return not (state is not None and getattr(state, "licensed", False))


def refusal():
    """The 403 to answer a connect with, or None when connecting is allowed."""
    if not needs_licence():
        return None
    state = _state()
    from flask import jsonify

    sentence = SENTENCE
    hint = ("Sign in with your licence in Settings › Licence, then connect again."
            if state is None or not getattr(state, "licensed", False) else "")
    return jsonify(success=False, error=sentence, license={
        "code": "license_required", "entitlement": "remote_servers", "label": "Remote servers",
        "summary": "Connecting to servers over SSH", "plan": "free",
        "state": getattr(state, "state", "free") if state is not None else "free",
        "hint": hint, "off_by_default": False,
    }), 403


def reset_for_tests() -> None:
    _memo.update(at=0.0, value=None)
