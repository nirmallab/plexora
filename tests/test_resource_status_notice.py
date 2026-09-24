"""Why a layer is missing, said out loud -- and only when it is news.

`/resource_status` has known the answer since data nodes arrived. A project
whose cell table is on a node that has gone opens with the images and without
the colours, which is the right behaviour, and deserves a sentence.

It used to be a strip across the top of the viewer, and it appeared in a
FRESH session: the server's node map outlives a restart, so a project opened
the next morning warned about yesterday's tunnel. Now it is a small notice in
the corner, raised only for a machine that was up earlier in the same tab.

The decisions worth pinning are all about NOT being noisy. They live in
tests/js/resource_status_probe.mjs, run in node against the shipped file.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "resource_status_probe.mjs"
CLIENT = REPO_ROOT / "plexora" / "client"

#: Every line the probe prints, so a check that is deleted fails here.
CHECKS = (
    "a project with everything here draws nothing",
    "a node that was fine earlier in this tab and is now missing gets a warning notice naming it",
    "...with Reconnect, which hands off to the one dialog that connects",
    "...and the project is read again, then the page",
    "a Reconnect that does not connect puts the notice back",
    "a re-report saying the same thing does not raise it again",
    "the notice goes when the project is whole again",
    "a node seen up through remoteState counts as up in this tab",
    "with no saved connection, the notice names the command and offers Settings",
    "a node reached only through the server raises nothing",
    "a slow node is a footnote on a notice that already exists",
    "dismissing it is remembered for this tab",
    "a project that opens whole forgets both answers",
    "a node never seen up in this tab is not called disconnected",
    "a connectable machine never seen up is asked about once, with a modal",
    "declining the offer leaves no strip and no notice",
    "the offer to connect returns once the situation has",
    "a status route that fails draws nothing and throws nothing",
    "a converting mask is a chip naming the node, with a percentage",
    "...and the mask is redrawn at its new version when it lands",
    "a conversion that fails again says the node's reason",
    "a failure and a read-only note are one dismissible notice",
)


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    proc = subprocess.run(
        [node, str(PROBE)], capture_output=True, text=True, encoding="utf-8",
        cwd=REPO_ROOT, timeout=60
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    return proc.stdout


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert f"ok - {line}" in probe, probe


def test_no_check_was_quietly_dropped(probe):
    assert probe.count("ok - ") == len(CHECKS), probe


def test_loaded_on_every_page_that_can_open_a_viewer():
    """It is main.js that calls it, but the script tag is in base.html, so a
    page added later gets it for free -- and the notice it raises is
    toast.js's, which base.html loads everywhere too."""
    base = (CLIENT / "templates" / "base.html").read_text(encoding="utf-8")
    assert "services/resourceStatus.js?v=" in base
    assert "services/toast.js?v=" in base


def test_the_strip_is_gone():
    """No strip means no rule that takes its height out of the viewer."""
    css = (CLIENT / "src" / "css" / "main.css").read_text(encoding="utf-8")
    assert ".resource-status-banner" not in css
    assert "body:has(> .resource-status" not in css


def test_main_js_no_longer_sweeps_strips():
    main = (CLIENT / "src" / "js" / "main.js").read_text(encoding="utf-8")
    assert "resource-status-banner" not in main
