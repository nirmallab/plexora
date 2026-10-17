"""What the contrast check sees, used by gating (`SessionOptions.observations`).

The capture: the contrast check sends a second picture -- a few bright and a
few dim cells per marker, spread across the image -- and its answer says
which bright tiles are debris and which dim cells carry the stain; that is
kept with the marker's contrast choice. The use, only with `observations:
use`: the looks are told, the start comes down to dim stained cells, the
fields keep away from the debris, a doubt caps confidence and keeps a clean
split from being accepted unseen. Off, the session is today's.
"""

import numpy as np
import pytest

from plexora.agent import AgentSession, invoke, registry
from tests.autogate_fixtures import FakeData, make_gating_project
from tests.test_gating_session import (Oracle, _to_display, answer, drive, next_packet, ok,
                                       start)

pytestmark = pytest.mark.paid


@pytest.fixture(autouse=True)
def _gating():
    registry.discover(["gating"])


# -- the spread picks -------------------------------------------------------------------


def test_spread_picks_are_deterministic_and_spread_out():
    from plexora.agent.evidence import cell_window

    rng = np.random.default_rng(3)
    cells = [{"cell_id": i, "x": float(x), "y": float(y), "value": 1.0}
             for i, (x, y) in enumerate(rng.uniform(0, 1000, (40, 2)), start=1)]
    discs = [(c, np.full(20, float(c["cell_id"]))) for c in cells]
    first = cell_window._spread(discs, np.median, 20.0, 6)
    assert first == cell_window._spread(list(reversed(discs)), np.median, 20.0, 6)
    assert len(first) == 6 and first[0]["cell_id"] == 20
    gaps = [min((a["x"] - b["x"]) ** 2 + (a["y"] - b["y"]) ** 2
                for b in first if b is not a) for a in first]
    # Farther apart than six cells taken in id order.
    near = cells[:6]
    near_gaps = [min((a["x"] - b["x"]) ** 2 + (a["y"] - b["y"]) ** 2
                     for b in near if b is not a) for a in near]
    assert min(gaps) > min(near_gaps)
    assert cell_window._spread([], np.median, 0.0, 4) == []


def test_the_calibration_keeps_spread_cells_and_their_column(tmp_path):
    from plexora.agent.evidence import calibration

    make_gating_project(tmp_path)
    session = AgentSession()
    try:
        record, _ = calibration.calibrate(session, "gsynth")
        entry = record["channels"]["CD8"]
        spread = entry["cells"]["spread"]
        assert 4 <= len(spread["bright"]) <= 6 and len(spread["dim"]) == 4
        assert entry["anchors"]["column"] == "CD8"
        assert record["version"] == calibration.VERSION == "7"
    finally:
        session.close()


# -- the capture ------------------------------------------------------------------------


def test_the_contrast_check_sends_the_spread_strip(tmp_path):
    from plexora.agent import artifacts

    info = make_gating_project(tmp_path)
    session = AgentSession()
    sid = start(session, markers=["CD3", "CD8"])["session_id"]
    packet = _to_display(session, sid, Oracle(info))
    roles = [image["role"] for image in packet["images"]]
    assert roles == ["display_grid", "display_spread"]
    tiles = packet["allowed"]["tiles"]
    assert set(tiles) == {"CD3", "CD8"}
    assert tiles["CD8"][:2] == ["b1", "b2"] and "d1" in tiles["CD8"]
    assert "pattern_seen" in packet["question"] and "dim_stained" in packet["question"]
    _png, sidecar = artifacts.get(packet["images"][1]["artifact_id"])
    rows = {r["marker"]: r for r in sidecar["manifest"]["rows"]}
    assert set(rows["CD8"]["tiles"]) == set(tiles["CD8"])
    assert all("cell_id" in cell for cell in rows["CD8"]["tiles"].values())


def _observe(session, sid, info, **extra):
    packet = _to_display(session, sid, Oracle(info))
    tiles = packet["allowed"]["tiles"]
    reply = {"kind": "display_setup",
             "background_ok": {m: ["c3"] for m in tiles}, "structure_ok": {m: ["c3"] for m in tiles},
             "pattern_seen": {"CD8": "membrane"}, "stain_location": {"CD8": "on_cells"},
             "bright_artifact": {"CD8": ["b1", "b2"]}, "dim_stained": {"CD8": ["d1", "d3"]},
             "notes": "specks on CD8", **extra}
    answer(session, sid, packet, reply)
    return packet


