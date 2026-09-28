"""QC's smaller promises: AnnData writes, overlap membership, the mirror
script, sheet determinism, strictness bands, cycle inference."""

import dataclasses
import json

import numpy as np
import pytest

from plexora.agent import AgentSession, invoke, registry
from plexora.agent.errors import AgentError
from plexora.agent.policy import Policy
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


# -- AnnData --------------------------------------------------------------------------------


def _anndata_project(tmp_path, info):
    import anndata as ad
    import pandas as pd

    from tests.helpers import ALL_CONFIRMED, anndata_spec, image_spec, project

    cells = info["cells"]
    markers = list(info["channels"])
    X = np.array([[c[m] for m in markers] for c in cells], dtype=np.float32)
    adata = ad.AnnData(X=X, obs=pd.DataFrame({"imageid": ["A"] * len(cells)},
                                             index=[f"cell_{c['id']}" for c in cells]),
                       var=pd.DataFrame(index=markers))
    adata.obsm["spatial"] = np.array([[c["x"], c["y"]] for c in cells], dtype=np.float64)
    adata.uns["theirs"] = "left alone"
    path = tmp_path / "cells.h5ad"
    adata.write_h5ad(path)
    record = project("qcad", dataset=anndata_spec(path, markers=markers),
                     image=image_spec(channels=markers, width=info["size"],
                                      height=info["size"], src=info["image_path"]),
                     confirmed=ALL_CONFIRMED)
    record = dataclasses.replace(record, image=dataclasses.replace(
        record.image, max_level=3, tile_width=128, tile_height=128))
    config_path = tmp_path / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    config["qcad"] = record.to_entry()
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return path


def test_the_calls_are_written_into_an_anndata_file(tmp_path):
    import anndata as ad

    from plexora.plugins.qc.server import schemas

    info = make_qc_project(tmp_path, size=512, grid=20, table=False, mask=False)
    path = _anndata_project(tmp_path, info)
    session = AgentSession()
    ok(invoke(session, "create_roi", {"project": "qcad", "category": "QC: Tissue fold",
                                      "geometry": _box(0, 0, 256, 256)}))
    ok(invoke(session, "refresh_qc", {"project": "qcad"}))
    written = ok(invoke(session, "write_qc_to_source", {"project": "qcad", "confirm": True},
                        policy=Policy.from_flags(allow_source_writes=True)))
    assert written["written"]["n_fail"] > 0
    adata = ad.read_h5ad(path)
    assert str(adata.obs["plexora_qc_pass"].dtype) == "boolean"
    inside = [f"cell_{c['id']}" for c in info["cells"] if c["x"] < 256 and c["y"] < 256]
    assert not adata.obs.loc[inside, "plexora_qc_pass"].any()
    assert (adata.obs.loc[inside, "plexora_qc_primary_reason"] == "region:tissue_fold").all()
    flags = adata.obsm["plexora_qc_flags"]
    assert list(flags.columns) == list(schemas.REASONS)
    assert flags.loc[inside, "region:tissue_fold"].all()
    assert adata.uns["plexora_qc"]["result_id"]
    assert adata.uns["theirs"] == "left alone"
    refused = invoke(session, "write_qc_to_source", {"project": "qcad", "confirm": True},
                     policy=Policy.from_flags(allow_source_writes=True))
    assert refused["error"]["code"] == "conflict"
    again = invoke(session, "write_qc_to_source", {"project": "qcad", "confirm": True,
                                                   "replace": True},
                   policy=Policy.from_flags(allow_source_writes=True))
    assert again["ok"]


# -- propagation -----------------------------------------------------------------------------


