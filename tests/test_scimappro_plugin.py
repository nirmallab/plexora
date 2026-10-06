"""The contract SCIMAP Pro's Plexora plugin must satisfy, pinned against a fake.

SCIMAP Pro ships `scimappro_plexora` (entry point `plexora.plugins:
scimappro`), which turns its function catalogue into Plexora capabilities.
Plexora cannot import SCIMAP Pro in its own suite, so tests/scimappro_fixtures.py
carries a fake plugin built to the contract (spatialbridge docs/contract.md)
and this file is what holds it -- and the real one -- to it: naming, flags, a
schema built from the function's parameters, a run as a job with a receipt and
an audit line, and every refusal arriving with the code an agent branches on.
"""

from __future__ import annotations

import sys
import time

import pytest

from plexora.agent import AgentSession, Policy, registry
from plexora.agent.audit import AuditLog
from tests.scimappro_fixtures import (REGISTRY, install_fake_scimappro,
                                      make_anndata_project, tool_name)

ALLOW = Policy(allow_source_writes=True)


@pytest.fixture
def plugin(monkeypatch):
    descriptor, module = install_fake_scimappro(monkeypatch)
    registry._reset_for_tests()
    registry.discover(["scimappro"])
    yield descriptor, module
    registry._reset_for_tests()


@pytest.fixture
def made(tmp_path):
    return make_anndata_project(tmp_path)


def _wait(job_id, timeout=30.0):
    from plexora.agent import jobs

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = jobs.store().get(job_id)
        if record and record["status"] in jobs.TERMINAL:
            return record
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


def _run(name, arguments, *, policy=ALLOW, audit=None, notify=None):
    return registry.invoke(AgentSession(), name, arguments, policy=policy,
                           audit=audit or AuditLog(), notify=notify)


def test_capabilities_are_named_by_the_contract(plugin):
    owned = {cap.name: cap for cap in registry.all_capabilities() if cap.owner == "scimappro"}
    for key in REGISTRY:
        cap = owned[f"scimappro.{key}"]
        assert cap.tool_name == tool_name(key)
        assert cap.tool_name.startswith("scimappro_") and cap.tool_name == cap.tool_name.lower()
    assert owned["scimappro.sp.spatialNeighbors"].tool_name == "scimappro_sp_spatial_neighbors"


def test_loading_the_plugin_does_not_import_scimappro(monkeypatch):
    """Plexora reads every plugin's descriptor at start-up; importing SCIMAP Pro
    there would import its whole library into the viewer process."""
    descriptor, _module = install_fake_scimappro(monkeypatch)
    monkeypatch.delitem(sys.modules, "scimappro")
    registry._reset_for_tests()
    try:
        registry.discover(["scimappro"])
        assert "scimappro" not in sys.modules
        assert any(cap.owner == "scimappro" for cap in registry.all_capabilities())
    finally:
        registry._reset_for_tests()


def test_flags_follow_what_each_function_does(plugin):
    write = registry.get("scimappro_sp_spatial_neighbors")
    assert write.permission == "source_file_write" and write.source_file_write
    assert write.execution == "job" and write.persistent and not write.reversible
    assert write.egress == "aggregates" and not write.remote_safe
    assert "analysis" in write.tags and "spatial.neighborhood" in write.tags
    plot = registry.get("scimappro_pl_spatial_scatter_plot")
    assert plot.permission == "read" and plot.visual_output
    assert plot.egress == "rendered_pixels" and plot.execution == "immediate"
    # Free in Plexora: SCIMAP Pro licenses its own functions.
    assert all(cap.entitlement in (None, "free") for cap in registry.all_capabilities()
               if cap.owner == "scimappro")


def test_the_schema_is_the_functions_parameters(plugin):
    schema = registry.get("scimappro_sp_spatial_neighbors").describe()["input_schema"]
    properties = schema["properties"]
    assert {"project", "radius", "phenotype", "label", "expected_revision",
            "confirm"} <= set(properties)
    assert "data" not in properties and "imageId" not in properties
    assert schema["required"] == ["project"]
    assert "radius" in properties["radius"].get("description", "").lower()


def test_a_run_is_a_job_with_a_receipt_and_an_audit_line(plugin, made, tmp_path):
    _descriptor, module = plugin
    audit = AuditLog(tmp_path / "audit.jsonl")
    told = []
    answer = _run("scimappro_sp_spatial_neighbors",
                  {"project": made["name"], "radius": 20, "confirm": True,
                   "label": "ne_roi"}, audit=audit,
                  notify=lambda *args: told.append(args) or 1)
    assert answer["ok"], answer
    record = _wait(answer["result"]["job_id"])
    assert record["status"] == "done", record
    result = record["result"]
    assert result["experimental_unit"] == {"unit": "image", "nReplicates": 1}
    assert result["warnings"]
    receipt = result["receipt"]
    assert receipt["persistent_state"] == "source_file" and receipt["source_file_modified"]
    assert receipt["bridge"]["peer"] == "scimappro" and "token" not in receipt["bridge"]
    assert module.calls[-1]["subset"] == made["image_id"]
    lines = [line for line in audit.entries() if line.get("operation_id") ==
             answer["operation_id"]]
    assert {line["status"] for line in lines} >= {"started", "ok"}
    assert told and told[-1][1:3] == ("core", "dataset.changed")


