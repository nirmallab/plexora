"""The site's switches (plan Phase 5): the loader reads flags.json before
anything else (services/shell.js `readFlags`; tools/browser_build/publish.py
flags) -- the kill switch pauses the website without loading Python, a minimum
version stops an older site, the preview is marked, a model can be switched
off, and a missing or broken file changes nothing (tests/browser/flags.mjs,
in Chrome). And a Chrome too old for the website's Python is asked to update,
and counted (services/refusalCount.js) only where the build names the
telemetry service (here tests/browser/mock_telemetry.py) and the visitor's
choices allow it, once a day.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from plexora.client import static_build

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "browser" / "flags.mjs"
PLAYWRIGHT = ROOT / "tools" / "poc_browser_node" / "node_modules" / "playwright-core"
BUNDLE = Path("C:/plexora-bench/browser_node/web")

pytestmark = pytest.mark.skipif(
    not shutil.which("node") or not PLAYWRIGHT.is_dir() or not BUNDLE.is_dir(),
    reason="needs node, playwright-core and a Python bundle (tools/browser_build/build.py)")


@pytest.fixture(scope="module")
def driven(tmp_path_factory):
    site = tmp_path_factory.mktemp("site")
    flags = tmp_path_factory.mktemp("flags")
    static_build.build(site, flags="/fx/flags.json")
    done = subprocess.run(["node", str(DRIVER), "--site", str(site), "--flags-dir", str(flags)],
                          capture_output=True, text=True, encoding="utf-8", timeout=900)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def cases(driven):
    return driven["cases"]


@pytest.fixture(scope="module")
def refusals(driven):
    return driven["refusals"]


def test_the_kill_switch_pauses_the_site_without_loading_python(cases):
    paused = cases["paused"]
    assert paused.get("said") == "Back at noon." and paused["button"] == "Get the desktop app"
    assert paused["app"] is False and paused["python_requests"] == 0


def test_an_older_site_than_the_minimum_stops_and_offers_a_reload(cases):
    stale = cases["stale"]
    assert stale.get("said") == "This copy of Plexora is out of date." and stale["button"] == "Reload"
    assert stale["app"] is False and stale["python_requests"] == 0


def test_the_preview_is_marked_and_a_model_switched_off_says_so(cases):
    preview = cases["preview"]
    assert preview.get("app") is True and preview["mark"] == "Preview", preview
    assert preview["sam"]["hint"] == "Magic select is switched off on the website for now."
    assert preview["sam"]["runtime"]["installed"] is False


@pytest.mark.parametrize("name", ["absent", "broken"])
def test_a_missing_or_broken_flags_file_changes_nothing(cases, name):
    row = cases[name]
    assert row.get("app") is True and row["mark"] is None, row
    assert row["python_requests"] > 0
    assert row["sam"].get("hint") != "Magic select is switched off on the website for now."


def test_a_chrome_too_old_for_the_websites_python_is_asked_to_update(cases):
    """Chrome 131 (no JSPI) crashed on a Mac starting the website's Python:
    the loader says so before anything starts (pythonClient.js `unsupported`)."""
    old = cases["oldChrome"]
    assert old["said"] == "Update Chrome to use Plexora here (version 131; it needs 137 or later).", old
    assert old["buttons"] == ["How to update Chrome", "Get the desktop app"], old
    assert old["python_requests"] == 0 and old["app"] is False, old


def test_a_build_without_telemetry_counts_nothing(cases):
    """The preview deployed by hand is built without `--telemetry`: a refused
    browser there makes no request off the site and loads no counting script,
    under the enforced Content-Security-Policy, which refuses nothing."""
    old = cases["oldChrome"]
    assert old["offsite"] == [] and old["refusal_script"] is False, old
    assert old["csp"] == [], old


REFUSED_SAID = "Update Chrome to use Plexora here (version 131; it needs 137 or later)."


def test_a_refused_browser_is_counted_once_a_day(refusals, driven):
    """On a build that names the service: after the same message, exactly one
    registration and one well-formed batch, from the site's origin, to the
    service only; nothing from the Python bundle; no CSP refusal; and the next
    visit that day sends nothing."""
    from plexora.telemetry import schema

    row = refusals["counted"]
    assert row["said"] == REFUSED_SAID and row["buttons"] == ["How to update Chrome", "Get the desktop app"], row
    assert row["posts"] == ["POST /v1/telemetry/register", "POST /v1/telemetry/events"], row
    assert row["origins"] == [driven["site_origin"]], row
    assert row["python_requests"] == 0 and row["csp"] == [], row
    assert row["authorization"] is True and row["encoding"] is None
    batch = row["batch"]
    assert batch["schema"] == schema.SCHEMA_VERSION and schema.validate_client(batch["client"])
    assert batch["client"]["install_id"] == row["register"]["install_id"] == row["kept"]["id"]
    assert batch["client"]["mode"] == "anonymous" and batch["client"]["runtime"] == "web"
    assert [event["type"] for event in batch["events"]] == ["site.summary"]
    assert batch["events"][0]["rows"] == [{"k": "refused", "d": {"browser": "chrome", "major": 131, "os": "windows"}, "n": 1}]
    assert all(schema.validate_upload_event(event, schema.ANONYMOUS) for event in batch["events"])
    # Off the site: the service and nothing else.
    assert {where.rsplit("/v1/", 1)[0] for where in row["offsite"]} == {row["offsite"][0].rsplit("/v1/", 1)[0]}
    assert row["again_requests"] == 0, row
    # Reading the visitor's earlier choice made no database.
    assert "plexora-telemetry" not in (row["databases"] or []), row


@pytest.mark.parametrize("name", ["dnt", "gpc", "chosenOff"])
def test_do_not_track_privacy_control_or_off_count_nothing(refusals, name):
    row = refusals[name]
    assert row["said"] == REFUSED_SAID, row
    assert row["posts"] == [] and row["all_requests"] == 0 and row["again_requests"] == 0, row
    assert row["offsite"] == [] and row["kept"] is None and row["csp"] == [], row
    # Do Not Track and Privacy Control do not even load the script.
    assert row["refusal_script"] is (name == "chosenOff"), row
