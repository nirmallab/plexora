"""Automatic gating estimates on the QC-passed cells only.

A fold, a blurred field or a misregistered patch QC has called a failure must
not pull the mixture fit, the strata, the galleries or the validation fields
toward itself (plexora/agent/cell_exclusions.py, plugins/qc/server/
exclusions.py). The gate that comes out still applies to every cell.
"""

import csv
import json

import numpy as np
import pytest

from plexora.agent import AgentSession, cell_exclusions, invoke, registry
from tests.autogate_fixtures import make_gating_project

BOX = (0, 0, 330, 330)


@pytest.fixture(autouse=True)
def _plugins():
    registry.discover(["roi", "qc", "gating"])
    cell_exclusions._reset_for_tests()
    yield
    cell_exclusions._reset_for_tests()


def ok(result):
    assert result["ok"], json.dumps(result.get("error"), default=str)[:4000]
    return result["result"]


def _box(x0, y0, x1, y1):
    return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1],
                                                [x0, y0]]]}


def _in_box(cell, box=BOX, margin=0):
    return box[0] + margin < cell["x"] < box[2] - margin \
        and box[1] + margin < cell["y"] < box[3] - margin


def _fold_project(tmp_path, marker="CD3", level=5.6):
    """A synthetic image whose corner is a "fold": every cell there reads an
    intermediate `marker` level, as cells in folded tissue read bright."""
    info = make_gating_project(tmp_path, grid=24, size=960)
    path = tmp_path / "_gsynth_files" / "cells.csv"
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    rng = np.random.default_rng(3)
    fold = set()
    for row in rows:
        if _in_box({"x": float(row["X_centroid"]), "y": float(row["Y_centroid"])}):
            row[marker] = f"{float(np.expm1(level + rng.normal(0, 0.15))):.4f}"
            fold.add(int(row["CellID"]))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    info["fold"] = fold
    return info


def _draw(session, name="QC: Tissue fold", box=BOX, roi_name=None):
    """A QC region drawn by hand -- and no refresh_qc: the calls are derived
    when gating first asks for them (`current`)."""
    args = {"project": "gsynth", "category": name, "geometry": _box(*box)}
    if roi_name:
        args["name"] = roi_name
    drawn = ok(invoke(session, "create_roi", args))
    assert cell_exclusions.current(session.data("gsynth")) is not None
    return drawn


def _left_out(mode="strict"):
    from plexora.plugins.qc.server import results

    cells = results.cells("gsynth")
    actions = ("exclude", "warn") if mode == "strict" else ("exclude",)
    return set(cells.filter(cells["action"].is_in(list(actions)))["cell_id"].to_list())


def _values(ds, marker):
    from plexora.agent.cell_exclusions import row_ids

    return (np.asarray(ds.table.columns([marker])[marker], dtype=np.float64),
            row_ids(ds))


def test_the_fit_ignores_cells_qc_failed_and_off_reproduces_the_bias(tmp_path):
    from plexora.plugins.gating.server import model

    info = _fold_project(tmp_path)
    session = AgentSession()
    _draw(session)
    ds = session.data("gsynth")
    with cell_exclusions.mode("off"):
        biased = model.fit_for(ds, "CD3")["gate"]
    clean = model.fit_for(ds, "CD3")["gate"]
    left = _left_out()
    # A hand-drawn region is in the calls without a refresh_qc.
    assert {c["id"] for c in info["cells"] if _in_box(c, margin=20)} <= left
    values, ids = _values(ds, "CD3")
    expected, _bg, _pos = model.auto_gate(values[~np.isin(ids, list(left))],
                                          ds.table.log_transformed)
    assert clean == pytest.approx(expected)
    allcells, _bg, _pos = model.auto_gate(values, ds.table.log_transformed)
    assert biased == pytest.approx(allcells)
    assert clean != pytest.approx(biased)

    truth = set(info["truth"]["CD3"])
    kept = ~np.isin(ids, list(left))

    def errors(gate):
        called = set(ids[kept & (values > gate)].tolist())
        return len(called ^ (truth & set(ids[kept].tolist())))

    assert errors(clean) <= errors(biased)


