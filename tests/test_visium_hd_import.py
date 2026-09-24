"""A Visium HD run, from the folder to a sample with bins, a table and a mask.

Every registration is checked against the affine the fixture PLANTED (a
180-degree turn and a mirror, the way the pancreas run is), so a fit that was
merely self-consistent could not pass.
"""

import numpy as np
import pytest

import plexora

from plexora.server.models import data_model, import_proposal, import_sample
from plexora.server.models.project import Project
from plexora.server.utils import ngff_transform, spatial_scene

from tests.tenx_fixtures import (GRID_TO_FULLRES, HIRES_SCALEF,
                                 MICRONS_PER_PIXEL, write_visium_hd_run)

pytest.importorskip("pyarrow")


@pytest.fixture
def run(tmp_path):
    return write_visium_hd_run(tmp_path / "Pancreas_run", grid=40,
                               levels=(2, 8, 16), segmented=True)


def _sample(paths, answers=None):
    proposal = import_proposal.inspect_paths([str(p) for p in paths],
                                             answers=answers or {})
    assert len(proposal.samples) == 1, proposal.to_dict()
    return proposal.samples[0]


def _layer(sample, layer_id):
    return next(l for l in sample.layers if l.id == layer_id)


# -- detection ----------------------------------------------------------------

def test_a_run_is_found_from_every_folder_somebody_might_pick(run):
    outs, truth = run
    for picked in (outs, outs / "binned_outputs",
                   truth["levels"][8]["dir"], outs.parent):
        assert (spatial_scene.is_visium_hd_run(picked)
                or import_proposal._lone_bundle(picked) is not None), picked
    # Checked before standard Visium, which a lone level folder resembles.
    assert not spatial_scene.is_visium_run(outs)
    levels = spatial_scene.visium_hd_levels(outs)
    assert [level.size_um for level in levels] == [2.0, 8.0, 16.0]


def test_a_download_folder_of_side_files_still_finds_the_run(run):
    """10x's public datasets unpack to `outs/` beside run-level side files.

    The metrics CSV reads as a one-row cell table and the barcode mappings
    as an unknown parquet; either one counted as found and the run beside
    them was never looked at.
    """
    outs, _ = run
    wrapper = outs.parent
    prefix = "Visium_HD_Human_Pancreas_"
    (wrapper / f"{prefix}metrics_summary.csv").write_text(
        "Sample ID,Number of Reads,Mean Reads per Bin\nP1,100,2.5\n")
    table = pytest.importorskip("pyarrow.parquet")
    import pyarrow as pa
    table.write_table(pa.table({"square_002um": ["s_002um_00000_00000-1"],
                                "square_008um": ["s_008um_00000_00000-1"],
                                "cell_id": ["cellid_000000001-1"],
                                "in_nucleus": [True], "in_cell": [True]}),
                      wrapper / f"{prefix}barcode_mappings.parquet")
    (wrapper / f"{prefix}web_summary.html").write_text("<html></html>")

    proposal = import_proposal.inspect_paths([str(wrapper)])
    assert not proposal.to_dict()["warnings"]
    sample = _sample([wrapper])
    assert {"tissue_image", "bins", "cells"} <= {l.id for l in sample.layers}

    # Picked on its own, a side file says what to pick instead.
    alone = import_proposal.inspect_paths(
        [str(wrapper / f"{prefix}barcode_mappings.parquet")]).to_dict()
    assert not alone["samples"]
    assert "outs folder" in alone["unrecognised"][0]["reason"]


def test_the_grid_is_registered_by_the_affine_the_run_used(run):
    outs, truth = run
    level = spatial_scene.visium_hd_levels(outs)[0]
    transform, residual = spatial_scene.visium_hd_bin_transform(level)
    assert residual < 1e-6
    assert np.allclose(transform, GRID_TO_FULLRES, atol=1e-6)
    # A similarity by construction, mirror included: nothing for the viewer
    # to refuse as shear or anisotropy.
    assert ngff_transform.supported_by_osd(transform)
    assert ngff_transform.decompose(transform)["flipped"]
    assert spatial_scene.visium_hd_grid_shape(level) == (40, 40)


# -- the proposal ---------------------------------------------------------------