def test_membership_is_by_mask_overlap_and_kept_as_a_fraction(tmp_path):
    from plexora.plugins.qc.server import propagate, strictness
    from plexora.plugins.qc.server.cells import calls

    info = make_qc_project(tmp_path, size=512, grid=16, artifacts=())
    ds = AgentSession().data("qcsynth")
    # A region whose edge runs through a column of cells: those are partly in.
    cell = next(c for c in info["cells"] if c["x"] > 200)
    edge = cell["x"]
    region = {"roi_id": "r1", "geometry": _box(0, 0, edge, 512)}
    pairs, per_roi = propagate.propagate(ds, [region])
    assert per_roi["r1"]["method"] == "mask", per_roi
    fractions = dict(zip(pairs["cell_id"].to_list(), pairs["fraction"].to_list()))
    on_edge = [c["id"] for c in info["cells"] if abs(c["x"] - edge) < 1]
    assert on_edge and all(0.3 < fractions[i] < 0.7 for i in on_edge)
    inside = [c["id"] for c in info["cells"] if c["x"] < edge - 20]
    assert all(fractions[i] > 0.99 for i in inside)
    # Lenient needs most of a cell inside, strict a quarter: the edge column
    # is out under one and in under the other.
    result = {"result_id": "r", "candidates": {"c": {"id": "c", "roi_id": "r1",
                                                     "class": "tissue_fold",
                                                     "action": "exclude"}},
              "cells": {"modules": {}}}
    failing = {}
    for preset in ("lenient", "strict"):
        frame, _p, _s = calls.derive(ds, result, strictness.thresholds(preset), pairs=pairs)
        failing[preset] = set(frame.filter(~frame["pass"])["cell_id"].to_list())
    assert set(on_edge) <= failing["strict"] and not set(on_edge) & failing["lenient"]
    assert failing["lenient"] <= failing["strict"]


def test_membership_falls_back_to_centroids_without_a_mask(tmp_path):
    from plexora.plugins.qc.server import propagate

    make_qc_project(tmp_path, size=512, grid=16, artifacts=(), mask=False)
    ds = AgentSession().data("qcsynth")
    pairs, per_roi = propagate.propagate(ds, [{"roi_id": "r1",
                                               "geometry": _box(0, 0, 200, 200)}])
    assert per_roi["r1"]["method"] == "centroid"
    assert set(pairs["method"].to_list()) == {"centroid"}


# -- the mirror script -------------------------------------------------------------------------


def test_the_mirror_script_shows_the_candidate_outline():
    from plexora.plugins.qc.server import mirror_script

    geometry = _box(10, 10, 60, 60)
    packet = {"kind": "artifact_confirm", "units": [{"project": "p", "type": "candidate",
                                                     "id": "cand_x"}],
              "evidence": {}, "images": [{"artifact_id": "art_1", "role": "confirm_sheet"}]}
    unit = {"type": "candidate", "id": "cand_x", "channel": "CD3", "bbox": [10, 10, 60, 60],
            "class_hint": "tissue_fold", "variants": {"standard": {"geometry": geometry}}}
    script = mirror_script.script_for(packet, unit, None, current_project="p")
    kinds = [c["type"] for c in script]
    assert kinds == ["set_cell_render_mode", "fit_region", "show_shapes", "show_evidence"]
    shapes = next(c for c in script if c["type"] == "show_shapes")["arguments"]["shapes"]
    assert shapes[0]["geometry"] == geometry
    assert script == mirror_script.script_for(packet, unit, None, current_project="p")
    other = mirror_script.script_for(packet, unit, None, current_project="q")
    assert other[0]["type"] == "open_project"


def test_the_viewer_command_refuses_a_shape_with_neither_geometry_nor_bounds():
    from plexora.agent.core.viewer import ShapesInput, ViewerShape

    shapes = ShapesInput(shapes=[ViewerShape(id="a")])
    with pytest.raises(AgentError):
        from plexora.agent.core import viewer

        viewer.show_shapes(None, shapes)


# -- sheets and the render ------------------------------------------------------------------------


def test_the_shapes_overlay_is_deterministic_and_in_the_manifest(tmp_path):
    from plexora.agent import render
    from plexora.agent.render_spec import Bounds, ChannelSpec, RenderInput, ShapeSpec

    make_qc_project(tmp_path, size=512, grid=16, artifacts=())
    spec = RenderInput(project="qcsynth", bounds=Bounds(x=0, y=0, width=512, height=512),
                       channels=[ChannelSpec(name="DNA_1")],
                       shapes=[ShapeSpec(id="a", geometry=_box(50, 50, 200, 200),
                                         fill_alpha=0.3, label="c1"),
                               ShapeSpec(id="b", bounds=Bounds(x=300, y=300, width=100,
                                                               height=100), dash=True)])
    first = render.render_region(AgentSession(), spec, store=False)
    second = render.render_region(AgentSession(), spec, store=False)
    assert first["png"] == second["png"]
    assert [s["id"] for s in first["manifest"]["shapes"]] == ["a", "b"]
    assert "shapes" in first["manifest"]["overlays"]
    plain = render.render_region(AgentSession(), spec.model_copy(update={"shapes": None}),
                                 store=False)
    assert plain["png"] != first["png"]


