"""Who is reporting: a random install id, a per-process session id, and the
names of other people's plugins turned into something that is not their name.

Nothing here is derived from the machine, the account or the network. The
install id is `uuid4().hex`, minted the first time telemetry is on and kept in
settings.json; `plexora telemetry reset` throws it away. There is no hardware
fingerprint: two machines that copy a settings file are, as far as analytics
go, one install, and that is an acceptable price for not having one.
"""

from __future__ import annotations

import hashlib
import re
import threading
import uuid

from plexora.telemetry import config
from plexora.telemetry.schema import FIRST_PARTY

_ID = re.compile(r"[0-9a-f]{32}")
_lock = threading.Lock()
_session_id = uuid.uuid4().hex
#: Used when settings.json cannot be written: telemetry still works for this
#: process, and the next one is a stranger, which is the honest outcome.
_ephemeral: str | None = None


def session_id() -> str:
    return _session_id


def install_id(*, mint=True) -> str | None:
    """This installation's id, minting (and saving) one when `mint` is set."""
    global _ephemeral
    with _lock:
        stored = config.read_prefs().get("install_id")
        if isinstance(stored, str) and _ID.fullmatch(stored):
            return stored
        if _ephemeral is not None:
            return _ephemeral
        if not mint:
            return None
        fresh = uuid.uuid4().hex
        if config.write_prefs(install_id=fresh) is None:
            _ephemeral = fresh
        return fresh


def reset_install_id() -> str:
    """Forget the id and mint a new one. Returns the new id."""
    global _ephemeral
    with _lock:
        _ephemeral = None
        fresh = uuid.uuid4().hex
        if config.write_prefs(install_id=fresh) is None:
            _ephemeral = fresh
        return fresh


def owner_label(name) -> str:
    """A plugin/tool id as telemetry may say it: ours verbatim, anyone
    else's as `ext:<first 8 hex of sha256(name)>`."""
    text = str(name or "").strip()
    if text in FIRST_PARTY:
        return text
    return "ext:" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:8]


def _reset_for_tests():
    global _ephemeral, _session_id
    _ephemeral = None
    _session_id = uuid.uuid4().hex
