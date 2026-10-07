"""Tissue first: one feathered tissue mask, and the glass beyond it written as
a Background ROI whose cells are annotated, never excluded."""

import csv
import json

import numpy as np
import pytest

from plexora.agent import AgentSession, invoke, registry


@pytest.fixture(autouse=True)
def _qc():
    registry.discover(["roi", "qc"])
    yield
    from plexora.agent import render
    from plexora.server.utils import source_image

    render.close_masks()
    source_image.close_readers()


def ok(answer):
    assert answer["ok"], json.dumps(answer.get("error"), default=str)[:3000]
    return answer["result"]


def _square(size, a, b):
    mask = np.zeros((size, size), dtype=bool)
    mask[a:b, a:b] = True
    return mask


def test_the_feather_adds_exactly_the_rim():
    from plexora.plugins.qc.server import tissue

    size, a, b, px = 64, 20, 44, 5
    mask = _square(size, a, b)
    feathered = tissue.feather(mask, px)
    yy, xx = np.mgrid[0:size, 0:size]
    dy = np.maximum(np.maximum(a - yy, yy - (b - 1)), 0)
    dx = np.maximum(np.maximum(a - xx, xx - (b - 1)), 0)
    expected = np.hypot(dy, dx) <= px
    assert (feathered == expected).all()
    assert (feathered & mask).sum() == mask.sum()
    # Nothing to grow: no tissue, every pixel tissue, no feather.
    assert not tissue.feather(np.zeros((8, 8), bool), 3).any()
    assert tissue.feather(np.ones((8, 8), bool), 3).all()
    assert (tissue.feather(mask, 0) == mask).all()
    # The feather in pixels: microns with a pixel size, pixels without.
    assert tissue.feather_px_at(0.5, 4.0) == pytest.approx(25.0 / 2.0)
    assert tissue.feather_px_at(None, 4.0) == pytest.approx(10.0)


def test_the_background_is_the_glass_with_the_tissue_a_hole():
    from shapely.geometry import shape

    from plexora.plugins.qc.server import background, consolidate, polygons, tissue

    size, factor = 96, 4.0
    feathered = tissue.feather(_square(size, 24, 72), 6)
    geometry = background.geometry_of_mask(feathered, factor, pixel_um=1.0,
                                           image_size=(size * factor, size * factor))
    assert geometry["type"] == "Polygon"
    found = shape(geometry)
    assert len(found.interiors) == 1           # the tissue, a hole in the glass
    assert found.bounds[0] >= 0 and found.bounds[2] <= size * factor
    from plexora.plugins.roi.server import geometry as roi_geometry

    assert roi_geometry.vertex_count(geometry) <= consolidate.LAYER_MAX_VERTICES
    grid = {"shape": (size, size), "cell_full_px": factor,
            "image_size": (size * factor, size * factor)}
    back = polygons.geometry_to_grid(geometry, grid)
    agree = float((back == ~feathered).mean())
    assert agree > 0.97, agree
    # No glass at all: no region.
    assert background.geometry_of_mask(np.ones((8, 8), bool), 4.0, pixel_um=1.0) is None


def test_the_scan_keeps_the_feathered_mask_beside_the_tight_one(tmp_path):
    from plexora.plugins.qc.server import scan as scanmod
    from tests.qc_fixtures import SCAN_PARAMS, make_qc_project

    make_qc_project(tmp_path, size=512, grid=20, levels=3)
    result, _reused = scanmod.load_or_run(AgentSession(), "qcsynth", params=SCAN_PARAMS)
    tight = result.tissue_overview()
    feathered = result.tissue_overview(feathered=True)
    assert tight is not None and feathered is not None
    assert (feathered | tight).sum() == feathered.sum() and feathered.sum() > tight.sum()
    assert (result.tissue(feathered=True) | result.tissue()).sum() == \
        result.tissue(feathered=True).sum()
    assert result.meta["tissue"]["feather_um"] == 25.0


@pytest.fixture
def glass_project(tmp_path):
    from tests.qc_fixtures import make_qc_project

    info = make_qc_project(tmp_path, size=1024, grid=40, levels=4, seg_errors={"glass": 6})
    assert len(info["truth"]["segmentation"]["glass"]) == 6
    return info


