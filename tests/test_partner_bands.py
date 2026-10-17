"""Partner bands: where a marker's background ends, from its partners' own cells.

No partner gate is needed: each partner's own mixture says which of its
cells are surely positive and surely negative, and the marker's values among
them give a floor (cells that cannot carry it), an anchor (where a superset
partner's bright cells split it) and a sensitivity bound. Several partners
vote; the median is used. Off (`partners: gated`), gating is as it was.
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


#: log1p means per phenotype: CD8 T, CD4 T, B, other (as the synthetic scene).
LEVELS = {"CD3": (7.2, 7.0, 4.2, 4.0), "CD8": (7.4, 4.1, 4.0, 4.2),
          "CD20": (4.0, 4.1, 7.3, 4.0)}


def _panel_data(n=40_000, seed=0, spill=None):
    rng = np.random.default_rng(seed)
    kind = rng.choice(4, size=n, p=[0.15, 0.2, 0.15, 0.5])
    columns = {}
    for marker, means in LEVELS.items():
        logs = np.asarray(means)[kind] + rng.normal(0, 0.18, n)
        columns[marker] = np.expm1(logs).astype(np.float32)
    if spill:
        # An exclusive partner whose bright cells are lit by this marker's spill.
        logs = np.where(columns["CD8"] > np.expm1(6.0), 7.3, 4.0) + rng.normal(0, 0.18, n)
        columns["SPILL"] = np.expm1(logs).astype(np.float32)
    return FakeData(columns, seed=seed), kind


def _partners(*rows):
    return [{"marker": m, "relation": r, "direction": d} for m, r, d in rows]


def test_floors_from_every_partner_and_an_anchor_from_the_superset():
    from plexora.plugins.gating.server.autogate import bands

    ds, _kind = _panel_data()
    out = bands.partner_bands(ds, "CD8", _partners(("CD3", "subset", "forward"),
                                                   ("CD20", "exclusive", "forward")))
    rows = {r["marker"]: r for r in out["partners"]}
    assert rows["CD3"]["sure"]["rule"] == "mixture"
    assert "floor" in rows["CD3"] and "anchor" in rows["CD3"] and "sens" not in rows["CD3"]
    assert "floor" in rows["CD20"] and "anchor" not in rows["CD20"]
    consensus = out["consensus"]
    assert consensus["floor"]["from"] == ["CD20", "CD3"]
    # The floor sits over the CD8-negative cells (4.0-4.2), the anchor between
    # the CD8- and CD8+ T cells (4.1 vs 7.4), in log1p units.
    assert 4.3 < consensus["floor"]["fit"] < 4.9
    assert 4.9 < consensus["anchor"]["fit"] < 6.6
    assert consensus["floor"]["fit"] < consensus["anchor"]["fit"]
    assert abs(consensus["anchor"]["low"] - float(np.expm1(consensus["anchor"]["fit"]))) < 1e-3
    assert out["flags"] == []
    ids = [c[0] for c in bands.candidates(out)]
    assert {"panchor", "pfloor", "anchor:CD3", "floor:CD3", "floor:CD20"} <= set(ids)


def test_a_child_gives_a_sensitivity_bound():
    from plexora.plugins.gating.server.autogate import bands

    ds, _kind = _panel_data()
    out = bands.partner_bands(ds, "CD3", _partners(("CD8", "subset", "reverse")))
    sens = out["consensus"]["sens"]
    assert sens["from"] == ["CD8"]
    assert 6.6 < sens["fit"] < 7.2          # the low end of CD3 on CD8 T cells


def test_partners_that_disagree_are_flagged_and_never_lift():
    from plexora.plugins.gating.server.autogate import bands, scoring

    ds, _kind = _panel_data(spill=True)
    out = bands.partner_bands(ds, "CD8", _partners(("CD20", "exclusive", "forward"),
                                                   ("SPILL", "exclusive", "forward")))
    assert "partners_disagree" in out["flags"]
    scored = scoring.score(ds, "CD8", partners=(), bands=bands.compact(out))
    assert "pfloor" not in str(scored["proposal_source"])


def test_without_bands_the_scoring_is_as_it_was():
    from plexora.plugins.gating.server.autogate import bands, scoring

    ds, _kind = _panel_data()
    plain = scoring.score(ds, "CD8", partners=())
    assert "bands" not in plain
    assert not any(c["family"] in ("partner_anchor", "partner_floor")
                   or c["id"].startswith(("panchor", "pfloor", "psens"))
                   for c in plain["candidates"])
    held = bands.compact(bands.partner_bands(ds, "CD8", _partners(
        ("CD3", "subset", "forward"), ("CD20", "exclusive", "forward"))))
    banded = scoring.score(ds, "CD8", partners=(), bands=held)
    assert banded["bands"]["fingerprint"] == held["fingerprint"]
    assert banded["leading"] == "partners"
    assert {"panchor", "pfloor"} <= {c["id"] for c in banded["candidates"]}
    # The cache keeps the two apart.
    assert scoring.score(ds, "CD8", partners=()) == plain


def test_the_lattice_takes_the_band_points_and_a_chain_can_aim_at_one():
    from plexora.plugins.gating.server.autogate import bands, lattice, scoring
    from plexora.plugins.gating.server.autogate.engine import compact_scoring

    ds, _kind = _panel_data()
    held = bands.compact(bands.partner_bands(ds, "CD8", _partners(
        ("CD3", "subset", "forward"), ("CD20", "exclusive", "forward"))))
    scored = compact_scoring(scoring.score(ds, "CD8", partners=(), bands=held), [])
    lat = lattice.build(ds, "CD8", gmm=None, scoring=scored)
    anchor = lattice.point(lat, "panchor")
    assert anchor is not None
    top = max(p["low"] for p in lat["points"])
    aimed = lattice.chain(lat, top, "down", until=anchor["low"])
    assert aimed and aimed[-1]["low"] == anchor["low"] and len(aimed) <= lattice.MAX_CHAIN
    assert lattice.chain(lat, anchor["low"], "down", until=anchor["low"]) == []
    # Baseline families keep their order: a merged point's name is unchanged.
    order = lattice.PRIORITY
    assert order.index("ctrl") < order.index("within") < order.index("bio") \
        < order.index("score") < order.index("gmm")


def test_a_node_without_the_operation_is_answered_here(monkeypatch):
    from plexora.plugins.gating.server.autogate import tableops
    from plexora.server.providers import ResourceError

    ds, _kind = _panel_data(n=5000)
    ds.table.is_local = False

    def refuses(*_args, **_kw):
        raise ResourceError("data node 'o2' failed: 501 no table operation named "
                            "'gating.autogate.bands' is registered")

    monkeypatch.setattr(tableops, "local_or_node", refuses)
    result, where = tableops.local_or_node_or_fallback(ds, "gating.autogate.bands", {
        "marker": "CD8", "partners": _partners(("CD3", "subset", "forward"))})
    assert where == "primary" and result["consensus"]
    monkeypatch.setattr(tableops, "local_or_node", lambda *a, **k: (_ for _ in ()).throw(
        ResourceError("data node 'o2' disagrees about the request: bad")))
    with pytest.raises(ResourceError):
        tableops.local_or_node_or_fallback(ds, "gating.autogate.bands", {"marker": "CD8"})


def _bands_unit(anchor=None, floor=None, sens=None, gate_fit=None, flags=()):
    consensus = {}
    for kind, value in (("anchor", anchor), ("floor", floor), ("sens", sens)):
        if value is not None:
            consensus[kind] = {"fit": value, "low": float(np.expm1(value)), "from": ["P"]}
    return {"version": "1", "consensus": consensus, "flags": list(flags),
            "background": {"sd_fit": 0.2}, "gate_fit": gate_fit}


def test_the_partner_target_and_the_window():
    from plexora.plugins.gating.server.autogate import bands
    from plexora.plugins.gating.server.autogate.engine import Engine

    held = _bands_unit(anchor=5.0, floor=4.4, gate_fit=6.0)
    assert bands.window(held) == pytest.approx((4.0, 5.4))
    fake = SimpleNamespace(call=None)
    unit = {"bands": held, "gate_fit": 6.0}
    assert Engine.partner_sanity(fake, unit)                          # over the anchor
    unit["bands"] = _bands_unit(anchor=5.0, floor=4.4, gate_fit=5.2)
    assert Engine.partner_sanity(fake, unit) is None
    unit["bands"] = _bands_unit(anchor=4.0, floor=4.4, gate_fit=9.0,
                                flags=["floor_above_anchor"])
    assert Engine.partner_sanity(fake, unit) is None                  # no verdict


def test_the_partner_look_is_one_code_move_and_its_rows_decide():
    from plexora.plugins.gating.server.autogate import transitions

    logged = []
    target = {"point": "panchor", "low": 100.0, "fit": 4.6, "direction": "down",
              "from": ["CD3"]}
    engine = SimpleNamespace(
        options={"max_tier": 4}, partners_bands_on=lambda: True,
        rounds_left=lambda unit: True, partner_target=lambda unit: target,
        log=lambda **e: logged.append(e))
    unit = {"project": "p", "marker": "CD8", "candidate": 400.0, "state": "awaiting_t2"}
    outcome = transitions._partner_look(engine, unit, "about_right")
    assert outcome["partner_look"] == "panchor" and unit["state"] == "awaiting_t4"
    assert unit["direction"] == "down" and unit["code_moves"] == ["down:partners"]
    assert "directions" not in unit                                   # not a look's turn
    assert transitions._partner_look(engine, unit, "about_right") is None   # once
    for direction in ("up", "down", "no_positives"):
        assert transitions._partner_look(engine, {"marker": "X"}, direction) is None
    engine.partners_bands_on = lambda: False
    assert transitions._partner_look(engine, {"marker": "X"}, "cannot_tell") is None


def test_a_vision_session_draws_bands_and_runs_to_the_end(tmp_path):
    from plexora.agent import AgentSession
    from plexora.plugins.gating.server.autogate import engine

    info = make_gating_project(tmp_path, grid=24, size=1024)
    session = AgentSession()
    try:
        started = start(session, strategy="vision", markers=["CD3", "CD8", "CD20"])
        record = engine.store().load(started["session_id"])
        options = record["options"]
        assert options["partners"] == "bands" and options["shapes"] is True
        assert options["observations"] == "use" and options["vision_first"] is True
        assert options["reference_sheet"] == "auto"
        assert (record.get("reference") or {}).get("status") in ("none", "on")
        drive(session, started["session_id"], Oracle(info))
        record = engine.store().load(started["session_id"])
        states = {u["marker"]: u["state"] for u in record["units"].values()}
        assert all(s in engine.TERMINAL for s in states.values()), states
        drawn = [u for u in record["units"].values() if u.get("bands")]
        assert drawn, "no unit drew partner bands"
        for unit in drawn:
            assert unit["bands"].get("skipped") is None, unit["bands"]
    finally:
        session.close()


def test_an_explicit_option_beats_the_strategy(tmp_path):
    from plexora.plugins.gating.capabilities_session import SessionOptions, resolve_strategy
    from plexora.plugins.gating.server.autogate import schemas

    assert set(schemas.VISION_STRATEGY) <= set(SessionOptions.model_fields)
    plain = resolve_strategy(SessionOptions(project="p"))
    assert plain.partners == "gated" and plain.shapes is False
    vision = resolve_strategy(SessionOptions(project="p", strategy="vision", shapes=False))
    assert vision.shapes is False and vision.partners == "bands"
    assert resolve_strategy(vision) == vision                        # idempotent
