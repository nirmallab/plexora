"""Which mode is in force, and why. Pure, stdlib-only, never raises.

The mode resolves highest-first, and every rule that can only *reduce* what
is sent comes before the ones that can raise it:

1. the test suite (pytest, or `PLEXORA_TESTING`)           -> off
2. `DO_NOT_TRACK` set to anything but `0`                  -> off
3. `PLEXORA_TELEMETRY=off|anonymous|diagnostics`           -> as given
4. the server has paused this version (`disabled_until`)   -> off
5. `telemetry.mode` in settings.json                       -> as given
6. nothing set                                             -> anonymous

and then the server's `level_max` ceiling, which can only lower what 3, 5 or
6 chose -- an environment variable in somebody's job script cannot raise a
ceiling the server set.

The settings block holds only what the user decided: the mode, the install
id, and whether the notice was shown. What the uploader learns from the
server (token, backoff, ceiling) lives in the queue's `state` table, so a
routine upload never rewrites the user's settings file.
"""

from __future__ import annotations

import os
import sys
import time
from typing import NamedTuple

from plexora.telemetry.schema import ANONYMOUS, DIAGNOSTICS, MODES, OFF

DEFAULT_MODE = ANONYMOUS

ENV_MODE = "PLEXORA_TELEMETRY"
ENV_DO_NOT_TRACK = "DO_NOT_TRACK"
ENV_TESTING = "PLEXORA_TESTING"
ENV_ENDPOINT = "PLEXORA_TELEMETRY_ENDPOINT"
ENV_DEBUG = "PLEXORA_TELEMETRY_DEBUG"

SETTINGS_KEY = "telemetry"

#: Where uploads go when `PLEXORA_TELEMETRY_ENDPOINT` does not say. Empty
#: until the Worker is deployed (backend/README.md): with no endpoint the
#: queue still fills -- bounded, and visible in `plexora telemetry preview`
#: -- but no uploader thread starts and nothing leaves the machine.
DEFAULT_ENDPOINT = ""

_OFF_WORDS = {"0", "false", "no", "off", "none", "disable", "disabled"}
_ON_WORDS = {"1", "true", "yes", "on", "enable", "enabled"}

#: Set by the test fixture that exercises telemetry end to end, and by
#: nothing else: the whole suite otherwise runs with telemetry pinned off.
_testing_override: bool | None = None


class Resolved(NamedTuple):
    mode: str
    #: Which rule chose it: `testing`, `do_not_track`, `env`, `server`,
    #: `ceiling`, `settings` or `default`.
    source: str
    #: The server's ceiling, when it has set one.
    ceiling: str | None


def under_pytest() -> bool:
    if _testing_override is not None:
        return _testing_override
    flag = os.environ.get(ENV_TESTING, "").strip()
    if flag == "0":
        # Said explicitly: the suite's own subprocess tests, whose children
        # inherit PYTEST_CURRENT_TEST but are exercising telemetry on purpose.
        return False
    if flag:
        return True
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) or "pytest" in sys.modules


def env_mode(env=None) -> str | None:
    """What `PLEXORA_TELEMETRY` asks for, or None when it asks for nothing
    this module understands (a typo is ignored, not read as consent)."""
    env = os.environ if env is None else env
    value = (env.get(ENV_MODE) or "").strip().lower()
    if not value:
        return None
    if value in MODES:
        return value
    if value in _OFF_WORDS:
        return OFF
    if value in _ON_WORDS:
        return ANONYMOUS
    return None


def do_not_track(env=None) -> bool:
    env = os.environ if env is None else env
    return (env.get(ENV_DO_NOT_TRACK) or "").strip() not in ("", "0")


def clamp(mode: str, ceiling) -> str:
    """The stricter of `mode` and `ceiling`."""
    if ceiling not in MODES or mode not in MODES:
        return mode
    return min(mode, ceiling, key=MODES.index)


def resolve(env=None, prefs=None, server=None, *, testing=None, now=None) -> Resolved:
    """The mode in force. Every argument defaults to the live source."""
    try:
        return _resolve(env, prefs, server, testing, now)
    except Exception:  # pragma: no cover - a resolver that raised would be worse
        return Resolved(OFF, "error", None)


def _resolve(env, prefs, server, testing, now):
    env = os.environ if env is None else env
    testing = under_pytest() if testing is None else testing
    server = server if isinstance(server, dict) else {}
    ceiling = server.get("level_max") if server.get("level_max") in MODES else None
    if testing:
        return Resolved(OFF, "testing", ceiling)
    if do_not_track(env):
        return Resolved(OFF, "do_not_track", ceiling)
    asked = env_mode(env)
    source = "env"
    if asked == OFF:
        return Resolved(OFF, "env", ceiling)
    until = server.get("disabled_until")
    try:
        if until and (time.time() if now is None else now) < float(until):
            return Resolved(OFF, "server", ceiling)
    except (TypeError, ValueError):
        pass
    if asked is None:
        prefs = read_prefs() if prefs is None else prefs
        stored = prefs.get("mode")
        if stored in MODES:
            asked, source = stored, "settings"
        else:
            asked, source = DEFAULT_MODE, "default"
    clamped = clamp(asked, ceiling)
    if clamped != asked:
        return Resolved(clamped, "ceiling", ceiling)
    return Resolved(asked, source, ceiling)


# -- the settings block -------------------------------------------------------


def read_prefs() -> dict:
    """The `telemetry` block of settings.json, or {} -- never raises."""
    try:
        from plexora import paths

        block = paths.read_settings().get(SETTINGS_KEY)
    except Exception:
        return {}
    return dict(block) if isinstance(block, dict) else {}


def write_prefs(**changes) -> dict | None:
    """Merge `changes` into the block (None removes a key). Returns the new
    block, or None when the file could not be written. Never raises."""
    try:
        from plexora import paths

        data = paths.read_settings()
        block = data.get(SETTINGS_KEY)
        block = dict(block) if isinstance(block, dict) else {}
        for key, value in changes.items():
            if value is None:
                block.pop(key, None)
            else:
                block[key] = value
        data[SETTINGS_KEY] = block
        paths.write_settings(data)
        return block
    except Exception:
        return None


def endpoint(env=None) -> str:
    """The Worker's base URL, without a trailing slash, or "" for none."""
    env = os.environ if env is None else env
    value = (env.get(ENV_ENDPOINT) or DEFAULT_ENDPOINT or "").strip()
    return value.rstrip("/")


def debug(message: str) -> None:
    """One line on stderr, only under `PLEXORA_TELEMETRY_DEBUG=1`."""
    if os.environ.get(ENV_DEBUG, "").strip() not in ("", "0"):
        try:
            print(f"plexora telemetry: {message}", file=sys.stderr)
        except Exception:
            pass


__all__ = ["ANONYMOUS", "DIAGNOSTICS", "OFF", "MODES", "DEFAULT_MODE", "Resolved",
           "resolve", "clamp", "read_prefs", "write_prefs", "endpoint", "under_pytest",
           "env_mode", "do_not_track", "debug"]