def test_what_the_check_saw_is_kept_with_the_choice(tmp_path):
    from plexora.agent.evidence import calibration

    info = make_gating_project(tmp_path)
    session = AgentSession()
    sid = start(session, markers=["CD3", "CD8"])["session_id"]
    _observe(session, sid, info)
    record = calibration.load("gsynth")
    seen = calibration.observed(record, "CD8")
    assert seen["pattern_seen"] == "membrane" and seen["stain_location"] == "on_cells"
    assert [b["id"] for b in seen["bright"] if b["artifact"]] == ["b1", "b2"]
    assert [d["id"] for d in seen["dim"] if d["stained"]] == ["d1", "d3"]
    assert all({"cell_id", "x", "y", "value"} <= set(d) for d in seen["dim"])
    assert seen["notes"] == "specks on CD8"
    status = ok(invoke(session, "gating_session_status", {"session_id": sid}))
    line = status["display"]["projects"]["gsynth"]["observed"]["CD8"]
    assert line.startswith("membrane, on cells; 2 of 4 dim cells stained; 2 of ")
    assert calibration.observed(record, "CD3") is None
    # A recompute keeps it; a redo forgets it.
    again, _ = calibration.calibrate(session, "gsynth", force=True)
    assert calibration.observed(again, "CD8") == seen
    forgotten, names = calibration.forget_choices("gsynth")
    assert "CD8" in names and calibration.observed(forgotten, "CD8") is None


def test_an_unknown_row_in_an_observation_is_refused(tmp_path):
    info = make_gating_project(tmp_path)
    session = AgentSession()
    sid = start(session, markers=["CD3", "CD8"])["session_id"]
    packet = _to_display(session, sid, Oracle(info))
    refused = invoke(session, "gating_answer", {
        "session_id": sid, "packet_id": packet["packet_id"], "include_next": False,
        "answer": {"kind": "display_setup", "background_ok": {"CD8": ["c3"]},
                   "structure_ok": {"CD8": ["c3"]}, "dim_stained": {"CD99": ["d1"]}}})
    assert not refused["ok"] and refused["error"]["code"] == "invalid_input"


def test_an_answer_without_observations_still_applies(tmp_path):
    from plexora.agent.evidence import calibration

    info = make_gating_project(tmp_path)
    session = AgentSession()
    sid = start(session, markers=["CD3", "CD8"])["session_id"]
    packet = _to_display(session, sid, Oracle(info))
    answer(session, sid, packet, Oracle(info).answer(packet))
    record = calibration.load("gsynth")
    assert set(record["choices"]) == {"CD3", "CD8"}
    assert calibration.observed(record, "CD8") is None


# -- the use ----------------------------------------------------------------------------


def _session_with(tmp_path, observations, *, stained_below=True):
    """A session whose contrast check saw CD8's dim cells stained (the
    highest truly negative cells, so below any sensible gate) and two of its
    brightest as debris; returns (session, sid, info)."""
    from plexora.agent.evidence import calibration

    info = make_gating_project(tmp_path)
    session = AgentSession()
    record, _ = calibration.calibrate(session, "gsynth")
    negatives = sorted((c for c in info["cells"] if c["id"] not in info["truth"]["CD8"]),
                       key=lambda c: c["CD8"])
    dim = [{"id": f"d{i + 1}", "cell_id": c["id"], "x": c["x"], "y": c["y"],
            "value": c["CD8"], "stained": stained_below and i < 3}
           for i, c in enumerate(negatives[-4:])]
    bright = [{"id": f"b{i + 1}", **{k: c[k] for k in ("x", "y")}, "cell_id": c["id"],
               "value": c["CD8"], "artifact": i < 2}
              for i, c in enumerate(sorted(info["cells"], key=lambda c: -c["CD8"])[:6])]
    choices = {name: {"column": "c3", "gain": 1.0, "band": "negatives", "chosen_by": "vision"}
               for name in ("CD3", "CD8")}
    choices["CD8"]["observed"] = {"pattern_seen": "membrane", "stain_location": "on_cells",
                                  "bright": bright, "dim": dim}
    calibration.record_choices("gsynth", choices)
    sid = start(session, markers=["CD3", "CD8"], observations=observations)["session_id"]
    return session, sid, info


def _first_look(session, sid, info, marker):
    from tests.test_gating_session import first_packet_for

    return first_packet_for(session, sid, marker, info)


def _unit(sid, marker):
    from plexora.plugins.gating.server.autogate import engine

    record = engine.store().load(sid)
    return record["units"][engine.unit_key("gsynth", marker)]


def test_off_the_session_is_todays(tmp_path):
    session, sid, info = _session_with(tmp_path, "off")
    seen = drive(session, sid, Oracle(info))
    assert not any("observed" in (p.get("evidence") or {}) for p in seen)
    unit = _unit(sid, "CD8")
    assert "observed" not in unit and "observed" not in (unit.get("start") or {})
    assert "bright_artifacts" not in unit["flags"]
    assert not any(c["id"] == "obs:dim" for c in (unit.get("scoring") or {}).get("candidates")
                   or [])


def test_on_the_looks_are_told_and_the_start_comes_down(tmp_path):
    session, sid, info = _session_with(tmp_path, "use")
    packet = _first_look(session, sid, info, "CD8")
    assert packet["kind"] == "t2_confirm"
    observed = packet["evidence"]["observed"]
    assert observed["pattern_seen"] == "membrane"
    assert observed["dim"].startswith("3 of 4 dim cells stained")
    assert observed["bright"] == "2 of 6 of the brightest look like artifacts"
    assert "observed" in packet["evidence"]["guide"]
    unit = _unit(sid, "CD8")
    assert unit["start"]["observed"] == "dim"
    assert unit["start"]["low"] < unit["start"]["before"]
    assert any(c["id"] == "obs:dim" for c in unit["scoring"]["candidates"])
    assert "bright_artifacts" in unit["flags"]
    assert "dim cells stained" in unit["observed"]["line"]


