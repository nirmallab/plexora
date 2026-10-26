"""How QC's findings reach cells: true overlap fractions, cells named by id,
categories switched off, distinct counts, incremental pairs, the inputs the
calls were derived from, hand regions scoped by their notes, blur by cycle."""

import json

import numpy as np
import polars as pl
import pytest

from plexora.agent import AgentSession, invoke, registry
from plexora.plugins.qc.server import propagate, results, schemas, strictness
from plexora.plugins.qc.server.cells import calls
from tests.qc_fixtures import make_qc_project


@pytest.fixture(autouse=True)
def _qc():
    registry.discover(["roi", "qc"])


def ok(result):
    assert result["ok"], json.dumps(result.get("error"), default=str)[:4000]
    return result["result"]


def _box(x0, y0, x1, y1):
    return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1],
                                                [x0, y0]]]}


def _pairs(roi_id, cell_ids):
    n = len(cell_ids)
    return pl.DataFrame({"cell_id": pl.Series(list(cell_ids), dtype=pl.Int64),
                         "roi_id": pl.Series([roi_id] * n, dtype=pl.Utf8),
                         "fraction": pl.Series([1.0] * n, dtype=pl.Float32),
                         "method": pl.Series(["mask"] * n, dtype=pl.Utf8)})


# -- overlap -------------------------------------------------------------------------------


def test_an_overlap_is_a_fraction_of_the_whole_cell(tmp_path):
    """The denominator is each cell's area over the whole mask (cached per
    mask), so the fraction is what the labels say -- not a count clipped to
    the region's box."""
    info = make_qc_project(tmp_path, size=512, grid=16, artifacts=())
    ds = AgentSession().data("qcsynth")
    labels = info["labels"]
    edge = next(c for c in info["cells"] if c["x"] > 200)["x"]
    pairs, per_roi = propagate.propagate(ds, [{"roi_id": "r1",
                                               "geometry": _box(0, 0, edge, 512)}])
    assert per_roi["r1"]["method"] == "mask"
    assert per_roi["r1"]["area"] == "whole" and per_roi["r1"]["area_fallback"] == 0
    got = dict(zip(pairs["cell_id"].to_list(), pairs["fraction"].to_list()))
    whole = np.bincount(labels.ravel())
    inside = np.bincount(labels[:, :int(round(edge))].ravel(), minlength=whole.size)
    on_edge = [c["id"] for c in info["cells"] if abs(c["x"] - edge) < 1]
    assert on_edge
    for cid in on_edge:
        assert abs(got[cid] - inside[cid] / whole[cid]) < 0.06, cid
    # The areas are kept with QC's store, one file per mask.
    folder = results._store("qcsynth").directory() / "label_areas"
    assert len(list(folder.glob("*.npz"))) == 1


def test_a_region_that_names_its_cells_holds_exactly_them(tmp_path):
    """Object review's `labels` region: its ids, method "labels"; an id the
    table does not hold is not invented."""
    info = make_qc_project(tmp_path, size=512, grid=16, artifacts=())
    ds = AgentSession().data("qcsynth")
    named = [info["cells"][3]["id"], info["cells"][40]["id"], 999_999]
    pairs, per_roi = propagate.propagate(ds, [{
        "roi_id": "s1", "geometry": _box(0, 0, 10, 10),
        "solid": {"kind": "labels", "ids": named}}])
    assert sorted(pairs["cell_id"].to_list()) == sorted(named[:2])
    assert set(pairs["method"].to_list()) == {"labels"}
    assert per_roi["s1"] == {"method": "labels", "n_cells": 2}


# -- the derivation --------------------------------------------------------------------------


def _two_findings(ds):
    """A fold over the first 20 cells and debris over the first 10 (one
    category, two reasons), and blur on CD3 over cells 30-50."""
    ids = calls._rows(ds)[0].tolist()
    pairs = pl.concat([_pairs("r_fold", ids[:20]), _pairs("r_debris", ids[:10]),
                       _pairs("r_blur", ids[30:50])])
    result = {"result_id": "qr_t", "cycles": [], "candidates": {
        "r_fold": {"id": "r_fold", "roi_id": "r_fold", "class": "tissue_fold",
                   "scope": "all_channels", "channels": [], "action": "exclude"},
        "r_debris": {"id": "r_debris", "roi_id": "r_debris",
                     "class": "debris_or_foreign_object", "scope": "all_channels",
                     "channels": [], "action": "exclude"},
        "r_blur": {"id": "r_blur", "roi_id": "r_blur", "class": "out_of_focus",
                   "scope": "channel", "channels": ["CD3"], "action": "exclude"}}}
    return ids, pairs, result


