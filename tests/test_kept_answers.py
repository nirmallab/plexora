"""Kept answers: after a reload of the website, a project's viewer is drawn
from Python's own answers, kept, while Python starts again (client
services/keptAnswers.js; plexora/browser/routes.py `KEPT`).

Measured on the CODEX tonsil fixture (M2 Pro, Chrome 157, the CDN bundle
0.0.41): a reload drew the viewer in 6.9 s, 1.9 s after Python had started
(5.0 s); with kept answers, in 0.5 s, with every tile read by the browser node
-- and the 14 answers given asked of Python again once it was up, all the same.
With one of them made stale on purpose (the channel list's colour), the
page's write was refused, never reached Python, and the page was replaced.

The node probe (tests/js/kept_answers_probe.mjs) stands in Cache Storage, the
route table, Python and the picks.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from plexora.browser import routes

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "plexora" / "client" / "src" / "js"
KEPT = (JS / "services" / "keptAnswers.js").read_text(encoding="utf-8")
SHELL = (JS / "services" / "shell.js").read_text(encoding="utf-8")
BACKEND = (JS / "services" / "backend.js").read_text(encoding="utf-8")
CLIENT = (JS / "services" / "pythonClient.js").read_text(encoding="utf-8")
NODE = (JS / "services" / "browserNode.js").read_text(encoding="utf-8")
PICKS = (JS / "services" / "pickedFiles.js").read_text(encoding="utf-8")
TABLE = (JS / "services" / "backendRoutes.js").read_text(encoding="utf-8")
PROBE = ROOT / "tests" / "js" / "kept_answers_probe.mjs"


def _function(source, name):
    start = source.index(f"function {name}(")
    depth, at = 0, source.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(source[at], 0)
        if depth == 0:
            return source[start:at + 1]
        at += 1


@pytest.fixture(scope="module")
def probe():
    if not shutil.which("node"):
        pytest.skip("needs node")
    done = subprocess.run(["node", str(PROBE)], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_kept_list_is_one_list():
    found = re.search(r"const ENDPOINTS = Object\.freeze\(\[(.*?)\]\);", KEPT, re.S)
    assert found
    assert tuple(re.findall(r'"([a-z_.]+)"', found.group(1))) == routes.KEPT


def test_every_kept_route_is_a_get_of_the_app():
    rows = [json.loads(line.strip().rstrip(","))
            for line in TABLE.splitlines() if line.strip().startswith('{"class"')]
    gets = {row["endpoint"] for row in rows if "GET" in row["methods"]}
    for endpoint in routes.KEPT:
        assert endpoint in gets, endpoint
        # Nothing the page writes, nothing a handoff answers.
        assert routes.classify(endpoint) in ("js", "python", "static"), endpoint


def test_answers_are_kept_by_route_and_project(probe):
    assert probe["locate"] == {
        "config": {"normal": "/config", "project": "_"},                 # ?t= is a cache-buster
        "names": {"normal": "/get_channel_names?datasource=image&shortNames=true", "project": "image"},
        "transform": {"normal": "/view_transform/my%20image", "project": "my image"},
        "page": {"normal": "/my%20image", "project": "my image"},
    }
    # A kept route's 200 only: not another route, not a fragment of the page,
    # not a routing answer naming a node, and a failure drops the copy.
    assert probe["keptKeys"] == [
        "/__plexora/kept/_//config",
        "/__plexora/kept/image//get_channel_names?datasource=image",
        "/__plexora/kept/image//get_saved_channel_list?datasource=image",
        "/__plexora/kept/image//image",
        "/__plexora/kept/image//view_transform/image",
    ]
    assert probe["keptPage"].startswith("<!doctype html>")


def test_kept_answers_are_given_only_while_python_starts(probe):
    assert probe["waiting"] is True and probe["config"]["kept"] == "1"
    assert json.loads(probe["config"]["body"])["image"]["lastOpenedAt"] == "a"
    assert probe["names"] == '["DAPI"]'
    # Nothing that was not kept, never the page by fetch, never a write.
    assert probe["cells"] is None and probe["pageByFetch"] is None and probe["post"] is None
    assert probe["waitingAfter"] is False and probe["afterUp"] is None
    # Another version's answers are not this one's, and are dropped.
    assert probe["otherVersion"] is None and probe["purged"] == 0


def test_the_spec_and_the_page_shown_at_once(probe):
    assert probe["spec"] == {"fromPython": True, "kept": "/picked/abc/image.ome.tif"}
    assert probe["early"]["project"] == "image"
    # Only the pick the spec names was opened again, before Python.
    assert probe["reopened"] == ["abc"]
    assert probe["earlyOther"] is None
    # A reload with a tool open (`?tool=`, written back by toolLoader): the
    # plain viewer at once, the tool opened in it once Python is up.
    assert probe["earlyQuery"]["project"] == "image"
    assert probe["earlyQuery"]["opens"] == "gating" and probe["earlyQuery"]["page"] == "/image"
    # Any other question of the page: it waits for Python, as before.
    assert probe["earlyOtherQuery"] is None


def test_the_same_answers_let_the_writes_go(probe):
    # /config's lastOpenedAt changes with every start: not a difference.
    assert probe["checked"] == {"checked": 3, "changed": []}
    assert probe["cleared"] is True
    # The spec: Python's while none was kept, then asked again by the check.
    assert probe["asked"] == ["spec:image", "/config", "/get_channel_names?datasource=image",
                              "/get_saved_channel_list?datasource=image", "spec:image"]
    # The browser node checks its project again, with Python's spec.
    assert probe["revalidated"] == 1
    # A page given nothing from here never waits.
    assert probe["otherClearedBefore"] is True


def test_a_changed_answer_refuses_the_writes_and_is_kept_anew(probe):
    assert probe["changedCleared"] is False
    assert probe["changedChecked"] == {"checked": 1, "changed": ["/get_saved_channel_list?datasource=image"]}
    assert probe["report"]["refused"] == 1
    assert json.loads(probe["rekept"]) == [{"channel": "DAPI", "r": 255}]
    assert probe["warnings"] == ["kept answers: changed since they were kept: "
                                 "/get_saved_channel_list?datasource=image"]


def test_a_write_has_its_projects_state_asked_again(probe):
    # The channels' names too (a rename), and the browser node's spec (a mask
    # attached): kept as Python gives them now, or the next reload's check
    # finds them changed and refuses the page's writes.
    assert probe["recaptured"] == ["/config", "/get_channel_names?datasource=image",
                                   "/get_saved_channel_list?datasource=image", "/image",
                                   "/view_transform/image", "spec:image"]
    assert probe["equalVolatile"] is True and probe["unequal"] is False


def test_what_python_moves_on_by_itself_is_no_change(probe):
    """Measured on LSP11385 with a plain (not pyramidal) mask: the spec was
    kept while the page built the mask's pyramid, and auto-contrast's answer
    while it was calibrating; a reload found both "changed", and every write
    of the page was refused -- the channel list unsaved, the agent bridge
    signing in again every 5 s."""
    assert probe["specPending"] == {"checked": 1, "changed": []}
    assert probe["specGeneration"] == {"checked": 1, "changed": []}
    assert probe["specMoved"] == {"checked": 1, "changed": ["spec:image"]}
    assert probe["specWarnings"] == ["kept answers: changed since they were kept: spec:image"]
    assert probe["contrastMovedOn"] is True
    assert probe["contrastChanged"] is False


def test_the_tabs_agent_session_is_never_held_back():
    unheld = BACKEND[BACKEND.index("const UNHELD = new Set(["):]
    unheld = unheld[:unheld.index("]);")]
    for endpoint in ("register_session", "ack_command", "leave_session", "post_capture"):
        assert f'"agent_v1.{endpoint}"' in unheld
        assert f'"agent_v1.{endpoint}"' in TABLE
    answer = BACKEND[BACKEND.index("async function answer(request)"):BACKEND.index("function supports(")]
    assert "!UNHELD.has(row.endpoint)" in answer[:answer.index("kept.cleared(global)")]


def test_the_gate_answers_from_kept_answers_and_holds_writes():
    answer = BACKEND[BACKEND.index("async function answer(request)"):BACKEND.index("function supports(")]
    asked = answer.index("kept.answer(request, global)")
    gate = answer.index("await gate;")
    cleared = answer.index("kept.cleared(global)")
    assert asked < gate < cleared
    assert '!["GET", "HEAD"].includes(request.method)' in answer[gate:cleared + 40]
    assert "409" in answer[cleared:cleared + 400]


def test_python_answers_are_observed_and_the_spec_is_kept():
    send = _function(CLIENT, "send")
    assert "global.PlexoraKept.observe(url.href, method, status, answered, payload, headers)" in send
    assert "global.PlexoraKept.spec(name, () => global.PlexoraPython.nodeSpec(name))" in NODE


def test_the_host_shows_a_kept_viewer_and_checks_it():
    boot = _function(SHELL, "hostBoot")
    assert 'services/keptAnswers.js"' in boot
    kept = boot[boot.index("const kept = route === \"/\""):boot.index("const licenceModule")]
    assert "global.PlexoraKept.early(route)" in kept
    assert boot.index("global.PlexoraShellStarted = started;") < boot.index("global.PlexoraKept.starting(started);")
    assert boot.index("global.PlexoraKept.starting(started);") < boot.index("const early = await kept;")
    frame = _function(SHELL, "frameBoot")
    assert "global.PlexoraKept = host.PlexoraKept || null;" in frame
    early = _function(SHELL, "earlyPage")
    assert "global.PlexoraNode.project(project)" in early
    assert early.index("await host.PlexoraShellStarted;") < early.index("host.PlexoraKept.checked(global)")
    # An unused page is replaced; a used one offers a reload.
    assert early.index("if (!used) {") < early.index('title: "This project changed"')


def test_picks_open_again_without_python_and_mount_once_it_is_up():
    restore = _function(PICKS, "restore")
    assert "await Promise.all([...reopening.values()]);" in restore
    assert "unmounted.get(record.id)" in restore
    reopen = _function(PICKS, "reopen")
    assert "later(calls)" in reopen and "global.PlexoraPython" not in reopen.replace("later(calls)", "")
