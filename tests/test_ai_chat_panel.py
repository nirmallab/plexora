"""The Plexora AI chat panel (client/src/js/views/chatPanel.js), run under node,
and the viewer page that loads it.

`tests/js/chat_panel_probe.mjs` runs the real file against a stand-in DOM and
a fetch spy (no EventSource, so the held-poll fallback is what is exercised).
Skipped without node, like every other probe wrapper.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

import plexora

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = Path(plexora.__file__).parent / "client" / "templates"

CHECKS = [
    "boot mounts the panel, hidden, under the viewer wrapper, and no corner chip (the sidebar AI button opens it)",
    "opening starts a conversation with the viewer tools, and the server's disclosure event draws no line",
    "Minimize folds the chat into a Restore / Close pill and Restore brings back the draft; Send is glass until there is one",
    "Send posts the text and the attached images, and the user's line shows their thumbnails",
    "Stop shows while a turn runs and posts {action: stop}",
    "...the control route got it",
    "text deltas stream into one assistant line, as text (never markup)",
    "the whole text replaces the streamed line",
    "tool chips name the tool and say cached; a reversible write carries Undo and its images",
    "Undo posts the operation id to the conversation's undo route",
    "an approval card says what the call is and that it cannot be undone",
    "Approve posts {approval_id, decision: approve}",
    "Deny posts decision: deny, and a source-file write says so",
    "the credit meter runs",
    "sub-agents and a credit pause read as muted lines, and the turn's end clears busy",
    "an event at or before the cursor is ignored",
    "a reload reopens the same conversation, minimized as it was, and the replay redraws the user's line and a decided approval",
    "a line sent from this page is not drawn twice when its user_message comes back",
    "a conversation the server no longer has is not resumed: a new one starts in its place",
    "on Free the paid-feature modal opens and the panel stays closed",
    "the panel never parses markup",
]


@pytest.fixture(scope="module")
def probe():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return subprocess.run([node, str(REPO_ROOT / "tests" / "js" / "chat_panel_probe.mjs")],
                          capture_output=True, text=True, cwd=REPO_ROOT, timeout=120)


@pytest.mark.parametrize("name", CHECKS)
def test_chat_panel(probe, name):
    lines = probe.stdout.splitlines()
    assert f"PASS {name}" in lines, probe.stdout + probe.stderr


def test_the_probe_runs_no_unlisted_check(probe):
    ran = {line.split(" ", 1)[1] for line in probe.stdout.splitlines() if line.startswith(("PASS ", "FAIL "))}
    assert ran == set(CHECKS)


def test_the_viewer_loads_the_panel_and_its_stylesheet_with_cache_tags():
    html = (TEMPLATES / "index.html").read_text(encoding="utf-8")
    script = re.search(r'src="[^"]*/client/src/js/views/chatPanel\.js\?v=([^"]+)"', html)
    style = re.search(r'href="[^"]*/client/src/css/chatPanel\.css\?v=([^"]+)"', html)
    assert script and style
    # After agentPanel.js and before main.js, like the other agent views.
    assert html.index("views/agentPanel.js") < html.index("views/chatPanel.js") < html.index("js/main.js")
    root = Path(plexora.__file__).parent / "client" / "src"
    assert (root / "js" / "views" / "chatPanel.js").is_file() and (root / "css" / "chatPanel.css").is_file()
