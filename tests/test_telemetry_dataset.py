"""What opening a project tells telemetry: kinds and bands, never content."""

import json

import pytest

from plexora.datasource import register_image_datasource
from plexora.server.models import data_model
from plexora.server.models.project import Project
from plexora.telemetry import dataset, schema
from tests.brightfield_fixtures import write_planar_fluorescence, write_rgb_ome_tiff


def records(t, event):
    t.sync()
    t.sync()  # the first runs the deferred descriptor, which emits
    return [json.loads(r[3]) for r in t.queue.snapshot()[1] if r[2] == event]


def test_multiplex_descriptor(tmp_path):
    path = write_planar_fluorescence(tmp_path / "patient_29384.ome.tif", height=512, width=640,
                                     names=("CD45", "TP53", "DAPI"))
    register_image_datasource("patient_29384", path)
    project = Project.find("patient_29384")
    props = dataset.describe(project, {"dtype": "uint16", "rows": 12345})
    assert schema.validate_record("dataset.opened", props)
    assert props["image_kind"] == "ome_tiff"
    assert props["width"] == "100" and props["height"] == "100"
    assert props["pixels"] == "100k"
    assert props["channels_band"] == "2-3" and props["channels"] == 3
    assert props["bit_depth"] == "16" and props["rows"] == "10k"
    text = json.dumps(props)
    for secret in ("patient", "CD45", "TP53", str(tmp_path)):
        assert secret not in text


def test_brightfield_descriptor(tmp_path):
    path = write_rgb_ome_tiff(tmp_path / "he.ome.tif", height=512, width=640)
    register_image_datasource("he", path)
    props = dataset.describe(Project.find("he"), {"dtype": "uint8"})
    assert schema.validate_record("dataset.opened", props)
    assert props["image_kind"] == "brightfield" and props["bit_depth"] == "rgb"
    assert props["image_type"] == "brightfield"


def test_describe_survives_a_broken_project():
    class Broken:
        @property
        def image(self):
            raise RuntimeError("no")

    props = dataset.describe(Broken())
    assert schema.validate_record("dataset.opened", props)


def test_load_emits_project_load_and_one_descriptor(telemetry_enabled, tmp_path):
    t, _fake = telemetry_enabled
    t.start(None, "terminal", upload=False)
    path = write_planar_fluorescence(tmp_path / "panel.ome.tif", height=256, width=256)
    register_image_datasource("panel", path)
    data_model.load_datasource("panel")
    data_model.load_datasource("panel", reload=True)
    loads = records(t, "project.load")
    assert [r["outcome"] for r in loads] == ["ready", "ready"]
    assert {"total_ms", "image_ms"} <= set(loads[0])
    opened = records(t, "dataset.opened")
    assert len(opened) == 1  # once per project per session
    assert opened[0]["bit_depth"] == "16"
    assert "panel" not in json.dumps(loads + opened)


def test_missing_image_is_an_outcome_not_a_message(telemetry_enabled, tmp_path):
    t, _fake = telemetry_enabled
    t.start(None, "terminal", upload=False)
    path = write_planar_fluorescence(tmp_path / "gone.ome.tif", height=256, width=256)
    register_image_datasource("gone", path)
    path.unlink()
    with pytest.raises(Exception):
        data_model.load_datasource("gone")
    loads = records(t, "project.load")
    assert loads and loads[-1]["outcome"] in ("missing", "corrupt", "error")
    assert "gone" not in json.dumps(loads)
    assert not records(t, "dataset.opened")


def test_outcome_mapping():
    assert dataset.outcome_of(None) == "ready"
    assert dataset.outcome_of(FileNotFoundError("/x")) == "missing"
    assert dataset.outcome_of(PermissionError("/x")) == "inaccessible"
    assert dataset.outcome_of(KeyboardInterrupt()) == "error"


def test_import_summary(telemetry_enabled):
    from types import SimpleNamespace

    from plexora.server.models import import_sample

    t, _fake = telemetry_enabled
    t.start(None, "terminal", upload=False)
    proposal = SimpleNamespace(bundles=[{"format": "xenium", "root": "/Users/alice/run"}],
                               layers=[SimpleNamespace(role="layer", reference=True)] * 3)
    import_sample._report_import(proposal, "registered", 1200.0, replaced=False)
    (record,) = records(t, "import.summary")
    assert record == {"kind": "xenium", "outcome": "registered", "replaced": False,
                      "layers": 3, "register_ms": "1k-2.5k"}


def test_connect_outcomes():
    import socket

    from plexora.server.models.remote_sessions import connect_outcome

    assert connect_outcome(socket.gaierror("host.example")) == "dns"
    assert connect_outcome(ConnectionRefusedError()) == "refused"
    assert connect_outcome(TimeoutError()) == "timeout"
    assert connect_outcome(RuntimeError("alice@login.cluster")) == "other"
