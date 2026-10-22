"""Bookmarks: views kept as captures, and their tray.

Core keeps no store of its own (client/src/js/services/bookmarks.js): Figure
Builder's captures bin is the store (plugins/figure_builder/static/
figureBookmarkStore.js), a bookmark a capture with a flag. The service, the
tray (views/bookmarkTray.js) and the store's handshake are run in node against
stand-ins (tests/js/bookmarks_probe.mjs); the page checks pin where the pieces
are loaded. The server half is in
plugins/figure_builder/tests/test_figure_builder_captures.py.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENT = REPO_ROOT / "plexora" / "client"
JS = CLIENT / "src" / "js"


def _node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return node


def test_the_bookmarks_probe_passes():
    """The store registers whichever script runs first; a bookmark is read at
    once and written as a bookmarked capture, then its picture; a restore is
    kept; removing takes the flag off and nothing else; the icon only toggles
    the tray; K saves with the tray closed, never while typing, with a
    modifier, held, with a dialog up or once taken."""
    proc = subprocess.run([_node(), str(REPO_ROOT / "tests" / "js" / "bookmarks_probe.mjs")],
                          capture_output=True, text=True, cwd=REPO_ROOT, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "all " in proc.stdout and "checks passed" in proc.stdout
    assert proc.stdout.count("ok - ") >= 30


def test_the_bookmark_scripts_parse():
    node = _node()
    for path in (JS / "services" / "bookmarks.js", JS / "views" / "bookmarkTray.js",
                 JS / "services" / "viewRaster.js",
                 REPO_ROOT / "plexora" / "plugins" / "figure_builder" / "static" / "figureBookmarkStore.js"):
        proc = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, f"{path.name}: {proc.stderr}"


def test_the_viewer_page_loads_the_bookmarks_and_their_icon():
    """The service before the tray, both before main.js, which attaches the
    tray; the icon in the sidebar header beside the AI button, hidden until a
    store is there; the picture of the screen on every page (base.html), as
    Figure Builder's capture preview needs it too."""
    page = (CLIENT / "templates" / "index.html").read_text(encoding="utf-8")
    order = [page.index(name) for name in (
        "js/services/bookmarks.js?v=", "js/views/bookmarkTray.js?v=", "js/main.js?v=")]
    assert order == sorted(order)
    button = page[page.index('id="viewer_bookmark_button"') - 200:page.index('id="viewer_bookmark_button"') + 400]
    assert "hidden" in button and "fa-bookmark" in button
    assert page.index('id="viewer_bookmark_button"') < page.index('id="plexora_ai_button"')
    base = (CLIENT / "templates" / "base.html").read_text(encoding="utf-8")
    assert base.index("js/services/viewerScene.js?v=") < base.index("js/services/viewRaster.js?v=")
