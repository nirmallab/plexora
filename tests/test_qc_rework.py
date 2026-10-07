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
    # The visual pass notes itself not run wherever magic select is absent:
    # not this test's concern (tests/test_qc_visual_session.py pins it).
    assert {n["check"]: n["status"] for n in notes if n["check"] != "visual"} == {
        "blur": "widened", "registration": "not_run"}


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
    ({"refinement": {"status": "fallback"}}, None),
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


# -- round two: review rules that hold, fragments joined, channels by one rule ----------


BAR = {"value": 0.5, "step": 0.05}


def _regions(*means, cells=30):
    return [{"mean": m, "cells": cells} for m in means]


def test_an_unclear_edge_writes_only_the_far_regions_and_probes_the_rest():
    from plexora.plugins.qc.server import transitions

    regions = _regions(0.70, 0.62, 0.55, 0.52, 0.58)
    plan = transitions._plan_regions({"strongly_abnormal": "artifact",
                                      "borderline_above": "mixed"}, regions, BAR)
    assert plan["decided"] == [0, 1]
    # One step above the bar is no longer enough: confirmed, strongest first.
    assert plan["confirm"] == [4, 2, 3] and not plan["dismissed"]
    # A clear edge still decides everything; a normal one dismisses the near.
    assert transitions._plan_regions({"strongly_abnormal": "artifact",
                                      "borderline_above": "artifact"}, regions,
                                     BAR)["decided"] == [0, 1, 2, 3, 4]
    normal = transitions._plan_regions({"strongly_abnormal": "artifact",
                                        "borderline_above": "normal"}, regions, BAR)
    assert normal["decided"] == [0, 1, 2, 4] and normal["dismissed"] == [3]


def test_an_unclear_edge_decides_at_most_the_largest_few():
    from plexora.plugins.qc.server import transitions

    cap = int(schemas.ENGINE["check_max_manual_regions"])
    regions = [{"mean": 0.9, "cells": 10 + i} for i in range(cap + 3)]
    plan = transitions._plan_regions({"strongly_abnormal": "artifact",
                                      "borderline_above": "cannot_tell"}, regions, BAR)
    assert len(plan["decided"]) == cap
    assert set(plan["decided"]) == set(range(3, cap + 3))       # the largest
    assert sorted(plan["confirm"]) == [0, 1, 2]


def test_a_mixed_clustered_row_stands_in_for_a_missing_edge_row():
    """An artifact sheet has no just-above row: the tear wrote both regions on
    `clustered: mixed`. The heart of the largest regions is the edge then."""
    from plexora.plugins.qc.server import transitions

    regions = _regions(0.95, 0.56)
    plan = transitions._plan_regions({"strongly_abnormal": "artifact",
                                      "clustered": "mixed"}, regions, BAR)
    assert plan["decided"] == [0] and plan["confirm"] == [1]
    clear = transitions._plan_regions({"strongly_abnormal": "artifact",
                                       "clustered": "artifact"}, regions, BAR)
    assert clear["decided"] == [0, 1]
    # A bar moved on the last look is never a clear edge.
    moved = transitions._plan_regions({"strongly_abnormal": "artifact",
                                       "borderline_above": "artifact"}, regions, BAR,
                                      moved_unseen=True)
    assert moved["decided"] == [0] and moved["confirm"] == [1]


def _registration_field(values, auto=0.3):
    from plexora.plugins.qc.server import score_fields

    ny, nx = values.shape
    return score_fields.ScoreField(
        check="registration", values=values, weight=np.isfinite(values).astype(float),
        grid={"x0": 0.0, "y0": 0.0, "step": 10.0, "nx": nx, "ny": ny,
              "image_size": [nx * 10, ny * 10]},
        fingerprint="f", auto_threshold=auto, pixel_um=0.65)


def test_registration_fragments_close_into_one_region_and_specks_drop():
    """Two 15-cell blobs two cells apart are one misregistered place; a
    20-cell blob alone is under the 24-cell floor."""
    from plexora.plugins.qc.server import score_fields

    values = np.zeros((40, 60))
    values[5:8, 5:10] = 0.9            # 15 cells
    values[5:8, 12:17] = 0.9           # 15 cells, a two-cell gap
    values[25:29, 40:45] = 0.9         # 20 cells, far away
    found = score_fields.regions(_registration_field(values), 0.5)
    assert found["n_regions"] == 1
    region = found["regions"][0]
    # Its size and outline are the flagged cells themselves, not the gap.
    assert region["cells"] == 30
    mask = score_fields.region_mask(found, 0)
    assert mask.sum() == 30 and not mask[5:8, 10:12].any()


