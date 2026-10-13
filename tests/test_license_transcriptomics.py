"""Spatial transcriptomics is a Paid root, and on Free it is not there at all.

Without `transcriptomics` a Xenium, Visium or Visium HD run is not detected at
import: no layers, no bundle, and no warning that names a licence -- the run
reads as nothing Plexora can open, which on that plan is what it is. Nor does
the folder fall through to the loose-file ladder, which would offer the run's
morphology TIFF and its transcripts parquet piecemeal. A loose Xenium
transcripts parquet is likewise not offered (and not misread as a cell table).
With the grant every one of them is the bundle it always was; each child
(`transcriptomics:xenium`, `transcriptomics:visium`) unlocks its own runs.

An already-imported run still opens on Free. The two plugins stay installed --
their routes refuse with the `license` block, their static files load -- but
the page mounts neither section and emits neither plugin's scripts, while the
layer itself keeps its card and core's layer route keeps serving its tiles.
Nothing is deleted.

The agent side needs nothing: neither plugin contributes a capability, and the
core capabilities that read a transcript layer (render, describe) are reads,
which stay Free on every plan.
"""

from __future__ import annotations

import json

import numpy as np
import pytest
import tifffile

import plexora
from plexora.server.models import import_proposal
from plexora.server.models import transcript_tiles as tt
from plexora.server.models.project import ImageSpec, LayerSpec, Project

from tests.helpers import use_data_root
from tests.spatial_fixtures import write_transcripts_parquet

pytest.importorskip("pyarrow")


# -- fixtures -----------------------------------------------------------------

def _xenium_run(root):
    from tests.test_import_proposal import _xenium_run as write

    return write(root)


def _visium_run(root):
    pytest.importorskip("h5py")
    from tests.test_visium_import import write_visium_run

    return write_visium_run(root)


def _visium_hd_run(root):
    pytest.importorskip("h5py")
    from tests.tenx_fixtures import write_visium_hd_run

    outs, _truth = write_visium_hd_run(root)
    return outs


def _nothing_said_about_a_licence(proposal):
    said = " ".join([*proposal.warnings,
                     *(entry["reason"] for entry in proposal.unrecognised)]).lower()
    assert "licen" not in said and "paid" not in said, said


def _found_nothing(proposal):
    assert proposal.samples == [], [s.name for s in proposal.samples]
    assert [entry["reason"] for entry in proposal.unrecognised] == [
        "Nothing Plexora can read here."]
    assert proposal.warnings == []
    _nothing_said_about_a_licence(proposal)


def _bundle_format(proposal):
    assert len(proposal.samples) == 1, proposal.to_dict()
    formats = {layer.bundle["format"] for layer in proposal.samples[0].layers
               if layer.bundle}
    assert len(formats) == 1, formats
    return formats.pop()


# -- import: not detected on Free -------------------------------------------------

def test_a_xenium_run_is_not_detected_on_free(tmp_path):
    run = _xenium_run(tmp_path / "download" / "run_0042")
    _found_nothing(import_proposal.inspect_paths([str(run)]))
    # The wrapper folder still resolves to the run inside it -- and so to
    # nothing -- rather than offering the run's files one by one.
    _found_nothing(import_proposal.inspect_paths([str(tmp_path / "download")]))
    # Picked as a declared layer too: nothing, and no sentence about why.
    _found_nothing(import_proposal.inspect_paths(
        [str(run)], answers={f"added-as:{run}": "layer"}))


def test_a_xenium_run_is_the_bundle_it_was_with_the_grant(tmp_path, license_issuer):
    run = _xenium_run(tmp_path / "run_0042")
    license_issuer.install(license_issuer.issue(ent=["transcriptomics:xenium"]))
    proposal = import_proposal.inspect_paths([str(run)])
    assert _bundle_format(proposal) == "xenium"
    assert "transcripts" in {layer.modality for layer in proposal.samples[0].layers}


def test_a_visium_run_is_not_detected_on_free_and_is_with_the_grant(
        tmp_path, license_issuer):
    outs = _visium_run(tmp_path / "outs")
    _found_nothing(import_proposal.inspect_paths([str(outs)]))
    # The Xenium child does not open Visium.
    license_issuer.install(license_issuer.issue(ent=["transcriptomics:xenium"]))
    _found_nothing(import_proposal.inspect_paths([str(outs)]))
    license_issuer.install(license_issuer.issue(ent=["transcriptomics:visium"]))
    assert _bundle_format(import_proposal.inspect_paths([str(outs)])) == "visium"


