"""What the image checks sample and send on: places with nuclei only, one-cycle
places as tissue loss (not misregistration), the reviewed class carried, a
registration crop that shows the two cycles, and the probe rule that spares
looks at a check's look-alike regions."""

import numpy as np
import pytest

from plexora.agent import AgentSession, invoke, registry
from tests.qc_fixtures import make_qc_project
from tests.test_qc_session import QCOracle, drive, ok, start

pytestmark = pytest.mark.paid


@pytest.fixture(autouse=True)
def _qc():
    registry.discover(["roi", "qc"])


def _region(info, name):
    return next(r for r in info["truth"]["regions"] if r["name"] == name)


def _inside(mask, x, y):
    x, y = int(x), int(y)
    return 0 <= y < mask.shape[0] and 0 <= x < mask.shape[1] and bool(mask[y, x])


def _deep(mask, px):
    """The part of `mask` at least `px` from its edge: a place there shows
    nothing of the outside, whatever a smoothed map or a tile mixes in."""
    from scipy import ndimage

    return ndimage.binary_erosion(mask, iterations=int(px))


def _with_haze(monkeypatch):
    """Cycle 2's lost square keeps a faint, textured haze (autofluorescent
    debris, an uneven background): no nuclei, but bright enough and with
    enough coarse structure to pass the level and noise rules -- the black
    tiles of the live run."""
    from scipy import ndimage

    from plexora.ai import qc_scenes
    from tests import qc_fixtures

    def scene(**kwargs):
        image, labels, cells, channels, truth = qc_scenes.qc_scene(**kwargs)
        lost = next(r for r in truth["regions"] if r["name"] == "cycle_dropout")["mask"]
        rng = np.random.default_rng(1)
        texture = ndimage.gaussian_filter(rng.normal(0, 1, lost.shape), 6.0)
        texture = texture / max(1e-9, float(np.abs(texture).max()))
        haze = qc_scenes.BACKGROUND + 160.0 + 120.0 * texture
        index = list(channels).index("DNA_2")
        image[index][lost] = np.clip(haze[lost], 0, 65535).astype(np.uint16)
        return image, labels, cells, channels, truth

    monkeypatch.setattr(qc_fixtures, "qc_scene", scene)


def _record(sid):
    from plexora.plugins.qc.server.engine import store

    return store().load(sid)


# -- 1. Blur: a nucleus-free place is never scored nor sampled ------------------------------


def test_blur_strata_never_sample_a_place_without_nuclei(tmp_path, monkeypatch):
    """Cycle 2 lost a square of tissue: Nucleus2 there is a haze without
    nuclei. It scored as the most blurred tissue of the image and every
    just-above tile of the live run was black."""
    from plexora.plugins.qc.server import blur, score_fields

    _with_haze(monkeypatch)
    info = make_qc_project(tmp_path, artifacts=("blur_local", "cycle_dropout"))
    lost = _region(info, "cycle_dropout")["mask"]
    deep = _deep(lost, 20)
    summary, _ = blur.load_or_run(AgentSession(), "qcsynth", channel="DNA_2")
    assert summary["nuclear"]["foreground_level"] is not None
    assert summary["nuclear"]["cells_without_nuclei"] > 0
    arrays = blur.load_arrays("qcsynth", summary["fingerprint"])
    s = summary["grid"]["cell_full_px"]
    evaluable = np.asarray(arrays["evaluable"], dtype=bool)
    ny, nx = evaluable.shape
    whole = [(iy, ix) for iy in range(ny) for ix in range(nx)
             if lost[int(iy * s):int((iy + 1) * s), int(ix * s):int((ix + 1) * s)].all()]
    assert whole and not any(evaluable[iy, ix] for iy, ix in whole)
    field = score_fields.from_blur(summary, arrays)
    floor = field.sample_floor()
    assert floor is not None and floor >= summary["params"]["min_nuclear_fraction"]
    # A bar low enough that every row has places to pick from.
    bar = score_fields.bar(field, -3)
    for threshold in (bar["value"], score_fields.bar(field, 3)["value"]):
        found = score_fields.regions(field, threshold, min_cells=1)
        strata = score_fields.sample_strata(field, threshold, bar["step"], found)
        places = [p for row in strata.values() for p in row]
        assert places
        for place in places:
            assert not _inside(deep, place["x"], place["y"]), place
            iy, ix = place["cell"]
            assert field.content[iy, ix] >= floor


