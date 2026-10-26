"""QC's per-cell calls leaving QC: the user's own file (CSV, AnnData, one image
of a shared AnnData), cells.h5ad, the category switch, explain_cell, the ROI
count, the per-cell annotations Cell Explorer colours by, and the SCIMAP Pro
hand-off."""

import csv
import dataclasses
import json
from pathlib import Path

import numpy as np
import pytest

from plexora.agent import AgentSession, invoke, registry
from plexora.agent.policy import Policy
from plexora.plugins.qc.server import results
from tests.helpers import ALL_CONFIRMED, anndata_spec, image_spec, project
from tests.qc_fixtures import make_qc_project
from tests.scimappro_fixtures import make_anndata_project

WRITES = Policy.from_flags(allow_source_writes=True)


@pytest.fixture(autouse=True)
def _qc():
    registry.discover(["roi", "qc"])


def ok(result):
    assert result["ok"], json.dumps(result.get("error"), default=str)[:4000]
    return result["result"]


def _box(x0, y0, x1, y1):
    return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1],
                                                [x0, y0]]]}


def _fold(session, project, box):
    made = ok(invoke(session, "create_roi", {"project": project, "category": "QC: Tissue fold",
                                             "geometry": box}))
    ok(invoke(session, "refresh_qc", {"project": project}))
    return made["receipt"]["after"]["id"]


def _blur_on(session, project, box, channel):
    """A registration region on one channel: marker-level, the cells kept."""
    made = ok(invoke(session, "create_roi", {
        "project": project, "category": "QC: Registration issue", "geometry": box,
        "notes": f"qc-channels: {channel}"}))
    ok(invoke(session, "refresh_qc", {"project": project}))
    return made["receipt"]["after"]["id"]


# -- a CSV table ------------------------------------------------------------------------


def test_a_csv_write_adds_the_columns_backs_up_and_records_itself(tmp_path):
    """status, pass and one column per category; the whole file backed up
    first; the write in the QC document's ledger; a second write refused."""
    info = make_qc_project(tmp_path, artifacts=())
    session = AgentSession()
    _fold(session, "qcsynth", _box(100, 100, 300, 300))
    path = tmp_path / "_qcsynth_files" / "cells.csv"
    original = path.read_text(encoding="utf-8")
    written = ok(invoke(session, "write_qc_to_source", {"project": "qcsynth", "confirm": True},
                        policy=WRITES))["written"]
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    assert len(rows) == len(info["cells"])
    for column in ("plexora_qc_status", "plexora_qc_pass", "plexora_qc_tissue_acquisition",
                   "plexora_qc_blur_focus", "plexora_qc_source", "plexora_qc_roi_ids"):
        assert column in rows[0], column
    statuses = {r["plexora_qc_status"] for r in rows}
    assert statuses == {"pass", "excluded"}
    excluded = [r for r in rows if r["plexora_qc_status"] == "excluded"]
    assert all(r["plexora_qc_tissue_acquisition"] == "excluded" for r in excluded)
    assert all(r["plexora_qc_source"] == "manual" for r in excluded)
    assert written["backup"] and Path(written["backup"]).read_text(encoding="utf-8") \
        == original
    ledger = results.load("qcsynth")["source_writes"]
    assert len(ledger) == 1 and ledger[0]["path"] == str(path)
    assert ledger[0]["status_column"] == "plexora_qc_status"
    again = invoke(session, "write_qc_to_source", {"project": "qcsynth", "confirm": True},
                   policy=WRITES)
    assert again["error"]["code"] == "conflict"
    ok(invoke(session, "write_qc_to_source", {"project": "qcsynth", "confirm": True,
                                              "replace": True}, policy=WRITES))
    assert len(results.load("qcsynth")["source_writes"]) == 1


# -- AnnData ------------------------------------------------------------------------------


def _second_image(tmp_path, made, name, image_id):
    """Another project on the same AnnData: the rows of `image_id`."""
    from tests.agent_fixtures import CHANNELS

    spec = anndata_spec(
        made["table"], coordinates={"source": "obs", "x_column": "X_centroid",
                                    "y_column": "Y_centroid"},
        obs_id_field="CellID", cell_id="CellID", celltype="phenotype", markers=CHANNELS,
        obs_columns=["CellID", "X_centroid", "Y_centroid", "imageid", "phenotype"],
        subset={"column": "imageid", "value": image_id})
    record = project(name, dataset=spec, segmentation=made["mask_path"],
                     image=image_spec(channels=CHANNELS, width=256, height=256,
                                      src=made["image_path"]), confirmed=ALL_CONFIRMED)
    record = dataclasses.replace(record, image=dataclasses.replace(
        record.image, max_level=2, tile_width=128, tile_height=128))
    config_path = Path(tmp_path) / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config[name] = record.to_entry()
    config_path.write_text(json.dumps(config), encoding="utf-8")


