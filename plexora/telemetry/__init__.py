"""Optional, anonymous, allowlisted usage telemetry.

Nothing in Plexora depends on this package working. Importing it is free --
no SQLite, no settings read, no thread -- and every hook it offers is a no-op
until `telemetry.start()` has resolved a mode other than `off`. See
`schema.py` for exactly what can be sent, `client.py` for how it is
aggregated, and `uploader.py` for the only code that opens a socket.

    from plexora.telemetry import telemetry
    telemetry.count("tool.summary", "open", tool="gating")

Off switches, any one of which wins: `DO_NOT_TRACK=1`,
`PLEXORA_TELEMETRY=off`, `plexora telemetry off`, the Settings page.
"""

from plexora.telemetry.client import Telemetry, telemetry


def count(event, key="n", n=1, /, **dims):
    telemetry.count(event, key, n, **dims)


def observe(event, key, ms, /, **dims):
    telemetry.observe(event, key, ms, **dims)


def emit(event, props, priority=None):
    telemetry.emit(event, props, priority)


def feature(key, plugin="core"):
    telemetry.feature(key, plugin)


def reset_for_tests():
    telemetry.reset_for_tests()


__all__ = ["Telemetry", "telemetry", "count", "observe", "emit", "feature",
           "reset_for_tests"]