def test_the_audit_sheet_is_the_same_bytes_every_time(tmp_path):
    from plexora.plugins.qc.server import scan, sheets

    make_qc_project(tmp_path, size=512, grid=16, artifacts=())
    session = AgentSession()
    result, _ = scan.load_or_run(session, "qcsynth", params={"cell_um": 25.0})
    rows = [{"number": i + 1, "channel": c["name"], "cycle": c.get("cycle"),
             "flags": c["flags"], "candidates": []} for i, c in enumerate(result.channels)]
    one = sheets.audit_sheet(session, "qcsynth", result, rows, index=1, total=1, fmt="png",
                             pixel=None, calibration=None, store=False)
    two = sheets.audit_sheet(session, "qcsynth", result, rows, index=1, total=1, fmt="png",
                             pixel=None, calibration=None, store=False)
    assert one["image"] == two["image"]
    width, height = one["size"]
    # The budget the skill promises: about a hundred vision tokens a channel.
    from plexora.agent.sessions.budget import vision_tokens

    assert vision_tokens(width * height) / sheets.CHANNELS_PER_SHEET < 140


# -- small rules -------------------------------------------------------------------------------------


def test_a_custom_strictness_outside_the_band_is_refused():
    from plexora.plugins.qc.server import strictness

    with pytest.raises(AgentError):
        strictness.thresholds("custom", {"area.k": 10.0})
    with pytest.raises(AgentError):
        strictness.thresholds("custom", {"not.a.key": 1.0})
    table = strictness.thresholds("custom", {"area.k": 3.2})
    assert table["area.k"] == 3.2


@pytest.mark.parametrize("names,method,groups", [
    (["DNA_1", "CD3", "CD8", "DNA_2", "CD20", "CD4"], "nuclear_repeats",
     [["DNA_1", "CD3", "CD8"], ["DNA_2", "CD20", "CD4"]]),
    (["CD3_c1", "CD8_c1", "CD20_c2", "CD4_c2"], "name_suffix",
     [["CD3_c1", "CD8_c1"], ["CD20_c2", "CD4_c2"]]),
    (["CD3", "CD8", "CD20"], "none", []),
])
def test_cycles_are_inferred_from_the_names(names, method, groups):
    from plexora.plugins.qc.server import cycles

    found = cycles.infer(names)
    assert found["method"] == method
    assert [c["channels"] for c in found["cycles"]] == groups


def test_a_cycle_override_wins():
    from plexora.plugins.qc.server import cycles

    found = cycles.infer(["A", "B", "C", "D"], override={"period": 2})
    assert found["method"] == "user"
    assert [c["channels"] for c in found["cycles"]] == [["A", "B"], ["C", "D"]]


def test_the_vocabulary_is_closed():
    from plexora.plugins.qc.server import schemas

    assert len(set(schemas.ARTIFACT_CLASSES)) == len(schemas.ARTIFACT_CLASSES) == 18
    assert set(schemas.CLASS_WORDS) == set(schemas.ARTIFACT_CLASSES)
    assert set(schemas.CLASS_COLORS) == set(schemas.ARTIFACT_CLASSES)
    assert set(schemas.PRIMARY_ORDER) == set(schemas.REASONS) - {
        "region:uncertain_manual_review"} | {"region:uncertain_manual_review"}
    for klass in schemas.ARTIFACT_CLASSES:
        assert schemas.class_of_label(schemas.roi_category_label(klass)) == klass
        assert schemas.class_of_category(schemas.roi_category_id(klass)) == klass
    for action in schemas.ACTIONS:
        assert schemas.action_of_name(schemas.roi_name(action, "tissue_fold", ["CD3"])) == action