def test_the_background_roi_annotates_the_cells_on_the_glass(glass_project, tmp_path):
    import plexora
    from plexora.agent.policy import Policy
    from plexora.plugins.qc.server import results, schemas

    info = glass_project
    glass = set(info["truth"]["segmentation"]["glass"])
    session = AgentSession()
    written = ok(invoke(session, "write_qc_background_roi", {"project": "qcsynth"}))
    assert len(written["written"]) == 1 and written["n_background"] == len(glass)
    rois = ok(invoke(session, "list_rois", {"project": "qcsynth"}))["rois"]
    assert [r["category_id"] for r in rois] == ["qc_background"]
    assert rois[0]["name"].startswith("QC note:")
    meta = results.roi_meta("qcsynth")
    assert meta["class"].to_list() == [schemas.BACKGROUND_CLASS]
    assert meta["action"].to_list() == ["note"]
    # The panel's cell layer: one `background|note` group, the glass cells.
    client = plexora.app.test_client()
    groups = client.get("/plugins/qc/cells?datasource=qcsynth").get_json()["groups"]
    group = next(g for g in groups if g["reason"] == "background")
    assert group["status"] == "note" and group["category"] == "background"
    assert set(group["ids"]) == glass
    cells = results.cells("qcsynth")
    summary = results.active(results.load("qcsynth"))["cells"]
    assert summary["n_fail"] == 0 and summary["n_background"] == len(glass)
    assert cells["pass"].all()
    assert set(cells.filter(cells["background"])["cell_id"].to_list()) == glass
    # Every export keeps every row.
    exported = ok(invoke(session, "export_qc", {"project": "qcsynth"}))
    rows = list(csv.DictReader(open(exported["files"]["cells"], encoding="utf-8")))
    assert len(rows) == len(info["cells"])
    assert {int(r["cell_id"]) for r in rows if r["background"] == "true"} == glass
    ok(invoke(session, "write_qc_to_source", {"project": "qcsynth", "confirm": True},
              policy=Policy.from_flags(allow_source_writes=True)))
    table = list(csv.DictReader(open(tmp_path / "_qcsynth_files" / "cells.csv",
                                     encoding="utf-8")))
    assert len(table) == len(info["cells"])
    assert {int(r["CellID"]) for r in table if r["plexora_qc_background"] == "true"} == glass
    assert not [r for r in table if r["plexora_qc_pass"] == "false"]


def test_the_background_is_the_users_to_reshape_or_extend(glass_project):
    from plexora.plugins.qc.server import results, schemas
    from plexora.plugins.roi.server import service

    session = AgentSession()
    first = ok(invoke(session, "write_qc_background_roi", {"project": "qcsynth"}))
    roi_id = first["written"][0]
    ds = session.image_data("qcsynth")
    # Reshaped by the user: a second write keeps theirs and writes nothing.
    service.update_roi(ds, roi_id, geometry={"type": "Polygon", "coordinates": [
        [[0, 0], [1024, 0], [1024, 60], [0, 60], [0, 0]]]})
    again = ok(invoke(session, "write_qc_background_roi", {"project": "qcsynth"}))
    assert again["written"] == [] and again["kept"] == [roi_id]
    rois = ok(invoke(session, "list_rois", {"project": "qcsynth"}))["rois"]
    assert [r["id"] for r in rois] == [roi_id]
    # A polygon the user adds to "QC: Background" is adopted as a note.
    service.create_roi(ds, category=schemas.BACKGROUND["name"],
                       points=[[1000, 1000], [1020, 1000], [1020, 1020], [1000, 1020]])
    refreshed = ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
    assert refreshed["sync"]["adopted"]
    result = results.active(results.load("qcsynth"))
    adopted = [c for c in result["candidates"].values() if c.get("created_by") == "user"]
    assert adopted and all(c["class"] == schemas.BACKGROUND_CLASS for c in adopted)
    assert all(c["action"] == "note" for c in adopted)
    # Renaming it "QC exclude: ..." is the user removing those cells.
    service.update_roi(ds, roi_id, name="QC exclude: Background (outside the tissue)")
    ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
    summary = results.active(results.load("qcsynth"))["cells"]
    assert summary["by_reason"].get("background"), summary["by_reason"]
