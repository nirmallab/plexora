"""Agent capabilities and the Python API, as telemetry sees them: names,
outcomes and timings -- never arguments."""

import json

import pytest
from pydantic import BaseModel

from plexora.agent import registry
from plexora.agent.errors import AgentError
from plexora.agent.registry import Capability
from plexora.agent.schemas import AgentModel
from plexora.telemetry import agent_hooks


class Free(AgentModel):
    text: str = ""


@pytest.fixture(autouse=True)
def clean_registry():
    registry._reset_for_tests()
    agent_hooks._reset_for_tests()
    yield
    registry._reset_for_tests()
    agent_hooks._reset_for_tests()


def _cap(name, owner="core", handler=None, **kwargs):
    return Capability(name=name, owner=owner, purpose="p", permission="read",
                      input_model=Free, handler=handler or (lambda call, inp: {"ok": 1}),
                      **kwargs)


def rows(t, event):
    t.sync()
    return [{"key": r[2], "dims": json.loads(r[3]), "n": r[5]}
            for r in t.queue.snapshot()[0] if r[1] == event]


def test_invoke_is_counted_without_its_arguments(telemetry_enabled):
    t, _fake = telemetry_enabled
    t.start(None, "terminal", upload=False)
    registry.register(_cap("gating.auto", owner="gating"))

    def fail(call, inp):
        raise AgentError("precondition_missing", "patient_29384 needs a mask at /Users/alice")

    registry.register(_cap("roi.create", owner="roi", handler=fail))
    secret = {"text": "patient_29384 /Users/alice/secret.csv CD45"}
    assert registry.invoke(object(), "gating.auto", secret)["ok"]
    assert not registry.invoke(object(), "roi.create", secret)["ok"]
    found = rows(t, "capability.summary")
    outcomes = {(r["dims"]["name"], r["dims"]["outcome"]) for r in found if r["key"] == "n"}
    assert outcomes == {("gating.auto", "ok"), ("roi.create", "precondition_missing")}
    text = json.dumps(found)
    for leak in ("patient", "alice", "CD45", "secret"):
        assert leak not in text


def test_third_party_capabilities_are_hashed(telemetry_enabled):
    t, _fake = telemetry_enabled
    t.start(None, "terminal", upload=False)
    registry.register(_cap("acme.private_thing", owner="acme_lab_plugin"))
    registry.invoke(object(), "acme.private_thing", {})
    (row,) = [r for r in rows(t, "capability.summary") if r["key"] == "n"]
    assert row["dims"]["name"].startswith("ext:") and row["dims"]["owner"].startswith("ext:")
    assert "acme" not in json.dumps(row)


def test_nested_calls_and_transitions(telemetry_enabled):
    t, _fake = telemetry_enabled
    t.start(None, "terminal", upload=False)

    def outer(call, inp):
        return registry.invoke(call.session, "project.inspect", {})

    registry.register(_cap("project.inspect"))
    registry.register(_cap("project.scene", handler=outer))
    session = object()
    registry.invoke(session, "project.inspect", {})
    registry.invoke(session, "project.scene", {})
    found = rows(t, "capability.summary")
    nested = [r for r in found if r["key"] == "n" and r["dims"]["transport"] == "nested"]
    assert [r["dims"]["name"] for r in nested] == ["project.inspect"]
    transitions = [r["dims"] for r in rows(t, "capability.transition")]
    assert transitions == [{"from": "project.inspect", "to": "project.scene"}]


def test_mcp_transport_is_recorded(telemetry_enabled):
    t, _fake = telemetry_enabled
    t.start(None, "terminal", upload=False)
    registry.register(_cap("project.inspect"))
    token = t.call_source.set("http")
    try:
        registry.invoke(object(), "project.inspect", {})
    finally:
        t.call_source.reset(token)
    (row,) = [r for r in rows(t, "capability.summary") if r["key"] == "n"]
    assert row["dims"]["transport"] == "web"


def test_off_changes_nothing():
    registry.register(_cap("project.inspect"))
    assert registry.invoke(object(), "project.inspect", {})["ok"]


def test_python_api_is_counted_once_resolved(telemetry_enabled, monkeypatch):
    import plexora

    t, _fake = telemetry_enabled
    t.start(None, "terminal", upload=False)
    monkeypatch.delitem(plexora.__dict__, "list_datasets", raising=False)
    function = plexora.list_datasets
    assert function.__wrapped__.__name__ == "list_datasets"
    function()
    found = rows(t, "function.summary")
    assert {"key": "n", "dims": {"fn": "list_datasets", "source": "python"}, "n": 1} in found
