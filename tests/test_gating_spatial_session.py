"""A gating session with spatial evidence, through the capability layer.

What these pin is the switch's promise (`SessionOptions.spatial`): `off` is
today's session byte for byte -- the same packets, the same memo keys, the
same gates; `diagnose` records and changes nothing; `decide` puts a move the
words and the numbers tie to bright neighbours to a like-for-like look, holds
a gate whose errors are concentrated, still moves one whose errors are
everywhere, and never moves on an inconclusive look.
"""

import json

import numpy as np
import pytest

from plexora.agent import AgentSession, invoke, registry
from tests.autogate_fixtures import make_gating_project, make_spatial_gating_project
from tests.test_gating_session import Oracle, drive, ok, units

pytestmark = pytest.mark.paid


@pytest.fixture(autouse=True)
def _gating():
    registry.discover(["gating"])


def start(session, project, **options):
    from plexora.agent import jobs

    started = ok(invoke(session, "gating_session_start", {"scope": "project",
                                                          "project": project, **options}))
    jobs.drain(120)
    return started


def _visible(packet):
    """What the agent reads of a packet, less what names the session."""
    return json.dumps({k: packet.get(k) for k in ("kind", "question", "evidence", "allowed")},
                      sort_keys=True, default=str)


def test_spatial_off_is_todays_session(tmp_path):
    info = make_gating_project(tmp_path, grid=24, size=960, markers=("CD3", "CD8", "CD4"))
    session = AgentSession()
    # Runs without the option until one asks nothing (a first rerun can
    # re-ask a look -- so on the commit before the switch existed)...
    for _ in range(3):
        first = start(session, "gsynth", mode="propose", agent="same")
        seen = drive(session, first["session_id"], Oracle(info))
    gates = units(session, first["session_id"])
    plain = ok(invoke(session, "gating_session_status", {"session_id": first["session_id"]}))
    # ...then the switch said out loud: the same packets, so the same memo
    # keys -- every answer replayed, none asked again.
    again = start(session, "gsynth", mode="propose", agent="same", spatial="off")
    replayed = drive(session, again["session_id"], Oracle(info))
    status = ok(invoke(session, "gating_session_status", {"session_id": again["session_id"]}))
    assert seen == [] and replayed == []
    assert status["summary"]["replayed"] == plain["summary"]["replayed"] > 0
    assert {m: (u["state"], u.get("final"), u.get("proposed")) for m, u in gates.items()} == \
        {m: (u["state"], u.get("final"), u.get("proposed"))
         for m, u in units(session, again["session_id"]).items()}
    for unit in status["units"]:
        assert "spatial" not in unit
    assert "spatial_held" not in status["summary"]
    from plexora.plugins.gating.server.autogate import provenance

    for row in provenance.read("gsynth").values():
        assert "spatial" not in (row.get("detail") or {})
    from plexora.plugins.gating.server.autogate import engine as engines

    store = engines.store()
    for event in store.decisions(again["session_id"]):
        if event.get("event") != "issued":
            continue
        packet, _images = store.read_packet(again["session_id"], event["packet_id"])
        assert "spatial" not in packet["evidence"] and "rows_env" not in packet["evidence"]
        assert "explanations" not in (packet.get("allowed") or {})


def test_diagnose_records_the_evidence_and_changes_no_gate(tmp_path):
    info = make_gating_project(tmp_path, grid=24, size=960, markers=("CD3", "CD8", "CD4"))
    session = AgentSession()
    plain = start(session, "gsynth", mode="propose", agent="a", reuse_answers=False)
    drive(session, plain["session_id"], Oracle(info))
    measured = start(session, "gsynth", mode="propose", agent="b", reuse_answers=False,
                     spatial="diagnose")
    seen = drive(session, measured["session_id"], Oracle(info))
    before, after = units(session, plain["session_id"]), units(session, measured["session_id"])
    assert {m: (u["state"], u.get("proposed")) for m, u in before.items()} == \
        {m: (u["state"], u.get("proposed")) for m, u in after.items()}
    for unit in after.values():
        assert unit["spatial"]["mode"] == "A"
    looks = [p for p in seen if p["kind"] == "t2_confirm"]
    for packet in looks:
        assert packet["evidence"]["spatial"]["mode"] == "A"
        assert "explanations" not in packet["allowed"]
        assert set(packet["allowed"]["rows"]) <= {"below", "near", "above"}
    from plexora.plugins.gating.server.autogate import provenance

    details = [row["detail"].get("spatial") for row in provenance.read("gsynth").values()]
    assert any(d and d["mode"] == "A" and d["influenced"] is False for d in details)


