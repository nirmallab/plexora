"""A page of the website gets pointer capture: the viewer pans
(tests/browser/pointer_capture.mjs).

On the website the image zoomed but never panned. Every page of the app is
written into the site's frame with document.open/write (client
src/js/services/shell.js frameBoot), and Chrome (157) gives a document opened
while the pointer is over its frame no pointer capture, ever: setPointerCapture
does nothing, and every pointer event arrives with a pointerover/pointerenter
from the document down, as though the pointer had just come in. OpenSeadragon
takes the position from that pointerenter as the drag's last position, so each
drag step measured 0 px. On the preview, an image picked through Browse panned
in 2 of 10 runs before and 5 of 5 after. The desktop app loads its pages
normally and never had it.

The loader now takes its frame out of the host's layout for two of the host's
frames around document.open (hiddenForOpen). The driver reproduces Chrome's
fault with a host page, a frame and document.open alone (the control), then
opens the same page through shell.js's own hiddenForOpen with the pointer
still and moving.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "browser" / "pointer_capture.mjs"
SHELL = ROOT / "plexora" / "client" / "src" / "js" / "services" / "shell.js"
PLAYWRIGHT = Path(os.environ.get("PLEXORA_PLAYWRIGHT_MODULES")
                  or ROOT / "tools" / "poc_browser_node" / "node_modules") / "playwright-core"


@pytest.fixture(scope="module")
def record(tmp_path_factory):
    if not shutil.which("node") or not PLAYWRIGHT.is_dir():
        pytest.skip("needs node and playwright-core")
    out = tmp_path_factory.mktemp("pointer-capture") / "record.json"
    env = {**os.environ, "PLEXORA_PYTHON": os.environ.get("PLEXORA_PYTHON") or sys.executable}
    done = subprocess.run(["node", str(DRIVER), "--out", str(out)], cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8", timeout=300, env=env)
    try:
        result = json.loads(out.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pytest.fail(f"pointer_capture.mjs wrote no record: {done.stderr[-3000:]}")
    assert not result["failed"], result["failed"]
    return result["cases"]


def test_chrome_breaks_capture_in_a_document_opened_under_the_pointer(record):
    # The control. If this fails, Chrome may have fixed it: check the site
    # pans without hiddenForOpen before taking it out.
    plain = record["plain"]
    assert plain["hasCaptureAfterSet"] is False and plain["gotCapture"] is False
    assert plain["entersDuringDrag"] >= 3
    assert plain["movesOutsideReached"] == 0


@pytest.mark.parametrize("case", ["still", "moving"])
def test_the_loaders_document_captures_the_pointer(record, case):
    opened = record[case]
    assert opened["hasCaptureAfterSet"] is True and opened["gotCapture"] is True
    assert opened["entersDuringDrag"] == 0
    assert opened["movesOutsideReached"] == 2
    assert opened["hiddenMs"] is not None


def test_frame_boot_opens_the_page_while_the_frame_is_hidden():
    source = SHELL.read_text(encoding="utf-8")
    write = source[source.index("async function writePage("):source.index("const parsed = ")]
    hide, opened, shown, written = (write.index("await hiddenForOpen()"), write.index("doc.open()"),
                                    write.index("shown()"), write.index("doc.write(html)"))
    # Shown again as soon as it is open: the page's scripts run in a frame
    # with a box (they measure the window).
    assert hide < opened < shown < written
    # Every page is written that way: Python's answer and a kept page both.
    assert source.count("doc.open()") == 1
    boot = source[source.index("async function frameBoot()"):]
    early = source[source.index("async function earlyPage("):source.index("async function frameBoot()")]
    assert "await writePage(html, project, address)" in boot
    assert "await writePage(html, \"\", address)" in early