def test_a_cell_straddling_a_lost_cycle_is_not_scored_as_misregistration():
    from plexora.plugins.qc.server import registration, score_fields

    share = np.full((4, 4), 0.8, dtype=np.float32)
    nucleus = np.full((4, 4), 0.5, dtype=np.float32)
    reference = np.full((4, 4), 0.5, dtype=np.float32)
    comparison = np.full((4, 4), 0.5, dtype=np.float32)
    comparison[0, 0] = 0.5 * registration.MAP_BALANCE * 0.5   # unbalanced
    comparison[1, 1] = 0.5 * registration.MAP_BALANCE * 1.2   # balanced enough
    entry = {"factor": 1.0, "field": {"map": {
        "grid": {"x0": 0, "y0": 0, "step": 10, "nx": 4, "ny": 4}, "share": share,
        "nucleus": nucleus, "reference": reference, "comparison": comparison}}}
    field = score_fields.from_registration(entry, pixel_um=0.65, fingerprint="f",
                                           image_size=(40, 40))
    assert np.isnan(field.values[0, 0])
    assert np.isfinite(field.values[1, 1]) and np.isfinite(field.values[2, 2])


class _Units:
    def __init__(self, units):
        self._units = units

    def units_of(self, kind, project=None):
        return [u for u in self._units if u["type"] == kind]


def test_channel_effects_flag_only_what_a_channel_scoped_finding_names():
    """Registration of cycle 2 (a check, scope cycle), a fold over every
    channel (a check), segmentation, and a CD3 aggregate (scope channel):
    only CD3 is flagged; the second cycle's channels are reached; DNA stays
    clean."""
    from plexora.plugins.qc.server import finalize

    names = ["DNA_1", "CD3", "DNA_2", "CD20", "CD8"]
    units = [{"type": "channel", "id": n, "state": "clean"} for n in names]
    units += [
        {"type": "candidate", "id": "reg", "state": "confirmed_exclude", "origin": "check",
         "detector": "registration", "class": "cross_cycle_registration_error",
         "channels": ["DNA_2", "CD20"], "audit_channel": "DNA_2", "scope_hint": "cycle"},
        {"type": "candidate", "id": "fold", "state": "confirmed_exclude", "origin": "check",
         "detector": "artifacts", "class": "tissue_fold", "channels": list(names),
         "audit_channel": "DNA_1", "scope_hint": "all_channels"},
        {"type": "candidate", "id": "seg", "state": "confirmed_warn", "origin": "check",
         "detector": "segmentation", "class": "segmentation_error", "channels": [],
         "audit_channel": "DNA_1", "scope_hint": "all_channels"},
        {"type": "candidate", "id": "agg", "state": "confirmed_exclude",
         "class": "antibody_aggregate", "channels": ["CD3"], "audit_channel": "CD3",
         "scope_hint": "channel"},
    ]
    engine = _Units(units)
    effects = finalize.channel_effects(engine)
    assert effects["affected"] == {"CD3"} and not effects["failed"]
    assert effects["reached"]["CD20"] == ["reg", "fold"]
    finalize._settle_channel_statuses(engine)
    states = {u["id"]: u["state"] for u in engine.units_of("channel")}
    assert {n for n, s in states.items() if s == "flagged"} == {"CD3"}
    assert states["DNA_1"] == "clean"
    by_id = {u["id"]: u for u in engine.units_of("channel")}
    assert by_id["CD20"]["reached_by"] == ["fold", "reg"]


def test_a_failed_channel_verdict_fails_its_channel():
    from plexora.plugins.qc.server import finalize

    units = [{"type": "channel", "id": n, "state": "clean"} for n in ("DNA_1", "CD20")]
    units.append({"type": "candidate", "id": "f", "state": "confirmed_exclude",
                  "class": "empty_or_failed_channel", "channel_level": True,
                  "channels": ["CD20"], "audit_channel": "CD20", "scope_hint": "channel"})
    assert finalize.channel_effects(_Units(units))["failed"] == {"CD20"}