class Scripted(Oracle):
    """The oracle, with a script for the spatial looks: `first` the rows and
    explanations of a first look of `marker`, `contrast` those of its
    like-for-like look."""

    def __init__(self, info, marker, first, contrast, explanations=()):
        super().__init__(info)
        self.marker, self.first, self.contrast = marker, first, contrast
        self.explanations = list(explanations)
        self.contrasts = 0

    def answer(self, packet):
        kind = packet["kind"]
        marker = packet["units"][0]["marker"] if packet["units"] else None
        allowed = packet.get("allowed") or {}
        if kind == "t3_contrast":
            self.contrasts += 1
            rows = {k: self.contrast.get(k, "cannot_tell") for k in allowed["rows"]}
            return {"kind": kind, "rows": rows, "pattern": "membrane", "confidence": "sure",
                    "explanations": self.explanations}
        if kind in ("t2_confirm", "t3_biological") and marker == self.marker \
                and "explanations" in allowed and not getattr(self, "_answered", False):
            self._answered = True
            rows = {k: self.first.get(k, "cannot_tell") for k in allowed["rows"]}
            return {"kind": kind, "rows": rows, "pattern": "membrane", "confidence": "sure",
                    "explanations": self.explanations}
        return super().answer(packet)


SPILL_LOOK = {"below": "unstained", "near": "neighbour_only", "above": "mixed",
              "near_bright": "neighbour_only"}


def _spill_project(tmp_path):
    made = make_spatial_gating_project(tmp_path, "spill", scenario="dense_spill", size=768,
                                       markers=("CD3", "CD8", "CD4"))
    info = {"cells": [{"id": int(i), "CD3": float(made["values"]["CD3"][k]),
                       "CD8": float(made["values"]["CD8"][k]),
                       "CD4": float(made["values"]["CD4"][k])}
                      for k, i in enumerate(made["ids"])],
            "truth": {m: [int(i) for i, t in zip(made["ids"], made["truth"][m]) if t]
                      for m in ("CD3", "CD8", "CD4")}}
    return made, info


def _contrast_session(tmp_path, first, contrast, explanations=("possible_neighbor_spillover",)):
    _made, info = _spill_project(tmp_path)
    session = AgentSession()
    agent = Scripted(info, "CD3", first, contrast, explanations)
    started = start(session, "spill", mode="propose", markers=["CD3"], spatial="decide",
                    reuse_answers=False)
    seen = drive(session, started["session_id"], agent)
    return session, started, seen, agent


def test_a_spill_look_is_held_not_raised(tmp_path):
    session, started, seen, agent = _contrast_session(
        tmp_path, SPILL_LOOK, {"bright": "neighbour_only", "clear": "own_stain",
                               "dense_clear": "own_stain", "reference": "own_stain"})
    kinds = [p["kind"] for p in seen]
    assert "t3_contrast" in kinds, kinds
    assert "t4_candidates" not in kinds[kinds.index("t3_contrast"):]
    contrast = next(p for p in seen if p["kind"] == "t3_contrast")
    assert contrast["evidence"]["side"] == "above"
    assert {"bright", "reference"} <= set(contrast["allowed"]["rows"])
    unit = units(session, started["session_id"])["CD3"]
    assert unit["spatial"]["case"] == "B" and "spill_concentrated" in unit["spatial"]["flags"]
    assert unit["confidence"] != "high"
    from plexora.plugins.gating.server.autogate import engine as engines

    record = engines.store().load(started["session_id"])["units"]["spill::CD3"]
    look = next(p for p in seen if p["kind"] == "t2_confirm")
    # Held: written where the first look saw it (snapped to the slider's grid).
    assert record["proposed"] == pytest.approx(look["evidence"]["candidate"]["low"], abs=0.05)


def test_a_consistent_false_positive_still_reaches_t4(tmp_path):
    session, started, seen, _agent = _contrast_session(
        tmp_path, SPILL_LOOK, {"bright": "unstained", "clear": "unstained",
                               "dense_clear": "unstained", "low_snr": "unstained",
                               "reference": "own_stain"}, explanations=("threshold_error",))
    kinds = [p["kind"] for p in seen]
    assert "t3_contrast" in kinds, kinds
    assert "t4_candidates" in kinds[kinds.index("t3_contrast"):], kinds
    unit = units(session, started["session_id"])["CD3"]
    assert unit["spatial"]["case"] == "A"


def test_contrast_counts_against_the_budget_once(tmp_path):
    session, started, seen, agent = _contrast_session(
        tmp_path, SPILL_LOOK, {"bright": "unstained", "clear": "unstained",
                               "dense_clear": "unstained", "reference": "own_stain"},
        explanations=("threshold_error",))
    assert agent.contrasts == 1
    from plexora.plugins.gating.server.autogate import engine as engines

    record = engines.store().load(started["session_id"])["units"]["spill::CD3"]
    assert record["contrast_done"] and int(record["used"]["packets"]) <= 6
    assert sum(1 for p in seen if p["kind"] == "t3_contrast") == 1