def test_a_cell_with_two_reasons_of_one_category_is_counted_once(tmp_path):
    """by_category counts distinct cells; a marker-only finding is "channel"."""
    make_qc_project(tmp_path, artifacts=())
    ds = AgentSession().data("qcsynth")
    ids, pairs, result = _two_findings(ds)
    frame, _p, summary = calls.derive(ds, result, strictness.thresholds("standard"),
                                      pairs=pairs)
    tissue = summary["by_category"]["tissue_acquisition"]
    assert tissue["n_excluded"] == 20 and tissue["n_any"] == 20
    blur = summary["by_category"]["blur_focus"]
    assert blur["n_marker_only"] == 20 and blur["n_excluded"] == 0 and blur["enabled"]
    assert abs(tissue["fraction"] - 20 / summary["n"]) < 1e-9
    assert "row" in frame.columns and "category_status" in frame.columns
    assert frame["row"].to_list() == list(range(frame.height))
    assert schemas.CELLS_VERSION == "7" and summary["cells_version"] == "7"
    by_id = dict(zip(frame["cell_id"].to_list(), frame["category_status"].to_list()))
    assert by_id[ids[0]] == ["tissue_acquisition|excluded"]
    assert by_id[ids[35]] == ["blur_focus|channel"]


def test_a_category_switched_off_notes_its_cells_and_keeps_the_evidence(tmp_path):
    """Exclude and warn become notes; marker flags become `|note` and leave
    `unreliable_markers`; the reasons stay listed."""
    make_qc_project(tmp_path, artifacts=())
    ds = AgentSession().data("qcsynth")
    ids, pairs, result = _two_findings(ds)
    table = strictness.thresholds("standard")
    frame, _p, summary = calls.derive(ds, result, table, pairs=pairs,
                                      disabled={"tissue_acquisition", "blur_focus"})
    assert summary["n_fail"] == 0 and summary["n_warn"] == 0
    assert summary["disabled_categories"] == ["blur_focus", "tissue_acquisition"]
    tissue = summary["by_category"]["tissue_acquisition"]
    assert tissue["n_noted"] == 20 and tissue["n_excluded"] == 0 and not tissue["enabled"]
    by_id = {r["cell_id"]: r for r in frame.to_dicts()}
    first = by_id[ids[0]]
    assert first["pass"] and set(first["reasons"]) == {"region:tissue_fold",
                                                       "region:debris_or_foreign_object"}
    assert set(first["noted_by"]) == set(first["reasons"])
    blurred = by_id[ids[35]]
    assert blurred["marker_flags"] == ["CD3|region:out_of_focus|note"]
    assert blurred["unreliable_markers"] == []
    assert summary["marker_noted"]["CD3"]["region:out_of_focus"] == 20
    assert summary["evidence"]["region:tissue_fold"]["filtered"]["category"] \
        == "tissue_acquisition"
    # Switched on again, the same calls as before.
    again, _p, back = calls.derive(ds, result, table, pairs=pairs)
    assert back["n_fail"] == 20


def test_a_category_switched_off_in_the_document_reaches_the_stored_calls(tmp_path):
    """write_for_active reads `exclusion_filter`, stamps the inputs, and the
    exclusion record follows."""
    from plexora.agent import cell_exclusions

    make_qc_project(tmp_path, artifacts=())
    session = AgentSession()
    ok(invoke(session, "create_roi", {"project": "qcsynth", "category": "QC: Tissue fold",
                                      "geometry": _box(100, 100, 400, 400)}))
    ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
    result = results.active(results.load("qcsynth"))
    assert result["cells"]["n_fail"] > 0
    inputs = result["cells"]["inputs"]
    assert {"mask_fp", "table_fp", "pixel_um", "transform_hash", "cells_version",
            "strictness_hash", "filter_hash"} <= set(inputs)
    ds = session.data("qcsynth")
    before = cell_exclusions.current(ds)
    assert before.whole.size == result["cells"]["n_fail"]
    ok(invoke(session, "set_qc_categories", {"project": "qcsynth",
                                             "disabled_categories": ["tissue_acquisition"]}))
    after = results.active(results.load("qcsynth"))["cells"]
    assert after["n_fail"] == 0 and after["exclusion_filter"]["disabled_categories"] == [
        "tissue_acquisition"]
    record = cell_exclusions.current(session.data("qcsynth"))
    assert record is None or record.whole.size == 0
    assert record is None or record.fingerprint != before.fingerprint


def test_a_region_left_as_it_was_keeps_its_pairs(tmp_path):
    """Reshaping one of two regions reads only that one from the mask."""
    make_qc_project(tmp_path, artifacts=())
    session = AgentSession()
    a = ok(invoke(session, "create_roi", {"project": "qcsynth", "category": "QC: Tissue fold",
                                          "geometry": _box(100, 100, 300, 300)}))
    ok(invoke(session, "create_roi", {"project": "qcsynth", "category": "QC: Tissue fold",
                                      "geometry": _box(500, 500, 700, 700)}))
    ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
    first = results.active(results.load("qcsynth"))["cells"]["propagation"]
    assert len(first) == 2 and not any(v.get("reused") for v in first.values())
    moved = a["receipt"]["after"]["id"]
    ok(invoke(session, "update_roi", {"project": "qcsynth", "roi_id": moved,
                                      "geometry": _box(100, 100, 350, 350)}))
    ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
    second = results.active(results.load("qcsynth"))["cells"]["propagation"]
    other = next(k for k in second if k != moved)
    assert second[other].get("reused") and not second[moved].get("reused")
    assert second[other]["geometry_hash"] == first[other]["geometry_hash"]
    pairs = results.cell_rois("qcsynth")
    assert "geometry_hash" in pairs.columns
    assert set(pairs["roi_id"].unique().to_list()) == {moved, other}


