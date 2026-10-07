"""Cell-level QC rules: what each call rests on, and what it may never rest on.

Each rule the cell calls follow is pinned here on the synthetic project:
whole-cell reasons only for causes that corrupt every channel; one marker
flagged (the cell kept) for a region seen in some channels; a signal-raising
region flags nothing the cells' own values do not bear out; autofluorescence
needs its evidence.
"""

import numpy as np
import polars as pl
import pytest

from plexora.agent import AgentSession
from plexora.plugins.qc.server import class_rules, schemas, strictness
from plexora.plugins.qc.server.cells import calls
from tests.qc_fixtures import make_qc_project


@pytest.fixture
def ds(tmp_path):
    make_qc_project(tmp_path, artifacts=())
    return AgentSession().data("qcsynth")


def _marker(ds):
    from plexora.plugins.qc.server.cycles import is_nuclear

    return next(m for m in ds.table.markers if not is_nuclear(m))


def _values(ds, marker):
    return np.asarray(ds.table.columns([marker])[marker], dtype=np.float64)


def _ids(ds):
    return calls._rows(ds)[0]


def _result(*candidates):
    return {"result_id": "qr_rules", "cycles": [],
            "candidates": {c["roi_id"]: {"id": c["roi_id"], **c} for c in candidates}}


def _pairs(roi_id, cell_ids, fraction=1.0):
    return pl.DataFrame({"cell_id": pl.Series(list(cell_ids), dtype=pl.Int64),
                         "roi_id": pl.Series([roi_id] * len(cell_ids), dtype=pl.Utf8),
                         "fraction": pl.Series([fraction] * len(cell_ids), dtype=pl.Float32),
                         "method": pl.Series(["mask"] * len(cell_ids), dtype=pl.Utf8)})


def _derive(ds, result, pairs, preset="standard"):
    return calls.derive(ds, result, strictness.thresholds(preset), pairs=pairs)


# -- the class a region may claim -------------------------------------------------------


def test_autofluorescence_needs_its_evidence():
    kept, note = class_rules.supported_class("autofluorescence", channels=["FOXP3"],
                                             image_channels=["DNA_1", "FOXP3", "CD3"])
    assert kept == "excessive_background" and note["from"] == "autofluorescence"
    assert "FOXP3" in note["why"]
    # The same structures bright in two markers: broad-spectrum, kept.
    assert class_rules.supported_class("autofluorescence", channels=["FOXP3", "CD3"],
                                       image_channels=["FOXP3", "CD3"]) == (
        "autofluorescence", None)
    # A nuclear stain is not a second marker.
    assert class_rules.supported_class("autofluorescence", channels=["FOXP3", "DNA_1"],
                                       image_channels=["FOXP3", "DNA_1"])[0] \
        == "excessive_background"
    # An autofluorescence channel in the image: kept.
    assert class_rules.supported_class("autofluorescence", channels=["FOXP3"],
                                       image_channels=["FOXP3", "AF_1"])[1] is None
    # Compact bright objects stay what the detector measured, so they are
    # still traced round the objects.
    assert class_rules.supported_class("autofluorescence", channels=["SMA"],
                                       image_channels=["SMA"],
                                       hint="antibody_aggregate")[0] == "antibody_aggregate"
    assert class_rules.supported_class("autofluorescence", channels=["SMA"],
                                       image_channels=["SMA"],
                                       hint="tissue_fold")[0] == "excessive_background"
    # Every other class passes untouched.
    assert class_rules.supported_class("tissue_fold", channels=[], image_channels=[]) == (
        "tissue_fold", None)


def test_only_a_channel_acquired_for_autofluorescence_counts_as_one():
    for name in ("AF", "af1", "AF 2", "Autofluorescence", "autofluor_488", "Blank_488",
                 "blank", "Unstained", "background", "BG2", "empty-555"):
        assert class_rules.is_af_channel(name), name
    # Alexa Fluor dye names are markers, not autofluorescence channels.
    for name in ("CD3_AF488", "AF488", "CD45-AF647", "FOXP3", "DAPI"):
        assert not class_rules.is_af_channel(name), name


def test_what_a_region_does_to_cells():
    assert class_rules.region_level("tissue_fold", "channel", ["CD3"]) == ("cell", [])
    assert class_rules.region_level("cycle_specific_tissue_loss", "cycle",
                                    ["DNA_2", "CD20"]) == ("cell", [])
    assert class_rules.region_level("antibody_aggregate", "channel", ["SMA"]) == (
        "marker", ["SMA"])
    assert class_rules.region_level("out_of_focus", "all_channels", ["CD3"]) == ("cell", [])
    assert class_rules.region_level("excessive_background", "channel", []) == ("cell", [])
    # Reaching the stain the cells were segmented from: the cells are unreadable.
    assert class_rules.region_level("out_of_focus", "cycle", ["DNA_1", "CD3"],
                                    segmentation="DNA_1") == ("cell", [])


