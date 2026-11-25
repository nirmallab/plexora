"""What the in-app harness no longer pays twice for (docs/internal/AUTOQC_PARALLEL.md,
"Tokens paid twice").

- The cached prefix: the qc-image skill cut to what judges a packet, and
  neither guide carries `answer_schemas` (every call has its own schema) or
  `answer_with` (the harness calls the tool). On Anthropic each answer schema
  is a cache entry of its own ahead of the system prompt, so the prefix is
  written once per schema a run uses: halving it halves each of those writes.
- Independent packets (a blind stain reading, a gating look) get a worker of
  their own and are never written to the cache; the task's worker waits
  parked for its next packet.
- A keep-warm ping after such a packet is the prefix alone.
- A serial gating run's context sheets are whole (a trimmed sheet pointed at
  rows another worker was shown).
- QC's visual pass asks a worker with no tools to list, not to draw.
- QC's confirm sees the class the audit named; a deeper look drops the
  neighbourhood look 1 drew.
- Gating's `qc_confirm` read real by its first reader on a stain QC read real
  is not read twice more.
"""

import json
import time

import pytest
from spatialagents.wire import ModelRequest, ModelResponse, Usage, text_block

from plexora.ai import skills
from plexora.ai.harness import cache_plan, prefix, schema
from plexora.ai.harness.decision import (GATING, KEEP_WARM_TEXT, OWN_SCHEMA, QC, GatingOptions, GatingRun,
                                         QCOptions, QCRun)
from plexora.ai.harness.gateway import GatewayClient, TokenSource
from plexora.ai.harness.trace import TraceStore


def _gateway():
    return GatewayClient("http://127.0.0.1:9", tokens=TokenSource("BIOCAI1.test"), sleep=lambda s: None)


def _gating_run(tmp_path, **options):
    return GatingRun(GatingOptions(project="gsynth", **options), gateway=_gateway(),
                     trace=TraceStore(tmp_path / "t.sqlite"))


def _qc_run(tmp_path, **options):
    return QCRun(QCOptions(project="qcsynth", **options), gateway=_gateway(),
                 trace=TraceStore(tmp_path / "t.sqlite"))


# -- the prefix ----------------------------------------------------------------------------


def test_the_qc_prefix_carries_the_skills_judgement_not_its_tool_steps():
    full = skills.read_skill(prefix.QC_SKILL)
    digest = prefix.skill_digest(full)
    assert digest is not None, "the qc-image skill lost a landmark skill_digest cuts on"
    sent = prefix.qc_prefix()[1]["text"]
    assert digest in sent and len(digest) < 0.6 * len(full)
    # Every packet kind a worker answers is explained ...
    for kind in ("channel_audit", "score_review", "artifact_confirm", "artifact_scope",
                 "artifact_localize", "artifact_grid", "final_qc_review"):
        assert f"- `{kind}`:" in digest, kind
    assert "## Uncertainty" in digest and "## Failure modes" in digest
    # ... and the external agent's steps are not.
    for gone in ("## Tools", "## Mutation policy", "qc_session_start", "qc_session_finish",
                 "export_qc", "- `visual_scan`:"):
        assert gone not in digest, gone


def test_neither_guide_in_a_prefix_repeats_the_answer_schemas():
    for build in (prefix.qc_prefix, prefix.gating_prefix):
        guide_text = build()[-1]["text"]
        guide = json.loads(guide_text.split("\n", 1)[1])
        assert not set(prefix.HARNESS_DROPS) & set(guide), build.__name__
    # The prefix is the same bytes every time, and much smaller than it was:
    # QC 13.9k and gating 7.6k tokens before (2026-11-24).
    assert prefix.qc_prefix() == prefix.qc_prefix()
    assert cache_plan.expected_tokens(prefix.qc_prefix()) < 8_000
    assert cache_plan.expected_tokens(prefix.gating_prefix()) < 5_000


def test_the_schema_sent_carries_every_fields_words_but_no_developers_docstring():
    for label, sent in schema.sendable():
        assert "description" not in sent, label
    stain = schema.for_kind("stain_check", "qc")
    assert "credits" not in json.dumps(stain)
    assert stain["properties"]["verdict"]["description"].startswith("real:")


def test_a_packet_pointing_at_the_guides_schemas_is_pointed_at_its_own(tmp_path, monkeypatch):
    run = _qc_run(tmp_path)
    packet = {"kind": "channel_audit", "packet_id": "pk1",
              "answer_schema": {"see": "reading_guide.answer_schemas.channel_audit"},
              "allowed": {"see": "reading_guide.answer_schemas.channel_audit"}, "evidence": {}}
    text = run._content(dict(packet), [])[-1]["text"]
    shown = json.loads(text)
    assert shown["allowed"] == OWN_SCHEMA and "answer_schema" not in shown
    # A schema the provider refused is asked in words -- the schema itself,
    # not a pointer into a guide the prefix no longer holds.
    monkeypatch.setattr(schema, "_REFUSED", {schema._fingerprint(schema.for_kind("channel_audit", "qc"))})
    text = run._content(dict(packet), [])[-1]["text"]
    assert schema.IN_WORDS in text
    said = json.loads(text.split(schema.IN_WORDS, 1)[1])
    assert said == schema.for_kind("channel_audit", "qc")


