"""A whole hand-off INTO Plexora, driven by the protocol's own engine.

The analysis side (a stand-in for SCIMAP Pro's adapter) holds a table; it hands
Plexora a gating step on one image and asks for the gates and regions back.
Every step is the real one: `spatialbridge.handoff.run` binds the table to a
Plexora project (`bridge_bind`), runs the capability through Plexora's MCP
server with the bridge origin, and collects the result objects
(`bridge_collect`) -- which then land in the table in the layout SCIMAP Pro's
`pp.rescale` reads.
"""

from __future__ import annotations

import pytest

from plexora.agent import registry
from plexora.agent.registry import ORIGIN_BRIDGE
from tests.scimappro_fixtures import make_anndata_project


@pytest.fixture
def plexora_peer():
    from spatialbridge.client import PeerClient
    from spatialbridge.transports.mcp import McpTransport

    from plexora.mcp.server import build_server

    registry._reset_for_tests()
    peer = PeerClient("plexora", McpTransport(
        build_server(names=["gating", "roi", "qc"], origin=ORIGIN_BRIDGE), provider="plexora"))
    yield peer
    peer.close()
    registry._reset_for_tests()


def _analysis(table):
    from spatialbridge.adapter import Adapter

    class Analysis(Adapter):
        provider = "scimappro"

        def __init__(self):
            self.recorded = []

        def table_for(self, dataset):
            return {"table": table, "table_name": ""}

        def record(self, handoff, result):
            self.recorded.append(result)
            return result

    return Analysis()


def test_gates_and_regions_come_back_in_the_protocols_shape(tmp_path, plexora_peer):
    from spatialbridge import handoff
    from spatialbridge.schema import Gates, Handoff, HandoffContext, Regions

    made = make_anndata_project(tmp_path)
    local = _analysis(made["table"])
    request = Handoff(to="plexora", capability="plexora:gating.set",
                      arguments={"marker": "CD8", "low": 500.0},
                      context=HandoffContext(image_id="slide_A"),
                      returns=["gates", "regions"])
    outcome = handoff.run(request, local, peer=plexora_peer)
    assert outcome.status == "done"
    assert outcome.receipt.operation_id and outcome.receipt.origin == "bridge"
    gates = Gates.model_validate(outcome.objects["gates"])
    cd8 = next(g for g in gates.gates if g.marker == "CD8")
    assert cd8.value == 500.0 and cd8.image_id == "slide_A"
    assert cd8.operation_id == outcome.receipt.operation_id
    regions = Regions.model_validate(outcome.objects["regions"])
    assert regions.bridge.producer == "roi" and regions.bridge.image_id == "slide_A"
    assert outcome.objects["regions"]["plexora"]["datasource"] == made["name"]
    assert local.recorded == [outcome]
    # The project the engine bound is the one already reading this table.
    from spatialbridge import workspace

    ws = workspace.find(made["table"])
    assert ws.read()["active"]["project"] == made["name"]


def test_collected_gates_land_in_the_table_as_pp_rescale_reads_them(tmp_path, plexora_peer):
    import anndata as ad
    from spatialbridge.anndata import write_gates

    made = make_anndata_project(tmp_path)
    plexora_peer.invoke("set_gate", {"project": made["name"], "marker": "CD8", "low": 640.0})
    gates = plexora_peer.invoke("bridge_collect", {"kind": "gates", "image_id": "slide_A",
                                                   "arguments": {"project": made["name"]}})
    adata = ad.read_zarr(made["table"])
    changed = write_gates(adata, gates)
    assert changed["images"] == ["slide_A"]
    table = adata.uns["gates"]
    assert table.index.name == "var_names" and table.loc["CD8", "slide_A"] == 640.0
    assert "operation_id" in adata.uns["gates_provenance"].columns


def test_a_table_plexora_has_never_seen_needs_the_image_once(tmp_path, plexora_peer):
    from spatialbridge import handoff
    from spatialbridge.errors import BridgeError
    from spatialbridge.schema import Handoff, HandoffContext

    made = make_anndata_project(tmp_path, register=False)
    local = _analysis(made["table"])
    request = Handoff(to="plexora", capability="list_rois", arguments={},
                      context=HandoffContext(image_id="slide_A"), returns=[])
    with pytest.raises(BridgeError) as refused:
        handoff.run(request, local, peer=plexora_peer)
    assert refused.value.code == "precondition_missing"
    assert refused.value.detail["step"] == "ensure_bound"


def test_qc_without_a_result_is_a_precondition(tmp_path, plexora_peer):
    from spatialbridge.errors import BridgeError

    made = make_anndata_project(tmp_path)
    with pytest.raises(BridgeError) as refused:
        plexora_peer.invoke("bridge_collect", {"kind": "qc",
                                               "arguments": {"project": made["name"]}})
    assert refused.value.code == "precondition_missing"
    with pytest.raises(BridgeError) as unknown:
        plexora_peer.invoke("bridge_collect", {"kind": "cell_labels",
                                               "arguments": {"project": made["name"]}})
    assert unknown.value.code == "capability_unavailable"