def test_results_say_which_cells_were_left_out(tmp_path):
    _fold_project(tmp_path)
    session = AgentSession()
    _draw(session)
    left = _left_out()
    auto = ok(invoke(session, "suggest_auto_gate", {"project": "gsynth", "marker": "CD3"}))
    block = auto["qc_exclusion"]
    assert block["applied"] and block["mode"] == "strict"
    assert block["n_left_out"] == len(left)
    summary = auto["summary_at_auto_gate"]
    # The gate applies to every cell; the fraction is of the QC-passed ones.
    assert summary["n_cells"] == ds_size(session)
    assert summary["n_qc_excluded"] == len(left)
    assert summary["n_finite"] == ds_size(session) - len(left)
    dist = ok(invoke(session, "get_marker_distribution", {"project": "gsynth",
                                                          "marker": "CD3"}))
    assert dist["stats"]["count"] == ds_size(session) - len(left)
    every = ok(invoke(session, "get_marker_distribution", {"project": "gsynth",
                                                           "marker": "CD3", "qc": "off"}))
    assert every["stats"]["count"] == ds_size(session)
    assert every["qc_exclusion"]["applied"] is False
    found = ok(invoke(session, "get_qc_exclusions", {"project": "gsynth"}))
    assert found["n_left_out"] == len(left)
    assert found["by_reason"].get("region:tissue_fold") == len(left)


def ds_size(session):
    return int(session.data("gsynth").table.geometry().height)


def test_samples_galleries_and_fields_never_draw_on_left_out_cells(tmp_path):
    from plexora.agent.gate_sampling import sample_gate_validation_regions
    from plexora.plugins.gating.server.autogate import sampler

    info = _fold_project(tmp_path)
    session = AgentSession()
    _draw(session)
    left = _left_out()
    ds = session.data("gsynth")
    gate = 300.0
    sample = sampler.stratified_cells(ds, "CD3", gate, n_per_stratum=12)
    drawn = {c["cell_id"] for cells in sample["strata"].values() for c in cells}
    drawn |= {c["cell_id"] for c in sample.get("inconsistent") or []}
    assert drawn and not drawn & left
    gallery = ok(invoke(session, "render_cell_gallery", {
        "project": "gsynth", "marker": "CD3", "select": "brightest", "n": 24,
        "low": gate}))
    shown = {t["cell_id"] for t in gallery["manifest"]["tiles"]}
    assert shown and not shown & left
    fields = sample_gate_validation_regions(
        ds, "CD3", gate, float(np.nanmax(_values(ds, "CD3")[0])), field_px=200,
        image_size=(960, 960), classes=("clear_positive", "borderline", "high_density"),
        n_per_class=3)
    assert fields["fields"]
    for field in fields["fields"]:
        total = field["cells"] + field["qc_left_out"]
        assert field["qc_left_out"] <= cell_exclusions.FIELD_QC_MAX_FRACTION * total
    assert fields["qc_exclusion"]["applied"]
    assert len(info["fold"]) > 20


def test_warn_regions_are_left_out_in_strict_mode_only(tmp_path):
    from plexora.plugins.gating.server import model

    _fold_project(tmp_path)
    session = AgentSession()
    _draw(session, roi_name="QC warn: tissue fold")
    warned = _left_out("strict") - _left_out("exclude")
    assert warned
    ds = session.data("gsynth")
    strict = model.gated_summary(ds, "CD3", 300.0)
    with cell_exclusions.mode("exclude"):
        lenient = model.gated_summary(ds, "CD3", 300.0)
    assert strict["n_qc_excluded"] == len(warned)
    # QC applies, and leaves out nothing: the region only warns.
    assert lenient["n_qc_excluded"] == 0
    assert lenient["n_finite"] == strict["n_cells"]


def test_a_marker_flag_leaves_cells_out_of_that_marker_only(tmp_path):
    from plexora.plugins.gating.server import model

    make_gating_project(tmp_path, grid=24, size=960)
    session = AgentSession()
    ds = session.data("gsynth")
    _values_cd8, ids = _values(ds, "CD8")
    flagged = ids[:120]
    record = cell_exclusions.make_record([], {"CD8": flagged}, fingerprint="t1")
    with cell_exclusions.mode("off"):
        plain = {m: model.fit_for(ds, m)["gate"] for m in ("CD3", "CD8")}
    with cell_exclusions.using(record):
        scoped = {m: model.fit_for(ds, m)["gate"] for m in ("CD3", "CD8")}
        assert cell_exclusions.row_mask(ds, "CD3") is None
        assert int((~cell_exclusions.row_mask(ds, "CD8")).sum()) == 120
    assert scoped["CD3"] == pytest.approx(plain["CD3"])
    assert scoped["CD8"] != pytest.approx(plain["CD8"])


