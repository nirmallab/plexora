"""A session-only viewer command (pan, zoom, a contrast shown in one tab) is
receipted, but it changed nothing a cached read describes: viewer reads are
never cached (ai/harness/toolcache.py `cacheable`). Bumping the project
revision for it flushed every cached tool result of the project on every pan
(audit ai-agent-core-8). A persisted one still bumps."""

from types import SimpleNamespace

import pytest

from plexora.agent import revision
from plexora.agent.core import viewer


class _Audit:
    path = "audit.jsonl"

    def __init__(self):
        self.lines = []

    def append(self, line):
        self.lines.append(line)


def _call():
    capability = SimpleNamespace(name="viewer.set_contrast", version="1", persistent=False,
                                 owner="core", reversible=True)
    return SimpleNamespace(capability=capability, extras={}, policy=None, operation_id="op1",
                           project_name="demo", notify=None, arguments={}, audit=_Audit(),
                           receipted=False)


@pytest.fixture
def bumps(monkeypatch):
    seen = []
    monkeypatch.setattr(revision, "bump", lambda project=None: seen.append(project) or {})
    return seen


ACK = {"type": "set_contrast", "arguments": {}, "result": {}, "command_id": "c1",
       "status": "ok"}


def test_a_session_only_command_does_not_move_the_revision(bumps):
    out = viewer._receipted(_call(), {"view_id": "v1", "project": "demo"}, ACK)

    assert out["receipt"]["changed"] is True
    assert bumps == []


def test_a_persisted_command_moves_the_revision(bumps):
    viewer._receipted(_call(), {"view_id": "v1", "project": "demo"}, ACK, persisted=True)

    assert bumps == ["demo"]
