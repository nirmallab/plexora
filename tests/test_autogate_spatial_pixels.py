"""The spatial evidence layer, image-side: evidence modes, the background map,
and the targeted pass inside a real collage -- on synthetic projects whose
spill-over, background and dim positives are painted into the pixels."""

import numpy as np
import pytest

from plexora.agent import AgentSession
from tests.autogate_fixtures import make_spatial_gating_project

pytestmark = pytest.mark.paid


def _mode(session, name, marker="CD3"):
    from plexora.plugins.gating.server.autogate import spatial

    ds = session.data(name)
    return spatial.detect_mode(session, ds, marker)


def test_the_evidence_mode_follows_what_the_project_has(tmp_path):
    make_spatial_gating_project(tmp_path, "withmask", scenario="dense_spill", size=512)
    make_spatial_gating_project(tmp_path, "nomask", scenario="dense_spill_nomask", size=512)
    make_spatial_gating_project(tmp_path, "blank", scenario="graph_only", size=512)
    session = AgentSession()
    try:
        found = _mode(session, "withmask")
        assert found["mode"] == "A", found
        assert found["mask"]["usable"] and found["mask"]["probe"]["found_share"] >= 0.5
        nomask = _mode(session, "nomask")
        assert nomask["mode"] == "B" and not nomask["mask"]["usable"]
        assert _mode(session, "blank")["mode"] == "C"
        # A marker with no channel is expression-only whatever the image.
        assert _mode(session, "withmask", marker="NOT_A_MARKER")["mode"] == "C"
    finally:
        session.close()


def test_an_uncalibrated_image_is_measured_in_pixels_not_downgraded(tmp_path):
    make_spatial_gating_project(tmp_path, "uncal", scenario="dense_spill_uncal", size=512)
    session = AgentSession()
    try:
        found = _mode(session, "uncal")
        assert found["mode"] == "A" and found["pixel_source"] == "none"
        assert found["pixel_um"] is None
    finally:
        session.close()


def test_the_background_map_finds_the_painted_bump(tmp_path):
    from plexora.plugins.gating.server.autogate import spatial

    made = make_spatial_gating_project(tmp_path, "bump", scenario="low_snr", size=768)
    session = AgentSession()
    try:
        ds = session.data("bump")
        bgmap = spatial.background_map(session, ds, "CD4")
        assert bgmap is not None and bgmap["info"]["structure"] in ("smooth", "patchy")
        flat = spatial.background_map(session, ds, "CD8")
        assert flat["info"]["heterogeneity_log"] < bgmap["info"]["heterogeneity_log"]
        feats = spatial.per_cell(ds, "CD4", bgmap=bgmap)
        raised = made["cell_truth"]["CD4"]["background_region"] != "flat"
        assert np.nanmean(feats.local_bg[raised]) > 1.5 * np.nanmean(feats.local_bg[~raised])
    finally:
        session.close()


def test_the_density_strata_find_the_painted_regions(tmp_path):
    from plexora.plugins.gating.server.autogate import spatial

    made = make_spatial_gating_project(tmp_path, "regions", scenario="dense_spill", size=768)
    session = AgentSession()
    try:
        ds = session.data("regions")
        feats = spatial.per_cell(ds, "CD3")
        dense = made["cell_truth"]["CD3"]["density_region"] == "dense"
        agree = np.mean(feats.dense_env == dense)
        assert agree >= 0.9, agree
    finally:
        session.close()


def test_the_crop_hook_adds_no_reads_and_changes_no_pixel(tmp_path):
    from plexora.agent.evidence import collage
    from plexora.plugins.gating.server.autogate import cells as cellmod
    from plexora.plugins.gating.server.autogate import spatial

    make_spatial_gating_project(tmp_path, "hook", scenario="dense_spill", size=512)
    session = AgentSession()
    try:
        ds = session.data("hook")
        c = cellmod.cells(ds)
        rows = [{"key": "a", "label": "a", "cells": [
            {"cell_id": int(c.ids[i]), "x": float(c.xs[i]), "y": float(c.ys[i]),
             "value": 1.0} for i in range(0, 60, 10)]}]
        plain = collage.render_collage(session, ds, layout="t2", rows=rows, marker="CD3",
                                       gate=100.0, store=False, fmt="png")
        seen = {}

        def hook(crops, cells):
            seen["n"] = len(crops)
            return spatial.targeted_from_crops(crops, "CD3", cells, mode="A",
                                               compartment="membrane", radius_px=10)

        hooked = collage.render_collage(session, ds, layout="t2", rows=rows, marker="CD3",
                                        gate=100.0, store=False, fmt="png", crop_hook=hook)
        assert hooked["png"] == plain["png"]
        assert hooked["manifest"]["reads"] == plain["manifest"]["reads"]
        assert seen["n"] == 6 and len(hooked["per_cell"]) == 6
        placed = hooked["manifest"]["rows"][0]["cells"]
        assert all(cell["spatial"]["mode"] == "A" for cell in placed)
    finally:
        session.close()


def test_the_targeted_pass_supports_painted_spill_more_than_clean_cells(tmp_path):
    from plexora.agent.evidence import crops as cropmod
    from plexora.agent.render import resolve_channel
    from plexora.plugins.gating.server.autogate import cells as cellmod
    from plexora.plugins.gating.server.autogate import spatial
    from plexora.server.utils import source_image

    made = make_spatial_gating_project(tmp_path, "spill", scenario="dense_spill", size=768)
    session = AgentSession()
    try:
        ds = session.data("spill")
        truth = made["cell_truth"]["CD3"]
        c = cellmod.cells(ds)
        contaminated = np.flatnonzero(truth["contaminated"])[:24]
        clean = np.flatnonzero(~truth["contaminated"] & ~truth["positive"]
                               & (truth["density_region"] == "sparse"))[:24]
        record = ds.project
        _i, found_channel = resolve_channel("CD3", list(record.image.real_channels))
        key = source_image.channel_key(found_channel)
        side = 48
        found = {}
        feats = spatial.per_cell(ds, "CD3")
        gate = spatial.at_gate(ds, "CD3", feats, float(np.expm1(np.percentile(
            feats.vf[feats.eligible], 50))), mode="A")
        for name, rows in (("spill", contaminated), ("clean", clean)):
            cells = [{"cell_id": int(c.ids[r]), "x": float(c.xs[r]), "y": float(c.ys[r]),
                      **{k: v for k, v in spatial.cell_record(feats, gate, int(r)).items()
                         if k == "brightest"}}
                     for r in rows]
            crops, _ = cropmod.read_cell_crops(session, record, cells,
                                               channels={"CD3": key}, crop_px=side,
                                               tile_px=side, level=0)
            out = spatial.targeted_from_crops(crops, "CD3", cells, mode="A",
                                              compartment="membrane", radius_px=10)
            found[name] = np.mean([r["spill"] == "supported" for r in out.values()])
        assert found["spill"] > found["clean"]
    finally:
        session.close()
