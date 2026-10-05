"""The Auto QC rework (2026-10): every applicable check runs and every check
not run is said so; one ROI per category and action; an exclusion needs a
measured signal; a wrong answer cannot hold a packet; QC packets fit."""

import json
from types import SimpleNamespace

import numpy as np

import pytest

from plexora.plugins.qc import capabilities_session as cs
from plexora.plugins.qc.server import consolidate, packets, schemas, strictness


def _record(segmentation=True):
    return SimpleNamespace(segmentation=SimpleNamespace(available=segmentation))


@pytest.fixture
def no_reference(monkeypatch):
    monkeypatch.setattr(cs, "registration_reference",
                        lambda project, record, names, nuclear: nuclear[0])


# -- planning -------------------------------------------------------------------------


def test_blur_falls_back_to_every_channel_when_no_dna_is_recognisable(no_reference):
    notes = []
    names = ["ch01", "ch02", "ch03"]
    units = cs.plan_checks(None, _record(), "p", names, cs.QCChecks(), notes=notes)
    assert sorted(u["channel"] for u in units if u["check"] == "blur") == names
    assert {n["check"]: n["status"] for n in notes} == {"blur": "widened",
                                                        "registration": "not_run"}


def test_registration_compares_every_dna_channel_without_a_cap(no_reference):
    names = [f"DNA_{i}" for i in range(1, 13)] + ["CD3"]
    notes = []
    units = cs.plan_checks(None, _record(), "p", names, cs.QCChecks(), notes=notes)
    pairs = [u for u in units if u["check"] == "registration"]
    assert len(pairs) == 11 and {u["reference"] for u in pairs} == {"DNA_1"}
    assert not [n for n in notes if n["check"] == "registration"]


def test_one_dna_channel_skips_registration_and_says_why(no_reference):
    notes = []
    units = cs.plan_checks(None, _record(), "p", ["DNA_1", "CD3"], cs.QCChecks(), notes=notes)
    assert not [u for u in units if u["check"] == "registration"]
    note = next(n for n in notes if n["check"] == "registration")
    assert note["status"] == "not_run" and "only one DNA channel" in note["reason"]


def test_the_artifact_detector_runs_by_default(no_reference, monkeypatch):
    monkeypatch.delenv(cs.CHECKS_ENV, raising=False)
    units = cs.plan_checks(None, _record(), "p", ["DNA_1", "DNA_2"], cs.QCChecks(), notes=[])
    assert {u["check"] for u in units} == {"blur", "registration", "segmentation", "artifacts"}


def test_no_mask_says_segmentation_was_not_run(no_reference):
    notes = []
    cs.plan_checks(None, _record(segmentation=False), "p", ["DNA_1"], cs.QCChecks(),
                   notes=notes)
    assert any(n["check"] == "segmentation" and n["status"] == "not_run" for n in notes)


# -- strictness -------------------------------------------------------------------------


def _table():
    return strictness.thresholds("standard", None)


SURE = {"artifact_class": "tissue_fold", "severity": "severe", "confidence": "sure"}


def test_an_exclusion_nothing_measured_is_capped_at_warn():
    decided = strictness.decide_artifact(SURE, {"tissue_fraction": 0.02, "support": None},
                                         _table())
    assert decided["action"] == "warn" and "measured" in decided["reason"]


def test_an_exclusion_a_detector_measured_stands():
    decided = strictness.decide_artifact(SURE, {"tissue_fraction": 0.02,
                                                "support": "detector"}, _table())
    assert decided["action"] == "exclude"


def test_a_record_from_before_support_is_unchanged():
    assert strictness.decide_artifact(SURE, {"tissue_fraction": 0.02},
                                      _table())["action"] == "exclude"


@pytest.mark.parametrize("unit,expected", [
    ({"origin": "check"}, "check"),
    ({"detector": "audit", "refinement": {"status": "refined"}}, "traced"),
    ({"detector": "focus"}, "detector"),
    ({"detector": "audit", "refinement": {"status": "fallback"}}, None),
])
def test_measured_support(unit, expected):
    assert strictness.measured_support(unit) == expected


# -- consolidation ----------------------------------------------------------------------


def _finding(cid, klass, channels, state, box):
    x0, y0, x1, y1 = box
    return {"id": cid, "class": klass, "decision": {"artifact_class": klass},
            "channels": channels, "state": state,
            "geometry": {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1],
                                                             [x0, y1], [x0, y0]]]}}


