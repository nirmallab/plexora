"""`plexora telemetry off` typed in another terminal while a server runs.

The serve banner tells the user to turn telemetry off with that command, and
docs/TELEMETRY.md promises that `off` sends nothing and empties the local
queue. The command runs in its own process, which never started telemetry;
the running server shares the queue under the data root and decides for
itself whether to upload. Both halves have to honour the setting.
"""

from plexora.telemetry import config, report, uploader
from plexora.telemetry import cli as telemetry_cli
from plexora.telemetry.client import Telemetry

OLD = "2026-01-01T10"


def _serving(telemetry_enabled, monkeypatch):
    """A server counting in the default mode (settings decide, not the env)."""
    t, fake = telemetry_enabled
    monkeypatch.delenv("PLEXORA_TELEMETRY")
    assert t.start(None, "terminal", upload=True)
    assert t._ready.wait(5) and t.enabled and t.mode == config.ANONYMOUS
    t.count("tool.summary", "open", tool="roi")
    t.emit("project.load", {"outcome": "ready"})
    assert t.sync()
    # A finished hour too, which a routine upload (not only the last one) sends.
    t.queue.add_counters(OLD, [("tool.summary", "open", '{"tool":"roi"}', "count", 3,
                                None, 0.0, 0.0)])
    pending = t.queue.pending()
    assert pending["counter_rows"] >= 2 and pending["records"] >= 1
    return t, fake


def _off_from_a_terminal(monkeypatch):
    """`plexora telemetry off` as its own process runs it: a Telemetry that was
    never started, writing the same settings.json and seeing the same queue."""
    terminal = Telemetry()
    monkeypatch.setattr(report, "telemetry", terminal)
    lines = []
    assert telemetry_cli.run(["off"], log=lines.append) == 0
    assert config.read_prefs()["mode"] == config.OFF
    return terminal


def test_off_from_a_terminal_empties_the_shared_queue(telemetry_enabled, monkeypatch):
    """The rows the server queued before the user said off are deleted."""
    t, _fake = _serving(telemetry_enabled, monkeypatch)
    _off_from_a_terminal(monkeypatch)
    pending = t.queue.pending()
    assert pending.get("counter_rows") == 0 and pending.get("records") == 0, pending


def test_the_running_server_uploads_nothing_after_off(telemetry_enabled, monkeypatch):
    """The server's next upload sees the setting and sends nothing."""
    t, fake = _serving(telemetry_enabled, monkeypatch)
    _off_from_a_terminal(monkeypatch)
    assert uploader.upload_once(t) == "off"
    assert fake.of("/v1/telemetry/events") == []


def test_the_running_server_uploads_nothing_when_it_quits_after_off(telemetry_enabled,
                                                                     monkeypatch):
    """The last upload a server makes on its way out honours the setting too."""
    t, fake = _serving(telemetry_enabled, monkeypatch)
    _off_from_a_terminal(monkeypatch)
    t.shutdown(budget_s=3.0)
    assert fake.of("/v1/telemetry/events") == []