def test_a_write_without_confirm_is_refused(plugin, made):
    answer = _run("scimappro_sp_spatial_neighbors", {"project": made["name"]})
    assert not answer["ok"] and answer["error"]["code"] == "permission_required"
    assert answer["error"]["detail"]["needs"] == "confirm"


def test_a_missing_column_is_a_precondition(plugin, made):
    answer = _run("scimappro_tl_summarize_samples",
                  {"project": made["name"], "column": "no_such_column", "confirm": True})
    record = _wait(answer["result"]["job_id"])
    assert record["status"] == "failed"
    assert record["error"]["code"] == "precondition_missing"


def test_a_stale_revision_is_a_conflict(plugin, made):
    from spatialbridge import workspace

    from tests.scimappro_fixtures import add_obs_column

    ws = workspace.ensure(made["table"], producer="test")
    # Another tool wrote the very column this run would write, after revision 0.
    add_obs_column(made["table"], "ne_label", ["a"] * len(made["cells"]))
    ws.commit(["obs:ne_label"], "external-tool")
    answer = _run("scimappro_sp_spatial_neighbors",
                  {"project": made["name"], "label": "ne", "confirm": True,
                   "expected_revision": 0})
    record = _wait(answer["result"]["job_id"])
    assert record["status"] == "failed" and record["error"]["code"] == "conflict"


def test_a_modality_it_does_not_serve_is_said_so(plugin, made):
    answer = _run("scimappro_sp_spatial_neighbors",
                  {"project": made["name"], "phenotype": "__modality__", "confirm": True})
    record = _wait(answer["result"]["job_id"])
    assert record["error"]["code"] == "unsupported_modality"


def test_scimappros_licence_refusal_arrives_verbatim(plugin, made):
    _descriptor, module = plugin
    module.licensed = False
    answer = _run("scimappro_sp_spatial_neighbors", {"project": made["name"], "confirm": True})
    record = _wait(answer["result"]["job_id"])
    error = record["error"]
    assert error["code"] == "license_required"
    assert error["detail"]["provider"] == "scimappro"
    assert "SCIMAP Pro licence" in error["message"]


def test_without_scimappro_installed_the_capability_says_so(monkeypatch, tmp_path):
    install_fake_scimappro(monkeypatch, with_module=False)
    made = make_anndata_project(tmp_path)
    registry._reset_for_tests()
    try:
        registry.discover(["scimappro"])
        answer = _run("scimappro_sp_spatial_neighbors",
                      {"project": made["name"], "confirm": True})
        record = _wait(answer["result"]["job_id"])
        assert record["error"]["code"] == "capability_unavailable"
        assert "pip install" in record["error"]["detail"]["hint"]
    finally:
        registry._reset_for_tests()


def test_a_plot_comes_back_as_an_image(plugin, made):
    answer = _run("scimappro_pl_spatial_scatter_plot", {"project": made["name"]},
                  policy=Policy())
    assert answer["ok"], answer
    images = answer["result"]["_images"]
    assert images and images[0].startswith(b"\x89PNG")


def test_a_project_without_a_table_is_refused_before_running(plugin, tmp_path):
    from tests.agent_fixtures import make_synthetic_project

    make_synthetic_project(tmp_path, table=False)
    answer = _run("scimappro_sp_spatial_neighbors", {"project": "synth", "confirm": True})
    assert not answer["ok"] and answer["error"]["code"] == "precondition_missing"


def test_the_generic_runner_and_the_undo_are_offered(plugin, made):
    """`scimappro_run` (for slim profiles) runs any function by key; its undo,
    `scimappro_drop_result`, is destructive -- it removes a column from the
    user's table -- so it needs the server's permission and `confirm`."""
    answer = _run("scimappro_run", {"project": made["name"], "key": "sp.spatialNeighbors",
                                    "params": {"radius": 15}, "label": "ne2",
                                    "confirm": True})
    record = _wait(answer["result"]["job_id"])
    assert record["status"] == "done", record
    assert record["result"]["label"] == "ne2"
    drop = registry.get("scimappro_drop_result")
    assert drop.permission == "destructive"
    refused = _run("scimappro_drop_result", {"project": made["name"], "label": "ne2",
                                             "confirm": True})
    assert refused["error"]["code"] == "permission_required"