def test_a_visium_hd_run_is_not_detected_on_free_and_is_with_the_grant(
        tmp_path, license_issuer):
    outs = _visium_hd_run(tmp_path)
    for picked in (outs, outs / "binned_outputs",
                   outs / "binned_outputs" / "square_008um", tmp_path):
        _found_nothing(import_proposal.inspect_paths([str(picked)]))
    license_issuer.install(license_issuer.issue(ent=["transcriptomics"]))
    assert _bundle_format(import_proposal.inspect_paths([str(outs)])) == "visium_hd"


def test_a_loose_transcripts_parquet_is_not_offered_on_free(tmp_path, license_issuer):
    path = write_transcripts_parquet(tmp_path / "transcripts.parquet", n=500,
                                     width=400, height=300)
    # Not as transcripts, and not as the cell table its x/y columns would
    # otherwise make it.
    _found_nothing(import_proposal.inspect_paths([str(path)]))
    license_issuer.install(license_issuer.issue(ent=["transcriptomics"]))
    sample = import_proposal.inspect_paths([str(path)]).samples[0]
    assert [layer.modality for layer in sample.layers] == ["transcripts"]


def test_a_free_licence_still_imports_what_is_not_transcriptomics(tmp_path):
    """The gate is the modality, not the folder: a plain image beside a run's
    worth of nothing still imports."""
    image = tmp_path / "slide.ome.tif"
    tifffile.imwrite(image, np.zeros((2, 64, 64), dtype=np.uint16))
    proposal = import_proposal.inspect_paths([str(image)])
    assert len(proposal.samples) == 1


def test_a_licensing_failure_reads_as_not_granted(tmp_path, monkeypatch, paid_license):
    from plexora import licensing

    def broken(_entitlement):
        raise RuntimeError("damaged licence file")

    monkeypatch.setattr(licensing, "allows", broken)
    assert import_proposal._licensed("transcriptomics:xenium") is False
    _found_nothing(import_proposal.inspect_paths([str(_xenium_run(tmp_path / "run"))]))


# -- the 10x matrix conversion ------------------------------------------------------

def test_the_10x_matrix_is_not_converted_on_free(tmp_path, monkeypatch):
    from plexora.server.routes import import_routes
    from plexora.server.utils import tenx_matrix

    outs = _visium_run(tmp_path / "outs")
    matrix = outs / "filtered_feature_bc_matrix.h5"

    def no_io(_path):
        raise AssertionError("the matrix was looked at on Free")

    monkeypatch.setattr(tenx_matrix, "is_tenx_matrix", no_io)
    assert import_routes._tenx_context("demo", matrix, None) is None


def test_the_10x_matrix_is_converted_with_the_grant(tmp_path, license_issuer):
    from plexora.server.routes import import_routes

    outs = _visium_run(tmp_path / "outs")
    license_issuer.install(license_issuer.issue(ent=["transcriptomics:visium"]))
    context = import_routes._tenx_context(
        "demo", outs / "filtered_feature_bc_matrix.h5", None)
    assert context is not None and context["converted"].is_file()


# -- an imported run on Free --------------------------------------------------------

GENES = ["EPCAM", "CD3E", "PTPRC"]


@pytest.fixture
def xenium_project(tmp_path, monkeypatch):
    """A Xenium sample already imported (on a Paid licence, say): an image, a
    transcripts layer and its built tile cache."""
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    image = tmp_path / "morphology.ome.tif"
    tifffile.imwrite(image, np.zeros((1, 256, 256), dtype=np.uint16))
    Project(
        name="xen",
        image=ImageSpec(src=str(image), width=256, height=256, max_level=1,
                        tile_width=256, tile_height=256,
                        channels=({"name": "c0", "fullname": "DAPI", "src": "/t/0"},)),
    ).with_layer(LayerSpec(id="tx", kind="points", label="Transcripts",
                           modality="transcripts")).save()
    rng = np.random.default_rng(3)
    n = 600
    source = tmp_path / "transcripts.parquet"
    source.write_bytes(b"stand-in; only its size and mtime are read")
    tt.build("xen", "tx", genes=GENES,
             gene_index=rng.integers(0, len(GENES), size=n).astype(np.uint16),
             x=rng.uniform(0, 256, size=n).astype(np.float32),
             y=rng.uniform(0, 256, size=n).astype(np.float32),
             expected=tt.expected_manifest(source, width=256, height=256,
                                           tile_size=128, layer_id="tx"))
    with plexora.app.test_client() as client:
        yield client