# -- workers ------------------------------------------------------------------------------


def test_an_independent_packet_gets_a_worker_of_its_own_and_is_never_cached(tmp_path):
    run = _qc_run(tmp_path)
    lane = run.lanes[0]
    review = {"kind": "score_review", "units": [{"type": "check", "id": "blur:DNA1"}]}
    run._rotate_if_due(review, lane)
    task_worker = lane.worker
    task_worker.task, task_worker.packets, task_worker.units = run.task_of(review), 1, {"check:blur:DNA1"}
    assert not run._last_in_worker(task_worker)
    stain = {"kind": "stain_check", "units": [{"type": "stain", "id": "CD3"}]}
    run._rotate_if_due(stain, lane)
    first_stain = lane.worker
    assert first_stain is not task_worker and first_stain.solo
    assert run._last_in_worker(first_stain)          # its turn is never written to the cache
    first_stain.packets = 1
    run._rotate_if_due({"kind": "stain_check", "units": [{"type": "stain", "id": "CD8"}]}, lane)
    assert lane.worker is not first_stain and lane.worker.solo   # the next marker: another fresh one
    lane.worker.packets = 1
    run._rotate_if_due(review, lane)
    assert lane.worker is task_worker                # the task's worker goes on where it stopped
    assert QC.solo_kinds >= {"stain_check"} and GATING.solo_kinds >= {"cell_panel", "qc_confirm"}


def test_a_gating_look_is_a_worker_of_its_own(tmp_path):
    run = _gating_run(tmp_path)
    lane = run.lanes[0]
    seen = []                                        # the workers themselves: an id is reused once one is freed
    for marker in ("CD3", "CD3", "CD8"):
        run._rotate_if_due({"kind": "cell_panel", "units": [{"marker": marker}]}, lane)
        assert lane.worker.solo and all(lane.worker is not w for w in seen)
        assert run._last_in_worker(lane.worker)
        seen.append(lane.worker)
        lane.worker.packets = 1


class _PingGateway:
    def __init__(self, usage):
        self.usage, self.sent = usage, []

    def messages(self, request, *, idempotency_key, **_):
        self.sent.append(request)
        return ModelResponse(text="", stop_reason="max_tokens", usage=self.usage, charged_micro=40)


def test_after_a_retiring_packet_the_keep_warm_ping_is_the_prefix_alone(tmp_path):
    run = _gating_run(tmp_path)
    run.prefix_tokens = 6_000
    lane = run.lanes[0]
    run.gateway = _PingGateway(Usage(input_uncached=10, cache_read=6_000, output_tokens=1))
    history = [{"role": "user", "content": [text_block("x" * 40_000)]}]
    lane.last_request = ModelRequest(capability="vision_judgement", system=[text_block("prefix")],
                                     messages=history, max_tokens=4096,
                                     output_schema={"type": "object"}, context={"feature": "gating"},
                                     task="gating.image_inspection")
    lane.last_input_tokens, lane.last_final = 16_000, True
    lane.last_call_at = time.monotonic() - 250
    assert run._keep_warm(lane)
    ping = run.gateway.sent[-1]
    assert ping.messages == [{"role": "user", "content": [text_block(KEEP_WARM_TEXT)]}]
    assert ping.output_schema == {"type": "object"} and ping.system == lane.last_request.system
    # Another lane called the task since: its call kept the prefix warm.
    lane.last_call_at = time.monotonic() - 250
    run._task_called["gating.image_inspection"] = time.monotonic() - 10
    assert not run._keep_warm(lane)


# -- start arguments -----------------------------------------------------------------------


def test_the_harness_asks_for_whole_sheets_and_says_it_has_no_tools():
    gating = GATING.start_arguments(GatingOptions(project="p"))
    assert gating["sheets"] == "full" and gating["evidence"] == "full"
    assert QC.start_arguments(QCOptions(project="p"))["agent_tools"] is False


# -- gating's second reading ---------------------------------------------------------------


class _Engine:
    def __init__(self):
        self.options = {"check_rereads": "all"}
        self.logged = []

    def log(self, **event):
        self.logged.append(event)


@pytest.mark.parametrize("stain, first, settled", [
    ("real", "real_signal", "real_signal"),          # QC agrees: one reading stands
    ("real", "technical_failure", None),             # disagrees: read again
    ("weak", "real_signal", None),                   # QC unsure: read again
    (None, "real_signal", None),                     # no stain verdict: read again
])
def test_a_check_read_real_on_a_stain_qc_read_real_is_not_read_again(stain, first, settled):
    from plexora.plugins.gating.server.autogate import second_verdict

    unit = {"project": "p", "marker": "CD3"}
    if stain:
        unit["stain"] = {"verdict": stain, "contested": False}
    engine = _Engine()
    packet = {"kind": "qc_confirm", "packet_id": "pk1", "evidence": {}}
    assert second_verdict.settle(engine, unit, packet, first) == settled
    assert ("verdict_reads" in unit) == (settled is None)
    # A strip is read twice whatever the stain says.
    strip_unit = {"project": "p", "marker": "CD3", "stain": {"verdict": "real", "contested": False}}
    assert second_verdict.settle(engine, strip_unit, {"kind": "t1_strip", "packet_id": "pk2",
                                                      "evidence": {}}, "ok") is None