def test_the_proposal_is_one_sample_drawn_in_the_hires_picture(run):
    outs, truth = run
    sample = _sample([outs], answers={"bin-size": "8"})
    assert sample.name == "Pancreas_run"
    image = _layer(sample, "tissue_image")
    assert image.reference and image.role == "image"
    assert image.transform is None
    assert image.pixel_size == pytest.approx(MICRONS_PER_PIXEL / HIRES_SCALEF)

    bins = _layer(sample, "bins")
    assert bins.kind == "points" and bins.modality == "visium_bins"
    assert bins.render["pointKind"] == "bin"
    assert bins.render["gridShape"] == [40, 40]
    expected = ngff_transform.compose(
        (HIRES_SCALEF, 0, 0, HIRES_SCALEF, 0, 0), GRID_TO_FULLRES)
    assert np.allclose(bins.transform, expected, atol=1e-6)
    assert bins.frame is None                    # composed exactly once

    table = _layer(sample, "cells")
    assert table.role == "table" and table.src.endswith(
        "square_008um/filtered_feature_bc_matrix.h5")
    notes = {l.id for l in sample.layers if l.role == "note"}
    assert {"table_2", "table_16", "table_cells", "cell_boundaries"} <= notes
    question = next(q for q in sample.questions if q.id == "bin-size")
    assert [o["value"] for o in question.options] == ["2", "8", "16", "cells"]
    # A bin table joins no cell polygons: the mask waits for the cells table.
    assert "mask" in sample.missing


def test_a_segmented_run_defaults_to_the_cells_its_mask_belongs_to(
        run, tmp_path):
    """The polygons can only be the mask of the cells table, so a run that
    has them analyses cells unless told otherwise; one without falls back to
    8 µm bins."""
    outs, _ = run
    sample = _sample([outs])
    question = next(q for q in sample.questions if q.id == "bin-size")
    assert question.default == "cells"
    assert "(recommended)" in question.options[-1]["label"]
    assert _layer(sample, "cells").src.endswith("filtered_feature_cell_matrix.h5")
    assert _layer(sample, "cell_boundaries").role == "mask"

    bins_only, _ = write_visium_hd_run(tmp_path / "Bins_only", grid=40,
                                       levels=(2, 8, 16))
    sample = _sample([bins_only])
    question = next(q for q in sample.questions if q.id == "bin-size")
    assert question.default == "8"
    assert _layer(sample, "cells").src.endswith(
        "square_008um/filtered_feature_bc_matrix.h5")


def test_choosing_cells_makes_the_polygons_the_mask(run):
    outs, _ = run
    sample = _sample([outs], answers={"bin-size": "cells"})
    table = _layer(sample, "cells")
    assert table.src.endswith("filtered_feature_cell_matrix.h5")
    mask = _layer(sample, "cell_boundaries")
    assert mask.role == "mask"
    assert np.allclose(mask.transform,
                       (HIRES_SCALEF, 0, 0, HIRES_SCALEF, 0, 0))
    assert "mask" not in sample.missing


def test_finishing_twice_does_not_scale_twice(run):
    outs, _ = run
    sample = _sample([outs])
    before = _layer(sample, "bins").transform
    import_proposal._finish(sample)
    assert _layer(sample, "bins").transform == before


# -- registration -----------------------------------------------------------------

def _register(outs, answers=None, monkeypatch=None):
    return import_sample.import_sample([str(outs)],
                                       answers=answers or {"bin-size": "8"})


