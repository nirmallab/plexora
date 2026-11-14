"""`plexora --remote --bind-node` listens on every interface of a compute node.

Under plain `--remote` the viewer is tokenless on purpose: it is bound to the
compute node's loopback, so only the ssh tunnel reaches it. `--bind-node`
removes that loopback (the login node forwards to the node over the cluster's
network), and with it the reason the viewer could go without a token. Any
other account on the cluster can then reach the port with plain HTTP, which
the browser guard does not judge, because curl sends no Sec-Fetch-Site.
"""

import os
import sys

import pytest

# Imported for real before `_fake_serve` swaps a stand-in in for `plexora`:
# main() and the root conftest's teardowns reach these by submodule path,
# which only resolves once they are loaded (as they are in a wider run).
import plexora._lifetime  # noqa: F401
import plexora.server.models.layer_jobs  # noqa: F401
import plexora.server.volume.prefetch  # noqa: F401

from tests.test_cli import _fake_serve, cli

# Confirmed by the 2026-11 audit and not fixed: the fix changes how a login
# node reaches the viewer, so it waits for a decision
# (docs/internal/AUDIT_2026-11-14.md, ep-cli-1). Strict, so a fix shows here.
pytestmark = pytest.mark.xfail(strict=True, reason="audit ep-cli-1: confirmed, not fixed")


def test_remote_bind_node_guards_its_network_port_with_a_token(monkeypatch):
    """The `--ood` launch binds 0.0.0.0 for the same reason and mints a token
    for exactly this; a `--remote --bind-node` launch reachable from the same
    internal network must not be left open to every account on it."""
    monkeypatch.setenv("PLEXORA_AUTH_TOKEN", "")
    monkeypatch.setenv("PLEXORA_BASE_URL", "")
    served = {}
    _fake_serve(monkeypatch, served)
    monkeypatch.setattr(cli, "_print_remote_instructions", lambda args, port: None)

    cli.main(["--remote", "--bind-node", "--no-browser", "--port", "0",
              "--no-detect"])

    config = sys.modules["plexora"].app.config
    assert served["host"] == "0.0.0.0"
    assert config.get("PLEXORA_AUTH_TOKEN"), (
        "a viewer bound to 0.0.0.0 by --remote --bind-node has no auth token")
    assert os.environ["PLEXORA_AUTH_TOKEN"] == config["PLEXORA_AUTH_TOKEN"]
