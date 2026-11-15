"""QC and ROI leads from the Nov 2026 audit (docs/internal/AUDIT_2026-11-14.md
§7), each pinned by the symptom it had:

- pg-qc-c-4: the QC export wrote cells.csv / cells.h5ad from `qc_cells`
  whatever result those calls were derived for, labelled as the active one
  (a new result activated whose cell write failed kept the old calls);
- pg-qc-c-5: every panel download (`/download/<project>`) left a full export
  folder under agent_root/exports behind, never removed;
- pg-roi-9: a GeoJSON import (`roi.bulk_create`) scanned every stored ROI
  for each feature's id -- quadratic in the import;
- pg-qc-a-7: DNA retention compared cycles on a guessed cycle structure
  (names only), which registration QC refuses, and turned it into
  exclude-level `dna_loss` marker flags;
- the client leads (pg-qc-static-1/2/3, pg-roi-4/6): tests/js/
  qc_roi_audit_leads_3_probe.mjs, run below.
"""

import shutil
import subprocess
import types
from pathlib import Path

import polars as pl
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# -- pg-qc-c-4 ------------------------------------------------------------------------------


def _export_world(monkeypatch, tmp_path, cells_result_id):
    from plexora.plugins.qc.server import export, results
    from plexora.plugins.qc.server.segqc import run as segqc

    active = {"result_id": "qr_new", "candidates": {}, "channels": []}
    document = {"active_result_id": "qr_new", "results": {"qr_new": active}}
    cells = pl.DataFrame({"cell_id": [1, 2], "result_id": [cells_result_id] * 2})
    written = {}
    monkeypatch.setattr(results, "load", lambda project: document)
    monkeypatch.setattr(results, "cells", lambda project: cells)
    monkeypatch.setattr(results, "roi_meta", lambda project: None)
    monkeypatch.setattr(segqc, "current", lambda project: None)
    monkeypatch.setattr(export, "_root", lambda project, rid: tmp_path)
    monkeypatch.setattr(export, "cells_csv",
                        lambda frame, path, *a, **k: written.setdefault("csv", frame))
    monkeypatch.setattr(export, "cells_h5ad",
                        lambda ds, frame, *a, **k: written.setdefault("h5ad", frame))
    call = types.SimpleNamespace(session=types.SimpleNamespace(image_data=lambda p: None))
    return export, call, written


def test_the_export_leaves_out_calls_derived_for_another_result(monkeypatch, tmp_path):
    export, call, written = _export_world(monkeypatch, tmp_path, "qr_old")
    out = export.export(call, "p", what="cells")
    assert "cells" not in out["files"] and "csv" not in written
    out = export.export(call, "p", what="anndata")
    assert "anndata" not in out["files"] and "h5ad" not in written


def test_the_export_writes_the_active_results_own_calls(monkeypatch, tmp_path):
    export, call, written = _export_world(monkeypatch, tmp_path, "qr_new")
    out = export.export(call, "p", what="cells")
    assert out["files"].get("cells") and written["csv"] is not None


# -- pg-qc-c-5 ------------------------------------------------------------------------------


def test_a_panel_download_removes_its_export_folder(monkeypatch, tmp_path):
    from flask import Flask

    from plexora import api, paths
    from plexora.plugins.qc.server import routes

    root = tmp_path / "agent"
    monkeypatch.setattr(paths, "agent_root", lambda: root)
    folder = root / "exports" / "qc_p_qr_1_20261114T000000"
    folder.mkdir(parents=True)
    (folder / "cells.csv").write_text("cell_id\n1\n", encoding="utf-8")
    (folder / "summary.json").write_text("{}", encoding="utf-8")

    def invoke(tool, arguments, **_kw):
        return api.json_response({"ok": True, "folder": str(folder),
                                  "files": {"cells": str(folder / "cells.csv"),
                                            "summary": str(folder / "summary.json")}})

    monkeypatch.setattr(routes, "_invoke", invoke)
    app = Flask(__name__)
    app.register_blueprint(routes.qc_bp, url_prefix="/qc")
    with app.test_client() as client:
        response = client.get("/qc/download/p?kind=cells.csv")
        assert response.status_code == 200
        assert response.get_data() == b"cell_id\n1\n"
        response.close()
    assert not folder.exists()