def test_sample_strata_skip_cells_without_content():
    from plexora.plugins.qc.server import score_fields

    values = np.full((30, 30), 0.1)
    values[:10, :10] = 0.9                    # high score...
    content = np.full((30, 30), 0.3)
    content[:10, :10] = 0.0                   # ...where there are no nuclei
    values[20:25, 20:25] = 0.8
    field = score_fields.ScoreField(
        check="blur", values=values, weight=np.ones_like(values),
        grid={"x0": 0.0, "y0": 0.0, "step": 10.0, "nx": 30, "ny": 30,
              "image_size": [300, 300]},
        fingerprint="fp", auto_threshold=0.5, cell_um=10.0, pixel_um=1.0,
        content=content, content_floor=0.02)
    found = score_fields.regions(field, 0.5, min_cells=4)
    strata = score_fields.sample_strata(field, 0.5, 0.1, found)
    cells = [tuple(p["cell"]) for row in strata.values() for p in row]
    assert cells and not any(iy < 10 and ix < 10 for iy, ix in cells)
    # The nucleus-free region still exists (it is scored), but its peak is
    # never a place to look at without content, and no row samples it.
    assert strata.get("strongly_abnormal")


# -- 2. Registration: one cycle only is tissue loss, not misregistration -----------------


@pytest.mark.parametrize("haze", [False, True])
def test_one_cycle_places_are_not_registration_scores(tmp_path, monkeypatch, haze):
    from plexora.plugins.qc.server import check_candidates, registration, score_fields

    if haze:
        _with_haze(monkeypatch)
    info = make_qc_project(tmp_path, artifacts=("cycle_dropout",), table=False, mask=False)
    lost = _region(info, "cycle_dropout")["mask"]
    deep = _deep(lost, 12)
    session = AgentSession()
    out = registration.compute(session, "qcsynth", registration.load_state("qcsynth"),
                               comparison="DNA_2", include_overlay=False)
    entry = registration.field_entry("qcsynth", out["field_fingerprint"])
    field = score_fields.from_registration(
        entry, pixel_um=1.0, fingerprint=out["field_fingerprint"], image_size=[1024, 1024],
        reference=out["reference"], comparison=out["comparison"], stats=out["stats"])
    # No map cell of the lost square is scored.
    finite = np.argwhere(np.isfinite(field.values))
    assert finite.size
    assert not any(_inside(deep, *field.centre(iy, ix)) for iy, ix in finite)
    for offset in (-3, 0, 3):
        bar = score_fields.bar(field, offset)
        found = score_fields.regions(field, bar["value"])
        strata = score_fields.sample_strata(field, bar["value"], bar["step"], found)
        for row in strata.values():
            for place in row:
                assert not _inside(deep, place["x"], place["y"]), place
    # The same square is the one-cycle signal: lost in the comparison.
    split = check_candidates.one_cycle_found(
        entry, pixel_um=1.0, fingerprint=out["field_fingerprint"], image_size=[1024, 1024],
        reference=out["reference"], comparison=out["comparison"])
    assert split and split[0][0] == "comparison"
    region = split[0][2]["regions"][0]
    assert _inside(lost, *region["peak"])


