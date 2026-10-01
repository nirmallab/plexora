"""Segmentation QC: DNA peaks against the mask, on synthetic merges, splits, dim
nuclei in whole-cell labels, big nuclei cut into pieces and a mask off its nuclei."""

import json
import os
import subprocess
import sys

import numpy as np
import pytest

from plexora.agent.errors import AgentError


def _project(tmp_path, **kwargs):
    from tests.qc_fixtures import make_qc_project

    kwargs.setdefault("seg_errors", {"merge": 6, "split": 6})
    return make_qc_project(tmp_path, size=768, grid=30, levels=3, **kwargs)


def _run(project, **kwargs):
    from plexora.agent import AgentSession
    from plexora.plugins.qc.server.segqc import run

    return run.load_or_run(AgentSession(), "qcsynth", **kwargs)


def _flagged(frame, word):
    return set(frame.filter(frame["status_word"] == word)["cell_id"].to_list())


def test_merges_and_splits_are_found_and_clean_cells_are_not(tmp_path):
    from plexora.plugins.qc.server.segqc import run

    made = _project(tmp_path)
    truth = made["truth"]["segmentation"]
    assert len(truth["under"]) == 6 and len(truth["over"]) == 6
    summary, reused = _run(made)
    assert not reused
    frame = run.frame("qcsynth")
    under, over = _flagged(frame, "under_segmented"), _flagged(frame, "over_segmented")
    true_under, true_over = set(truth["under"]), set(truth["over"])
    assert len(under & true_under) / max(1, len(under)) >= 0.95
    assert len(over & true_over) / max(1, len(over)) >= 0.95
    assert len(under & true_under) / len(true_under) >= 0.8
    assert len(over & true_over) / len(true_over) >= 0.8
    clean = frame.height - len(true_under) - len(true_over)
    false = len(under - true_under) + len(over - true_over)
    assert false / clean < 0.005
    # Cells and area are separate numbers, and say so.
    assert summary["pct_cells"]["under"] != summary["pct_area"]["under"]
    assert summary["denominators"]["area"] == "segmented (label) area"
    # The fragment points at the cell it was cut from.
    partners = frame.filter(frame["status_word"] == "over_segmented")["partner_id"].to_list()
    assert all(p > 0 for p in partners)
    # The scale is the nuclei's, from the DNA (a disc of radius 9 px here).
    assert summary["scale_method"] == "dna"
    assert 10 <= summary["d_nucleus_px"] <= 22
    assert summary["peaks_on_labels_pct"] > 90 and "notice" not in summary


def test_expanded_labels_with_dim_nuclei_are_not_fragments(tmp_path):
    """A whole-cell mask (labels grown into the cytoplasm, so neighbours touch)
    with a third of the nuclei dim: a dim nucleus beside a bright one is its
    own cell, and the real splits are still found."""
    from plexora.plugins.qc.server.segqc import run

    made = _project(tmp_path, seg_errors={"split": 6, "dim": 0.3, "expand": 8})
    truth = made["truth"]["segmentation"]
    assert len(truth["dim"]) > 100
    _run(made)
    frame = run.frame("qcsynth")
    over = _flagged(frame, "over_segmented")
    unsure = _flagged(frame, "ambiguous")
    assert not over & set(truth["dim"])
    assert not unsure & set(truth["dim"])
    true_over = set(truth["over"])
    assert len(over & true_over) / len(true_over) >= 0.8
    assert len(over - true_over) / (frame.height - len(true_over)) < 0.005


def test_a_big_nucleus_cut_into_pieces(tmp_path):
    """One nucleus 2.2x a cell's radius cut into a centre and four quadrants:
    each piece holds a normal nucleus's DNA (the pair test cannot see it), and
    the blob rule finds the pieces and points them at the centre."""
    from plexora.plugins.qc.server.segqc import run

    made = _project(tmp_path, seg_errors={"big": 4})
    truth = made["truth"]["segmentation"]
    assert len(truth["big"]) == 4
    summary, _ = _run(made)
    frame = run.frame("qcsynth")
    row = {r["cell_id"]: r for r in frame.iter_rows(named=True)}
    for big in truth["big"]:
        assert row[big["home"]]["status_word"] == "pass"
        found = [p for p in big["pieces"] if row[p]["status_word"] == "over_segmented"
                 and row[p]["partner_id"] == big["home"]]
        assert len(found) >= 3, (big, [row[p] for p in big["pieces"]])
    assert summary["big_nuclei"] >= 4
    assert _flagged(frame, "over_segmented") <= set(truth["over"])


