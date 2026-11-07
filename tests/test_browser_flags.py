"""The site's switches (plan Phase 5): the loader reads flags.json before
anything else (services/shell.js `readFlags`; tools/browser_build/publish.py
flags) -- the kill switch pauses the website without loading Python, a minimum
version stops an older site, the preview is marked, a model can be switched
off, and a missing or broken file changes nothing (tests/browser/flags.mjs,
in Chrome).
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
def cases(tmp_path_factory):
    site = tmp_path_factory.mktemp("site")
    flags = tmp_path_factory.mktemp("flags")
    static_build.build(site, flags="/fx/flags.json")
    done = subprocess.run(["node", str(DRIVER), "--site", str(site), "--flags-dir", str(flags)],
                          capture_output=True, text=True, encoding="utf-8", timeout=900)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])["cases"]


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
