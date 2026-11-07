"""How often the website turns a browser away (client src/js/services/
refusalCount.js, sent by the loader, services/shell.js, after it asks a
Chrome or Edge without JSPI to update).

That page never starts Python, so the website's telemetry (plexora/browser/
telemetry.py) cannot count it: the loader sends the one count itself, to the
same service in the same upload format, under the same rules as
plexora/telemetry/config.py `resolve` -- the build's switch, Do Not Track /
Global Privacy Control, the service's pause, the visitor's earlier choice in
the site's IndexedDB, else anonymous -- at most once per browser per day.
tests/js/refusal_count_probe.mjs runs it against stand-ins; the bodies it
sends are validated here against the schema the Worker's allowlist is
generated from. tests/browser/flags.mjs runs it in Chrome against
tests/browser/mock_telemetry.py (tests/test_browser_flags.py).
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from plexora.telemetry import schema

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests" / "js" / "refusal_count_probe.mjs"
SERVICES = ROOT / "plexora" / "client" / "src" / "js" / "services"


@pytest.fixture(scope="module")
def probe():
    if not shutil.which("node"):
        pytest.skip("needs node")
    done = subprocess.run(["node", str(PROBE)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _sent(row):
    return row["requests"] == ["POST /v1/telemetry/register keepalive", "POST /v1/telemetry/events keepalive"]


def test_a_refused_browser_is_counted_once_in_the_upload_format(probe):
    on = probe["cases"]["on"]
    assert on["outcome"] == "sent" and _sent(on), on
    register, events = probe["first"]["register"], probe["first"]["events"]
    assert register["url"] == "https://analytics.example/v1/telemetry/register"
    assert events["url"] == "https://analytics.example/v1/telemetry/events"
    for call in (register, events):
        assert call["mode"] == "cors" and call["credentials"] == "omit" and call["keepalive"] is True
    assert events["headers"]["Authorization"] == "Bearer PLEXORAT1.a.b"
    assert "Authorization" not in register["headers"]

    # Registration: the install id, and a client block without it (as uploader.register).
    body = register["body"]
    assert set(body) == {"schema", "install_id", "client"} and body["schema"] == schema.SCHEMA_VERSION
    assert re.fullmatch(r"[0-9a-f]{32}", body["install_id"]) and "install_id" not in body["client"]
    assert schema.validate_client(body["client"])

    # The batch: what the Worker's cleanBatch accepts, anonymous, one row.
    batch = events["body"]
    assert set(batch) == {"schema", "batch_id", "client", "events"} and batch["schema"] == schema.SCHEMA_VERSION
    assert re.fullmatch(r"[0-9a-f]{32}", batch["batch_id"]) and batch["batch_id"] != body["install_id"]
    assert schema.validate_client(batch["client"]) and batch["client"]["install_id"] == body["install_id"]
    assert batch["client"] == {"install_id": body["install_id"], **body["client"]}
    assert batch["client"] == {"install_id": body["install_id"], "os": "windows", "runtime": "web",
                               "launch_mode": "web", "deployment": "web", "install_kind": "web",
                               "mode": "anonymous", "plexora_version": "0.0.36"}
    assert batch["events"] == [{"type": "site.summary", "window": "2026-11-07T14",
                                "rows": [{"k": "refused", "d": {"browser": "chrome", "major": 131, "os": "windows"},
                                          "n": 1}]}]
    assert all(schema.validate_upload_event(event, schema.ANONYMOUS) for event in batch["events"])


def test_at_most_once_a_day_with_one_install_id(probe):
    cases = probe["cases"]
    assert cases["sameDay"]["outcome"] == "counted_today" and cases["sameDay"]["requests"] == []
    assert cases["nextDay"]["outcome"] == "sent" and _sent(cases["nextDay"])
    ids = probe["idsAcross"]
    assert ids["on"] == ids["nextDay"] and re.fullmatch(r"[0-9a-f]{32}", ids["on"])
    # Only what the protocol needs is kept: the day, the id (and a pause).
    assert set(cases["nextDay"]["mark"]) == {"id", "day"} and cases["nextDay"]["mark"]["day"] == "2026-11-08"


@pytest.mark.parametrize("name, outcome", [
    ("noEndpoint", "off:build"), ("noOptions", "off:build"),
    ("dnt", "off:do_not_track"), ("navDnt", "off:do_not_track"), ("gpc", "off:do_not_track"),
    ("prefsOff", "off:settings"),
    ("idbFails", "off:unknown_choice"), ("idbHangs", "off:unknown_choice"),
    ("noStorage", "no_storage"), ("storageThrows", "no_storage"), ("storageReadOnly", "no_storage"),
])
def test_nothing_is_sent_or_kept_when_the_rules_say_no(probe, name, outcome):
    row = probe["cases"][name]
    assert row["outcome"] == outcome and row["requests"] == [], row
    assert row["mark"] is None, row


@pytest.mark.parametrize("name", ["prefsDiagnostics", "prefsAnonymous", "prefsNone", "prefsBroken", "noIndexedDb"])
def test_otherwise_it_is_anonymous_whatever_was_chosen(probe, name):
    """As config.resolve: a stored mode other than off, none, or an unreadable
    block (read_prefs's {}) leaves it on -- and only ever anonymous."""
    row = probe["cases"][name]
    assert row["outcome"] == "sent" and _sent(row), row
    assert row["batch"]["client"]["mode"] == "anonymous"
    assert all(schema.validate_upload_event(event, schema.ANONYMOUS) for event in row["batch"]["events"])


def test_a_browser_that_never_ran_python_gets_no_database(probe):
    """Reading the earlier choice must not create the site's IndexedDB."""
    assert probe["idAbortedFresh"] == {"mode": None, "aborted": True}


def test_the_services_pause_is_honoured_and_kept_until_it_ends(probe):
    cases = probe["cases"]
    paused = cases["paused"]
    assert paused["outcome"] == "paused" and paused["requests"] == ["POST /v1/telemetry/register keepalive"]
    assert paused["mark"]["paused_until"] > 0
    later = cases["pausedNextDay"]
    assert later["outcome"] == "off:server" and later["requests"] == []
    over = cases["pauseOver"]
    assert over["outcome"] == "sent" and _sent(over) and "paused_until" not in over["mark"]


def test_failures_are_swallowed_and_an_old_keepalive_is_asked_again(probe):
    cases = probe["cases"]
    assert cases["offline"]["outcome"] == "failed"
    assert cases["noKeepalive"]["outcome"] == "sent"
    assert cases["noKeepalive"]["requests"] == [
        "POST /v1/telemetry/register keepalive", "POST /v1/telemetry/register",
        "POST /v1/telemetry/events keepalive", "POST /v1/telemetry/events"]


def test_values_stay_inside_the_schema(probe):
    row = probe["cases"]["edgeClamped"]
    batch = row["batch"]
    assert batch["events"][0]["rows"][0]["d"] == {"browser": "edge", "major": 999, "os": "other"}
    # A site version the schema does not take is left out, not sent for the Worker to drop.
    assert "plexora_version" not in batch["client"] and schema.validate_client(batch["client"])
    assert all(schema.validate_upload_event(event, schema.ANONYMOUS) for event in batch["events"])


def test_the_script_and_the_schema_agree():
    text = (SERVICES / "refusalCount.js").read_text(encoding="utf-8")
    key = schema.EVENTS["site.summary"].keys["refused"]
    assert key.agg == "count" and key.mode == schema.ANONYMOUS
    assert set(key.dims) == {"browser", "major", "os"}
    assert key.dims["browser"].type.values == ("chrome", "edge")
    assert key.dims["major"].type.maximum == int(re.search(r"MAJOR_MAX = (\d+);", text).group(1))
    assert all(field.mode == schema.ANONYMOUS for field in key.dims.values())
    assert f"const SCHEMA = {schema.SCHEMA_VERSION};" in text
    version = re.search(r"const VERSION = /\^(.*)\$/;", text).group(1)
    assert version == schema.CLIENT_BLOCK["plexora_version"].type.regex


def test_only_the_loader_counts_and_only_after_the_message():
    shell = (SERVICES / "shell.js").read_text(encoding="utf-8")
    refused = shell.index("const old = global.PlexoraPython.unsupported();")
    assert shell.index("stop(`Update ${old.browser}", refused) < shell.index("countRefusal(shell, old)", refused)
    # Loaded only for a build that names the service and a browser not declining.
    assert "if (!options || options.dnt) return;" in shell
    # The self-check says nothing is sent, and sends nothing.
    assert "refusalCount" not in (SERVICES / "selfcheck.js").read_text(encoding="utf-8")