def test_a_ring_mask_gets_a_notice(tmp_path):
    """Labels displaced off their nuclei (what a cell-ring or cytoplasm mask
    looks like to the DNA): most peaks fall between labels, and the summary
    says so rather than passing the calls off as meaningful."""
    from plexora.agent import AgentSession
    from plexora.plugins.qc.server.segqc import run

    made = _project(tmp_path, seg_errors={"shift": 11})
    summary, _ = _run(made)
    assert summary["peaks_on_labels_pct"] < 50
    assert "ring" in summary["notice"]
    status = run.public_status(AgentSession(), "qcsynth")
    assert status["summary"]["notice"] == summary["notice"]


def test_the_result_is_deterministic_and_reused(tmp_path, monkeypatch):
    from plexora.plugins.qc.server.segqc import run
    from plexora.server.utils import source_image

    made = _project(tmp_path)
    first, _ = _run(made)
    a = run.frame("qcsynth")
    second, reused = _run(made, force=True)
    assert not reused
    assert a.equals(run.frame("qcsynth"))
    run._MEMORY.clear()

    def refuse(*args, **kwargs):
        raise AssertionError("a reused result must not read pixels")

    monkeypatch.setattr(source_image.SourceImage, "read", refuse)
    third, reused = _run(made)
    assert reused and third["fingerprint"] == first["fingerprint"]


def test_no_mask_and_no_dna_are_preconditions(tmp_path):
    from plexora.agent import AgentSession
    from plexora.plugins.qc.server.segqc import run
    from tests.qc_fixtures import make_qc_project

    make_qc_project(tmp_path, size=256, grid=10, mask=False, table=False)
    with pytest.raises(AgentError) as caught:
        run.load_or_run(AgentSession(), "qcsynth")
    assert caught.value.code == "precondition_missing"
    status = run.public_status(AgentSession(), "qcsynth")
    assert status["available"] is False and status["detected"] == "DNA_1"


def test_a_cancelled_job_stores_nothing(tmp_path, monkeypatch):
    from plexora.agent import AgentSession, jobs, registry
    from plexora.plugins.qc.server.segqc import run

    _project(tmp_path)
    monkeypatch.setattr(run, "TILE_PX", 128)
    registry.discover(["qc"])
    session = AgentSession()
    started = registry.invoke(session, "run_segmentation_qc", {"project": "qcsynth"})
    assert started["ok"]
    job_id = started["result"]["job_id"]
    jobs.store().cancel(job_id)
    record = jobs.store().wait(job_id, 30)
    assert record["status"] in ("cancelled", "done")
    if record["status"] == "cancelled":
        assert run.current("qcsynth") is None


def test_the_panel_runs_polls_and_reads_the_overlay(tmp_path):
    import plexora
    from plexora.agent import jobs

    made = _project(tmp_path)
    client = plexora.app.test_client()
    state = client.get("/plugins/qc/state?datasource=qcsynth").get_json()
    seg = state["checks"]["segmentation"]
    assert seg["available"] and seg["dna_channel"] == "DNA_1" and seg["summary"] is None
    started = client.post("/plugins/qc/segmentation/run",
                          data=json.dumps({"datasource": "qcsynth"})).get_json()
    assert started["ok"] and started["job_id"]
    jobs.store().wait(started["job_id"], 60)
    job = client.get(f"/plugins/qc/jobs/{started['job_id']}").get_json()["job"]
    assert job["status"] == "done", job
    assert job["result"]["summary"]["counts"]["under_segmented"] >= 5
    read = client.get("/plugins/qc/segmentation?datasource=qcsynth").get_json()
    assert read["segmentation_qc"]["summary"]["fingerprint"] == job["result"]["fingerprint"]
    assert read["segmentation_qc"]["stale"] is False
    cells = client.get("/plugins/qc/segmentation/cells?datasource=qcsynth").get_json()
    keys = [g["key"] for g in cells["groups"]]
    assert keys == ["seg:under", "seg:over", "seg:large", "seg:small", "seg:irregular"]
    under = next(g for g in cells["groups"] if g["key"] == "seg:under")
    assert set(under["ids"]) >= set(made["truth"]["segmentation"]["under"][:3])
    assert cells["flag_under"] == cells["flag_over"] == cells["stored_flag"] == 0.6
    assert "pct_cells" not in cells
    # The sliders: the stored scores re-thresholded, each side on its own,
    # with the counts at those bars.
    base = "/plugins/qc/segmentation/cells?datasource=qcsynth"
    stored = {g["key"]: set(g["ids"]) for g in cells["groups"]}

    def tuned(query):
        answer = client.get(f"{base}&{query}").get_json()
        groups = {g["key"]: set(g["ids"]) for g in answer["groups"]}
        assert answer["counts"]["over_segmented"] == len(groups["seg:over"])
        assert answer["counts"]["under_segmented"] == len(groups["seg:under"])
        return answer, groups

    answer, strict_over = tuned("flag_over=0.95")
    assert answer["flag_over"] == 0.95 and answer["flag_under"] == 0.6
    assert strict_over["seg:over"] <= stored["seg:over"]
    assert strict_over["seg:under"] == stored["seg:under"]
    answer, strict_under = tuned("flag_under=0.95")
    assert strict_under["seg:under"] <= stored["seg:under"]
    assert strict_under["seg:over"] == stored["seg:over"]
    _answer, both = tuned("flag_under=0.95&flag_over=0.95")
    assert both["seg:over"] == strict_over["seg:over"]
    assert client.get(f"{base}&flag_over=nan").status_code == 400
    assert client.get(f"{base}&flag_under=high").status_code == 400
    # The stored calls are unchanged by viewing.
    again = client.get("/plugins/qc/segmentation/cells?datasource=qcsynth").get_json()
    assert {g["key"]: set(g["ids"]) for g in again["groups"]} == stored
    assert client.get("/plugins/qc/jobs/not-a-job").status_code == 404
    cleared = client.post("/plugins/qc/segmentation/clear",
                          data=json.dumps({"datasource": "qcsynth"})).get_json()
    assert cleared["ok"] and cleared["cleared"]