def test_findings_of_one_category_and_action_become_one_roi():
    findings = [
        _finding("a", "out_of_focus", ["CD3"], "confirmed_exclude", (0, 0, 100, 100)),
        _finding("b", "out_of_focus", ["CD8"], "confirmed_exclude", (500, 500, 600, 600)),
        _finding("c", "tissue_fold", ["CD3", "CD8"], "confirmed_exclude", (900, 0, 1000, 100)),
        _finding("d", "out_of_focus", ["CD20"], "confirmed_warn", (0, 900, 100, 1000)),
    ]
    pieces = consolidate.plan(findings)
    keys = {key: sorted(u["id"] for u in units) for key, _piece, units in pieces}
    assert keys == {("blur_focus", "exclude"): ["a", "b"],
                    ("tissue_acquisition", "exclude"): ["c"],
                    ("blur_focus", "warn"): ["d"]}


def test_a_manual_review_region_keeps_its_ignore_action():
    unit = {"state": "manual_review_recommended", "action": "ignore"}
    assert consolidate._action(unit) == "ignore"
    assert consolidate._category({**unit, "class": "tissue_fold"}) == "review"


def test_a_layer_is_named_by_its_category_and_action():
    name = schemas.layer_name("exclude", "blur_focus", [{}, {}, {}])
    assert name.endswith("· 3 regions")
    assert schemas.action_of_name(name) == "exclude"


# -- packets ----------------------------------------------------------------------------


def test_an_oversized_final_review_drops_its_smallest_regions():
    from plexora.agent.sessions import budget

    regions = [{"label": f"r{i}", "class": "tissue_fold", "tissue_fraction": 1.0 / (i + 1),
                "neighbours": ["x" * 50] * 5, "padding": "y" * 400} for i in range(400)]
    packet = {"kind": "final_qc_review", "evidence": {"regions": regions}}
    assert len(json.dumps(packet)) > budget.PACKET_CHAR_LIMIT
    packets.trim(packet)
    assert len(json.dumps(packet)) <= budget.PACKET_CHAR_LIMIT
    kept = packet["evidence"]["regions"]
    assert kept and kept[0]["label"] == "r0" and packet["evidence"]["truncated"]
    assert "neighbours" not in kept[0]


def test_the_class_list_is_read_once_with_the_guide():
    packet = {"kind": "artifact_confirm", "evidence": {}, "images": [],
              "allowed": list(schemas.AGENT_CLASSES)}
    packets.lean(packet, {"reading": "once"})
    assert packet["allowed"] == {"see": "reading_guide.answer_schemas.artifact_confirm"}


# -- the harness ------------------------------------------------------------------------


def test_a_refusal_says_what_the_packet_allows():
    from plexora.ai.harness.decision import _refusal

    said = _refusal({"ok": False, "error": {"code": "invalid_input", "message": "bad label",
                                            "detail": {"allowed": ["c1", "c2"],
                                                       "schema": {"big": 1}}}})
    assert "bad label" in said and "c1" in said and "schema" not in said
    assert _refusal({"ok": False, "error": {"code": "conflict"}}) is None
    assert _refusal({"ok": True, "result": {}}) is None


@pytest.mark.paid
def test_a_second_unusable_answer_releases_the_packet(tmp_path):
    """A well-formed answer naming rows the packet does not hold is a strike:
    two of them send the channels to manual review, and the same packet is
    never served a third time."""
    from plexora.agent import AgentSession, invoke, jobs, registry
    from tests.qc_fixtures import make_qc_project

    registry.discover(["roi", "qc"])
    make_qc_project(tmp_path)
    session = AgentSession()
    started = invoke(session, "qc_session_start", {"project": "qcsynth", "map_cell_um": 25.0})
    assert started["ok"], started
    sid = started["result"]["session_id"]
    jobs.drain(180)
    packet = invoke(session, "qc_next", {"session_id": sid, "wait_s": 20})["result"]["packet"]
    assert packet["kind"] == "channel_audit"
    wrong = {"kind": "channel_audit", "verdicts": {"no_such_channel": {"verdict": "clean"}}}
    first = invoke(session, "qc_answer", {"session_id": sid, "packet_id": packet["packet_id"],
                                          "answer": wrong})
    assert not first["ok"] and first["error"]["code"] == "invalid_input"
    assert not (first["error"]["detail"] or {}).get("released")
    second = invoke(session, "qc_answer", {"session_id": sid, "packet_id": packet["packet_id"],
                                           "answer": wrong})
    assert not second["ok"] and second["error"]["detail"]["released"]
    after = invoke(session, "qc_next", {"session_id": sid, "wait_s": 5})["result"]
    assert (after.get("packet") or {}).get("packet_id") != packet["packet_id"]


# -- the segmentation model on Artifact Detector regions, and at the agent's ask --------


def _object_scene(tmp_path):
    from plexora.plugins.qc.server import refine
    from plexora.server.utils import source_image
    from tests.test_qc_magic_select import _dark_scene

    info, session, result, candidate, mask, geometry, region = _dark_scene(tmp_path)
    with source_image.SHELF.reader(session.image_data(info["name"])) as source:
        detector = refine.refine(candidate, mask, result, source, pixel_um=1.0,
                                 envelope=geometry)
    assert detector.refined
    return info, session, result, candidate, mask, detector.geometry, region