def test_an_anndata_write_carries_status_marker_warnings_and_versioned_uns(tmp_path):
    """obs status, the three obsm frames, uns version 2 with results keyed by
    image id; refused, then replaced."""
    import anndata as ad

    made = make_anndata_project(tmp_path, name="a", fmt="h5ad")
    session = AgentSession()
    _fold(session, "a", _box(0, 0, 128, 256))
    ok(invoke(session, "write_qc_to_source", {"project": "a", "confirm": True},
              policy=WRITES))
    back = ad.read_h5ad(made["table"])
    assert {"excluded", "pass"} == set(back.obs["plexora_qc_status"].astype(str))
    assert str(back.obs["plexora_qc_status"].dtype) == "category"
    for key in ("plexora_qc_flags", "plexora_qc_marker_flags", "plexora_qc_marker_warn"):
        assert key in back.obsm, key
    uns = back.uns["plexora_qc"]
    assert str(uns["version"]) == "2"
    assert "slide_A" in json.loads(uns["results"])
    assert "status" in json.loads(uns["columns"])
    refused = invoke(session, "write_qc_to_source", {"project": "a", "confirm": True},
                     policy=WRITES)
    assert refused["error"]["code"] == "conflict"
    ok(invoke(session, "write_qc_to_source", {"project": "a", "confirm": True,
                                              "replace": True}, policy=WRITES))


def test_two_images_of_one_anndata_are_written_without_touching_each_other(tmp_path):
    """Image B's first write is not refused because image A wrote first, and
    A's values (obs, obsm, uns) survive it."""
    import anndata as ad

    made = make_anndata_project(tmp_path, name="a", fmt="h5ad", extra_images=("slide_B",))
    _second_image(tmp_path, made, "b", "slide_B")
    session = AgentSession()
    _fold(session, "a", _box(0, 0, 128, 256))
    ok(invoke(session, "write_qc_to_source", {"project": "a", "confirm": True},
              policy=WRITES))
    after_a = ad.read_h5ad(made["table"])
    rows_a = (after_a.obs["imageid"] == "slide_A").to_numpy()
    status_a = after_a.obs["plexora_qc_status"].astype(str).to_numpy()[rows_a].copy()
    assert "excluded" in set(status_a)
    _fold(session, "b", _box(128, 0, 256, 256))
    ok(invoke(session, "write_qc_to_source", {"project": "b", "confirm": True},
              policy=WRITES))
    both = ad.read_h5ad(made["table"])
    rows_a = (both.obs["imageid"] == "slide_A").to_numpy()
    rows_b = ~rows_a
    assert list(both.obs["plexora_qc_status"].astype(str).to_numpy()[rows_a]) \
        == list(status_a)
    status_b = both.obs["plexora_qc_status"].astype(str).to_numpy()[rows_b]
    assert "excluded" in set(status_b)
    # The fold over A's left half and B's right half: different cells failed.
    xs = both.obs["X_centroid"].to_numpy()
    assert all(xs[rows_a][status_a == "excluded"] < 128)
    assert all(xs[rows_b][status_b == "excluded"] > 128)
    flags = both.obsm["plexora_qc_flags"]
    assert flags["region:tissue_fold"].to_numpy()[rows_a].any()
    assert set(json.loads(both.uns["plexora_qc"]["results"])) == {"slide_A", "slide_B"}


def test_cells_h5ad_is_an_obs_only_anndata(tmp_path):
    """export_qc what="anndata": status in obs, the flag frames in obsm."""
    import anndata as ad

    info = make_qc_project(tmp_path, artifacts=())
    session = AgentSession()
    _fold(session, "qcsynth", _box(100, 100, 300, 300))
    files = ok(invoke(session, "export_qc", {"project": "qcsynth", "what": "anndata"}))["files"]
    adata = ad.read_h5ad(files["anndata"])
    assert adata.n_obs == len(info["cells"]) and adata.n_vars == 0
    assert {"excluded", "pass"} == set(adata.obs["status"].astype(str))
    assert "plexora_qc_flags" in adata.obsm and "plexora_qc_marker_warn" in adata.obsm
    assert str(adata.uns["plexora_qc"]["version"]) == "2"
    assert "cells" not in files


# -- the category switch ----------------------------------------------------------------


def test_set_qc_categories_is_receipted_reversible_and_validated(tmp_path):
    """The undo is the previous set; the background and unknown categories
    are refused; get_qc_exclusions reports what is off; on again, the calls
    come back."""
    make_qc_project(tmp_path, artifacts=())
    session = AgentSession()
    _fold(session, "qcsynth", _box(100, 100, 300, 300))
    n_fail = results.active(results.load("qcsynth"))["cells"]["n_fail"]
    off = ok(invoke(session, "set_qc_categories", {
        "project": "qcsynth", "disabled_categories": ["tissue_acquisition"]}))
    assert off["disabled_categories"] == ["tissue_acquisition"] and off["n_fail"] == 0
    assert off["receipt"]["undo_hint"]["arguments"]["disabled_categories"] == []
    assert off["categories"]["tissue_acquisition"]["n_noted"] == n_fail
    exclusions = ok(invoke(session, "get_qc_exclusions", {"project": "qcsynth"}))
    assert exclusions.get("disabled_categories") == ["tissue_acquisition"] \
        or not exclusions.get("applied")
    for bad in (["background"], ["nonsense"]):
        refused = invoke(session, "set_qc_categories", {"project": "qcsynth",
                                                        "disabled_categories": bad})
        assert refused["error"]["code"] == "invalid_input"
    on = ok(invoke(session, "set_qc_categories", {"project": "qcsynth",
                                                  "disabled_categories": []}))
    assert on["n_fail"] == n_fail
    assert on["receipt"]["undo_hint"]["arguments"]["disabled_categories"] == [
        "tissue_acquisition"]
    state = ok(invoke(session, "get_qc_results", {"project": "qcsynth"}))
    assert state["exclusion_filter"] == {"disabled_categories": []}


