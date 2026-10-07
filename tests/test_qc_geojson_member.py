"""The QC regions export says what its coordinates mean, like the ROI export.

`qc_regions.geojson` used to be a bare FeatureCollection: the ROI tool refused
to import it ("not exported by Plexora") and SCIMAP Pro's `addExternalROI`
could not tell which image it belonged to. It now carries the ROI export's
`plexora` member -- producer `qc`, the image id, the coordinate space, its
categories, the result -- and a `category_id` on every feature.
"""

from __future__ import annotations

import json

import pytest

from plexora.agent import AgentSession, invoke, registry
from plexora.plugins.roi.server import geojson as roi_geojson
from tests.qc_fixtures import make_qc_project


@pytest.fixture(autouse=True)
def _qc():
    registry.discover(["roi", "qc"])


def _box(x0, y0, x1, y1):
    return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1],
                                                [x0, y0]]]}


def _ok(result):
    assert result["ok"], json.dumps(result.get("error"), default=str)[:2000]
    return result["result"]


@pytest.fixture
def exported(tmp_path):
    make_qc_project(tmp_path, artifacts=())
    session = AgentSession()
    _ok(invoke(session, "create_roi", {"project": "qcsynth", "category": "QC: Tissue fold",
                                       "geometry": _box(100, 100, 300, 300)}))
    _ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
    files = _ok(invoke(session, "export_qc", {"project": "qcsynth", "what": "rois"}))["files"]
    return json.loads(open(files["regions"], encoding="utf-8").read())


def test_the_export_carries_the_plexora_member(exported):
    from plexora.plugins.roi.server import schema

    member = exported["plexora"]
    assert member["producer"] == "qc" and member["datasource"] == "qcsynth"
    assert member["schema_version"] == schema.SCHEMA_VERSION
    assert member["coordinate_space"]["type"] == "image_pixels"
    assert member["coordinate_space"]["width"] and member["result_id"]
    assert member["exported_at"] and member["plugin_version"]


def test_every_feature_names_a_category_the_member_lists(exported):
    ids = {category["id"] for category in exported["plexora"]["categories"]}
    assert ids and all(i.startswith("qc_") for i in ids)
    for feature in exported["features"]:
        assert feature["properties"]["category_id"] in ids


def test_the_roi_tool_accepts_it(exported):
    from plexora.plugins.roi.server import schema

    errors, warnings = roi_geojson.validate_document(exported)
    assert errors == [] and warnings == {}
    op, report = roi_geojson.import_features(schema.default_state(), exported)
    assert report["imported"] == len(exported["features"])
    # Imported under their own categories, never merged into a user's.
    assert {c["id"] for c in op["categories"]} == {c["id"] for c in
                                                   exported["plexora"]["categories"]}
    assert {f["category_id"] for f in op["features"]} <= {c["id"] for c in op["categories"]}


def test_a_subset_project_names_its_image(tmp_path):
    """A project reading one image of a multi-image table says which."""
    from types import SimpleNamespace

    from plexora.plugins.qc.server import export
    from tests.scimappro_fixtures import make_anndata_project

    made = make_anndata_project(tmp_path)
    member = export._member(SimpleNamespace(name=made["name"]), {"result_id": "qr_x"}, [])
    assert member["image_id"] == "slide_A" and member["result_id"] == "qr_x"
    assert member["coordinate_space"]["width"] == 256


def test_the_protocol_reads_its_image_and_categories(exported):
    from spatialbridge.anndata import regions_from_geojson, regions_image_id

    regions = regions_from_geojson(exported)
    assert regions.bridge.producer == "qc"
    assert regions.bridge.coordinate_space.type == "image_pixels"
    assert len(regions.features) == len(exported["features"])
    # qcsynth reads a whole CSV, so its member names the project as datasource.
    assert regions_image_id(exported) == "qcsynth"


def test_the_bridge_collects_qc_as_the_protocols_object(exported):
    from spatialbridge.schema import QCResult
    from spatialbridge.tools import BridgeTools

    from plexora.agent.bridge_provider import PlexoraProvider

    collected = BridgeTools(PlexoraProvider()).call(
        "bridge_collect", {"kind": "qc", "arguments": {"project": "qcsynth"}})
    result = QCResult.model_validate(collected)
    assert result.result_id == exported["plexora"]["result_id"]
    assert result.excluded and all(isinstance(i, str) for i in result.excluded)
    assert set(result.reasons) <= set(result.excluded)
    assert result.regions.bridge.producer == "qc"
    assert result.summary["n_excluded"] == len(result.excluded)
    assert result.summary["cell_id"]["kind"] == "obs_column"