# -- regions -> cells ---------------------------------------------------------------------


def test_a_tissue_region_fails_whole_cells(ds):
    ids = _ids(ds)
    chosen = ids[:40]
    result = _result({"roi_id": "r_fold", "class": "tissue_fold", "scope": "all_channels",
                      "channels": ["DNA_1"], "action": "exclude"})
    frame, _p, summary = _derive(ds, result, _pairs("r_fold", chosen))
    failed = frame.filter(~frame["pass"])
    assert set(failed["cell_id"].to_list()) == set(chosen.tolist())
    assert all(r == ["region:tissue_fold"] for r in failed["excluded_by"].to_list())
    assert summary["evidence"]["region:tissue_fold"]["level"] == "cell"
    assert summary["evidence"]["region:tissue_fold"]["rois"] == ["r_fold"]
    assert frame["unreliable_markers"].list.len().sum() == 0


def test_a_channel_region_the_cells_do_not_bear_out_flags_nothing(ds):
    """The exemplar's SMA "aggregate": the cells inside are no brighter in SMA
    than anywhere else -- none of them is flagged, and the report says why."""
    marker = _marker(ds)
    values = _values(ds, marker)
    ids = _ids(ds)
    typical = ids[np.argsort(np.abs(values - np.median(values)))[:120]]
    result = _result({"roi_id": "r_agg", "class": "antibody_aggregate", "scope": "channel",
                      "channels": [marker], "action": "exclude"})
    frame, _p, summary = _derive(ds, result, _pairs("r_agg", typical))
    assert frame["pass"].all() and (frame["action"] == "pass").all()
    assert frame["marker_flags"].list.len().sum() == 0
    [missed] = summary["not_borne_out"]
    assert missed["roi_id"] == "r_agg" and missed["marker"] == marker
    assert "not brighter" in missed["why"]


