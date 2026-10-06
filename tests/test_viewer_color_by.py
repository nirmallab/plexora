"""`viewer_set_color_by`: colour every cell in an open tab by one column.

The capability sends one viewer command, `set_color_by`; the tab's
agentBridge.js offers it to Cell Explorer (cellExplorerAnalysisBridge.js),
opening the tool first when it is not. The Python half is tested here against
a fake tab; the browser half against the bridge probe below.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path

import pytest

import plexora
from plexora.agent import AgentSession, registry
from plexora.server.models import viewer_sessions as vs
from tests.agent_fixtures import make_synthetic_project

REPO = Path(__file__).resolve().parent.parent


class Tab:
    """A tab that acknowledges every command, or refuses the named ones."""

    def __init__(self, project="synth", *, refuse=(), result=None):
        self.view_id = vs.register(project=project, client="browser")["view_id"]
        self.seen = []
        self.refuse = set(refuse)
        self.result = result
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        after = 0
        while not self._stop.is_set():
            work = vs.wait_for_work(self.view_id, after=after, wait_s=0.2)
            if work is None:
                return
            for command in work["commands"]:
                after = max(after, command["seq"])
                self.seen.append(command)
                if command["type"] in self.refuse:
                    vs.ack(self.view_id, command["command_id"], status="unsupported",
                           error="Cell Explorer is not available on this page")
                else:
                    vs.ack(self.view_id, command["command_id"], status="done",
                           result=self.result or {"handled_by": "cell_explorer",
                                                  "column": command["arguments"].get("column"),
                                                  "selected": True},
                           resulting_revision=7)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self.thread.join(2)


@pytest.fixture
def serving(tmp_path, monkeypatch):
    make_synthetic_project(tmp_path)
    registry.discover([])
    vs._reset_for_tests()
    monkeypatch.setitem(plexora.app.config, "PLEXORA_SERVING", True)
    yield
    vs._reset_for_tests()


def test_the_command_reaches_the_tab_with_its_column(serving):
    with Tab() as tab:
        answer = registry.invoke(AgentSession(), "viewer_set_color_by",
                                 {"column": "phenotype", "project": "synth"})
        assert answer["ok"], answer
        sent = next(c for c in tab.seen if c["type"] == "set_color_by")
        assert sent["arguments"] == {"column": "phenotype"}
        result = answer["result"]
        assert result["ack"]["result"]["handled_by"] == "cell_explorer"
        # A page view's arrangement, receipted like any viewer change, saved nowhere.
        assert result["receipt"]["persistent_state"] == "none"


def test_a_palette_travels_when_given(serving):
    with Tab() as tab:
        registry.invoke(AgentSession(), "viewer_set_color_by",
                        {"column": "phenotype", "palette": "tab20"})
        assert next(c for c in tab.seen if c["type"] == "set_color_by")["arguments"] == \
            {"column": "phenotype", "palette": "tab20"}


def test_a_tab_without_cell_explorer_says_so(serving):
    with Tab(refuse=("set_color_by",)):
        answer = registry.invoke(AgentSession(), "viewer_set_color_by", {"column": "phenotype"})
    assert not answer["ok"]
    assert answer["error"]["code"] == "capability_unavailable"
    assert "Cell Explorer" in answer["error"]["message"]


def test_with_no_viewer_it_is_viewer_not_available(tmp_path):
    make_synthetic_project(tmp_path)
    registry.discover([])
    answer = registry.invoke(AgentSession(), "viewer_set_color_by", {"column": "phenotype"})
    assert answer["error"]["code"] == "viewer_not_available"


def test_an_empty_column_is_refused_before_anything_is_sent(serving):
    answer = registry.invoke(AgentSession(), "viewer_set_color_by", {"column": ""})
    assert answer["error"]["code"] == "invalid_input"


def test_the_viewer_selection_is_what_the_tab_reports(serving):
    from plexora.agent import Policy

    with Tab(result={"handled_by": "roi", "cell_ids": [3, 4], "n": 2}) as tab:
        refused = registry.invoke(AgentSession(), "viewer_get_selection", {"project": "synth"})
        # Cell ids are row-level: an agent connection reads them only when the
        # server was started to allow it.
        assert refused["error"]["code"] == "permission_required"
        answer = registry.invoke(AgentSession(), "viewer_get_selection", {"project": "synth"},
                                 policy=Policy.from_flags(egress=["row_level"]))
    assert answer["ok"], answer
    assert answer["result"]["selection"]["cell_ids"] == [3, 4]
    assert any(c["type"] == "get_selection" for c in tab.seen)


def test_the_bridge_and_cell_explorer_ship_the_command():
    """The browser half, as shipped: agentBridge.js has the command and the
    selection read, Cell Explorer's descriptor loads its analysis bridge
    before the controller that builds it, and both parse."""
    bridge = (REPO / "plexora/client/src/js/services/agentBridge.js").read_text(encoding="utf-8")
    assert "async set_color_by(args, call)" in bridge and "async get_selection(args)" in bridge
    assert '"dataset.changed"' in bridge
    from plexora.plugins.cell_explorer import PLUGIN

    scripts = list(PLUGIN.scripts)
    assert scripts.index("cellExplorerAnalysisBridge.js") < \
        scripts.index("cellExplorerSidebarController.js")
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    for path in ("plexora/client/src/js/services/agentBridge.js",
                 "plexora/plugins/cell_explorer/static/cellExplorerAnalysisBridge.js"):
        assert subprocess.run([node, "--check", str(REPO / path)]).returncode == 0