def test_an_inconclusive_look_beside_bright_neighbours_never_raises_the_gate(tmp_path):
    """The raise was argued by cells lit by their neighbours, and the
    like-for-like look settles nothing: held (case B, when the numbers say
    spill-over is common) or written for review (case E) -- never raised."""
    session, started, seen, _agent = _contrast_session(
        tmp_path, SPILL_LOOK, {"bright": "mixed", "reference": "own_stain"},
        explanations=("insufficient_evidence",))
    kinds = [p["kind"] for p in seen]
    assert "t3_contrast" in kinds and "t4_candidates" not in kinds, kinds
    unit = units(session, started["session_id"])["CD3"]
    assert unit["spatial"]["case"] in ("B", "E")
    look = next(p for p in seen if p["kind"] == "t2_confirm")
    from plexora.plugins.gating.server.autogate import engine as engines

    record = engines.store().load(started["session_id"])["units"]["spill::CD3"]
    written = record.get("final") if record.get("final") is not None else record["proposed"]
    assert written == pytest.approx(look["evidence"]["candidate"]["low"], abs=0.05)


def test_explanations_are_refused_where_they_are_not_offered(tmp_path):
    info = make_gating_project(tmp_path, grid=16, size=640, markers=("CD3", "CD8"))
    session = AgentSession()
    started = start(session, "gsynth", mode="propose", markers=["CD8"], audit_sheet=False,
                    spatial="stratify")
    result = ok(invoke(session, "gating_next", {"session_id": started["session_id"]}))
    while result["state"] == "decision" and result["packet"]["kind"] != "t2_confirm":
        packet = result["packet"]
        result = ok(invoke(session, "gating_answer", {
            "session_id": started["session_id"], "packet_id": packet["packet_id"],
            "answer": Oracle(info).answer(packet)}))["next"]
    if result["state"] != "decision":
        pytest.skip("CD8 was settled without a look")
    packet = result["packet"]
    answer = {**Oracle(info).answer(packet), "explanations": ["threshold_error"]}
    refused = invoke(session, "gating_answer", {"session_id": started["session_id"],
                                                "packet_id": packet["packet_id"],
                                                "answer": answer})
    assert not refused["ok"] and refused["error"]["code"] == "invalid_input"


def test_a_decide_session_replays_and_its_pictures_draw_again(tmp_path):
    from plexora.ai import bench

    _made, info = _spill_project(tmp_path)
    session = AgentSession()

    def agent():
        return Scripted(info, "CD3", SPILL_LOOK, {"bright": "neighbour_only",
                                                  "clear": "own_stain",
                                                  "reference": "own_stain"},
                        ("possible_neighbor_spillover",))

    runs = []
    for _ in range(3):
        started = start(session, "spill", mode="propose", markers=["CD3", "CD8"],
                        spatial="decide", agent="replay")
        runs.append((started, drive(session, started["session_id"], agent())))
    first = runs[0][0]
    rows = bench.replay_session(first["session_id"], agent(), rerender=True, session=session)
    drawn = [i for r in rows for i in r.get("images") or [] if i.get("rerendered")]
    assert drawn and all(i["identical"] for i in drawn)
    # The same answers from memory reach the same gates. (A first rerun may
    # re-ask a look whose context sheet a replayed packet no longer trimmed:
    # so without spatial evidence too; by the third, nothing is asked.)
    assert runs[-1][1] == []
    gates = [{m: u.get("proposed") for m, u in units(session, r["session_id"]).items()}
             for r, _seen in runs]
    assert gates[0] == gates[1] == gates[2]


def test_the_uncertain_layer_never_changes_a_call(tmp_path):
    from plexora.agent import cell_exclusions
    from plexora.plugins.gating.server.autogate import calls, spatial

    make_spatial_gating_project(tmp_path, "spill", scenario="dense_spill", size=768,
                                markers=("CD3", "CD8"))
    session = AgentSession()
    ds = session.data("spill")
    values = np.asarray(ds.table.columns(["CD3"])["CD3"], dtype=np.float32)
    low = float(np.percentile(values, 60))
    high = float(values.max())
    payload = spatial.payload_for(session, ds, "CD3", "A")
    mask = calls.uncertain_mask(ds, "CD3", low, high, spatial=payload)
    assert mask is not None and mask.any()
    plain = cell_exclusions.calls(ds, "CD3", values, low, high)
    layered = calls.calls_with_uncertainty(ds, "CD3", values, low, high, mask)
    assert ((layered == calls.UNCERTAIN) <= (plain == cell_exclusions.POSITIVE)).all()
    assert (layered == calls.UNCERTAIN).any()
    # Shown, never applied: put back, the calls are exactly the gate's.
    assert np.array_equal(np.where(layered == calls.UNCERTAIN, cell_exclusions.POSITIVE,
                                   layered), plain)
    assert calls.uncertain_mask(ds, "CD3", low, high, spatial=None) is None