def test_a_bar_with_nothing_at_or_above_it_settles_the_check(monkeypatch):
    from plexora.plugins.qc.server import checks_result, transitions

    values = np.full((20, 20), 0.1)
    values[3, 3] = 0.45                       # within a step below, not above
    closed = []

    class Engine:
        def close(self, unit, state, reason):
            unit["state"], unit["reason"] = state, reason
            closed.append(state)

        def settle_channels(self):
            pass

    monkeypatch.setattr(checks_result, "record", lambda engine, unit: None)
    unit = {"check": "registration", "offset_steps": 0}
    field = _registration_field(values, auto=0.5)
    assert transitions._nothing_at_bar(Engine(), unit, field)
    assert transitions.settle_if_nothing_at_bar(Engine(), unit, field)
    assert unit["state"] == "decided" and unit["n_regions"] == 0 and closed == ["decided"]
    # A place at the bar is something to judge.
    values[3, 3] = 0.6
    assert not transitions._nothing_at_bar(Engine(), {"check": "registration"},
                                           _registration_field(values, auto=0.5))


def test_an_overlapping_candidate_of_the_same_class_is_explained():
    """A second detector's fold over the same place as a confirmed fold
    (IoU >= `explained_iou`), even one only warned, costs no look."""
    from plexora.plugins.qc.server import engine as engines

    mask = np.zeros((20, 20), dtype=bool)
    mask[2:10, 2:10] = True
    shifted = np.zeros_like(mask)
    shifted[3:11, 3:11] = True
    other_place = np.zeros_like(mask)
    other_place[12:18, 12:18] = True
    warned = {"type": "candidate", "id": "a", "project": "p", "state": "confirmed_warn",
              "class": "tissue_fold", "_mask": mask}
    fresh = {"type": "candidate", "id": "b", "project": "p", "state": "awaiting_confirm",
             "class_hint": "tissue_fold", "_mask": shifted}
    elsewhere = {"type": "candidate", "id": "c", "project": "p", "state": "awaiting_confirm",
                 "class_hint": "tissue_fold", "_mask": other_place}
    blurred = {"type": "candidate", "id": "d", "project": "p", "state": "awaiting_confirm",
               "class_hint": "out_of_focus", "_mask": shifted}
    engine = engines.QCEngine.__new__(engines.QCEngine)
    units = [warned, fresh, elsewhere, blurred]
    engine.units_of = lambda kind, project=None: [u for u in units if u["type"] == kind]
    engine.mask_of = lambda unit: unit["_mask"]
    assert engine._explained_by(fresh) is warned
    assert engine._explained_by(elsewhere) is None
    # Another class over the same place is not the same artifact, and a
    # warned region does not swallow it.
    assert engine._explained_by(blurred) is None


def test_channel_token_is_pure():
    names = ["DNA_1", "CD3", "DNA_2", "CD20"]
    cycles = [{"index": 1, "channels": ["DNA_1", "CD3"]},
              {"index": 2, "channels": ["DNA_2", "CD20"]}]
    assert schemas.channel_token(names[::-1], names, cycles) == "all_channels"
    assert schemas.channel_token(["CD20", "DNA_2"], names, cycles) == "cycle 2"
    assert schemas.channel_token(["CD3"], names, cycles) == ["CD3"]
    assert schemas.channel_token([], names, cycles) == []


# -- packet cost: the final review table, audit candidates once, receipts ---------------


def _forty_channel_scan():
    names = [f"M{i:02d}" for i in range(40)]
    cycles = [{"index": 1, "channels": names[:20]}, {"index": 2, "channels": names[20:]}]
    return SimpleNamespace(channels=[{"name": n} for n in names],
                           meta={"cycles": {"cycles": cycles}}), names, cycles