def test_the_kernels_agree_without_numba(tmp_path):
    """The compiled kernels and their plain-Python twins give the same numbers."""
    code = (
        "import numpy as np, json\n"
        "from plexora.plugins.qc.server.segqc import kernels\n"
        "rng = np.random.default_rng(3)\n"
        "labels = rng.integers(0, 6, (40, 50)).astype(np.uint32)\n"
        "dna = rng.random((40, 50)).astype(np.float32)\n"
        "arrays = [np.zeros(6) for _ in range(7)]\n"
        "kernels.label_stats(labels, dna, 10, 20, 2, 38, 3, 47, 0.4, *arrays)\n"
        "lo, hi, value, py, px = kernels.pairs(labels, dna, 10, 20, (2, 38, 3, 47))\n"
        "print(json.dumps([[a.tolist() for a in arrays], lo.tolist(), hi.tolist(),\n"
        "                  np.round(value, 6).tolist(), py.tolist(), px.tolist()]))\n")
    outputs = []
    for off in ("0", "1"):
        env = {**os.environ, "PLEXORA_NO_NUMBA": off}
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                              env=env, check=True)
        outputs.append(json.loads(done.stdout))
    assert outputs[0][1:] == outputs[1][1:]
    assert np.allclose(np.array(outputs[0][0]), np.array(outputs[1][0]))


# -- export and the user's own file ------------------------------------------------------------


def _ok(answer):
    assert answer["ok"], json.dumps(answer.get("error"), default=str)[:2000]
    return answer["result"]


def test_export_and_a_csv_source_write_carry_the_seg_columns(tmp_path):
    import csv

    from plexora.agent import AgentSession, invoke, registry
    from plexora.agent.policy import Policy

    made = _project(tmp_path)
    registry.discover(["roi", "qc"])
    _run(made)
    session = AgentSession()
    exported = _ok(invoke(session, "export_qc", {"project": "qcsynth"}))
    rows = list(csv.DictReader(open(exported["files"]["cells"], encoding="utf-8")))
    assert {"cell_id", "seg_qc_status", "seg_qc_under_score", "seg_qc_over_score",
            "seg_qc_reason", "seg_qc_partner_id", "seg_qc_large", "seg_qc_small",
            "seg_qc_irregular"} <= set(rows[0])
    under = {int(r["cell_id"]) for r in rows if r["seg_qc_status"] == "under_segmented"}
    assert under == set(made["truth"]["segmentation"]["under"])
    summary = json.loads(open(exported["files"]["summary"], encoding="utf-8").read())
    assert summary["segmentation_qc"]["counts"]["under_segmented"] == len(under)
    allow = Policy.from_flags(allow_source_writes=True)
    written = _ok(invoke(session, "write_qc_to_source", {"project": "qcsynth", "confirm": True},
                         policy=allow))
    assert written["written"]["segmentation_qc"] is True
    assert "plexora_qc_pass" not in written["written"]["columns"]
    table = list(csv.DictReader(open(tmp_path / "_qcsynth_files" / "cells.csv",
                                     encoding="utf-8")))
    assert {"plexora_seg_qc_status", "plexora_seg_qc_under_score",
            "plexora_seg_qc_over_score", "plexora_seg_qc_partner_id",
            "plexora_seg_qc_under_segmented", "plexora_seg_qc_large"} <= set(table[0])
    assert {int(r["CellID"]) for r in table
            if r["plexora_seg_qc_status"] == "under_segmented"} == under
    again = invoke(session, "write_qc_to_source", {"project": "qcsynth", "confirm": True},
                   policy=allow)
    assert again["error"]["code"] == "conflict"
    assert invoke(session, "write_qc_to_source", {"project": "qcsynth", "confirm": True,
                                                  "replace": True}, policy=allow)["ok"]


