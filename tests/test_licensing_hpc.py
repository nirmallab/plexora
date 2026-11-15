"""Remote data nodes run Pro work only for a licensed primary -- without
ever holding a licence themselves.

One registration per cluster, job licences for containers and their expiry are
the `biocognia` package's (its `test_client.py` and `test_state.py`); what is
Plexora's is the entitlement proof a primary attaches for a node."""

import time

import polars as pl
import pytest
from biocognia import store

from plexora import licensing
from plexora.licensing import LICENSING, tokens


# -- entitlement proofs -------------------------------------------------------------------------

def test_a_proof_verifies_for_its_family():
    proof = tokens.mint_proof("node-token", ["ai"])
    assert tokens.verify_proof("node-token", proof, "ai:gating")
    narrow = tokens.mint_proof("node-token", ["ai:gating:analytics"])
    assert tokens.verify_proof("node-token", narrow, "ai:gating")
    other = tokens.mint_proof("node-token", ["ai:evidence"])
    assert not tokens.verify_proof("node-token", other, "ai:gating")


@pytest.mark.parametrize("mutate", [
    lambda p: p.replace("ai", "ax", 1),                      # grants edited
    lambda p: p[:-4] + "0000",                               # mac edited
    lambda p: ".".join(p.split(".")[:2] + [str(int(p.split(".")[2]) + 999999)] + p.split(".")[3:]),
    lambda p: "v2" + p[2:],
    lambda p: "",
    lambda p: "garbage",
])
def test_a_tampered_proof_is_refused(mutate):
    proof = tokens.mint_proof("node-token", ["ai"])
    assert not tokens.verify_proof("node-token", mutate(proof), "ai:gating")


def test_a_proof_for_another_node_or_out_of_time_is_refused():
    proof = tokens.mint_proof("node-token", ["ai"])
    assert not tokens.verify_proof("other-token", proof, "ai:gating")
    old = tokens.mint_proof("node-token", ["ai"], now=time.time() - 7200)
    assert not tokens.verify_proof("node-token", old, "ai:gating")
    future = tokens.mint_proof("node-token", ["ai"], now=time.time() + 7200)
    assert not tokens.verify_proof("node-token", future, "ai:gating")


def test_a_proof_survives_a_few_minutes_of_clock_skew_both_ways(monkeypatch):
    """ai-telemetry-4: the primary reuses a cached proof until a minute before
    it runs out by ITS clock; a node a few minutes ahead must still take it,
    as it takes a fresh proof from a primary a few minutes ahead."""
    now = time.time()
    proof = tokens.mint_proof("node-token", ["ai"], now=now)
    expires = now + tokens.PROOF_TTL_SECONDS
    # The node's clock 3 minutes ahead, near the end of the proof's hour.
    assert tokens.verify_proof("node-token", proof, "ai:gating", now=expires + 120)
    # The primary's clock 3 minutes ahead, a fresh proof.
    assert tokens.verify_proof("node-token", proof, "ai:gating", now=now - 180)
    # The primary never hands out a cached proof the node could already see as stale.
    tokens._cache.clear()
    monkeypatch.setattr(tokens, "_current_grants", lambda: ("ai",))
    first = tokens.proof_for("node-token")
    monkeypatch.setattr(tokens.time, "time", lambda: now + tokens.PROOF_TTL_SECONDS - 200)
    assert tokens.proof_for("node-token") != first
    tokens._cache.clear()


def test_the_primary_attaches_a_proof_only_when_licensed_and_only_for_pro_work(pro_license):
    assert tokens.headers_for("t", "roi.map_to_cells") == {}
    headers = tokens.headers_for("t", "gating.autogate.profile")
    assert tokens.verify_proof("t", headers[tokens.PROOF_HEADER], "ai:gating")


def test_no_proof_on_free():
    assert tokens.headers_for("t", "gating.autogate.profile") == {}


def test_a_job_admitted_on_a_valid_licence_keeps_its_proof(pro_license):
    grants = licensing.current().entitlements
    LICENSING.store.clear()
    licensing.reset_for_tests()
    tokens.reset_for_tests()
    assert tokens.headers_for("t", "gating.autogate.profile") == {}
    with tokens.admitted(grants):
        assert tokens.headers_for("t", "gating.autogate.profile")


# -- the node itself ------------------------------------------------------------------------------

@pytest.fixture
def node(tmp_path):
    from plexora.server.node.app import create_node_app

    table = tmp_path / "cells.csv"
    pl.DataFrame({"CellID": [1, 2, 3, 4], "X_centroid": [1.0, 2.0, 3.0, 4.0],
                  "Y_centroid": [1.0, 2.0, 3.0, 4.0], "CD3": [0.5, 1.5, 2.5, 3.5]}).write_csv(table)
    app = create_node_app([f"table:cells={table}"], token="node-secret", log=lambda *a, **k: None)
    return app.test_client()


def _op(node, operation, proof=None):
    headers = {"X-Plexora-Node-Token": "node-secret"}
    if proof:
        headers[tokens.PROOF_HEADER] = proof
    return node.post(f"/node/v1/table/cells/op/{operation}", json={"markers": ["CD3"]},
                     headers=headers)


def test_the_node_refuses_pro_work_without_a_proof(node):
    reply = _op(node, "gating.autogate.profile_all")
    assert reply.status_code == 403
    assert reply.get_json()["license"]["code"] == "license_required"


def test_the_node_runs_pro_work_with_a_proof(node):
    reply = _op(node, "gating.autogate.profile_all", tokens.mint_proof("node-secret", ["ai"]))
    assert reply.status_code != 403, reply.get_data(as_text=True)[:400]


def test_the_node_refuses_a_proof_minted_for_another_node(node):
    reply = _op(node, "gating.autogate.profile_all", tokens.mint_proof("someone-else", ["ai"]))
    assert reply.status_code == 403


def test_free_node_work_never_looks_for_a_proof(node, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("a Free operation looked for a licence proof")

    monkeypatch.setattr(tokens, "verify_proof", boom)
    reply = _op(node, "roi.no_such_free_operation")
    assert reply.status_code != 403


def test_the_node_never_holds_a_licence(node, tmp_path):
    """No licence file is read or written by serving: the node has none."""
    _op(node, "gating.autogate.profile_all", tokens.mint_proof("node-secret", ["ai"]))
    assert not LICENSING.store.path.exists()
    assert not store.environment_path().exists()