def _flask_variables(response):
    marker = b"window.flaskVariables = passVariablesToFrontend("
    start = response.data.index(marker) + len(marker)
    end = response.data.index(b");", start)
    return json.loads(response.data[start:end].decode("utf-8"))


def _plugin_scripts(html, plugin):
    return [part for part in html.split('"') if f"/plugins/{plugin}/static/" in part
            and part.split("?")[0].endswith(".js")]


def test_a_xenium_project_opens_on_free_without_its_section(xenium_project):
    page = xenium_project.get("/xen")
    assert page.status_code == 200
    assert _flask_variables(page)["layer_sections"] == []
    html = page.get_data(as_text=True)
    assert _plugin_scripts(html, "transcripts") == []
    assert "/plugins/transcripts/static/" not in html
    # The layer is still the project's, and its card is built from /config.
    entry = xenium_project.get("/config").get_json()["xen"]
    assert [layer["id"] for layer in entry["layers"]
            if layer.get("modality") == "transcripts"] == ["tx"]


def test_with_the_grant_the_section_and_its_scripts_are_there(xenium_project,
                                                              license_issuer):
    license_issuer.install(license_issuer.issue(ent=["transcriptomics"]))
    page = xenium_project.get("/xen")
    assert [s["name"] for s in _flask_variables(page)["layer_sections"]] == ["transcripts"]
    scripts = _plugin_scripts(page.get_data(as_text=True), "transcripts")
    assert any(s.split("?")[0].endswith("transcriptLayer.js") for s in scripts), scripts


def test_an_imported_transcript_layer_still_serves_its_tiles_on_free(xenium_project):
    """What a Paid feature made stays readable: the density raster is core's
    layer route, and it does not ask the licence."""
    tile = xenium_project.get("/generated/layer/xen/tx/density/0/0_0")
    assert tile.status_code == 200, tile.get_data(as_text=True)[:200]
    assert tile.mimetype.startswith("image/")


def test_the_plugins_own_routes_refuse_on_free_and_their_assets_load(xenium_project):
    refused = xenium_project.get("/plugins/transcripts/manifest?datasource=xen&layer=tx")
    assert refused.status_code == 403
    block = refused.get_json()["license"]
    assert block["code"] == "license_required"
    assert block["entitlement"] == "transcriptomics:xenium"
    refused = xenium_project.get("/plugins/visium_hd/state?datasource=xen")
    assert refused.status_code == 403
    assert refused.get_json()["license"]["entitlement"] == "transcriptomics:visium"
    assert xenium_project.get(
        "/plugins/transcripts/static/transcriptLayer.js").status_code == 200


def test_the_plugins_own_routes_answer_with_the_grant(xenium_project, license_issuer):
    license_issuer.install(license_issuer.issue(ent=["transcriptomics"]))
    body = xenium_project.get(
        "/plugins/transcripts/manifest?datasource=xen&layer=tx").get_json()
    assert body["genes"] == GENES


# -- the descriptors and the agent ------------------------------------------------

def test_the_two_plugins_name_their_transcriptomics_children():
    from plexora.licensing import manifest
    from plexora.plugins.transcripts import PLUGIN as transcripts
    from plexora.plugins.visium_hd import PLUGIN as visium_hd

    assert transcripts.entitlement == "transcriptomics:xenium"
    assert visium_hd.entitlement == "transcriptomics:visium"
    assert manifest.known(transcripts.entitlement)
    assert manifest.known(visium_hd.entitlement)
    # Nothing for discovery to inherit the entitlement onto: neither plugin
    # contributes a capability, so no agent tool turned Paid with them.
    assert transcripts.capabilities_factory is None
    assert visium_hd.capabilities_factory is None


def test_no_agent_capability_needs_transcriptomics():
    """Reading or drawing a transcript or bin layer is a read, Free on every
    plan; only importing a run is gated, and no capability imports one."""
    from plexora.agent import registry

    registry.discover()
    needing = sorted(cap.name for cap in registry.all_capabilities()
                     if str(cap.entitlement or "").startswith("transcriptomics"))
    assert needing == []


# -- the import screens -------------------------------------------------------------

def test_the_import_screens_offer_no_10x_run_on_free():
    """importHelp.js and browsePicker.js, run in node: on Free neither names a
    Xenium, Visium or Visium HD run or a transcripts file; with the grant both
    do. See tests/js/import_transcriptomics_probe.mjs."""
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    repo = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [node, str(repo / "tests" / "js" / "import_transcriptomics_probe.mjs")],
        capture_output=True, text=True, cwd=repo, timeout=60)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "all checks passed" in proc.stdout