def test_a_lost_square_becomes_a_tissue_loss_candidate_of_its_cycle(tmp_path):
    info = make_qc_project(tmp_path, artifacts=("cycle_dropout",))
    lost = _region(info, "cycle_dropout")["mask"]
    deep = _deep(lost, 12)
    session = AgentSession()
    started = start(session)
    sid = started["session_id"]
    record = _record(sid)
    check = next(u for u in record["units"].values()
                 if u["type"] == "check" and u["check"] == "registration")
    assert check["one_cycle"]["n_regions"] >= 1
    loss = [u for u in record["units"].values() if u["type"] == "candidate"
            and u.get("detector") == "registration" and u.get("one_cycle")]
    assert loss
    for unit in loss:
        assert unit["class_hint"] == "cycle_specific_tissue_loss"
        assert unit["scope_hint"] == "cycle" and unit["cycles"] == [2]
        assert unit["one_cycle"]["lost_in"] == "comparison"
    assert any(_inside(lost, *u["peak"]) for u in loss)
    drive(session, sid, QCOracle(info))
    reviews_shown = [u for u in _record(sid)["units"].values()
                     if u["type"] == "check" and u["check"] == "registration"]
    for unit in reviews_shown:
        for row in ((unit.get("shown") or {}).get("places") or {}).values():
            for place in row:
                assert not _inside(deep, place["x"], place["y"])
    final = _record(sid)["units"]
    written = [u for u in final.values() if u["type"] == "candidate"
               and u.get("one_cycle") and u.get("class")]
    assert written and all(u["class"] == "cycle_specific_tissue_loss" for u in written)
    assert not any(u.get("class") == "cross_cycle_registration_error"
                   and _inside(deep, *u["peak"]) for u in final.values()
                   if u["type"] == "candidate")


# -- 3. The reviewed class goes with every region the settle creates ----------------------


class _SaysDebris(QCOracle):
    """Judges the blur check's rows `verdict`, and names what it saw: debris."""

    def __init__(self, info, verdict):
        super().__init__(info)
        self.verdict = verdict

    def _score_review(self, unit, ev):
        answer = super()._score_review(unit, ev)
        if unit["check"] == "blur":
            answer["strata"] = {k: self.verdict for k in answer["strata"]}
            answer.update(artifact_class="debris_or_foreign_object", severity="minor")
        return answer


def _to_first_review(session, sid, agent):
    result = ok(invoke(session, "qc_next", {"session_id": sid, "wait_s": 20}))
    while result["state"] == "decision" and not (
            result["packet"]["kind"] == "score_review"
            and result["packet"]["evidence"]["check"] == "blur"):
        packet = result["packet"]
        result = ok(invoke(session, "qc_answer", {"session_id": sid,
                                                  "packet_id": packet["packet_id"],
                                                  "answer": agent.answer(packet, sid)}))["next"]
    return result["packet"]


@pytest.mark.parametrize("verdict, state", [("mixed", "awaiting_confirm"),
                                            ("cannot_tell", "manual_review_recommended"),
                                            ("artifact", None)])
def test_the_reviewed_class_is_carried_to_every_region(tmp_path, verdict, state):
    info = make_qc_project(tmp_path, artifacts=("blur_local",))
    session = AgentSession()
    sid = start(session)["session_id"]
    agent = _SaysDebris(info, verdict)
    packet = _to_first_review(session, sid, agent)
    unit_id = packet["units"][0]["id"]
    ok(invoke(session, "qc_answer", {"session_id": sid, "packet_id": packet["packet_id"],
                                     "answer": agent.answer(packet, sid)}))
    made = [u for u in _record(sid)["units"].values() if u.get("check_unit") == unit_id]
    assert made
    for unit in made:
        assert unit["class_hint"] == "debris_or_foreign_object"
        assert unit["review_hint"] == {"artifact_class": "debris_or_foreign_object",
                                       "severity": "minor", "source": "score_review"}
        if state:
            assert unit["state"] == state
        else:
            assert unit["class"] == "debris_or_foreign_object"
            assert unit["decision"]["severity"] == "minor"


# -- 4. A registration region's close crop shows the two cycles ----------------------------


