"""HPC and remote: one registration per cluster however many nodes, job
licences for containers, and data nodes that run Paid work only for a licensed
primary -- without ever holding a licence themselves."""

import json
import socket
import time

import polars as pl
import pytest

from plexora import licensing
from plexora.licensing import delegation, environment, store, tokens
from plexora.licensing.cli import run as run_cli


def _cli(argv):
    lines = []
    return run_cli(argv, log=lines.append), "\n".join(lines)


# -- one cluster, one environment ---------------------------------------------------------

def test_every_node_of_a_cluster_is_one_environment(license_service, monkeypatch):
    """Registered once on a login node; a compute node with another hostname,
    another SLURM job and the same $HOME is the same environment."""
    monkeypatch.setenv("SLURM_CLUSTER_NAME", "o2")
    monkeypatch.setattr(socket, "gethostname", lambda: "login01.o2.example.org")
    code, out = _cli(["activate", "PLEX-AAAA-BBBB-CCCC-DDDD", "--cluster"])
    assert code == 0, out
    registered = license_service.of("/v1/activate")[0]["json"]["environment"]
    assert registered["kind"] == "cluster" and registered["display_name"] == "o2 cluster"

    for host in ("compute-a-16-3.o2.example.org", "compute-e-2-7.o2.example.org"):
        monkeypatch.setattr(socket, "gethostname", lambda host=host: host)
        monkeypatch.setenv("SLURM_JOB_ID", str(abs(hash(host)) % 100000))
        licensing.reset_for_tests()
        assert licensing.allows("ai:gating")
        assert licensing.current().environment_type == "cluster"
    assert len(license_service.of("/v1/activate")) == 1, "no node registered itself"
    assert environment.binding() == registered["binding"]


def test_a_token_in_many_job_scripts_registers_the_cluster_once(license_service, monkeypatch):
    """PLEXORA_LICENSE_TOKEN set in a job script: the first job exchanges it and
    writes $HOME; every later job, on any node, reads that."""
    monkeypatch.setenv(store.ENV_TOKEN, "PLXT1_" + "h" * 43)
    for job in range(4):
        monkeypatch.setenv("SLURM_JOB_ID", str(1000 + job))
        licensing.reset_for_tests()
        assert licensing.allows("ai:gating:session")
    assert len(license_service.of("/v1/activate")) == 1


def test_a_cluster_licence_works_with_the_network_off(paid_license, monkeypatch):
    """Compute nodes with no outbound route: the $HOME certificate is enough."""
    import socket as sock

    def refuse(*args, **kwargs):
        raise AssertionError("a compute node contacted the network")

    monkeypatch.setattr(sock, "create_connection", refuse)
    licensing.reset_for_tests()
    assert licensing.allows("ai")


# -- containers and short jobs -----------------------------------------------------------------

@pytest.fixture
def cluster_parent(license_issuer):
    seed, public = environment.delegation_keypair(create=True)
    parent = license_issuer.issue(environment_type="cluster", delegation_pubkey=public)
    license_issuer.install(parent)
    return parent, seed


def test_a_container_without_home_runs_on_a_job_licence(cluster_parent, monkeypatch, tmp_path):
    code, out = _cli(["lease", "--ttl", "6h"])
    assert code == 0
    job = out.strip().splitlines()[0]
    # The container: a different, empty licence directory, and offline.
    monkeypatch.setenv(store.ENV_DIR, str(tmp_path / "container-home"))
    monkeypatch.setenv(store.ENV_JOB_CERT, job)
    licensing.reset_for_tests()
    st = licensing.current()
    assert st.paid and st.source == "job" and st.environment_type == "job"
    assert not store.license_path().exists(), "a job licence registers and writes nothing"


def test_a_short_job_licence_simply_runs_out(cluster_parent, monkeypatch):
    parent, seed = cluster_parent
    monkeypatch.setenv(store.ENV_JOB_CERT, delegation.mint(parent, seed, ttl=60))
    from plexora.licensing import state

    monkeypatch.setattr(state, "_clock", lambda: time.time() + 61)
    licensing.reset_for_tests()
    assert licensing.current().state == "expired"


def test_a_desktop_cannot_lease(paid_license):
    code, out = _cli(["lease"])
    assert code == 1
    assert "no delegation key" in out


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


def test_the_primary_attaches_a_proof_only_when_licensed_and_only_for_paid_work(paid_license):
    assert tokens.headers_for("t", "roi.map_to_cells") == {}
    headers = tokens.headers_for("t", "gating.autogate.profile")
    assert tokens.verify_proof("t", headers[tokens.PROOF_HEADER], "ai:gating")


def test_no_proof_on_free():
    assert tokens.headers_for("t", "gating.autogate.profile") == {}


def test_a_job_admitted_on_a_valid_licence_keeps_its_proof(paid_license):
    grants = licensing.current().entitlements
    store.clear_license()
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


def test_the_node_refuses_paid_work_without_a_proof(node):
    reply = _op(node, "gating.autogate.profile_all")
    assert reply.status_code == 403
    assert reply.get_json()["license"]["code"] == "license_required"


def test_the_node_runs_paid_work_with_a_proof(node):
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
    assert not store.license_path().exists()
    assert not store.environment_path().exists()