def test_on_a_clean_split_with_a_finding_gets_a_look(tmp_path):
    session, off, _info = _session_with(tmp_path, "off")
    unit = _unit(off, "CD8")
    # The fixture's CD8 is a clean split: accepted for the audit sheet when off.
    assert unit["t1"]["accept"] and unit["state"] == "accepted_t1"
    ok(invoke(session, "gating_session_finish", {"session_id": off, "action": "close"}))
    on = start(session, markers=["CD3", "CD8"], observations="use")["session_id"]
    unit = _unit(on, "CD8")
    assert unit["t1"]["accept"] and unit.get("observed_routed")
    assert unit["state"] == "awaiting_t2"
    assert set(unit["observed"]["findings"]) >= {"dim_below", "bright_artifacts"}


def test_a_doubt_caps_confidence():
    from plexora.plugins.gating.server.autogate import engine

    unit = {"path": "t2", "ai_confidence": 0.9, "metrics": {"d": 5.0}, "flags": []}
    assert engine.confidence_for(unit) == "high"
    for flag in ("bright_artifacts", "observed_off_pattern"):
        assert engine.confidence_for({**unit, "flags": [flag]}) == "moderate"


def test_off_pattern_follows_the_compartment(tmp_path):
    from plexora.plugins.gating.server.autogate import engine as engines

    session, sid, _info = _session_with(tmp_path, "use")
    with engines.engine_for(_call(session), sid, save=False) as engine:
        unit = dict(engine.record["units"][engines.unit_key("gsynth", "CD8")])
        for compartment, pattern, location, off in (
                ("membrane", "membrane", "on_cells", False),
                ("membrane", "nuclear", "on_cells", True),
                ("nuclear", "fibrous", "on_cells", True),
                ("cytoplasmic", "granular", "on_cells", False),
                ("membrane", "diffuse", "between_cells", True),
                ("extracellular", "diffuse", "between_cells", False),
                (None, "nuclear", "on_cells", False)):
            record = engine._calibration_of("gsynth")
            record["choices"]["CD8"]["observed"].update(pattern_seen=pattern,
                                                       stain_location=location)
            unit["context"] = {"compartment": compartment}
            assert engine.observed_for(unit)["off_pattern"] is off, (compartment, pattern)


def _call(session):
    from plexora.agent.audit import AuditLog
    from plexora.agent.policy import Policy
    from plexora.agent.receipts import operation_id
    from plexora.agent.registry import Call

    return Call(capability=registry.get("gating.answer"), session=session, policy=Policy(),
                operation_id=operation_id(), audit=AuditLog(), arguments={})


def test_the_fields_keep_away_from_debris_and_show_a_dim_stained_cell():
    from plexora.agent import gate_sampling

    rng = np.random.default_rng(0)
    n = 4000
    values = np.where(rng.random(n) < 0.2, 50.0, 5.0) + rng.normal(0, 0.5, n)
    data = FakeData({"M": values}, seed=1, extent=2000.0)
    plain = gate_sampling.sample_gate_validation_regions(
        data, "M", 20.0, 100.0, field_px=200, image_size=(2000, 2000), n_per_class=1)
    best = next(f for f in plain["fields"] if f["class"] == "clear_positive")
    b = best["bounds"]
    box = (b["x"], b["y"], b["x"] + b["width"], b["y"] + b["height"])
    avoided = gate_sampling.sample_gate_validation_regions(
        data, "M", 20.0, 100.0, field_px=200, image_size=(2000, 2000), n_per_class=1,
        avoid=[box], around=[(1500.0, 1500.0)])
    positive = next(f for f in avoided["fields"] if f["class"] == "clear_positive")
    assert positive["bounds"] != best["bounds"]
    dim = next(f for f in avoided["fields"] if f["class"] == "dim_stained")
    assert dim["center"] == [1500.0, 1500.0]


def test_observations_default_off_and_keep_their_answers_apart():
    from types import SimpleNamespace

    from plexora.plugins.gating import capabilities_session as cs
    from plexora.plugins.gating.server.autogate import memo
    from plexora.plugins.gating.server.autogate.engine import Engine

    assert cs.SessionOptions().observations == "off"
    assert cs.option_defaults()["observations"] == "off"
    packet = {"kind": "t2_confirm", "evidence": {"project": "p", "marker": "CD8"}}

    def key(mode):
        fake = SimpleNamespace(options={"tiles": False, "observations": mode})
        fake.tiles_on = lambda: Engine.tiles_on(fake)
        fake.observations_on = lambda: Engine.observations_on(fake)
        fake.vision_first_on = lambda: False
        fake.partners_bands_on = fake.shapes_on = fake.reference_on = lambda: False
        fake.observed_digest = lambda project: "abc"
        return Engine.memo_key(fake, packet, [])

    assert key("off") == memo.key(packet, [], versions=())
    assert key("use") != key("off")
