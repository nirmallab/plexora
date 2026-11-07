"""The website asks a browser that cannot run its Python to update, instead of
starting it (client src/js/services/pythonClient.js `unsupported`, used by
the loader, services/shell.js, and the self-check page).

Chrome 131 on a Mac ended the self-check's Python step with "Error code: 5"
(the renderer killed) and Chrome 131 on Windows never finished starting it,
though every compiled module in the bundle validates on an older engine: a
Chrome or Edge with no JSPI (before 137) is told to update. Firefox and
Safari without JSPI are not: they take the website's other path, which runs.

That other path runs in Chromium too: Chrome 156 with `WebAssembly.Suspending`
deleted at the top of the worker (so Pyodide's own detection also finds no
JSPI) passed the whole self-check, Python ready in 11 s over shared memory
(7 s with JSPI). What fails is Chrome 131's engine, not a feature it lacks;
tracing it needs that Chrome run here. The same Mac on Chrome 157 passed
every step.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests" / "js" / "unsupported_browser_probe.mjs"


@pytest.fixture(scope="module")
def probe():
    if not shutil.which("node"):
        pytest.skip("needs node")
    done = subprocess.run(["node", str(PROBE)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_an_old_chrome_or_edge_is_asked_to_update(probe):
    without = probe["withoutJspi"]
    assert without["macChrome131"] == {"browser": "Chrome", "version": 131}
    assert without["chrome131UaOnly"] == {"browser": "Chrome", "version": 131}
    assert without["edge131"] == {"browser": "Edge", "version": 131}


def test_firefox_safari_and_ios_take_the_other_path(probe):
    without = probe["withoutJspi"]
    assert without["firefox"] is None and without["safari"] is None
    assert without["iosChrome"] is None          # WebKit underneath, like Safari


def test_with_jspi_nothing_is_refused(probe):
    assert all(value is None for value in probe["withJspi"].values())


def test_the_loader_and_the_self_check_both_ask(probe):
    shell = (ROOT / "plexora" / "client" / "src" / "js" / "services" / "shell.js").read_text(encoding="utf-8")
    selfcheck = (ROOT / "plexora" / "client" / "src" / "js" / "services" / "selfcheck.js").read_text(encoding="utf-8")
    # The loader asks before starting Python, right after loading its client.
    assert shell.index("PlexoraPython.unsupported()") < shell.index("PlexoraPython.start(")
    assert "unsupported()" in selfcheck
