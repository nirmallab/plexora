"""The looks place a weak marker's gate (`SessionOptions.vision_first`).

On a marker whose distribution says little -- no valley, estimators that
disagree, an Auto gate calling nearly every cell positive -- the start no
longer sits at the noise ceiling: it comes down to the dim cells the
contrast check saw stained, else to the 15% rung; the candidates include
rungs by share of cells; cells below the gate seen mixed send it down; and
the distribution's and spatial whole-image checks no longer fail a gate the
looks placed. Off, the session is today's.
"""

from types import SimpleNamespace

import numpy as np
import pytest

from plexora.agent import registry
from tests.autogate_fixtures import FakeData, make_gating_project
from tests.test_gating_session import Oracle, drive, start

pytestmark = pytest.mark.paid


@pytest.fixture(autouse=True)
def _gating():
    registry.discover(["gating"])


def test_mixed_below_sends_the_gate_down_only_with_vision_first():
    from plexora.plugins.gating.server.autogate.verdicts import direction_from_rows

    rows = {"below": "mixed", "near": "mixed", "above": "own_stain"}
    assert direction_from_rows(rows)[0] == "about_right"
    assert direction_from_rows(rows, mixed_down=True)[0] == "down"
    # Background at the gate still says up; clean rows below are still about right.
    assert direction_from_rows({**rows, "near": "unstained"}, mixed_down=True)[0] == "up"
    assert direction_from_rows({**rows, "below": "unstained", "near": "unstained"},
                               mixed_down=True)[0] == "up"
    assert direction_from_rows({**rows, "below": "neighbour_only"},
                               mixed_down=True)[0] == "about_right"


def test_rungs_are_shares_of_cells():
    from plexora.plugins.gating.server.autogate import lattice
    from plexora.plugins.gating.server.autogate import profile as profmod

    values = np.random.default_rng(0).normal(5.0, 0.5, 5000)
    col = profmod.column(FakeData({"M": values}), "M")
    found = dict(lattice.rungs(col))
    assert list(found) == ["rung:40", "rung:25", "rung:15", "rung:8", "rung:3", "rung:1"]
    for name, value in found.items():
        share = float(name.split(":")[1]) / 100
        assert abs((values > value).mean() - share) < 0.002


def _fake_engine(vision_first=True):
    from plexora.plugins.gating.server.autogate.engine import Engine

    fake = SimpleNamespace(options={"vision_first": vision_first}, logged=[])
    fake.vision_first_on = lambda: Engine.vision_first_on(fake)
    fake.weak_distribution = lambda unit: Engine.weak_distribution(fake, unit)
    fake.clamp_to_judged = lambda unit, ds, low, why: low
    fake.log = lambda **event: fake.logged.append(event)
    return fake


def _weak_unit(start_low, anchor=None):
    candidates = [{"id": "ceiling", "low": start_low}]
    if anchor is not None:
        candidates.append({"id": "obs:dim", "low": anchor})
    return {"project": "p", "marker": "M", "class": "continuous", "flags": [],
            "summary": {"positive_fraction": 0.99}, "candidate": start_low,
            "start": {"source": "scoring", "low": start_low, "basis": "gmm+ceiling"},
            "scoring": {"candidates": candidates}}


def test_the_vision_start_takes_the_dim_cells_else_the_rung():
    from plexora.plugins.gating.server.autogate.engine import Engine

    values = np.random.default_rng(1).normal(5.0, 0.5, 5000)
    ds = FakeData({"M": values})
    rung15 = float(np.sort(values.astype(np.float32))[int(round(0.85 * 4999))])

    unit = _weak_unit(6.5, anchor=5.4)
    Engine.vision_start(_fake_engine(), unit, ds)
    assert unit["start"]["low"] == unit["candidate"] == 5.4
    assert unit["start"]["basis"] == "vision:obs" and unit["start"]["before"] == 6.5

    unit = _weak_unit(6.5)
    Engine.vision_start(_fake_engine(), unit, ds)
    assert unit["start"]["basis"] == "vision:rung:15"
    assert unit["candidate"] == pytest.approx(rung15)

    # A start already below the dim cells stays; off, nothing moves.
    unit = _weak_unit(5.0, anchor=5.4)
    Engine.vision_start(_fake_engine(), unit, ds)
    assert unit["start"]["low"] == 5.0 and "vision" not in unit["start"]
    unit = _weak_unit(6.5, anchor=5.4)
    Engine.vision_start(_fake_engine(vision_first=False), unit, ds)
    assert unit["start"]["low"] == 6.5

    # A distribution that places the gate is left to place it.
    unit = {**_weak_unit(6.5, anchor=5.4), "class": "bimodal",
            "summary": {"positive_fraction": 0.2}}
    Engine.vision_start(_fake_engine(), unit, ds)
    assert unit["start"]["low"] == 6.5


def test_vision_first_defaults_off_and_keeps_its_answers_apart():
    from plexora.plugins.gating import capabilities_session as cs
    from plexora.plugins.gating.server.autogate import memo
    from plexora.plugins.gating.server.autogate.engine import Engine

    assert cs.SessionOptions().vision_first is False
    assert cs.option_defaults()["vision_first"] is False
    packet = {"kind": "t2_confirm", "evidence": {"project": "p", "marker": "M"}}

    def key(on):
        fake = SimpleNamespace(options={"tiles": False, "observations": "off",
                                        "vision_first": on})
        fake.tiles_on = lambda: Engine.tiles_on(fake)
        fake.observations_on = lambda: Engine.observations_on(fake)
        fake.vision_first_on = lambda: Engine.vision_first_on(fake)
        return Engine.memo_key(fake, packet, [])

    assert key(False) == memo.key(packet, [], versions=())
    assert key(True) != key(False)


def test_a_vision_first_session_runs_to_the_end(tmp_path):
    from plexora.plugins.gating.server.autogate import engine

    info = make_gating_project(tmp_path, variant="flat")
    from plexora.agent import AgentSession

    session = AgentSession()
    try:
        sid = start(session, markers=["CD3", "CD8", "CD20"], vision_first=True,
                    observations="use")["session_id"]
        drive(session, sid, Oracle(info))
        record = engine.store().load(sid)
        states = {u["marker"]: u["state"] for u in record["units"].values()}
        assert all(s in engine.TERMINAL for s in states.values()), states
        lattices = [u.get("lattice") for u in record["units"].values() if u.get("lattice")]
        for unit in record["units"].values():
            if unit.get("lattice") and (unit.get("start") or {}).get("vision"):
                assert any(p["id"].startswith("rung:") or any(
                    s.startswith("rung:") for s in p["sources"])
                    for p in unit["lattice"]["points"])
        assert lattices or states
    finally:
        session.close()
