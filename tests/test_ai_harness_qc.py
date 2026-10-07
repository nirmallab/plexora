"""AutoQC answered by Plexora's own harness, end to end against a stand-in gateway.

The model is the scripted `QCOracle` from `test_qc_session.py`, which answers
each QC packet from the scene's painted truth; the gateway is `FakeGateway`.
What is pinned: a QC session runs and finishes with no external agent, its
regions written and its result active; one structured call per packet, the
gateway run declared with feature `qc` and one unit per channel; one stable
prefix (QC identity + skill + reading guide) the cache reads on every call
after the first; a credit refusal pauses the session and a resume finishes it
under the same gateway run; maps in answer schemas travel as key/value lists.
"""

import json

import pytest

from plexora.agent import AgentSession, invoke, registry
from plexora.ai import tasks
from plexora.ai.harness import cache_plan, prefix, schema
from plexora.ai.harness.decision import QCOptions, QCRun
from plexora.ai.harness.gateway import GatewayClient, TokenSource
from plexora.ai.harness.trace import TraceStore
from tests.ai_harness_fixtures import FakeGateway
from tests.qc_fixtures import make_qc_project
from tests.test_qc_session import QCOracle, classes_of, rois_of

ARTIFACTS = ("saturation", "fold")
#: The scan's map cell the QC session tests use: small enough to be quick.
FAST = {"map_cell_um": 25.0}


def client(gateway, **kw):
    return GatewayClient(gateway.url, tokens=TokenSource("BIOCAI1.test"), sleep=lambda s: None, **kw)


def qc_brain(info):
    oracle = QCOracle(info)

    def brain(packet, body):
        return oracle.answer(packet, body["context"]["session_id"])
    return brain


def options(**kw):
    return QCOptions(project="qcsynth", start_options=dict(FAST), **kw)


@pytest.fixture
def scene(tmp_path):
    registry.discover(["roi", "qc"])
    return make_qc_project(tmp_path, artifacts=ARTIFACTS)


def _packets(call):
    return [json.loads(m["content"][-1]["text"].split("\nANSWER SCHEMA:")[0])
            for m in call["body"]["request"]["messages"] if m["role"] == "user"
            and not m["content"][-1]["text"].startswith("That answer")]


# -- pieces that need no project ----------------------------------------------------------


def test_the_qc_prefix_is_byte_stable_and_carries_the_skill_and_guide():
    first, second = prefix.qc_prefix(), prefix.qc_prefix()
    assert first == second
    assert cache_plan.fingerprint(first) != cache_plan.fingerprint(prefix.gating_prefix())
    marks = [i for i, block in enumerate(first) if "cache_control" in block]
    assert marks == [len(first) - 1]
    assert first[1]["text"].startswith("SKILL (qc-image)")
    assert prefix.qc_guide_version() in first[-1]["text"]
    assert "{{" not in json.dumps(first)                     # placeholders filled
    assert cache_plan.expected_tokens(first) > 1024


def test_every_qc_answer_kind_has_a_provider_ready_schema_with_maps_as_entries():
    from plexora.plugins.qc.server import answers

    for kind in answers.BY_KIND:
        s = schema.for_kind(kind, "qc")
        assert s is not None, kind
        dumped = json.dumps(s)
        for banned in ('"$ref"', '"maxLength"', '"title"'):
            assert banned not in dumped, (kind, banned)

        def closed(node):
            if isinstance(node, dict):
                if node.get("type") == "object" or "properties" in node:
                    assert node.get("additionalProperties") is False, kind
                for v in node.values():
                    closed(v)
            elif isinstance(node, list):
                for v in node:
                    closed(v)
        closed(s)
    audit = schema.for_kind("channel_audit", "qc")["properties"]["verdicts"]
    assert audit["type"] == "array"
    assert audit["items"]["required"] == ["key", "value"]