# -- what QC says of one cell -------------------------------------------------------------


def test_explain_cell_and_the_roi_count_say_what_qc_leaves_out(tmp_path):
    """explain_cell's `qc` block: left out, why; count_cells_in_roi: how many
    of a region's cells QC leaves out."""
    info = make_qc_project(tmp_path, size=512, grid=16, artifacts=())
    session = AgentSession()
    roi_id = _fold(session, "qcsynth", _box(0, 0, 200, 200))
    inside = next(c for c in info["cells"] if c["x"] < 150 and c["y"] < 150)
    outside = next(c for c in info["cells"] if c["x"] > 300 and c["y"] > 300)
    told = ok(invoke(session, "explain_cell", {"project": "qcsynth", "cell_id": inside["id"],
                                               "include_crop": False, "k": 0}))
    qc = told["qc"]
    assert qc["left_out"] is True and qc["mode"] == "strict"
    finding = qc["findings"][0]
    assert finding["status"] == "excluded" and "excluded" in finding["why"]
    assert finding["reasons"][0]["reason"] == "region:tissue_fold"
    clean = ok(invoke(session, "explain_cell", {"project": "qcsynth", "cell_id": outside["id"],
                                                "include_crop": False, "k": 0}))["qc"]
    assert clean["left_out"] is False
    counted = ok(invoke(session, "count_cells_in_roi", {"project": "qcsynth",
                                                        "roi_id": roi_id}))
    assert counted["n_qc_excluded"] > 0
    assert counted["n_cells_passing"] == counted["n_cells"] - counted["n_qc_excluded"]
    assert counted["qc"]["applied"]


# -- per-cell annotations ---------------------------------------------------------------


def test_qc_status_is_an_annotation_cell_explorer_can_colour_by(tmp_path):
    """ProjectData.annotations has qc_status, one value per table row; Cell
    Explorer lists it as a derived column and encodes it."""
    from plexora import api
    from plexora.plugins.cell_explorer.server import values, variables

    info = make_qc_project(tmp_path, size=512, grid=16, artifacts=())
    session = AgentSession()
    _fold(session, "qcsynth", _box(0, 0, 200, 200))
    _blur_on(session, "qcsynth", _box(300, 300, 512, 512), "CD3")
    ds = api.project_data("qcsynth")
    found = ds.annotations()
    status = found["qc_status"]
    assert status["provider"] == "qc" and len(status["values"]) == len(info["cells"])
    assert {"excluded", "marker_only", "pass"} <= set(status["values"])
    assert set(found["qc_category"]["values"]) >= {"tissue_acquisition", None}
    listed = {d["name"]: d for d in variables.describe_all(ds)}
    assert listed["qc_status"]["derived"] and listed["qc_status"]["kind"] == "categorical"
    assert listed["qc_status"]["colors"]["excluded"]
    payload, kind, n = values.encode(ds, "qc_status", listed["qc_status"])
    assert kind == "categorical" and n == len(info["cells"]) and payload


# -- the SCIMAP Pro hand-off ---------------------------------------------------------------


def test_the_hand_off_states_its_mode_and_points_at_the_file_only_once_written(tmp_path):
    """`excluded` under the stated mode, `flagged` beside it, and `per_cell`
    only after Plexora wrote this result into the table."""
    from plexora.agent.bridge_provider import PlexoraProvider

    made = make_anndata_project(tmp_path, name="a", fmt="h5ad")
    session = AgentSession()
    _fold(session, "a", _box(0, 0, 128, 256))
    provider = PlexoraProvider(session)
    first = provider._collect_qc("a", image_id=None, operation_id="op_t", arguments={})
    assert first["summary"]["mode"] == "strict"
    assert first["excluded"] and first["flagged"] == []
    assert "per_cell" not in first and "status_ref" not in first["summary"]
    ok(invoke(session, "write_qc_to_source", {"project": "a", "confirm": True},
              policy=WRITES))
    second = provider._collect_qc("a", image_id=None, operation_id="op_t", arguments={})
    assert second["per_cell"] == {"uri": "obs:plexora_qc_pass"}
    assert second["summary"]["status_ref"] == "obs:plexora_qc_status"
    assert second["summary"]["per_cell"]["path"] == made["table"]
    assert np.isin(second["summary"]["status_counts"]["excluded"], len(second["excluded"]))