def test_an_anndata_source_write_of_segmentation_qc_alone(tmp_path):
    import dataclasses

    import anndata as ad
    import pandas as pd

    from plexora.agent import AgentSession, invoke, registry
    from plexora.agent.policy import Policy
    from plexora.plugins.qc.server.segqc import run
    from tests.helpers import ALL_CONFIRMED, anndata_spec, image_spec, project
    from tests.qc_fixtures import make_qc_project

    made = make_qc_project(tmp_path, size=768, grid=30, levels=3, table=False,
                           seg_errors={"merge": 4, "split": 4})
    cells = made["cells"]
    markers = list(made["channels"])
    adata = ad.AnnData(X=np.array([[c[m] for m in markers] for c in cells], dtype=np.float32),
                       obs=pd.DataFrame({"imageid": ["A"] * len(cells),
                                         "CellID": [c["id"] for c in cells]},
                                        index=[f"cell_{c['id']}" for c in cells]),
                       var=pd.DataFrame(index=markers))
    adata.obsm["spatial"] = np.array([[c["x"], c["y"]] for c in cells], dtype=np.float64)
    adata.uns["theirs"] = "left alone"
    path = tmp_path / "cells.h5ad"
    adata.write_h5ad(path)
    # The merges leave gaps in the ids: the table names its cells itself.
    record = project("qcad", dataset=anndata_spec(path, markers=markers, obs_id_field="CellID",
                                                  row_number_ids=False),
                     segmentation=str(tmp_path / "_qcsynth_files" / "mask.tif"),
                     image=image_spec(channels=markers, width=made["size"],
                                      height=made["size"], src=made["image_path"]),
                     confirmed=ALL_CONFIRMED)
    record = dataclasses.replace(record, image=dataclasses.replace(
        record.image, max_level=2, tile_width=128, tile_height=128))
    config_path = tmp_path / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["qcad"] = record.to_entry()
    config_path.write_text(json.dumps(config), encoding="utf-8")
    registry.discover(["roi", "qc"])
    session = AgentSession()
    run.load_or_run(session, "qcad")
    written = _ok(invoke(session, "write_qc_to_source", {"project": "qcad", "confirm": True},
                         policy=Policy.from_flags(allow_source_writes=True)))
    assert written["written"]["uns"] == "plexora_seg_qc"
    back = ad.read_h5ad(path)
    assert "plexora_qc_pass" not in back.obs.columns
    status = back.obs["plexora_seg_qc_status"]
    assert str(status.dtype) == "category"
    flagged = {int(i.split("_")[1]) for i in status[status == "under_segmented"].index}
    assert flagged == set(made["truth"]["segmentation"]["under"])
    assert str(back.obs["plexora_seg_qc_under_score"].dtype) == "float32"
    assert back.uns["plexora_seg_qc"]["fingerprint"]
    assert back.uns["theirs"] == "left alone"