def test_a_detector_outline_stands_without_the_model(tmp_path):
    from plexora.plugins.qc.server import refine_sam

    info, session, result, candidate, mask, outline, _region = _object_scene(tmp_path)
    traced = refine_sam.refine_object(candidate, mask, result, session.image_data(info["name"]),
                                      outline=outline, pixel_um=1.0)
    assert traced.method == "artifact_detector" and traced.geometry == outline


def test_a_detector_outline_is_sharpened_by_the_model_when_it_agrees(tmp_path, monkeypatch):
    from plexora.plugins.qc.server import refine_sam
    from plexora.vision import sam, sam_backend
    from tests.test_qc_magic_select import _truth_predictor

    info, session, result, candidate, mask, outline, region = _object_scene(tmp_path)
    _truth_predictor(monkeypatch, region["mask"])
    with sam.use_backend(sam_backend.FakeSamBackend()):
        traced = refine_sam.refine_object(candidate, mask, result,
                                          session.image_data(info["name"]),
                                          outline=outline, pixel_um=1.0)
    record = traced.to_record()
    assert traced.method == "sam", record
    assert record["guards"]["agrees_with_classical"]["ok"]
    assert record["params"]["alternative"]["method"] == "artifact_detector"


def test_a_redraw_is_not_held_to_the_trace_the_agent_rejected(tmp_path, monkeypatch):
    """A redraw the agent asked for is judged on every guard but agreement
    with the classical trace it rejected: the model's step is handed that
    trace unbound (`fallback`, so no agreement guard), for any class."""
    from plexora.plugins.qc.server import refine_sam
    from plexora.vision import sam, sam_backend
    from tests.test_qc_magic_select import _dark_scene, _truth_predictor

    info, session, result, candidate, mask, geometry, region = _dark_scene(tmp_path)
    _truth_predictor(monkeypatch, region["mask"])
    handed = {}
    original_run = refine_sam.run

    def run(job, classical):
        handed["status"] = classical.status
        handed["any_class"] = job is not None
        return original_run(job, classical)

    monkeypatch.setattr(refine_sam, "run", run)
    with sam.use_backend(sam_backend.FakeSamBackend()):
        traced = refine_sam.redraw(candidate, mask, result, session.image_data(info["name"]),
                                   pixel_um=1.0, envelope=geometry)
    assert handed == {"status": "fallback", "any_class": True}
    assert traced.method == "sam", traced.params.get("sam")
    assert traced.params["requested_by"] == "agent"
    assert "agrees_with_classical" not in traced.to_record()["guards"]


def test_a_redraw_without_the_model_keeps_the_trace_and_says_why(tmp_path):
    from plexora.plugins.qc.server import refine_sam
    from tests.test_qc_magic_select import _dark_scene

    info, session, result, candidate, mask, geometry, _region = _dark_scene(tmp_path)
    traced = refine_sam.redraw(candidate, mask, result, session.image_data(info["name"]),
                               pixel_um=1.0, envelope=geometry)
    assert traced.method != "sam"
    assert traced.params["sam"]["status"] == "not_applicable"


def test_redraw_is_offered_once_and_only_with_the_model(monkeypatch):
    from plexora.plugins.qc.server import refine_sam

    engine = SimpleNamespace(options={"refine": True})
    monkeypatch.setattr(refine_sam, "available", lambda: False)
    assert not packets.can_redraw(engine, {})
    monkeypatch.setattr(refine_sam, "available", lambda: True)
    assert packets.can_redraw(engine, {})
    assert not packets.can_redraw(engine, {"sam_redraws": 1})
    assert not packets.can_redraw(SimpleNamespace(options={"refine": False}), {})


def test_an_object_region_goes_to_the_model_before_its_detector_outline(monkeypatch):
    """`refine_unit` routes an Artifact Detector region (`trace: object`) to
    the model first, and keeps the detector's outline when it declines."""
    from plexora.plugins.qc.server import engine as engines

    calls = []
    monkeypatch.setattr(engines.QCEngine, "options", {"refine": True})
    engine = engines.QCEngine.__new__(engines.QCEngine)
    square = {"type": "Polygon", "coordinates": [[[0, 0], [9, 0], [9, 9], [0, 9], [0, 0]]]}
    engine.envelope_of = lambda unit: (square, None)
    engine._object_outline = lambda unit, envelope, mask: calls.append("sam") or None
    engine._map_outline = lambda unit, envelope: calls.append("detector") or {"status": "detector"}
    assert engine.refine_unit({"trace": "object"}) == {"status": "detector"}
    assert calls == ["sam", "detector"]