def _registration_candidate(tmp_path):
    from plexora.plugins.qc.server import registration, scan as scan_mod, score_fields
    from plexora.server.utils import pixel_scale

    info = make_qc_project(tmp_path, artifacts=("misregistration",), table=False, mask=False)
    session = AgentSession()
    out = registration.compute(session, "qcsynth", registration.load_state("qcsynth"),
                               comparison="DNA_2", include_overlay=False)
    entry = registration.field_entry("qcsynth", out["field_fingerprint"])
    field = score_fields.from_registration(
        entry, pixel_um=1.0, fingerprint=out["field_fingerprint"], image_size=[1024, 1024],
        reference=out["reference"], comparison=out["comparison"], stats=out["stats"])
    bar = score_fields.bar(field)
    found = score_fields.regions(field, bar["value"])
    assert found["regions"]
    region = found["regions"][0]
    iy, ix = region["peak_cell"]
    assert field.content[iy, ix] >= field.sample_floor()
    scan, _ = scan_mod.load_or_run(session, "qcsynth")
    pixel = pixel_scale.pixel_size(session.project("qcsynth"))
    candidate = {"id": "cand_reg", "label": "c1", "origin": "check", "detector": "registration",
                 "channel": "DNA_2", "channels": ["DNA_2", "CD20"], "reference": "DNA_1",
                 "class_hint": "cross_cycle_registration_error", "primary_metric": "",
                 "peak": list(region["peak"]), "bbox": list(region["bbox"]),
                 "variants": {"standard": {"geometry": region["geometry"]}}}
    from plexora.plugins.qc.server import polygons

    mask = polygons.geometry_to_grid(region["geometry"], scan.grid, touch=True)
    from plexora.agent.evidence import calibration as display

    # The session's own windows: fixed per channel, so glass stays dark.
    calibration = display.current(session, "qcsynth", [c["name"] for c in scan.channels])
    return info, session, scan, pixel, candidate, mask, calibration


def _coloured(picture):
    data = np.asarray(picture)[..., :3].astype(int)
    red = (data[..., 0] >= 60) & (data[..., 0] > data[..., 1] + 30)
    green = (data[..., 1] >= 60) & (data[..., 1] > data[..., 0] + 30)
    return float(red.mean()), float(green.mean())


def test_a_registration_crop_is_the_two_cycles_and_never_black(tmp_path):
    from plexora.plugins.qc.server import sheets

    _info, session, scan, pixel, candidate, mask, calibration = \
        _registration_candidate(tmp_path)
    (picture, _manifest), caption = sheets._close_crop(
        session, "qcsynth", scan, candidate, mask, "DNA_2", 200.0, sheets.BATCH_PX,
        pixel=pixel, calibration=calibration)
    assert "DNA_1 red" in caption and "DNA_2 green" in caption
    assert sheets.visible(picture)
    assert float(np.asarray(picture).std()) > 5.0
    red, green = _coloured(picture)
    assert red > 0.005 and green > 0.005          # the crescents of a shift
    # A peak on the glass: the crop moves to the region's strongest tissue.
    assert calibration and calibration.get("channels")
    on_glass = {**candidate, "peak": [25.0, 25.0]}
    channels, _words = sheets._crop_channels(candidate, scan, "DNA_2", calibration)
    glass, _m = sheets._draw(session, "qcsynth", scan,
                             sheets.square_around(25.0, 25.0, 50.0, [1024, 1024]), channels,
                             sheets.BATCH_PX, pixel=pixel)
    assert not sheets.visible(glass)               # what the live run sent
    (picture, _m), caption = sheets._close_crop(
        session, "qcsynth", scan, on_glass, mask, "DNA_2", 50.0, sheets.BATCH_PX,
        pixel=pixel, calibration=calibration)
    assert sheets.visible(picture) and "strongest tissue" in caption
    # The whole first-look row draws, its map panel not black either.
    sheet = sheets.confirm_batch_sheet(session, "qcsynth", scan, [candidate], [mask],
                                       fmt="png", pixel=pixel, calibration=calibration,
                                       store=False)
    captions = [p["caption"] for p in sheet["manifest"]["rows"][0]["panels"]]
    assert any("red" in c and "green" in c for c in captions)
    heat = sheets._map_panel(scan, candidate, mask, 64)
    assert sheets.visible(heat)


