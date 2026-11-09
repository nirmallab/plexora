"""⌘R / Ctrl+R / F5 on the website reload the app's frame, not the site
(client services/shell.js `reloadKey`, `reloadsFrame`).

A reload of the whole site starts Python over: 5.3 s on an M2 Pro with a warm
cache before anything of a project could be drawn again, against 0.28 s for
the local server. The frame is Python's answer again in a moment, with Python,
the picked files and the store as they are. Shift (a hard reload) is left to
the browser, the way out of a Python that has stopped answering.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "plexora" / "client" / "src" / "js" / "services" / "shell.js"


def _function(source, name):
    start = source.index(f"function {name}(")
    depth, end = 0, source.index("{", start)
    for end in range(end, len(source)):
        depth += {"{": 1, "}": -1}.get(source[end], 0)
        if depth == 0:
            break
    return source[start:end + 1]


def test_which_keys_reload_the_frame():
    if not shutil.which("node"):
        pytest.skip("needs node")
    body = _function(SHELL.read_text(encoding="utf-8"), "reloadKey")
    cases = {
        "cmdR": {"key": "r", "metaKey": True},
        "ctrlR": {"key": "r", "ctrlKey": True},
        "ctrlCapsR": {"key": "R", "ctrlKey": True},
        "f5": {"key": "F5"},
        "shiftCmdR": {"key": "R", "metaKey": True, "shiftKey": True},
        "shiftF5": {"key": "F5", "shiftKey": True},
        "ctrlF5": {"key": "F5", "ctrlKey": True},
        "altCmdR": {"key": "r", "metaKey": True, "altKey": True},
        "plainR": {"key": "r"},
        "cmdT": {"key": "t", "metaKey": True},
    }
    script = f"{body}\nconst cases = {json.dumps(cases)};\n" \
             "console.log(JSON.stringify(Object.fromEntries(Object.entries(cases).map(([k, e]) => " \
             "[k, reloadKey({shiftKey: false, altKey: false, metaKey: false, ctrlKey: false, ...e})]))));"
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)
    assert got == {"cmdR": True, "ctrlR": True, "ctrlCapsR": True, "f5": True,
                   "shiftCmdR": False, "shiftF5": False, "ctrlF5": False, "altCmdR": False,
                   "plainR": False, "cmdT": False}


def test_both_the_page_and_the_host_reload_the_frame():
    source = SHELL.read_text(encoding="utf-8")
    write = source[source.index("async function writePage("):source.index("function reveal()")]
    # On the document: document.open takes every listener off the window.
    assert "reloadsFrame(doc, () => global.frameElement)" in write
    boot = source[source.index("async function hostBoot()"):source.index("async function frameBoot()")]
    assert 'reloadsFrame(global, () => doc.getElementById("plexora-app"))' in boot
    assert "frame.contentWindow.location.reload()" in _function(source, "reloadsFrame")
