"""Enforcement at the one place every agent action passes: registry.invoke.

Free is the default here, as for a real install -- these tests are where the
refusals are asserted. The suites that exercise Paid capabilities in depth
run with `pytest.mark.paid`.
"""

import json
import threading
import time

import pytest
from pydantic import BaseModel

from plexora import licensing
from plexora.agent import AgentSession, invoke, registry
from plexora.agent.errors import CODES
from plexora.agent.registry import Capability
from plexora.licensing import manifest, state
from tests.agent_fixtures import make_synthetic_project

#: The classification, pinned: changing what is Paid is a product decision,
#: and this is where it has to be made on purpose.
PAID = {
    "gating.session_start": "ai:gating:session",
    "gating.session_bulk": "ai:gating:session",
    "gating.next": "ai:gating:session",
    "gating.answer": "ai:gating:session",
    "gating.session_status": "ai:gating:session",
    "gating.session_finish": "ai:gating:session",
    "gating.report": "ai:gating:session",
    "gating.get_panel_context": "ai:gating:session",
    "gating.set_panel_context": "ai:gating:session",
    "gating.sample_validation_regions": "ai:gating:session",
    "gating.render_validation": "ai:gating:session",
    "operation.report": "ai:gating:session",
    "gating.compare_images": "ai:gating:analytics",
    "gating.profile_marker": "ai:gating:analytics",
    "gating.calibrate_display": "ai:gating:analytics",
    "gating.sample_cells": "ai:gating:analytics",
    "gating.render_collage": "ai:gating:analytics",
    "gating.bivariate": "ai:gating:analytics",
    "gating.qc": "ai:gating:analytics",
    "viewer.show_evidence": "ai:evidence",
    "qc.session_start": "ai:qc:session",
    "qc.session_bulk": "ai:qc:session",
    "qc.next": "ai:qc:session",
    "qc.answer": "ai:qc:session",
    "qc.session_status": "ai:qc:session",
    "qc.session_finish": "ai:qc:session",
    "qc.report": "ai:qc:session",
    "qc.profile_image": "ai:qc:analytics",
    "qc.render_overview": "ai:qc:analytics",
}

#: Manual-equivalent gating that must stay Free (a user decision, among others).
MUST_BE_FREE = ("gating.set", "gating.get", "gating.get_all", "gating.distribution",
                "gating.auto", "gating.adjust", "gating.apply_to_dataset", "gating.reset",
                "gating.restore", "gating.export", "gating.write_source", "gating.set_status",
                "gating.provenance", "gating.summary", "viewer.preview_gate",
                "viewer.highlight_cells", "viewer.capture", "viewer.show_shapes",
                # Manual QC, and everything that reads or changes what QC made.
                "qc.get_results", "qc.list_results", "qc.activate_result",
                "qc.set_strictness", "qc.approve_roi", "qc.refresh", "qc.set_cycles",
                "qc.export", "qc.write_source", "qc.reset", "qc.restore",
                "roi.create", "roi.update", "roi.delete", "roi.list")


@pytest.fixture
def session(tmp_path):
    make_synthetic_project(tmp_path)
    registry.discover(["gating", "roi", "qc"])
    return AgentSession()


def _audit(tmp_path):
    path = tmp_path / ".agent" / "audit.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


# -- the classification -------------------------------------------------------------

def test_every_capability_names_a_known_entitlement(session):
    for cap in registry.all_capabilities():
        if cap.entitlement not in (None, "free"):
            assert manifest.known(cap.entitlement), (cap.name, cap.entitlement)


def test_the_paid_set_is_exactly_the_classification(session):
    entitled = {cap.name: cap.entitlement for cap in registry.all_capabilities()
                if cap.entitlement not in (None, "free")}
    assert entitled == PAID


def test_manual_gating_stays_free(session):
    names = {cap.name: cap for cap in registry.all_capabilities()}
    for name in MUST_BE_FREE:
        assert names[name].entitlement in (None, "free"), name


def test_paid_covers_everything_it_should():
    grants = manifest.PLAN_ENTITLEMENTS["paid"]
    for entitlement in set(PAID.values()):
        assert any(entitlement == g or entitlement.startswith(g + ":") for g in grants)