def _synthetic_regions(n, names, cycles):
    kinds = [("tissue_acquisition", "tissue_fold"), ("tissue_acquisition", "tissue_damage_or_detachment"),
             ("tissue_acquisition", "debris_or_foreign_object"), ("blur_focus", "out_of_focus"),
             ("registration", "cross_cycle_registration_error"), ("review", "tissue_fold")]
    regions = []
    for i in range(n):
        category, klass = kinds[i % len(kinds)]
        channels = names if i % 3 == 0 else cycles[i % 2]["channels"] if i % 3 == 1 \
            else names[i % 7:i % 7 + 3]
        regions.append({"label": f"r{i + 1}", "candidate": f"cand_{i:04d}", "class": klass,
                         "category": category, "action": ("exclude", "warn")[i % 2],
                         "geometry": {"type": "Polygon", "coordinates": [[[0, 0]] * 40]},
                         "channels": list(channels),
                         "tissue_fraction": 0.0001 * ((i * 37) % 97 + 1),
                         "refined_fraction": 0.00009,
                         "refinement": {"status": "refined", "method": "sam",
                                        "kept_fraction": 0.8}})
    return regions


def test_a_250_region_final_review_is_a_grouped_table_under_twelve_thousand_chars():
    scan, names, cycles = _forty_channel_scan()
    regions = _synthetic_regions(250, names, cycles)
    table = packets._review_table(regions, scan)
    size = len(json.dumps(table))
    listed = len(json.dumps([packets._compact_region(r, scan) for r in regions]))
    print(f"final review: table {size} chars, one row per region {listed} chars")
    assert size < 12_000
    assert table["n"] == 250 and sum(table["by_action"].values()) == 250
    groups = table["groups"]
    assert sum(g["count"] for g in groups) == 250
    assert all(len(g["top"]) <= packets.FINAL_REVIEW_TOP_PER_GROUP for g in groups)
    assert all(g["count"] == len(g["top"]) + int(g.get("more") or 0) for g in groups)
    # Excluding groups first, each group's largest regions first.
    ranks = [consolidate.ACTION_RANK[g["action"]] for g in groups]
    assert ranks == sorted(ranks)
    for g in groups:
        shares = [t["tissue_fraction"] for t in g["top"]]
        assert shares == sorted(shares, reverse=True)
        assert all(t["channels"] in ("all_channels", "cycle 1", "cycle 2")
                   or isinstance(t["channels"], list) for t in g["top"])
    # A single-class group does not repeat its class tally.
    assert all("classes" not in g for g in groups if g["category"] != "tissue_acquisition")


def test_the_review_still_resolves_a_label_the_table_only_counts(monkeypatch):
    """`units[0]["regions"]` keeps every label: a concern may name one the
    table counted under `more`."""
    scan, names, cycles = _forty_channel_scan()
    regions = _synthetic_regions(40, names, cycles)
    table = packets._review_table(regions, scan)
    shown = {t["label"] for g in table["groups"] for t in g["top"]}
    hidden = next(r for r in regions if r["label"] not in shown)
    unit = {"type": "final", "project": "p", "id": "final",
            "regions": [{k: r[k] for k in ("label", "candidate")} for r in regions]}
    labels = {r["label"]: r["candidate"] for r in unit["regions"]}
    assert labels[hidden["label"]] == hidden["candidate"]


def test_trim_shrinks_the_review_groups_before_anything_else():
    from plexora.agent.sessions import budget

    scan, names, cycles = _forty_channel_scan()
    regions = _synthetic_regions(250, names, cycles)
    table = packets._review_table(regions, scan, top=60)
    for g in table["groups"]:
        for t in g["top"]:
            t["note"] = "z" * 300
    packet = {"kind": "final_qc_review",
              "evidence": {"regions": table, "tissue": {"fraction": 0.7},
                           "planning_notes": [{"check": "blur", "status": "ok"}]}}
    assert len(json.dumps(packet)) > budget.PACKET_CHAR_LIMIT
    counts = [g["count"] for g in table["groups"]]
    packets.trim(packet)
    assert len(json.dumps(packet)) <= budget.PACKET_CHAR_LIMIT
    evidence = packet["evidence"]
    assert evidence["tissue"] == {"fraction": 0.7}  # TRIM_ORDER never reached
    assert evidence["truncated"] == {"top": True}
    groups = evidence["regions"]["groups"]
    assert [g["count"] for g in groups] == counts
    assert all(g["count"] == len(g["top"]) + int(g.get("more") or 0) for g in groups)
    # Round-robin: no group was emptied while another kept many more.
    sizes = [len(g["top"]) for g in groups if g["count"] >= 60]
    assert max(sizes) - min(sizes) <= 1