# -- 5. The probe rule ----------------------------------------------------------------------


class _Engine:
    """Just what the probe rule touches of a QCEngine."""

    def __init__(self):
        self.record = {"units": {}}

    def unit_key_of(self, ref):
        return f"{ref['project']}::{ref['type']}::{ref['id']}"

    def add(self, unit):
        self.record["units"][self.unit_key_of(unit)] = unit
        return unit

    def units_of(self, type_, project=None):
        return [u for u in self.record["units"].values() if u["type"] == type_
                and u["project"] == (project or "p")]

    def close(self, unit, state, reason):
        unit.update(state=state, reason=reason)

    def decide(self, unit):
        unit["class"] = unit["decision"]["artifact_class"]
        unit["state"] = "confirmed_warn"

    def mask_of(self, unit):
        return np.zeros((4, 4), dtype=bool)


def _group(n=7):
    engine = _Engine()
    check = engine.add({"type": "check", "project": "p", "id": "registration:DNA_2",
                        "check": "registration", "channel": "DNA_2"})
    units = [engine.add({"type": "candidate", "project": "p", "id": f"c{i}",
                         "origin": "check", "check_unit": check["id"],
                         "score": 0.9 - 0.05 * i, "state": "awaiting_confirm",
                         "class_hint": "cross_cycle_registration_error",
                         "scope_hint": "cycle"})
             for i in range(n)]
    return engine, check, units


@pytest.fixture
def _no_result(monkeypatch):
    from plexora.plugins.qc.server import checks_result

    monkeypatch.setattr(checks_result, "record", lambda engine, unit: None)


def _judge(units, state, klass=None, severity="moderate"):
    for unit in units:
        unit["state"] = state
        if klass:
            unit["class"] = klass
            unit["decision"] = {"verdict": "artifact", "artifact_class": klass,
                                "severity": severity}


def test_probes_are_the_strongest_and_the_rest_wait(_no_result):
    from plexora.plugins.qc.server import check_candidates, schemas

    engine, check, units = _group()
    k = schemas.ENGINE["check_confirm_probe"]
    group = check_candidates.hold_for_probes(engine, check, units)
    assert group["probes"] == [u["id"] for u in units[:k]]
    assert group["held"] == [u["id"] for u in units[k:]]
    assert check["confirm_groups"]["registration:DNA_2:confirm"] is group
    # Probes still open: the rest wait.
    assert all(check_candidates.still_held(engine, u) for u in units[k:])
    _judge(units[:k - 1], "dismissed")
    assert check_candidates.settle_held(engine, "p") == 0
    assert all(u.get("held_for") for u in units[k:])
    # A group no larger than the probes is confirmed as it is.
    small_engine, small_check, small = _group(k)
    assert check_candidates.hold_for_probes(small_engine, small_check, small) is None
    assert not any(u.get("held_for") for u in small)


def test_all_probes_not_artifact_dismiss_the_rest_as_extrapolated(_no_result):
    from plexora.plugins.qc.server import check_candidates, schemas

    engine, check, units = _group()
    k = schemas.ENGINE["check_confirm_probe"]
    check_candidates.hold_for_probes(engine, check, units)
    _judge(units[:k], "dismissed")
    assert check_candidates.settle_held(engine, "p") == len(units) - k
    for unit in units[k:]:
        assert unit["state"] == "dismissed" and not unit.get("held_for")
        assert unit["extrapolated"]["rule"] == "check_confirm_probe"
        assert unit["extrapolated"]["probes"] == [u["id"] for u in units[:k]]
        assert unit["extrapolated"]["verdict"] == "not_artifact"
    group = check["confirm_groups"]["registration:DNA_2:confirm"]
    assert group["state"] == "extrapolated" and group["carried"] == len(units) - k
    assert check["regions"]["extrapolated"] == len(units) - k