def test_a_channel_region_flags_its_marker_in_the_bright_cells_only(ds):
    marker = _marker(ds)
    values = _values(ds, marker)
    ids = _ids(ds)
    order = np.argsort(values)
    bright = ids[order[-30:]]
    typical = ids[order[len(order) // 2 - 45:len(order) // 2 + 45]]
    inside = np.concatenate([bright, typical])
    result = _result({"roi_id": "r_bg", "class": "excessive_background", "scope": "channel",
                      "channels": [marker], "action": "exclude"})
    frame, _p, summary = _derive(ds, result, _pairs("r_bg", inside, fraction=0.2))
    # No cell fails or is warned: the marker is the problem, not the cell.
    assert frame["pass"].all() and (frame["action"] == "pass").all()
    flagged = set(frame.filter(frame["unreliable_markers"].list.contains(marker))[
        "cell_id"].to_list())
    assert flagged and flagged <= set(bright.tolist())
    assert not flagged & set(typical.tolist())
    [record] = [e for e in summary["marker_evidence"] if e["roi_id"] == "r_bg"]
    assert record["borne_out"] and record["test"] == "signal"
    assert record["n_flagged"] == len(flagged)
    assert min(record["p_tail"], record["p_shift"]) <= 1e-3
    assert summary["marker_flags"][marker]["region:excessive_background"]["exclude"] \
        == len(flagged)
    row = frame.filter(pl.col("cell_id") == int(next(iter(flagged)))).row(0, named=True)
    assert f"{marker}|region:excessive_background|exclude" in row["marker_flags"]
    assert "r_bg" in row["roi_ids"]


def test_a_marker_s_positive_cells_do_not_pass_for_an_artifact(ds):
    """A region of ordinary tissue -- its share of positive cells what the
    tissue around it has -- is compared with that tissue, and flags nothing."""
    frame = ds.table.geometry()
    xs = frame[ds.schema.x].to_numpy()
    ys = frame[ds.schema.y].to_numpy()
    ids = frame[ds.schema.cell_id].to_numpy()
    marker = _marker(ds)
    box = (300, 300, 700, 700)
    inside = ids[(xs > box[0]) & (xs < box[2]) & (ys > box[1]) & (ys < box[3])]
    geometry = {"type": "Polygon", "coordinates": [[[box[0], box[1]], [box[2], box[1]],
                                                    [box[2], box[3]], [box[0], box[3]],
                                                    [box[0], box[1]]]]}
    result = _result({"roi_id": "r_bg", "class": "excessive_background", "scope": "channel",
                      "channels": [marker], "action": "exclude", "geometry": geometry})
    frame, _p, summary = _derive(ds, result, _pairs("r_bg", inside))
    [record] = summary["marker_evidence"]
    assert record["reference"] == "the cells around the region"
    assert not record["borne_out"] and frame["marker_flags"].list.len().sum() == 0


def test_marker_flags_are_nested_across_presets(ds):
    marker = _marker(ds)
    values = _values(ds, marker)
    ids = _ids(ds)
    order = np.argsort(values)
    inside = np.concatenate([ids[order[-60:]], ids[order[:200]]])
    result = _result({"roi_id": "r_bg", "class": "excessive_background", "scope": "channel",
                      "channels": [marker], "action": "warn"})
    flagged = []
    for preset in ("lenient", "standard", "strict"):
        frame, _p, _s = _derive(ds, result, _pairs("r_bg", inside), preset)
        flagged.append(set(frame.filter(frame["marker_flags"].list.len() > 0)[
            "cell_id"].to_list()))
    assert flagged[0] <= flagged[1] <= flagged[2] and flagged[2]


def test_a_non_signal_channel_region_flags_every_cell_it_covers(ds):
    marker = _marker(ds)
    ids = _ids(ds)
    result = _result({"roi_id": "r_blur", "class": "out_of_focus", "scope": "channel",
                      "channels": [marker], "action": "warn"})
    frame, _p, summary = _derive(ds, result, _pairs("r_blur", ids[:25]))
    assert frame["pass"].all() and (frame["action"] == "pass").all()
    rows = frame.filter(frame["marker_flags"].list.len() > 0)
    assert set(rows["cell_id"].to_list()) == set(ids[:25].tolist())
    assert rows["unreliable_markers"].list.len().sum() == 0     # warn: flagged, not unreliable
    assert summary["marker_flags"][marker]["region:out_of_focus"] == {"warn": 25}


def test_a_region_in_a_channel_the_table_does_not_measure_flags_nothing(ds):
    ids = _ids(ds)
    result = _result({"roi_id": "r_x", "class": "antibody_aggregate", "scope": "channel",
                      "channels": ["NOT_IN_TABLE"], "action": "exclude"})
    frame, _p, summary = _derive(ds, result, _pairs("r_x", ids[:25]))
    assert frame["pass"].all() and frame["marker_flags"].list.len().sum() == 0
    assert summary["not_borne_out"][0]["why"] == "the table does not measure this channel"


# -- Segmentation QC's calls -> per-cell reasons ---------------------------------------


def _seg(ids, **flags):
    """`segqc.calls`' (summary, frame, thresholds) with the named columns
    True on the given ids."""
    columns = {name: pl.Series([int(i) in set(map(int, flags.get(name, ())))
                                for i in ids], dtype=pl.Boolean)
               for name in ("under_segmented", "over_segmented", "large", "small",
                            "irregular")}
    frame = pl.DataFrame({"cell_id": pl.Series(ids, dtype=pl.Int64), **columns})
    summary = {"version": "seg-v", "fingerprint": "fp1", "dna_channel": "DNA_1"}
    th = {"under": 0.6, "over": 0.6, "large": 3.0, "small": 3.0, "irregular": 3.0}
    return summary, frame, th


def test_segmentation_calls_become_per_cell_flags(ds):
    """Merged and split cells are noted (kept, recorded); a size outlier
    warns -- excluded only where size alone may; an irregular shape warns;
    a dismissal clears the notes too. Never a region."""
    ids = _ids(ds)
    seg = _seg(ids, under_segmented=ids[:5], over_segmented=ids[5:9], large=ids[9:12],
               small=ids[12:14], irregular=ids[14:16])
    result = _result()
    frame, _p, summary = calls.derive(ds, result, strictness.thresholds("standard"),
                                      pairs=None, seg=seg)
    by_id = {r["cell_id"]: r for r in frame.iter_rows(named=True)}
    for cid in ids[:9]:
        row = by_id[int(cid)]
        assert row["pass"] and row["action"] == "pass", row
        assert row["noted_by"] and set(row["noted_by"]) <= {"seg_under", "seg_over"}
        assert set(row["noted_by"]) <= set(row["reasons"])
    for cid in ids[9:16]:
        row = by_id[int(cid)]
        assert row["pass"] and row["action"] == "warn" and not row["noted_by"], row
    assert by_id[int(ids[20])]["action"] == "pass" and not by_id[int(ids[20])]["reasons"]
    assert summary["note_by_reason"] == {"seg_under": 5, "seg_over": 4}
    assert summary["warn_by_reason"] == {"seg_large": 3, "seg_small": 2, "seg_irregular": 2}
    assert summary["n_fail"] == 0 and summary["n_warn"] == 7 and summary["n_noted"] == 9
    evidence = summary["evidence"]["seg_large"]
    assert evidence["tool"] == "segqc" and evidence["channels"] == ["DNA_1"]
    assert evidence["column"] == "large" and evidence["fingerprint"] == "fp1"
    # Strict lets size alone exclude; merges stay notes, irregular stays a warning.
    strict, _p, s2 = calls.derive(ds, result, strictness.thresholds("strict"), seg=seg)
    assert s2["by_reason"] == {"seg_large": 3, "seg_small": 2}
    assert s2["note_by_reason"] == {"seg_under": 5, "seg_over": 4}
    assert not strict.filter(strict["cell_id"].is_in([int(i) for i in ids[14:16]]))[
        "pass"].is_in([False]).any()
    # A dismissal clears the notes too.
    dismissed = {**result, "user_dismissed": [{"finding": "cell_reason",
                                               "reason": "seg_under"}]}
    after, _p, s3 = calls.derive(ds, dismissed, strictness.thresholds("standard"), seg=seg)
    assert "seg_under" not in s3["note_by_reason"]
    assert not any("seg_under" in (r or []) for r in after["reasons"].to_list())
    # The panel and the exports read the third status.
    from plexora.plugins.qc.server import provenance, viewer_data

    statuses = viewer_data._statuses(frame, result)
    noted = statuses.filter(statuses["status"] == "note")
    assert set(noted["reason"].to_list()) == {"seg_under", "seg_over"}
    record = provenance.cell_record(result, by_id[int(ids[0])])
    assert record["reasons"][0]["status"] == "note"
    reasons = {r["reason"]: r for r in provenance.cell_reason_records(
        {"cells": summary})}
    assert reasons["seg_under"]["n_noted"] == 5 and reasons["seg_under"]["tool"] == "segqc"


def test_a_noted_cell_is_kept_in_every_export(ds, tmp_path):
    """A note never fails a cell, never sets its category, and every row is
    written."""
    from plexora.plugins.qc.server import export, source_write

    ids = _ids(ds)
    seg = _seg(ids, under_segmented=ids[:5])
    frame, _p, _s = calls.derive(ds, _result(), strictness.thresholds("standard"), seg=seg)
    path = tmp_path / "cells.csv"
    export.cells_csv(frame, path)
    written = pl.read_csv(path)
    assert written.height == frame.height
    noted = written.filter(pl.col("noted").fill_null("") != "")
    assert noted.height == 5 and noted["pass"].all()
    assert noted["qc_category"].is_null().all()
    assert source_write._category("", ["seg_under"], ["seg_under"]) == ""
    assert source_write._category("", ["seg_large", "seg_under"], ["seg_under"]) \
        == "segmentation"


# -- retired keys -----------------------------------------------------------------------


def test_retired_strictness_keys_are_read_not_refused():
    assert not set(strictness.RETIRED_KEYS) & set(schemas.STRICTNESS_KEYS)
    # An old custom table that still holds them is read, not refused.
    assert strictness.thresholds("custom", {"cycle.k": 3.5, "outlier.k": 5.0}) == \
        strictness.thresholds("standard")


# -- what the viewer is given ----------------------------------------------------------


def test_the_panel_groups_cells_and_markers_with_their_evidence(tmp_path):
    import plexora
    from plexora.plugins.qc.server import results
    from plexora.plugins.roi.server import service

    make_qc_project(tmp_path, artifacts=())
    client = plexora.app.test_client()
    session = AgentSession()
    ds = session.data("qcsynth")
    marker = _marker(ds)
    made = client.post("/plugins/qc/categories",
                       data='{"datasource": "qcsynth", "class": "tissue_fold"}').get_json()
    service.create_roi(session.image_data("qcsynth"), category=made["label"],
                       points=[[50, 50], [250, 50], [250, 250], [50, 250]])
    assert client.post("/plugins/qc/refresh", data='{"datasource": "qcsynth"}').get_json()["ok"]
    # Put a marker flag beside the fold's whole-cell calls, the way derive writes them.
    frame = results.cells("qcsynth")
    first = int(frame.filter(frame["pass"])["cell_id"][0])
    frame = frame.with_columns(
        pl.when(pl.col("cell_id") == first)
        .then(pl.lit([f"{marker}|region:antibody_aggregate|exclude"]))
        .otherwise(pl.col("marker_flags")).alias("marker_flags"))
    results.put_cells("qcsynth", frame)
    answer = client.get("/plugins/qc/cells?datasource=qcsynth").get_json()
    # The class names the category; a region drawn there without a subtype is
    # that category's default class.
    fold = next(g for g in answer["groups"] if g["reason"] == "region:tissue_artifact")
    assert fold["category"] == "tissue_acquisition"
    assert fold["level"] == "cell" and fold["status"] == "fail"
    extreme = next(g for g in answer["groups"] if g["level"] == "marker")
    assert extreme["marker"] == marker and extreme["status"] == "unreliable"
    assert extreme["evidence_channels"] == [marker] and extreme["ids"] == [first]
    assert extreme["label"].startswith(f"{marker} · ")


# -- the region test itself ---------------------------------------------------------------


def _bimodal(rng, n, positive=0.3, shift=0.0):
    neg = rng.normal(5.0 + shift, 0.3, int(n * (1 - positive)))
    pos = rng.normal(8.0 + shift, 0.4, n - neg.size)
    return np.concatenate([neg, pos])


def test_thousands_of_cells_do_not_make_a_trivial_difference_count():
    """The exemplar's FOXP3 region: 8,000 cells, 0.1 log units brighter than
    around them -- a p-value of 1e-138 and no difference worth a flag."""
    rng = np.random.default_rng(4)
    ring, inside = _bimodal(rng, 6000), _bimodal(rng, 8000, shift=0.1)
    test = calls.signal_test(inside, ring, "FOXP3", 0.99)
    assert test["p_shift"] < 1e-10                     # "significant"...
    assert test["superiority"] < 0.64 and not test["borne_out"]    # ...and trivial
    assert not test["_flag"].any() and "margin that matters" in test["why"]


def test_background_over_a_region_is_borne_out_and_only_its_brightest_are_flagged():
    rng = np.random.default_rng(5)
    ring, inside = _bimodal(rng, 3000), _bimodal(rng, 800, shift=1.2)
    test = calls.signal_test(inside, ring, "CD57", 0.99)
    assert test["borne_out"] and test["tailed"] and test["shifted"]
    assert test["superiority"] >= 0.64
    flagged = inside[test["_flag"]]
    assert flagged.size and (flagged > np.quantile(ring, 0.99)).all()
    assert flagged.size < inside.size


def test_a_few_artifact_bright_cells_are_borne_out_by_the_tail():
    """An aggregate touches a handful of cells, very brightly; the rest of
    the envelope is ordinary tissue, so there is no overall shift."""
    rng = np.random.default_rng(6)
    ring = _bimodal(rng, 3000)
    inside = np.concatenate([_bimodal(rng, 280), np.full(15, 11.0)])
    test = calls.signal_test(inside, ring, "SMA", 0.99)
    assert test["borne_out"] and test["tailed"] and not test["shifted"]
    assert int(test["_flag"].sum()) >= 15


def test_a_region_of_ordinary_tissue_is_not_borne_out():
    rng = np.random.default_rng(7)
    test = calls.signal_test(_bimodal(rng, 500), _bimodal(rng, 3000), "CD3", 0.99)
    assert not test["borne_out"] and not test["_flag"].any()


def test_a_region_only_brighter_as_a_whole_flags_no_cell():
    """Brighter overall, but no more cells stand out than chance: the region
    is reported for what it is, and no single cell is blamed."""
    rng = np.random.default_rng(8)
    ring = rng.normal(5.0, 0.3, 4000)
    inside = rng.normal(5.25, 0.2, 3000)            # shifted, and narrower: no tail
    test = calls.signal_test(inside, ring, "FOXP3", 0.99)
    assert test["shifted"] and not test["tailed"] and not test["borne_out"]
    assert not test["_flag"].any() and "no cell stands out" in test["why"]


def test_a_small_region_says_it_is_too_small_to_tell():
    """Two very bright cells of eight: a 25x excess, but not enough cells to
    rule out chance -- and the record says exactly that."""
    rng = np.random.default_rng(9)
    ring = rng.normal(5.0, 0.3, 4000)
    inside = np.r_[np.full(6, 5.2), [9.0, 9.5]]
    test = calls.signal_test(inside, ring, "FOXP3", 0.99)
    assert not test["borne_out"] and test["why"].startswith("too few cells to tell")