def test_the_audit_describes_a_shared_candidate_once():
    shared = [{"id": f"cand_{k}", "label": f"c{k}", "class_hint": "tissue_fold",
               "score": 0.8, "tissue_fraction": 0.01, "channel_level": None,
               "geometry": {"type": "Polygon"}} for k in range(1, 6)]
    rows = [{"number": i + 1, "channel": f"M{i:02d}", "cycle": 1, "flags": [],
             "candidates": list(shared), "scan_hints": None} for i in range(12)]
    evidence = packets._audit_evidence(rows)
    assert sorted(evidence["candidates"]) == [f"c{k}" for k in range(1, 6)]
    assert all(row["candidates"] == [f"c{k}" for k in range(1, 6)]
               for row in evidence["rows"])
    text = json.dumps(evidence)
    assert all(text.count(f'"cand_{k}"') == 1 for k in range(1, 6))
    assert "geometry" not in text
    repeated = json.dumps([{**r, "candidates": [{k: c[k] for k in (
        "id", "label", "class_hint", "score", "tissue_fraction", "channel_level")}
        for c in r["candidates"]]} for r in rows])
    print(f"audit evidence: {len(text)} chars, repeated per row {len(repeated)} chars")
    assert len(text) < len(repeated) / 3


def test_an_answer_lists_a_few_receipts_and_counts_many():
    from plexora.agent.sessions import tools

    seven = [f"op.{i:03d}" for i in range(7)]
    assert tools.receipts_summary(seven) == {"count": 7, "first": "op.000", "last": "op.006"}
    assert tools.receipts_summary(seven[:3]) == seven[:3]
    assert tools.receipts_summary(None) == []
    assert tools.RECEIPTS_LISTED == 5


# -- text and metrics -------------------------------------------------------------------


def test_the_guide_lists_only_the_classes_an_answer_may_name():
    classes = packets.reading_guide()["classes"]
    assert list(classes) == list(schemas.AGENT_CLASSES)
    assert "tissue_artifact" not in classes


def test_a_score_review_narration_names_its_subject():
    said = packets.narrate({"kind": "score_review",
                            "evidence": {"check": "registration", "channel": None,
                                         "reference": "DNA_1"}})
    assert "DNA_1" in said and "the mask" not in said
    said = packets.narrate({"kind": "score_review", "evidence": {"check": "artifacts"}})
    assert "the image" in said
    said = packets.narrate({"kind": "score_review",
                            "evidence": {"check": "blur", "channel": "DNA_2"}})
    assert "DNA_2" in said


def test_a_check_region_brief_counts_score_cells_not_cells():
    from plexora.plugins.qc.server import check_candidates

    bar = {"value": 0.4, "auto": 0.4, "offset_steps": 0, "step": 0.05}
    region = {"cells": 30, "mean": 0.5, "max": 0.7, "area_um2": 5070.0}
    metrics = check_candidates._metrics({"check": "blur"}, bar, region, cell_um=13.0)
    assert metrics["score_cells"] == 30 and metrics["score_cell_um"] == 13.0
    assert metrics["area_um2"] == 5070.0 and "cells" not in metrics
    brief = packets._brief({"id": "c", "metrics": metrics}, None)
    assert brief["metrics"]["score_cells"] == 30


def test_a_layer_narrates_its_own_action_and_lists_the_rest_once():
    from plexora.plugins.qc.server import roi_link

    names = ["DNA_1", "CD3", "DNA_2", "CD20"]
    cycles = [{"index": 1, "channels": ["DNA_1", "CD3"]},
              {"index": 2, "channels": ["DNA_2", "CD20"]}]
    candidate = {"findings": [
        {"class": "tissue_fold", "channels": list(names), "action": "exclude",
         "primary": True, "share": 1.0},
        {"class": "out_of_focus", "channels": ["CD3"], "action": "warn", "primary": False,
         "share": 1.0}]}
    said = roi_link.findings_note(candidate, action="exclude", channel_names=names,
                                  cycles=cycles)
    assert said == "Findings: tissue fold in all_channels; also contains: out of focus " \
                   "in CD3 (warn)"
    # Without the image's channels the list is kept (short, and capped).
    said = roi_link.findings_note(candidate, action="exclude")
    assert said.startswith("Findings: tissue fold in DNA_1, CD3, DNA_2, CD20;")


def test_the_dna_digest_is_optional():
    assert packets._dna_digest(SimpleNamespace(record={}), "no_such_project") is None