# -- QC sessions ---------------------------------------------------------------------------


@pytest.fixture
def _qc_plugins():
    from plexora.agent import registry

    registry.discover(["roi", "qc"])
    yield


NO_CHECKS = {"blur": False, "registration": False, "segmentation": False, "artifacts": False,
             "visual": False, "stain": False}


@pytest.mark.pro
def test_a_confirm_sees_the_audits_class_and_a_deeper_look_is_one_row(tmp_path, _qc_plugins):
    from plexora.agent import AgentSession, invoke
    from tests.qc_fixtures import make_qc_project
    from tests.test_qc_session import QCOracle, ok, start

    class Hinting(QCOracle):
        """Names a class on every suspicious audit row; asks every
        candidate's first look for more."""

        def answer(self, packet, session_id):
            said = super().answer(packet, session_id)
            if packet["kind"] == "channel_audit":
                for verdict in said["verdicts"].values():
                    if verdict["verdict"] == "suspicious":
                        verdict["class_hint"] = "tissue_fold"
            elif packet["kind"] == "artifact_confirm" and packet["evidence"].get("look") == 1:
                labels = packet["evidence"].get("labels")
                return ({"kind": "artifact_confirm", "verdicts": {
                    label: {"verdict": "need_more_evidence"} for label in labels}} if labels
                        else {"kind": "artifact_confirm", "verdict": "need_more_evidence"})
            return said

    info = make_qc_project(tmp_path, size=512, grid=20, artifacts=("fold",))
    session = AgentSession()
    oracle = Hinting(info)
    sid = start(session, checks=NO_CHECKS)["session_id"]
    result = ok(invoke(session, "qc_next", {"session_id": sid, "wait_s": 20}))
    confirms = []
    for _ in range(60):
        if result["state"] != "decision":
            break
        packet = result["packet"]
        if packet["kind"] == "artifact_confirm":
            confirms.append(packet)
        result = ok(invoke(session, "qc_answer", {"session_id": sid, "packet_id": packet["packet_id"],
                                                  "answer": oracle.answer(packet, sid)}))["next"]
    assert confirms, "no confirm packet came"
    first = confirms[0]["evidence"]
    briefs = first.get("candidates") or [first.get("candidate")]
    assert any(b.get("audit_said") == ["tissue_fold"] for b in briefs), briefs
    deeper = [p for p in confirms if (p["evidence"].get("look") or 1) > 1]
    assert deeper, [p["evidence"].get("look") for p in confirms]
    from plexora.agent import artifacts
    from PIL import Image
    import io

    for packet in deeper:
        png, sidecar = artifacts.get(packet["images"][0]["artifact_id"])
        panels = (sidecar.get("manifest") or sidecar)["panels"]
        assert not any("neighbourhood" in p["caption"] for p in panels), panels
        width, height = Image.open(io.BytesIO(png)).size
        assert width > 2 * height                    # one row, not a 2x2 sheet


@pytest.mark.pro
def test_a_visual_pass_with_no_tools_asks_for_places_to_list(tmp_path, _qc_plugins):
    from plexora.agent import AgentSession, invoke
    from tests.qc_fixtures import make_qc_project
    from tests.test_qc_session import QCOracle, ok, start

    info = make_qc_project(tmp_path, size=512, grid=20, artifacts=("fold",))
    session = AgentSession()
    oracle = QCOracle(info)
    from plexora.vision import sam, sam_backend

    checks = {**NO_CHECKS, "visual": True}
    with sam.use_backend(sam_backend.FakeSamBackend()):
        _visual_without_tools(session, oracle, start(session, checks=checks, agent_tools=False)["session_id"])


def _visual_without_tools(session, oracle, sid):
    from plexora.agent import invoke
    from tests.test_qc_session import ok

    result = ok(invoke(session, "qc_next", {"session_id": sid, "wait_s": 20}))
    for _ in range(60):
        assert result["state"] == "decision", result
        packet = result["packet"]
        if packet["kind"] == "visual_scan":
            break
        result = ok(invoke(session, "qc_answer", {"session_id": sid, "packet_id": packet["packet_id"],
                                                  "answer": oracle.answer(packet, sid)}))["next"]
    else:
        raise AssertionError("no visual_scan packet came")
    evidence = packet["evidence"]
    assert not {"tools", "how", "session_id", "max_regions"} & set(evidence)
    assert "no tools" in packet["question"] and "`left`" in packet["question"]
    done = ok(invoke(session, "qc_answer", {"session_id": sid, "packet_id": packet["packet_id"], "answer": {
        "kind": "visual_scan", "status": "done",
        "left": [{"where": "top left quadrant", "why": "a bright doubled band, likely a fold"}]}}))
    assert done["next"]["state"] in ("decision", "decided", "done")