# -- hand regions scoped by their notes ------------------------------------------------------


def test_a_hand_region_is_scoped_by_its_qc_channels_token(tmp_path):
    """`qc-channels: CD3` makes a registration region flag CD3 alone, the
    cells kept; changing the notes rescopes it."""
    make_qc_project(tmp_path, size=512, grid=16, artifacts=())
    session = AgentSession()
    made = ok(invoke(session, "create_roi", {
        "project": "qcsynth", "category": "QC: Registration issue",
        "geometry": _box(0, 0, 200, 200), "notes": "qc-channels: CD3"}))
    roi_id = made["receipt"]["after"]["id"]
    ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
    result = results.active(results.load("qcsynth"))
    candidate = next(c for c in result["candidates"].values() if c.get("roi_id") == roi_id)
    assert candidate["scope"] == "channel" and candidate["channels"] == ["CD3"]
    cells = results.cells("qcsynth")
    assert cells["pass"].all()
    flagged = {f for fs in cells["marker_flags"].to_list() for f in fs}
    assert flagged == {"CD3|region:cross_cycle_registration_error|exclude"}
    ok(invoke(session, "update_roi", {"project": "qcsynth", "roi_id": roi_id,
                                      "notes": "qc-channels: CD3, CD8"}))
    refreshed = ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
    result = results.active(results.load("qcsynth"))
    candidate = next(c for c in result["candidates"].values() if c.get("roi_id") == roi_id)
    assert candidate["scope"] == "channels" and candidate["channels"] == ["CD3", "CD8"]
    markers = {f.split("|")[0] for fs in results.cells("qcsynth")["marker_flags"].to_list()
               for f in fs}
    assert markers == {"CD3", "CD8"}
    assert roi_id in json.dumps(refreshed, default=str)


def test_the_token_vocabulary():
    """all_channels, cycle N, and channel names (case-insensitive); nothing
    the image has -> None."""
    from plexora.plugins.qc.server import roi_link

    names = ["DNA_1", "CD3", "CD8", "DNA_2", "CD20"]
    cycles = [{"index": 1, "channels": ["DNA_1", "CD3", "CD8"]},
              {"index": 2, "channels": ["DNA_2", "CD20"]}]
    assert roi_link.channels_token("fold\nqc-channels: cycle 2\n") == "cycle 2"
    assert roi_link.scope_of_token("all_channels", names, cycles) == ("all_channels", [], [])
    assert roi_link.scope_of_token("cycle 2", names, cycles) == ("cycle", ["DNA_2", "CD20"],
                                                                 [2])
    assert roi_link.scope_of_token("cd3", names, cycles) == ("channel", ["CD3"], [])
    assert roi_link.scope_of_token("nope", names, cycles) is None


# -- blur is about its cycle ----------------------------------------------------------------


class _Scan:
    def __init__(self, resolved):
        self.channels = [{"name": "DNA_1", "cycle": 1}, {"name": "CD3", "cycle": 1},
                         {"name": "DNA_2", "cycle": 2}, {"name": "CD20", "cycle": 2}]
        confidence = 0.9 if resolved else 0.7
        self.meta = {"cycles": {"confidence": confidence, "method": "repeats", "cycles": [
            {"index": 1, "channels": ["DNA_1", "CD3"], "nuclear": "DNA_1"},
            {"index": 2, "channels": ["DNA_2", "CD20"], "nuclear": "DNA_2"}]}}

    def channel(self, name):
        return next((c for c in self.channels if c["name"] == name), None)


class _Engine:
    def __init__(self, resolved):
        self._scan = _Scan(resolved)

    def scan(self, project):
        return self._scan


def test_a_blur_region_is_about_its_cycle_when_the_cycles_are_known():
    """Focus is per acquisition: CD20 out of focus flags cycle 2's channels in
    a QC session (`check_candidates._scope`) as on the free path; with the
    cycles uncertain, CD20 alone."""
    from plexora.plugins.qc.server import check_candidates

    unit = {"check": "blur", "channel": "CD20", "project": "p"}
    assert check_candidates._scope(_Engine(True), unit) == ("cycle", ["DNA_2", "CD20"], [2])
    assert check_candidates._scope(_Engine(False), unit) == ("channel", ["CD20"], [2])