def test_the_dialog_can_watch_a_registration_before_the_sample_exists(
        run, monkeypatch):
    """`/import/status?token=` answers while `/import/sample` is running.

    The 10x conversion is minutes on a real run and happens before the
    sample has a name, so without this the dialog shows "waiting" on every
    row until it all lands at once.
    """
    from plexora.server.models import layer_jobs

    outs, _ = run
    seen = []
    original = layer_jobs.registration_progress

    def spy(done, total, label=None):
        original(done, total, label)
        seen.append(layer_jobs.registration("tok-1"))

    monkeypatch.setattr(layer_jobs, "registration_progress", spy)
    result = import_sample.import_sample([str(outs)], token="tok-1",
                                         answers={"bin-size": "8"})

    assert seen, "the conversion never reported"
    middle = seen[len(seen) // 2]["layers"]
    # Notes are recorded, not prepared, so they are not rows at all.
    assert set(middle) == {"tissue_image", "bins", "cells"}
    assert middle["tissue_image"]["status"] == "ready"
    assert middle["cells"]["status"] == "pending"
    assert middle["cells"]["stage"] == "registering"
    assert "10x matrix" in seen[-1]["layers"]["cells"]["stage_label"]
    assert [d["layers"]["cells"]["progress"] for d in seen][-1] == 100
    # Gone once the POST answers; the sample's own status takes over.
    assert layer_jobs.registration("tok-1") is None
    assert result["name"]

    client = plexora.app.test_client()
    body = client.get("/import/status?token=tok-1").get_json()
    assert body == {"layers": {}, "pending": False}


def test_an_imported_run_reads_as_a_sample_of_bins(run, monkeypatch):
    outs, truth = run
    # Wide below the fixture's six genes, so the lazy path is the one tested.
    monkeypatch.setenv("PLEXORA_WIDE_FEATURE_LIMIT", "3")
    result = _register(outs)
    project = Project.load(result["name"])
    bins = project.layer("bins")
    assert bins.status == "pending"             # nothing registered a builder
    assert bins.render["pointKind"] == "bin"
    assert "frameScale" not in bins.render
    assert project.image.pixel_size["value"] == pytest.approx(
        MICRONS_PER_PIXEL / HIRES_SCALEF)
    bundle = project.bundles[0]
    assert bundle["format"] == "visium_hd"
    assert bundle["frameScale"] == pytest.approx(HIRES_SCALEF)

    spec = project.dataset
    assert spec.type == "anndata"
    assert spec.src.endswith(".h5ad") and "tenx" in spec.src
    assert spec.coordinates["scale"] == pytest.approx(HIRES_SCALEF)
    assert "graphclust" in spec.columns.metadata
    assert spec.columns.markers[:3] == ("INS", "GCG", "PRSS1")

    data_model.load_datasource(result["name"], reload=True)
    level = truth["levels"][8]
    frame = data_model.get_datasource_df()
    assert frame.height == len(level["barcodes"])
    assert "INS" not in frame.columns           # wide: genes are not loaded
    a, b, c, d, e, f = level["transform"]
    x = (a * (level["cols"] + 0.5) + c * (level["rows"] + 0.5) + e) * HIRES_SCALEF
    assert np.allclose(frame["X"].to_numpy(), x)

    columns = data_model.get_filter_columns(result["name"], ["INS", "KRT19"])
    expected = level["pooled"][:, level["rows"], level["cols"]]
    assert np.array_equal(columns["INS"], expected[0])
    assert np.array_equal(columns["KRT19"], expected[3])

    described = data_model.get_datasource_description(result["name"])
    assert set(described) >= {"INS", "GCG", "INS-1", "X", "Y"}
    assert described["INS"]["max"] == float(expected[0].max())
    assert len(described["INS"]["histogram"]) == 50


def test_an_imported_run_of_cells_is_keyed_by_the_polygon_ids(run):
    outs, truth = run
    result = _register(outs, answers={"bin-size": "cells"})
    project = Project.load(result["name"])
    assert project.dataset.obs_id_field == "cell_id"
    assert project.dataset.roles.cell_id == "cell_id"
    segmentation = project.segmentation
    assert segmentation.source.endswith("cell_segmentations.geojson")
    assert np.allclose(segmentation.transform,
                       (HIRES_SCALEF, 0, 0, HIRES_SCALEF, 0, 0))

    data_model.load_datasource(result["name"], reload=True)
    frame = data_model.get_datasource_df()
    assert np.array_equal(frame["cell_id"].to_numpy(), truth["cells"]["ids"])


def test_the_cell_polygons_draw_where_the_cells_are(run, tmp_path):
    from plexora.server.utils import boundary_mask

    outs, truth = run
    geojson = outs / "segmented_outputs" / "cell_segmentations.geojson"
    assert boundary_mask.is_boundary_geojson(geojson)
    assert boundary_mask.is_boundary_source(geojson)
    polygons = boundary_mask.read_polygons(
        geojson, transform=(HIRES_SCALEF, 0, 0, HIRES_SCALEF, 0, 0))
    assert np.array_equal(polygons.labels, truth["cells"]["ids"])
    assert polygons.offsets[-1] == 5 * len(truth["cells"]["ids"])
    # Every cell's box contains its centroid, in hires pixels.
    centres = truth["cells"]["centres"] * HIRES_SCALEF
    assert ((polygons.bounds[:, 0] <= centres[:, 0])
            & (centres[:, 0] <= polygons.bounds[:, 2])).all()