def test_describe_and_the_mcp_description_say_paid(session):
    from plexora.mcp import tools

    described = {entry["name"]: entry for entry in registry.describe()}
    assert described["gating.session_start"]["plan"] == "paid"
    assert described["gating.set"]["plan"] == "free"
    assert "Part of Plexora Paid" in tools.description_for(registry.get("gating.session_start"))
    assert "Paid" not in tools.description_for(registry.get("gating.set"))


def test_license_required_is_a_known_code_everywhere():
    from plexora.telemetry.schema import AGENT_OUTCOMES

    assert "license_required" in CODES
    assert "license_required" in AGENT_OUTCOMES


# -- refusals on Free -----------------------------------------------------------------

def test_a_paid_capability_is_refused_on_free_with_a_structured_error(session, tmp_path):
    result = invoke(session, "gating_session_start", {"project": "synth"})
    assert result["ok"] is False
    error = result["error"]
    assert error["code"] == "license_required"
    assert error["retryable"] is False
    detail = error["detail"]
    assert detail["entitlement"] == "ai:gating:session"
    assert detail["plan_required"] == "paid"
    assert detail["state"] == "free" and detail["plan"] == "free"
    assert "Settings > License" in detail["hint"]
    assert _audit(tmp_path)[-1]["status"] == "refused"


def test_a_refused_job_never_starts(session):
    """compare_gates_across_images is a job: refused at submit, nothing queued."""
    result = invoke(session, "compare_gates_across_images", {"dataset": "anything"})
    assert result["error"]["code"] == "license_required"
    assert invoke(session, "job_list", {})["result"]["jobs"] == []


def test_the_same_call_passes_on_paid(session, paid_license):
    result = invoke(session, "gating_session_status", {})
    assert result["ok"], result


def test_a_narrow_licence_unlocks_only_its_branch(session, license_issuer):
    license_issuer.install(license_issuer.issue(entitlements=["ai:evidence"]))
    assert invoke(session, "gating_session_status", {})["error"]["code"] == "license_required"


def test_an_expired_licence_says_so_and_keeps_free_working(session, license_issuer, monkeypatch):
    now = int(time.time())
    license_issuer.install(license_issuer.issue(issued_at=now - 100 * 86400, expires_at=now - 30 * 86400,
                                                grace_days=14))
    refused = invoke(session, "gating_session_status", {})["error"]
    assert refused["detail"]["state"] == "expired"
    assert "still here and still editable" in refused["detail"]["hint"]
    # Manual gating on the same expired licence: untouched.
    assert invoke(session, "set_gate", {"project": "synth", "marker": "CD8", "low": 900})["ok"]
    assert invoke(session, "get_gate_distribution", {"project": "synth", "marker": "CD8"})["ok"]


def test_manual_gating_works_on_free(session):
    assert invoke(session, "set_gate", {"project": "synth", "marker": "CD8", "low": 900})["ok"]
    assert invoke(session, "suggest_auto_gate", {"project": "synth", "marker": "CD8"})["ok"]
    assert invoke(session, "adjust_gate", {"project": "synth", "marker": "CD8", "direction": "up",
                                           "magnitude": "small"})["ok"]
    assert invoke(session, "get_all_gates", {"project": "synth"})["ok"]
    assert invoke(session, "get_gate_provenance", {"project": "synth"})["ok"]


def test_the_free_path_never_reads_the_licence(session, monkeypatch):
    """With licensing deliberately broken, Free capabilities do not notice."""
    def broken(*args, **kwargs):
        raise AssertionError("a Free capability looked at the licence")

    monkeypatch.setattr(state, "current", broken)
    assert invoke(session, "set_gate", {"project": "synth", "marker": "CD8", "low": 900})["ok"]
    assert invoke(session, "list_projects", {})["ok"]


def test_permission_errors_outrank_licence_errors(session):
    """A refusal the user fixes in the policy is reported before one they fix
    with a licence: both are real, and the cheaper fix comes first."""
    from plexora.agent import Policy

    read_only = Policy(allow_writes=False)
    result = invoke(session, "gating_session_start", {"project": "synth"}, policy=read_only)
    assert result["error"]["code"] == "permission_required"