def test_a_download_never_removes_a_folder_outside_the_exports(monkeypatch, tmp_path):
    from flask import Flask

    from plexora import api, paths
    from plexora.plugins.qc.server import routes

    monkeypatch.setattr(paths, "agent_root", lambda: tmp_path / "agent")
    elsewhere = tmp_path / "mine"
    elsewhere.mkdir()
    (elsewhere / "cells.csv").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(routes, "_invoke", lambda tool, arguments, **_kw: api.json_response(
        {"ok": True, "folder": str(elsewhere), "files": {"cells": str(elsewhere / "cells.csv")}}))
    app = Flask(__name__)
    app.register_blueprint(routes.qc_bp, url_prefix="/qc")
    with app.test_client() as client:
        client.get("/qc/download/p?kind=cells.csv").close()
    assert (elsewhere / "cells.csv").is_file()


# -- pg-roi-9 -------------------------------------------------------------------------------


def test_a_bulk_import_looks_each_id_up_once(monkeypatch):
    from plexora.plugins.roi.server import operations, schema

    state = schema.default_state(100, 100)
    category = {"id": "cat_a", "label": "A", "color": "#ff0000"}
    features = [{"id": f"roi_{i}", "category_id": "cat_a", "name": "",
                 "geometry": {"type": "Polygon",
                              "coordinates": [[[i, 0], [i + 1, 0], [i + 1, 1], [i, 0]]]}}
                for i in range(400)]
    calls = {"n": 0}
    real = schema.find

    def counting(items, item_id):
        calls["n"] += 1
        return real(items, item_id)

    monkeypatch.setattr(schema, "find", counting)
    state = operations.apply_operations(state, [{"op": "roi.bulk_create",
                                                 "categories": [category],
                                                 "features": features}])
    entry = schema.image_entry(state, schema.DEFAULT_IMAGE)
    assert [f["id"] for f in entry["features"]] == [f["id"] for f in features]
    # Not one scan of the stored features (or categories) per imported one.
    assert calls["n"] < 10, calls["n"]


def test_a_bulk_import_still_refuses_a_duplicate_id():
    from plexora.plugins.roi.server import operations, schema

    state = schema.default_state(100, 100)
    category = {"id": "cat_a", "label": "A", "color": "#ff0000"}
    ring = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
    one = {"id": "roi_1", "category_id": "cat_a", "name": "", "geometry": ring}
    with pytest.raises(ValueError, match="already exists"):
        operations.apply_operations(state, [{"op": "roi.bulk_create", "categories": [category],
                                             "features": [one, dict(one)]}])


# -- pg-qc-a-7 ------------------------------------------------------------------------------


@pytest.mark.pro
def test_dna_retention_refuses_a_guessed_cycle_structure(tmp_path, monkeypatch):
    from plexora.agent import AgentSession, registry
    from plexora.agent.errors import AgentError
    from plexora.plugins.qc.server import cycles, dna_retention
    from tests.qc_fixtures import make_qc_project

    registry.discover(["roi", "qc"])
    make_qc_project(tmp_path, artifacts=("cycle_dropout",))
    session = AgentSession()
    real = cycles.infer

    def guessed(names, *, override=None):
        found = real(names, override=override)
        return {**found, "method": "name_suffix", "confidence": 0.7}

    monkeypatch.setattr(cycles, "infer", guessed)
    try:
        with pytest.raises(AgentError) as caught:
            dna_retention.plan(session, "qcsynth")
        assert caught.value.code == "precondition_missing"
        assert "set_qc_cycles" in caught.value.message
        monkeypatch.setattr(cycles, "infer", real)
        fp, _context = dna_retention.plan(session, "qcsynth")
        assert fp
    finally:
        from plexora.agent import render
        from plexora.server.utils import source_image

        render.close_masks()
        source_image.close_readers()


# -- the client leads -----------------------------------------------------------------------


node = shutil.which("node")

PROBE = REPO_ROOT / "tests" / "js" / "qc_roi_audit_leads_3_probe.mjs"

CHECKS = (
    "pg-qc-static-1: a channel QC places is levelled without a save",
    "pg-qc-static-1: a slot that will not level keeps no silent flag",
    "pg-qc-static-1: the registration check's channels are levelled without a save",
    "pg-qc-static-2: a region drawn while another saves is saved after it",
    "pg-qc-static-3: closing the registration check unbinds its colour listener",
    "pg-roi-4: map to cells waits for queued edits to land",
    "pg-roi-4: map to cells refuses while edits are refused",
    "pg-roi-4: save to file refuses while edits are unsaved",
    "pg-roi-6: a reload whose read was overtaken by an edit keeps the edit",
    "pg-roi-6: a reload whose read was overtaken by a save keeps the newer revision",
    "pg-roi-6: from cells saves queued edits first and never wipes them",
)


@pytest.mark.skipif(node is None, reason="node is not installed")
def test_client_probe():
    done = subprocess.run([node, str(PROBE)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    lines = [line.strip()[4:] for line in done.stdout.splitlines()
             if line.strip().startswith("ok  ")]
    assert tuple(lines) == CHECKS
