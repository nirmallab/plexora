"""`plexora --remote --bind-node` listens on every interface of a compute node.

Under plain `--remote` the viewer is tokenless on purpose: it is bound to the
compute node's loopback, so only the ssh tunnel reaches it. `--bind-node`
removes that loopback (the login node forwards to the node over the cluster's
network), and with it the reason the viewer could go without a token. Any
other account on the cluster can then reach the port with plain HTTP, which
the browser guard does not judge, because curl sends no Sec-Fetch-Site.

So a --bind-node viewer mints a token, prints it on its announce line, and
`plexora connect` reads it back there (audit ep-cli-1).
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

from plexora import _guard
from tests.test_cli import _fake_serve, cli
from tests.test_connect import FakeProcess, FakeResponse, connect_mod, rig  # noqa: F401


def _launch(monkeypatch, *flags):
    monkeypatch.setenv("PLEXORA_AUTH_TOKEN", "")
    monkeypatch.setenv("PLEXORA_BASE_URL", "")
    served, printed = {}, {}
    _fake_serve(monkeypatch, served)
    monkeypatch.setattr(cli, "_print_remote_instructions",
                        lambda args, port, token=None: printed.update(token=token))
    cli.main(["--remote", *flags, "--no-browser", "--port", "0", "--no-detect"])
    return served, printed, sys.modules["plexora"].app.config


def test_remote_bind_node_guards_its_network_port_with_a_token(monkeypatch):
    """The `--ood` launch binds 0.0.0.0 for the same reason and mints a token
    for exactly this; a `--remote --bind-node` launch reachable from the same
    internal network must not be left open to every account on it."""
    served, printed, config = _launch(monkeypatch, "--bind-node")

    assert served["host"] == "0.0.0.0"
    assert config.get("PLEXORA_AUTH_TOKEN"), (
        "a viewer bound to 0.0.0.0 by --remote --bind-node has no auth token")
    assert os.environ["PLEXORA_AUTH_TOKEN"] == config["PLEXORA_AUTH_TOKEN"]
    # And the instructions -- whose first line is the announce -- carry it.
    assert printed["token"] == config["PLEXORA_AUTH_TOKEN"]


def test_plain_remote_stays_tokenless_on_loopback(monkeypatch):
    served, printed, config = _launch(monkeypatch)

    assert served["host"] == "127.0.0.1"
    assert not config.get("PLEXORA_AUTH_TOKEN")
    assert printed["token"] is None


def test_the_announce_carries_the_token_after_the_port():
    lines = cli.remote_instructions("me", "login", 8123, node="c42",
                                    bind_node=True, token="s3cr3t")

    assert lines[0] == "[plexora-remote] node=c42 port=8123 token=s3cr3t"
    # An older `plexora connect` matches only node and port, and still does.
    assert connect_mod.parse_announce(lines[0]) == ("c42", 8123)
    assert connect_mod.parse_announce_token(lines[0]) == "s3cr3t"
    assert any("?token=s3cr3t" in line for line in lines)


def test_an_announce_without_a_token_has_none():
    assert connect_mod.parse_announce_token(
        "[plexora-remote] node=c42 port=8123") is None


def test_the_header_connect_sends_is_the_one_the_viewer_checks():
    assert connect_mod.VIEWER_TOKEN_HEADER == _guard.TOKEN_HEADER


def test_connect_opens_and_registers_with_the_announced_token(rig, monkeypatch):  # noqa: F811
    """The laptop end: the token read off the announce goes on the URL that is
    opened and on the registration it POSTs through the tunnel."""
    sent = []

    def urlopen(request, timeout=None):
        sent.append(request)
        response = FakeResponse()
        response.read = lambda: b"{}"
        return response

    monkeypatch.setattr(connect_mod, "_urlopen", urlopen)
    rig.queue = [
        FakeProcess(["[plexora-remote] node=compute-a-16 port=9999 token=s3cr3t",
                     "[plexora-node] host=127.0.0.1 port=41000 node_id=ab "
                     "token=nodetok"]),
        FakeProcess([]),
    ]
    session = connect_mod.Session("me@login", srun="-p x", bind_node=True,
                                  echo=rig.echo, datasource="proj",
                                  local_node=False, also_serve=["/data/x.tif"])
    session.establish()

    assert session.token == "s3cr3t"
    assert session.open_url == "http://127.0.0.1:9999/proj?token=s3cr3t"
    registrations = [r for r in sent if getattr(r, "method", None) == "POST"]
    assert registrations
    for request in registrations:
        assert request.get_header(connect_mod.VIEWER_TOKEN_HEADER.capitalize()) == "s3cr3t"
    session.stop()


def test_an_older_bind_node_remote_without_a_token_still_connects(rig):  # noqa: F811
    rig.queue = [
        FakeProcess(["[plexora-remote] node=compute-a-16 port=9999"]),
        FakeProcess([]),
    ]
    session = connect_mod.Session("me@login", srun="-p x", bind_node=True,
                                  echo=rig.echo, local_node=False)
    session.establish()

    assert session.token is None
    assert session.open_url == "http://127.0.0.1:9999/"
    session.stop()
