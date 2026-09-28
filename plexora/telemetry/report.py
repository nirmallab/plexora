"""What the Settings page and `plexora telemetry` show: the mode and why, and
exactly what the next upload would contain."""

from __future__ import annotations

from plexora.telemetry import batch, config, identity, redact, uploader
from plexora.telemetry.client import telemetry

#: How often a browser tab posts its aggregate to the local server.
INGEST_INTERVAL_S = 300


def resolved():
    queue = telemetry.queue
    server = (queue.get_state("server", {}) if queue is not None else None)
    return config.resolve(server=server)


def status(brief=False) -> dict:
    """The mode and why. `brief` is what every tab asks at page load: it
    touches no SQLite (the queue's lock is the writer thread's), only the
    settings file and what this process already holds in memory."""
    if brief:
        current = telemetry.resolved if telemetry._started else config.resolve()
    else:
        current = resolved()
    prefs = config.read_prefs()
    answer = {
        "mode": current.mode,
        "source": current.source,
        "ceiling": current.ceiling,
        "enabled": telemetry.enabled,
        "notice_pending": bool(current.mode != config.OFF and not prefs.get("notice_shown")
                               and current.source in ("settings", "default")),
        "ingest_interval_s": INGEST_INTERVAL_S,
        # So a tab labels a third-party plugin exactly as the server does;
        # this map never leaves the machine.
        "plugin_labels": plugin_labels(),
    }
    if brief:
        return answer
    queue = telemetry.queue
    pending = queue.pending() if queue is not None else {}
    return {
        "mode": current.mode,
        "source": current.source,
        "ceiling": current.ceiling,
        "enabled": telemetry.enabled,
        "notice_pending": bool(current.mode != config.OFF and not prefs.get("notice_shown")
                               and current.source in ("settings", "default")),
        "install_id": identity.install_id(mint=False),
        "session_id": identity.session_id(),
        "ingest_interval_s": INGEST_INTERVAL_S,
        "endpoint_configured": bool(config.endpoint()),
        # So a tab labels a third-party plugin exactly as the server does;
        # this map never leaves the machine.
        "plugin_labels": plugin_labels(),
        "pending": pending,
        "uploader": uploader.status(queue),
    }


def plugin_labels() -> dict:
    """`{plugin name: telemetry label}` for the plugins and tools mounted."""
    import sys

    labels = {}
    try:
        from plexora.server import plugins as registry

        app = getattr(sys.modules.get("plexora"), "app", None)
        if app is not None:
            for plugin in registry.tools(app):
                labels[plugin.name] = identity.owner_label(plugin.name)
    except Exception:
        pass
    return labels


def set_mode(mode) -> dict:
    if mode not in config.MODES:
        raise ValueError(f"mode must be one of {', '.join(config.MODES)}")
    config.write_prefs(mode=mode, notice_shown=True)
    telemetry.reconfigure()
    return status()


def notice_seen() -> dict:
    config.write_prefs(notice_shown=True)
    return status()


def reset() -> dict:
    """A new install id, and nothing queued under the old one."""
    identity.reset_install_id()
    queue = telemetry.queue or telemetry.open_queue()
    if queue is not None:
        queue.clear()
        queue.set_state("token", None)
    return status()


def preview() -> dict:
    """The body the next upload would send -- every queued row, the open
    window and the in-memory aggregate included -- built by the same code as
    the real thing."""
    current = resolved()
    mode = current.mode if current.mode != config.OFF else config.ANONYMOUS
    queue = telemetry.queue or telemetry.open_queue()
    counter_rows, records = queue.snapshot(include_open=True) if queue is not None else ([], [])
    live_counters, live_records = telemetry.live_rows()
    events, _used_c, _used_r, dropped = batch.build_events(
        list(counter_rows) + live_counters, list(records) + live_records, mode,
        redact.personal_names())
    body = batch.body("preview", telemetry.client_block(mode), events)
    return {"mode": current.mode, "would_send": current.mode != config.OFF
            and bool(config.endpoint()), "dropped": dropped, "body": body}