def test_a_nested_paid_call_is_refused_too(session):
    """A Free capability cannot launder a Paid one by calling it."""
    class Empty(BaseModel):
        pass

    def launder(call, inp):
        inner = invoke(call.session, "gating_session_status", {})
        return {"inner": inner["ok"], "code": (inner.get("error") or {}).get("code")}

    registry.register(Capability(name="test.launder", owner="core", purpose="test",
                                 permission="read", input_model=Empty, handler=launder))
    result = invoke(session, "test.launder", {})
    assert result["ok"] and result["result"] == {"inner": False, "code": "license_required"}


def test_a_job_that_started_on_a_valid_licence_finishes_after_it_lapses(session, paid_license):
    """Checked at submit, not mid-run: a running job is never cut off, and what
    it produced stays readable."""
    from plexora.agent import jobs

    class Empty(BaseModel):
        pass

    release = threading.Event()

    def slow(call, inp):
        release.wait(10)
        return {"done": True}

    registry.register(Capability(name="test.slow_paid", owner="core", purpose="test",
                                 permission="read", input_model=Empty, handler=slow,
                                 execution="job", entitlement="ai:gating:session"))
    submitted = invoke(session, "test.slow_paid", {})
    assert submitted["ok"], submitted
    job_id = submitted["result"]["job_id"]
    from plexora.licensing import store

    store.clear_license()
    licensing.reset_for_tests()
    assert not licensing.allows("ai:gating:session")
    release.set()
    jobs.drain(10)
    status = invoke(session, "job_get", {"job_id": job_id})
    assert status["ok"], status
    job = status["result"]["job"]
    assert job["status"] not in ("failed", "cancelled", "queued", "running"), job
    assert job["result"] == {"done": True}, job


def test_a_malformed_entitlement_is_refused_at_registration():
    class Empty(BaseModel):
        pass

    with pytest.raises(ValueError):
        Capability(name="test.bad", owner="core", purpose="x", permission="read",
                   input_model=Empty, handler=lambda c, i: None, entitlement="AI Gating")


# -- scope, server info, resources -----------------------------------------------------

def test_scope_says_license_required_on_free(session):
    from plexora.agent.policy import classify_scope

    answer = classify_scope(session, ["gating_session_start"], project="synth")
    assert answer["state"] == "can_recommend"
    assert answer["license_required"] == ["gating_session_start"]
    free = classify_scope(session, ["set_gate"], project="synth")
    assert "license_required" not in free


def test_server_info_names_the_plan(session, paid_license):
    from plexora.mcp.server import _license_info

    assert _license_info() == {"plan": "paid", "state": "paid_active", "entitlements": ["ai"]}


def test_the_packet_resource_is_guarded_on_free():
    from plexora.licensing import guards
    from plexora.agent.errors import AgentError

    with pytest.raises(AgentError) as info:
        guards.check("ai:gating:session", what="gating-packet")
    assert info.value.code == "license_required"
    guards.check(None)
    guards.check("free")


# -- plugin-level entitlement --------------------------------------------------------------

def test_a_plugin_entitlement_is_the_default_and_capability_level_wins(monkeypatch):
    class Empty(BaseModel):
        pass

    def handler(call, inp):
        return {"ran": True}

    class FakePlugin:
        name = "paidplug"
        entitlement = "plugin:paidplug"

        def load_capabilities(self):
            return [
                Capability(name="paidplug.analyze", owner="paidplug", purpose="x", permission="read",
                           input_model=Empty, handler=handler),
                Capability(name="paidplug.view", owner="paidplug", purpose="x", permission="read",
                           input_model=Empty, handler=handler, entitlement="free"),
            ]

    from plexora.server import plugins as plugin_registry

    monkeypatch.setattr(plugin_registry, "available_names", lambda: ["paidplug"])
    registry.discover(["paidplug"], loader=lambda name: FakePlugin())
    assert registry.get("paidplug.analyze").entitlement == "plugin:paidplug"
    assert registry.get("paidplug.view").entitlement == "free"
    sess = AgentSession()
    assert invoke(sess, "paidplug.analyze", {})["error"]["code"] == "license_required"
    assert invoke(sess, "paidplug.view", {})["ok"]