def test_a_map_given_as_entries_decodes_to_the_answer_model():
    from pydantic import TypeAdapter

    from plexora.plugins.gating.server.autogate import answers as gating_answers
    from plexora.plugins.qc.server import answers

    given = {"kind": "artifact_confirm", "verdicts": [
        {"key": "c1", "value": {"verdict": "artifact", "artifact_class": "tissue_fold"}},
        {"key": "c2", "value": {"verdict": "not_artifact"}}]}
    decoded = schema.decode(given, schema.model_schema("artifact_confirm", "qc"))
    assert decoded["verdicts"] == {"c1": {"verdict": "artifact", "artifact_class": "tissue_fold"},
                                   "c2": {"verdict": "not_artifact"}}
    TypeAdapter(answers.Answer).validate_python(decoded)
    # An answer already in the model's shape is left as it is.
    plain = {"kind": "channel_audit", "verdicts": {"CD3": {"verdict": "clean"}}}
    assert schema.decode(plain, schema.model_schema("channel_audit", "qc")) == plain
    # Gating's per-marker maps were closed empty objects before: a provider
    # honouring the schema could only have answered `{}`.
    strip = schema.for_kind("t1_strip")["properties"]["verdicts"]
    assert strip["type"] == "array"
    gating = schema.decode({"kind": "t1_strip", "verdicts": [{"key": "CD3", "value": "ok"}]},
                           schema.model_schema("t1_strip"))
    assert gating["verdicts"] == {"CD3": "ok"}
    TypeAdapter(gating_answers.Answer).validate_python(gating)


# -- end to end ----------------------------------------------------------------------------


@pytest.mark.paid
def test_the_harness_runs_qc_on_an_image_with_no_external_agent(scene, tmp_path):
    with FakeGateway(qc_brain(scene)) as gateway:
        trace = TraceStore(tmp_path / "trace.sqlite")
        events = []
        summary = QCRun(options(), gateway=client(gateway), trace=trace, on_event=events.append).run()
    assert summary["status"] == "done", summary
    assert summary["workflow"] == "qc"

    # The results are written: regions of the painted classes, and the
    # session's result is the project's active QC.
    session = AgentSession()
    assert rois_of(session)
    assert "saturation_or_clipping" in classes_of(session)
    assert summary["result"]["active"]
    results = invoke(session, "get_qc_results", {"project": "qcsynth"})
    assert results["ok"] and results["result"]["regions"]
    status = invoke(session, "qc_session_status", {"session_id": summary["session_id"]})
    assert status["result"]["state"] == "done"

    kinds = [_packets(c)[-1]["kind"] for c in gateway.calls]
    assert kinds[0] == "channel_audit" and "artifact_confirm" in kinds
    # Each packet names its QC task; a score review by the check it reviews.
    for call in gateway.calls:
        packet = _packets(call)[-1]
        check = (packet.get("evidence") or {}).get("check")
        assert call["body"].get("task") == tasks.wire_id(tasks.task_for("qc", packet["kind"], check=check),
                                                          module="qc"), packet["kind"]
    assert gateway.calls[0]["body"]["task"] == "plexora.qc.planning"
    assert "plexora.qc.artifact_inspection" in {c["body"].get("task") for c in gateway.calls}

    # One structured call per packet, through a run declared for QC, one
    # unit per channel, never a model id.
    assert summary["model_calls"] == summary["packets"] == len(gateway.calls)
    assert summary["invalid_answers"] == 0
    run = gateway.runs["run_1"]
    assert run["feature"] == "qc" and run["units"] == len(scene["channels"])
    assert run["status"] == "finished"
    for call in gateway.calls:
        body = call["body"]
        assert "model" not in body
        assert body["context"]["feature"] == "qc" and body["context"]["run_id"] == "run_1"
        assert body["context"]["session_id"] == summary["session_id"]
        assert body["request"]["output_schema"]["additionalProperties"] is False
        last = body["request"]["messages"][-1]["content"][-1]["text"]
        assert "answer_with" not in last and "\"narration\"" not in last
    assert len({c["idempotency_key"] for c in gateway.calls}) == len(gateway.calls)

    # Images travel as the stored WebP, at most two per packet.
    images = [b for c in gateway.calls for b in c["body"]["request"]["messages"][-1]["content"]
              if b["type"] == "image"]
    assert images and all(b["source"]["media_type"] == "image/webp" for b in images)

    # One prefix, identical on every call; read from cache after the first.
    systems = {json.dumps(c["body"]["request"]["system"], sort_keys=True) for c in gateway.calls}
    assert len(systems) == 1
    assert summary["cache"]["verdicts"].get("miss", 0) == 0
    assert summary["cache"]["verdicts"]["cold"] == 1
    for call in gateway.calls[1:]:
        assert call["usage"]["cache_read"] >= cache_plan.expected_tokens(prefix.qc_prefix()) * 0.8
    report = trace.cache_report(summary["run_id"])
    assert report["calls"] == len(gateway.calls) and report["verdicts"].get("miss", 0) == 0
    assert len(report["prefixes"]) == 1

    # Workers roll: none carries more than its packet allowance.
    for call in gateway.calls:
        assert len(_packets(call)) <= 8
    names = [e["event"] for e in events]
    assert names[:2] == ["started", "quoted"] and names[-1] == "finished"
    answered = [e for e in events if e["event"] == "answered"]
    assert answered[-1]["usage"]["packets"] == summary["packets"]