def test_a_qc_edit_invalidates_the_fit_and_marks_session_gates_stale(tmp_path):
    from plexora.plugins.gating.capabilities_autogate import stale_qc
    from plexora.plugins.gating.server import model
    from plexora.plugins.gating.server.autogate import provenance

    _fold_project(tmp_path)
    session = AgentSession()
    roi = _draw(session)
    ds = session.data("gsynth")
    before = model.fit_for(ds, "CD3")["gate"]
    block = cell_exclusions.describe(ds, marker="CD3")
    provenance.record("gsynth", "CD3", status="accepted", method="gmm", confidence="high",
                      detail={"qc_exclusion": {"mode": "strict", "applied": True,
                                               "fingerprint": block["fingerprint"],
                                               "n_left_out": block["n_left_out"]}})
    assert stale_qc(ds, provenance.read("gsynth")) == []
    # Reshaped in the ROI panel, no refresh_qc: off the fold, onto a corner
    # of ordinary tissue.
    ok(invoke(session, "update_roi", {"project": "gsynth", "roi_id": roi["roi"]["id"],
                                      "geometry": _box(700, 700, 960, 960)}))
    after = model.fit_for(ds, "CD3")["gate"]
    moved = cell_exclusions.current(ds)
    assert moved.fingerprint != block["fingerprint"]
    values, ids = _values(ds, "CD3")
    expected, _bg, _pos = model.auto_gate(values[~np.isin(ids, moved.whole)],
                                          ds.table.log_transformed)
    assert after == pytest.approx(expected)
    assert after != pytest.approx(before)
    stale = stale_qc(ds, provenance.read("gsynth"))
    assert [s["marker"] for s in stale] == ["CD3"]


def test_gates_still_apply_to_every_cell(tmp_path):
    from plexora.plugins.gating.server import model

    _fold_project(tmp_path)
    session = AgentSession()
    _draw(session)
    ok(invoke(session, "set_gate", {"project": "gsynth", "marker": "CD3", "low": 300.0}))
    ds = session.data("gsynth")
    values, _ids = _values(ds, "CD3")
    gate = model.get_gate(ds, "CD3")
    # The stored range covers the whole column, fold included.
    assert gate["high"] >= float(np.nanmax(values))
    assert gate["low"] == pytest.approx(300.0)


def test_no_qc_and_no_qc_plugin_change_nothing(tmp_path, monkeypatch):
    from plexora.plugins.gating.server import model

    make_gating_project(tmp_path, grid=24, size=960)
    session = AgentSession()
    ds = session.data("gsynth")
    assert cell_exclusions.current(ds) is None
    block = cell_exclusions.describe(ds)
    assert block["applied"] is False and block["reason"] == "no QC result for this image"
    monkeypatch.setenv("PLEXORA_PLUGINS", "gating")
    cell_exclusions._reset_for_tests()
    assert cell_exclusions.providers() == []
    values, _ids = _values(ds, "CD3")
    expected, _bg, _pos = model.auto_gate(values, ds.table.log_transformed)
    assert model.fit_for(ds, "CD3")["gate"] == pytest.approx(expected)


def test_the_record_survives_the_trip_to_a_data_node(tmp_path):
    from plexora.plugins.gating.server import model

    _fold_project(tmp_path)
    session = AgentSession()
    _draw(session)
    ds = session.data("gsynth")
    record = cell_exclusions.current(ds)
    payload = json.loads(json.dumps(cell_exclusions.encode(record)))
    back = cell_exclusions.decode(payload)
    assert np.array_equal(back.whole, record.whole)
    assert back.fingerprint == record.fingerprint
    assert cell_exclusions.decode(cell_exclusions.encode(None)) is None
    # The gmm operation, run as a node would run it: the record from the payload.
    sent = cell_exclusions.attach(ds, {"channel": "CD3", "selection_ids": []})
    with cell_exclusions.mode("off"):
        answered = ds.table.run("gating.gmm", sent)
    assert answered["gate"] == pytest.approx(model.fit_for(ds, "CD3")["gate"])


def test_a_rendered_field_draws_left_out_cells_grey_and_never_positive(tmp_path):
    _fold_project(tmp_path)
    session = AgentSession()
    _draw(session)
    left = _left_out()
    rendered = ok(invoke(session, "render_region", {
        "project": "gsynth", "bounds": {"x": 0, "y": 0, "width": 480, "height": 480},
        "channels": [{"name": "CD3", "color": "#ffff00"}], "segmentation": "outlines",
        "cells": {"highlight": {"kind": "marker", "marker": "CD3", "low": 100.0}},
        "output": {"width": 480, "height": 480}}))
    cells = rendered["manifest"]["cells"]
    assert cells["qc_left_out_cells"] > 0
    assert cells["qc_left_out_cells"] <= len(left)
    every = ok(invoke(session, "render_region", {
        "project": "gsynth", "bounds": {"x": 0, "y": 0, "width": 480, "height": 480},
        "channels": [{"name": "CD3", "color": "#ffff00"}], "segmentation": "outlines",
        "cells": {"highlight": {"kind": "marker", "marker": "CD3", "low": 100.0},
                  "qc_failures": "as_others"},
        "output": {"width": 480, "height": 480}}))
    assert "qc_left_out_cells" not in every["manifest"]["cells"]
    # The fold's cells read ~exp(5.6) > 100: drawn positive only when QC is ignored.
    assert every["manifest"]["cells"]["positive_cells"] > cells["positive_cells"]