def test_all_probes_one_class_decide_the_rest_with_it(_no_result):
    from plexora.plugins.qc.server import check_candidates, schemas

    engine, check, units = _group()
    k = schemas.ENGINE["check_confirm_probe"]
    check_candidates.hold_for_probes(engine, check, units)
    _judge(units[:k], "confirmed_warn", "cycle_specific_tissue_loss", severity="severe")
    check_candidates.settle_held(engine, "p")
    for unit in units[k:]:
        assert unit["state"] == "confirmed_warn"
        assert unit["class"] == "cycle_specific_tissue_loss"
        decision = unit["decision"]
        assert decision["source"] == "extrapolated" and decision["severity"] == "severe"
        assert decision["extrapolated_from"] == [u["id"] for u in units[:k]]
        assert decision["scope"] == "cycle"


@pytest.mark.parametrize("states", [
    [("dismissed", None), ("confirmed_warn", "cross_cycle_registration_error")],
    [("confirmed_warn", "cross_cycle_registration_error"),
     ("confirmed_warn", "cycle_specific_tissue_loss")],
    [("dismissed", None), ("manual_review_recommended", None)],
])
def test_any_disagreement_releases_the_rest_one_by_one(_no_result, states):
    from plexora.plugins.qc.server import check_candidates, schemas

    engine, check, units = _group()
    k = schemas.ENGINE["check_confirm_probe"]
    check_candidates.hold_for_probes(engine, check, units)
    first, second = states
    _judge(units[:k - 1], first[0], first[1])
    _judge(units[k - 1:k], second[0], second[1])
    check_candidates.settle_held(engine, "p")
    for unit in units[k:]:
        assert unit["state"] == "awaiting_confirm" and not unit.get("held_for")
        assert not unit.get("extrapolated")
    assert check["confirm_groups"]["registration:DNA_2:confirm"]["state"] == "released"
    assert not any(check_candidates.still_held(engine, u) for u in units[k:])


class _MixedChecks(QCOracle):
    def _score_review(self, unit, ev):
        answer = super()._score_review(unit, ev)
        answer["strata"] = {k: "mixed" for k in answer["strata"]}
        return answer


def test_a_session_asks_the_probes_and_carries_their_verdict(tmp_path, monkeypatch):
    """The engine never asks a held region; once its probes are judged the
    rest are settled (or released) and the session finishes."""
    from plexora.plugins.qc.server import schemas

    monkeypatch.setitem(schemas.ENGINE, "check_confirm_probe", 1)
    # Small regions, unjoined, so a check has more of them than probes.
    monkeypatch.setitem(schemas.ENGINE, "score_min_region_cells",
                        {**schemas.ENGINE["score_min_region_cells"], "blur": 1,
                         "registration": 1})
    monkeypatch.setitem(schemas.ENGINE, "score_region_close_cells", {})
    info = make_qc_project(tmp_path, artifacts=("blur_local",))
    session = AgentSession()
    sid = start(session)["session_id"]
    packets = drive(session, sid, _MixedChecks(info))
    units = _record(sid)["units"]
    groups = [g for u in units.values() if u["type"] == "check"
              for g in (u.get("confirm_groups") or {}).values()]
    assert groups, "no check had more regions to confirm than probes"
    asked = {ref["id"] for p in packets if p["kind"] == "artifact_confirm"
             for ref in p["units"]}
    for group in groups:
        assert group["state"] in ("extrapolated", "released")
        assert set(group["probes"]) <= asked
        if group["state"] == "extrapolated":
            assert not set(group["held"]) & asked
            for cid in group["held"]:
                held = units[f"qcsynth::candidate::{cid}"]
                assert held["extrapolated"]["probes"] == group["probes"]
    assert all(u["state"] != "awaiting_confirm" for u in units.values()
               if u["type"] == "candidate")