@pytest.mark.paid
def test_running_out_of_credit_pauses_qc_and_a_resume_finishes_it(scene, tmp_path):
    with FakeGateway(qc_brain(scene)) as gateway:
        trace = TraceStore(tmp_path / "trace.sqlite")
        original = gateway._messages

        def broke_after_two(handler, body):
            if len(gateway.calls) >= 2 and not getattr(gateway, "topped_up", False):
                return handler._json(402, {"error": {"code": "insufficient_credits", "message": "top up",
                                                     "details": {"top_up_url": "https://example.test/top"}}})
            return original(handler, body)
        gateway._messages = broke_after_two
        events = []
        first = QCRun(options(), gateway=client(gateway), trace=trace, on_event=events.append).run()
        assert first["status"] == "paused" and first["reason"] == "insufficient_credits"
        paused = [e for e in events if e["event"] == "paused"][0]
        assert paused["top_up_url"] == "https://example.test/top"
        held = invoke(AgentSession(), "qc_next", {"session_id": first["session_id"], "wait_s": 0})
        assert held["result"]["state"] == "paused"
        assert gateway.runs["run_1"]["status"] == "open"            # kept for the resume

        gateway.topped_up = True
        second = QCRun(options(resume_session=first["session_id"]), gateway=client(gateway),
                       trace=trace).run()
    assert second["status"] == "done", second
    # The resume lifted the pause itself and billed under the same run.
    assert len(gateway.runs) == 1 and gateway.runs["run_1"]["status"] == "finished"
    resumed = [c for c in gateway.calls if c["packet_id"]][2:]
    assert resumed and all(c["body"]["context"]["run_id"] == "run_1" for c in resumed)
    assert rois_of(AgentSession())


@pytest.mark.paid
def test_plexora_ai_run_qc_runs_from_the_command_line(scene, monkeypatch, capsys):
    from plexora import cli

    with FakeGateway(qc_brain(scene)) as gateway:
        monkeypatch.setenv("BIOCOGNIA_AI_GATEWAY", gateway.url)
        monkeypatch.setenv("BIOCOGNIA_AI_TOKEN", "BIOCAI1.test")
        code = cli.main(["ai", "run", "qc", "qcsynth", "--channels", ",".join(scene["channels"][:3]),
                         "--json"])
    out = capsys.readouterr().out
    summary = json.loads(out[out.index("{"):])
    assert code == 0 and summary["status"] == "done", summary
    assert summary["workflow"] == "qc" and gateway.runs["run_1"]["units"] == 3
