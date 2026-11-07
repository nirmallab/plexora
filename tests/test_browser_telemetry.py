"""Telemetry on the website (plexora/browser/telemetry.py), under CPython.

The website's Python has no threads and no sockets, so the same telemetry
client runs threadless (`PLEXORA_WORK=inline` here, `cooperative` there): the
worker pumps it, the uploader posts through urllib (the page's fetch on the
website), the settings block is a file the worker keeps in IndexedDB, and the
client block says `runtime: web`. The consent rules are the desktop's: Do Not
Track from the page turns it off, and a build with no endpoint sends nothing.
"""

import json
import os
import re
import time

import pytest

from plexora.browser import boot
from plexora.browser import telemetry as web
from plexora.telemetry import config, uploader
from plexora.telemetry.queue import window_of

OLD = "2026-01-01T10"


@pytest.fixture
def website(telemetry_enabled, monkeypatch, tmp_path):
    """The website's environment as boot.environment leaves it, against the
    fake Worker: no mode forced (the default applies), no threads."""
    t, fake = telemetry_enabled
    env = dict(os.environ)
    monkeypatch.setattr(os, "environ", env)
    monkeypatch.setattr(config, "_prefs_file", None)
    boot.environment("http://127.0.0.1:1", data_root=str(tmp_path / "data"),
                     licence_dir=str(tmp_path / "bioc"), telemetry={"endpoint": fake.url})
    env["PLEXORA_WORK"] = "inline"

    def no_pool(url):  # the desktop's transport: never used on the website
        raise AssertionError("urllib3 used on the website")

    monkeypatch.setattr(uploader, "_pool", no_pool)
    return t, fake, tmp_path / "telemetry", env


def test_the_website_counts_and_sends_with_no_thread(website):
    t, fake, home, _env = website
    started = web.start(str(home), keep_queue=True,
                        context={"os": "windows", "cpus": 12, "memory_gb": 8})
    assert started == {"enabled": True, "mode": "anonymous", "source": "default", "kept": True}
    assert t._writer is None and uploader._thread is None
    # The install id is the browser's own, in the file the worker keeps.
    prefs = json.loads((home / "prefs.json").read_text(encoding="utf-8"))
    assert re.fullmatch(r"[0-9a-f]{32}", prefs["install_id"])
    assert (home / "queue.sqlite").exists()

    t.count("tool.summary", "open", tool="roi")
    first = web.tick(now=100.0)
    # Only the open hour is queued: the desktop sends it later too.
    assert first["outcome"] == "nothing" and fake.requests == []
    t.queue.add_counters(OLD, [("tool.summary", "open", '{"tool":"gating"}', "count", 2,
                                None, 0.0, 0.0)])
    assert web.tick(now=200.0)["outcome"] == "flushed"  # the interval has not passed
    assert web.tick(now=100.0 + 3600)["outcome"] == "sent"
    assert [r["path"] for r in fake.requests] == ["/v1/telemetry/register", "/v1/telemetry/events"]
    client = fake.uploads[0]["client"]
    assert client["install_id"] == prefs["install_id"]
    assert {k: client[k] for k in ("runtime", "launch_mode", "deployment", "install_kind", "os",
                                   "arch", "scheduler")} == {
        "runtime": "web", "launch_mode": "web", "deployment": "web", "install_kind": "web",
        "os": "windows", "arch": "other", "scheduler": "none"}
    assert fake.uploads[0]["events"][0]["rows"] == [{"k": "open", "d": {"tool": "gating"}, "n": 2}]
    register = fake.of("/v1/telemetry/register")[0]["json"]
    assert register["client"]["runtime"] == "web"


def test_a_tab_without_the_queue_sends_its_open_hour_when_hidden(website):
    t, fake, home, _env = website
    web.start(str(home), keep_queue=False)
    assert not (home / "queue.sqlite").exists()
    t.count("tool.summary", "open", tool="roi")
    assert web.tick(now=10.0)["outcome"] == "nothing"
    answer = web.tick(hidden=True, now=20.0)
    assert answer["outcome"] == "sent" and answer["kept"] is False
    rows = fake.uploads[0]["events"][0]["rows"]
    assert rows == [{"k": "open", "d": {"tool": "roi"}, "n": 1}]
    assert fake.uploads[0]["events"][0]["window"] == window_of()


def test_settings_choices_are_kept_in_the_websites_file(website):
    t, _fake, home, _env = website
    web.start(str(home))
    config.write_prefs(mode="off", notice_shown=True)
    t.reconfigure()
    assert not t.enabled and t.resolved.source == "settings"
    kept = json.loads((home / "prefs.json").read_text(encoding="utf-8"))
    assert kept["mode"] == "off" and kept["notice_shown"] is True
    assert web.tick(now=1.0)["outcome"] == "off"


def test_do_not_track_from_the_page_turns_it_off(telemetry_enabled, monkeypatch, tmp_path):
    t, fake = telemetry_enabled
    env = dict(os.environ)
    monkeypatch.setattr(os, "environ", env)
    monkeypatch.setattr(config, "_prefs_file", None)
    boot.environment("http://127.0.0.1:1", telemetry={"endpoint": fake.url, "dnt": True})
    env["PLEXORA_WORK"] = "inline"
    assert env["DO_NOT_TRACK"] == "1" and "PLEXORA_TELEMETRY" not in env
    answer = web.start(str(tmp_path / "telemetry"))
    assert answer["enabled"] is False and answer["source"] == "do_not_track"
    t.count("tool.summary", "open", tool="roi")
    assert web.tick(now=time.monotonic())["outcome"] == "off"
    assert fake.requests == []


def test_a_build_with_no_endpoint_sends_nothing(monkeypatch):
    env = dict(os.environ)
    monkeypatch.setattr(os, "environ", env)
    boot.environment("http://127.0.0.1:1")
    assert env["PLEXORA_TELEMETRY"] == "off" and "PLEXORA_TELEMETRY_ENDPOINT" not in env
    boot.environment("http://127.0.0.1:1", telemetry={"endpoint": "https://analytics.example"})
    assert "PLEXORA_TELEMETRY" not in env
    assert env["PLEXORA_TELEMETRY_ENDPOINT"] == "https://analytics.example"


def test_the_desktop_says_runtime_desktop(telemetry_enabled):
    t, _fake = telemetry_enabled
    t.start(None, "terminal", upload=False)
    t._ready.wait(5)
    assert t.client_block()["runtime"] == "desktop"