def test_the_panel_downloads_and_saves_every_category_at_its_thresholds(tmp_path):
    import csv
    import io

    import plexora

    made = _project(tmp_path)
    _run(made)
    client = plexora.app.test_client()
    base = "/plugins/qc/segmentation"
    cells = client.get(f"{base}/cells?datasource=qcsynth").get_json()
    assert set(cells["sizes"]) == {"large", "small", "irregular"}
    assert cells["outlier_z"] == 3.0
    # A looser size threshold flags at least as many, and the group is the count.
    loose = client.get(f"{base}/cells?datasource=qcsynth&z_small=1.5&z_large=1.5").get_json()
    groups = {g["key"]: g for g in loose["groups"]}
    assert loose["sizes"]["small"]["n"] >= cells["sizes"]["small"]["n"]
    assert groups["seg:small"]["n"] == loose["sizes"]["small"]["n"] \
        == len(groups["seg:small"]["ids"])
    assert loose["sizes"]["small"]["z"] == 1.5 and loose["sizes"]["irregular"]["z"] == 3.0
    assert client.get(f"{base}/cells?datasource=qcsynth&z_large=big").status_code == 400
    # The download is what the viewer draws: every threshold as asked.
    got = client.get(f"{base}/download?datasource=qcsynth&flag_under=0.95&z_small=1.5"
                     "&z_large=1.5")
    assert got.status_code == 200
    assert "attachment" in got.headers["Content-Disposition"]
    rows = list(csv.DictReader(io.StringIO(got.get_data(as_text=True))))
    assert {"cell_id", "under_segmented", "over_segmented", "large", "small", "irregular",
            "status", "under_score", "over_score", "area_z", "circularity_z"} <= set(rows[0])
    small = {int(r["cell_id"]) for r in rows if r["small"] == "true"}
    assert small == set(groups["seg:small"]["ids"])
    strict = client.get(f"{base}/cells?datasource=qcsynth&flag_under=0.95").get_json()
    assert sum(r["under_segmented"] == "true" for r in rows) \
        == strict["counts"]["under_segmented"]
    state = client.get(f"{base}?datasource=qcsynth").get_json()
    assert state["segmentation_qc"]["table_kind"] == "csv"
    # Save: Segmentation QC's columns alone, at the thresholds sent.
    saved = client.post(f"{base}/write", data=json.dumps(
        {"datasource": "qcsynth", "z_small": 1.5, "z_large": 1.5})).get_json()
    assert saved["ok"], saved
    assert {"plexora_seg_qc_small", "plexora_seg_qc_under_segmented"} \
        <= set(saved["written"]["columns"])
    table = list(csv.DictReader(open(tmp_path / "_qcsynth_files" / "cells.csv",
                                     encoding="utf-8")))
    assert "plexora_qc_pass" not in table[0]
    ids = {int(r["CellID"]) for r in table}
    assert {int(r["CellID"]) for r in table if r["plexora_seg_qc_small"] == "true"} \
        == small & ids
    # A second Save is refused until the user says replace.
    again = client.post(f"{base}/write", data=json.dumps({"datasource": "qcsynth"}))
    assert again.status_code == 409
    assert client.post(f"{base}/write", data=json.dumps(
        {"datasource": "qcsynth", "replace": True})).get_json()["ok"]
    assert client.post(f"{base}/write", data=json.dumps(
        {"datasource": "qcsynth", "z_small": "tiny"})).status_code == 400


def test_the_density_map_follows_the_view(tmp_path):
    import base64

    import plexora
    from plexora.plugins.qc.server.segqc import run

    _project(tmp_path)
    summary, _reused = _run(None)
    client = plexora.app.test_client()
    base = "/plugins/qc/segmentation/density?datasource=qcsynth"
    whole = client.get(f"{base}&box=0,0,768,768&bins=16").get_json()
    assert whole["ok"] and whole["available"]
    grid = whole["grid"]
    assert (grid["nx"], grid["ny"]) == (16, 16)
    assert list(whole["layers"]) == list(run.DENSITY_CATEGORIES)
    under = np.frombuffer(base64.b64decode(whole["layers"]["under"]["values"]), np.uint8)
    assert under.size == 16 * 16 and under.max() > 0
    assert whole["layers"]["under"]["n"] == summary["counts"]["under_segmented"]
    support = np.frombuffer(base64.b64decode(whole["support"]), np.uint8)
    assert support.size == under.size and support.max() == 255
    # A uniform synthetic mask has almost no spread: the floor keeps ordinary
    # cells from being called outliers.
    for name in ("large", "small", "irregular"):
        assert whole["layers"][name]["pct_cells"] < 5, name
    # A looser size threshold flags no fewer.
    loose = client.get(f"{base}&box=0,0,768,768&bins=16&z_large=1.5").get_json()
    assert loose["layers"]["large"]["n"] >= whole["layers"]["large"]["n"]
    assert loose["thresholds"]["large"] == 1.5
    # A stricter viewing threshold flags no more.
    strict = client.get(f"{base}&box=0,0,768,768&bins=16&flag_under=0.95").get_json()
    assert strict["layers"]["under"]["n"] <= whole["layers"]["under"]["n"]
    # Zoomed in, the grid is finer -- but never finer than a cell and a half
    # (these cells are ~25 px apart).
    close = client.get(f"{base}&box=0,0,384,384&bins=64").get_json()
    assert 30 < close["grid"]["step"] < grid["step"]
    assert client.get(f"{base}&box=1,1,1,1").status_code == 400
    groups = client.get("/plugins/qc/segmentation/cells?datasource=qcsynth").get_json()
    assert groups["max_id"] >= max(max(g["ids"] or [0]) for g in groups["groups"])
