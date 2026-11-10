"""One Python per tab on the website, kept across a reload where the browser
keeps a SharedWorker a while after its last page (services/pythonClient.js
`spawn`; workers/pythonWorker.js `attach`, `watchPage`). The page's side,
run in Node with the browser stood in for (tests/js/tab_python_probe.mjs);
the worker's in tests/browser/tab_python.mjs, which needs Chrome."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKER = (ROOT / "plexora" / "client" / "src" / "js" / "workers" / "pythonWorker.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = ROOT / "tests" / "js" / "tab_python_probe.mjs"
    done = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_a_browser_that_keeps_no_shared_worker_keeps_the_pages_own(probe):
    assert probe["other"] == {"mode": "page", "dedicated": 1, "shared": 0}


def test_chrome_gives_each_tab_a_python_named_for_it(probe):
    first = probe["first"]
    assert first["mode"] == "tab" and first["dedicated"] == 0
    assert first["name"] == f"plexora-python:{first['stored']}"
    assert first["extended"] is True and first["module"] == "module"
    # The tab's lock is held while the page lives: a copy of the tab sees it.
    assert first["held"] is True
    # Posted before the worker connected, sent once it had.
    assert first["ops"] == ["warm", "boot"]
    # A SharedWorker shares no memory with its page: none is sent (it would
    # fail the boot message), and a job is stopped by message instead.
    assert first["bootShares"] == {"cancels": None, "inFlight": None}
    assert first["cancel"] is True and first["cancelMessage"] == ["job-7"]
    assert first["reattached"] is False


def test_the_self_checks_scratch_python_is_never_the_tabs(probe):
    assert probe["scratch"] == {"mode": "page", "shared": 0}


def test_a_reload_finds_the_tabs_python(probe):
    reload = probe["reload"]
    assert reload["same"] is True and reload["reattached"] is True
    assert reload["waitedMs"] < 500


def test_a_copy_of_a_live_tab_gets_its_own(probe):
    copy = probe["copy"]
    assert copy["differs"] is True and copy["kept"] is True
    assert copy["waitedAtLeastMs"] >= 1500


def test_a_python_that_failed_is_replaced(probe):
    # The worker told to quit, the tab's id forgotten, the page loaded again.
    assert probe["dead"] == {"quit": True, "stored": False, "reloads": 1}


def test_the_worker_answers_only_the_page_that_asked():
    """A reloaded page numbers its requests from 1 again: an answer to the page
    before must never reach it, and Python's questions to that page are
    answered with nothing (their fallback)."""
    drain = WORKER[WORKER.index("async function drain()"):WORKER.index("// What escaped every handler")]
    assert "if (pageNow(queued)) self.postMessage({ id, ok: true, result }, transfer);" in drain
    assert "if (pageNow(queued)) self.postMessage({ id, ok: false" in drain
    behind = WORKER[WORKER.index("function leftBehind("):WORKER.index("//: Whether the page that sent")]
    assert "queue.splice(i, 1)" in behind and "resolve(null)" in behind and "retrying.clear()" in behind
    # The worker closes once no page of its tab has come back.
    watch = WORKER[WORKER.index("function watchPage()"):WORKER.index("async function quit()")]
    assert "self.navigator.locks.request(self.name" in watch and "PAGE_GONE_MS" in watch


# -- in Chrome (tests/browser/tab_python.mjs) -----------------------------------
#
# Needs system Chrome, playwright-core and a Python bundle: the build
# tools/browser_build/build.py writes, at $PLEXORA_BENCH_HOME/browser_node/web/py
# (as tests/test_browser_python.py) or $PLEXORA_WEB_PY (a folder holding
# <version>/stages.json). Skipped, saying which, without them.
CHROME = Path(os.environ.get("PLEXORA_BENCH_CHROME")
              or r"C:\Program Files\Google\Chrome\Application\chrome.exe")
PLAYWRIGHT = Path(os.environ.get("PLEXORA_PLAYWRIGHT_MODULES")
                  or ROOT / "tools" / "poc_browser_node" / "node_modules") / "playwright-core"


def _bundle():
    for root in (os.environ.get("PLEXORA_WEB_PY"),
                 str(Path(os.environ.get("PLEXORA_BENCH_HOME") or "C:/plexora-bench") / "browser_node" / "web" / "py")):
        if not root or not Path(root).is_dir():
            continue
        versions = sorted(p.parent.name for p in Path(root).glob("*/stages.json"))
        if versions:
            return Path(root), versions[-1]
    return None, None


@pytest.fixture(scope="module")
def chrome(tmp_path_factory):
    root, version = _bundle()
    missing = [what for what, there in (("Chrome", CHROME.exists()), ("node", shutil.which("node")),
                                        ("a Python bundle", root is not None),
                                        ("playwright-core", PLAYWRIGHT.exists())) if not there]
    if missing:
        pytest.skip(f"needs {', '.join(missing)} (see the comment above)")
    from plexora.client import static_build

    site = tmp_path_factory.mktemp("site")
    static_build.build(site, py=f"/py/{version}/")
    done = subprocess.run(["node", str(ROOT / "tests" / "browser" / "tab_python.mjs"), "--site", str(site), "--py", str(root)],
                          cwd=ROOT, capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stderr[-3000:]
    out = json.loads(done.stdout.strip().splitlines()[-1])
    assert "fatal" not in out, out["fatal"]
    return out["checks"]


def test_in_chrome_a_reload_keeps_the_tabs_python(chrome):
    first, reload = chrome["first"], chrome["reload"]
    assert first["mode"] == "tab" and first["reattached"] is False
    assert first["workers"] == [f"plexora-python:{first['tab']}"]
    assert reload["tab"] == first["tab"] and reload["reattached"] is True and reload["sameBoot"] is True
    assert reload["workers"] == first["workers"]
    assert reload["health"]["status"] == 204


def test_in_chrome_a_copied_tab_has_its_own_and_a_closed_ones_goes(chrome):
    first, copy, closed = chrome["first"], chrome["copy"], chrome["closed"]
    assert copy["tab"] != first["tab"] and copy["reattached"] is False
    assert len(copy["workers"]) == 2
    # PAGE_GONE_MS (30 s) after the copy closed, give or take a poll.
    assert closed["goneMs"] is not None and 28000 <= closed["goneMs"] <= 45000
    assert closed["workers"] == first["workers"] and closed["health"]["status"] == 204


def test_in_chrome_restart_gives_the_tab_a_new_python(chrome):
    first, restart = chrome["first"], chrome["restart"]
    assert restart["tab"] != first["tab"] and restart["reattached"] is False
    assert restart["workers"] == [f"plexora-python:{restart['tab']}"]
    assert restart["health"]["status"] == 204
