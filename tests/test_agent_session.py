"""An agent session's handles: bounded, fresh, and never the viewer's."""

import dataclasses
import json
import os
import time

import pytest

from plexora.agent import AgentError, AgentSession
from plexora.server.models import data_model
from plexora.server.models.project import Project, ResourceBinding
from tests.agent_fixtures import make_synthetic_project


@pytest.fixture
def made(tmp_path):
    first = make_synthetic_project(tmp_path, "one")
    make_synthetic_project(tmp_path, "two", seed=1)
    make_synthetic_project(tmp_path, "three", seed=2)
    return first


def test_handles_are_kept_and_bounded(made):
    session = AgentSession(table_limit=2)
    a = session.data("one")
    assert session.data("one").table._provider is a.table._provider
    session.data("two")
    session.data("three")
    assert session.held() == ["two", "three"]
    assert data_model._loaded_source is None


def test_a_changed_file_drops_the_held_copy(made):
    session = AgentSession()
    first = session.data("one").table._provider
    path = made["csv_path"]
    text = open(path).read().replace("CellID,", "CellID,", 1)
    time.sleep(0.01)
    with open(path, "w") as handle:
        handle.write(text + "999,1,1,1,1\n")
    stat = os.stat(path)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
    second = session.data("one").table._provider
    assert second is not first
    assert session.data("one").table.geometry().height == 65


def test_describe_is_computed_once(made, monkeypatch):
    session = AgentSession()
    data = session.data("one")
    calls = []
    inner = data.table._provider._provider
    original = inner.describe
    monkeypatch.setattr(inner, "describe", lambda: calls.append(1) or original())
    data.table.describe()
    session.data("one").table.describe()
    assert len(calls) == 1


def test_unknown_project_is_structured(made):
    with pytest.raises(AgentError) as caught:
        AgentSession().data("One")
    assert caught.value.code == "unknown_project"
    assert caught.value.detail["did_you_mean"] == ["one"]


def test_a_node_that_is_gone_is_a_structured_problem(made, tmp_path):
    record = Project.load("one")
    record = record.with_resource("table", ResourceBinding(
        kind="table", provider="node", node="ghost", resource_id="r1"))
    config = json.loads((tmp_path / "config.json").read_text())
    config["one"] = record.to_entry()
    (tmp_path / "config.json").write_text(json.dumps(config))
    with pytest.raises(AgentError) as caught:
        AgentSession().data("one")
    assert caught.value.code == "resource_unavailable"
    assert caught.value.retryable is True


def test_image_only_handles_refuse_table_reads(made):
    data = AgentSession().image_data("one")
    assert data.image.size == (512, 512)
    with pytest.raises(AgentError):
        data.table.describe()
