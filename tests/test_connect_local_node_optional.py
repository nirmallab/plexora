"""The optional local data node is not the connection (audit lead nr-connect-8).

It is spawned first, so it was `watchers[0]`: the missing-command hint read
the node's output instead of the ssh's, and its early exit failed the whole
connection during establishment, though `Session.wait` and
`_register_local_node` both treat it as a layer the viewer can do without.
"""
import pytest

from tests.test_connect import FakeProcess, connect_mod, rig  # noqa: F401


def test_a_missing_remote_plexora_is_named_with_the_local_node_running(rig):
    rig.queue = [FakeProcess([]),  # the local node, alive and quiet
                 FakeProcess(["bash: plexora: command not found"], dead_with=127)]

    session = connect_mod.Session("me@host", echo=rig.echo)
    with pytest.raises(connect_mod.ConnectError) as caught:
        session.establish()

    assert "command not found" in str(caught.value)


def test_a_local_node_that_dies_early_leaves_the_viewer_working(rig, monkeypatch):
    monkeypatch.setattr(connect_mod, "_now", lambda: 0.0)
    rig.queue = [FakeProcess(["Traceback: no module"], dead_with=1),  # the node
                 FakeProcess([])]  # the ssh carrying the viewer

    session = connect_mod.Session("me@host", echo=rig.echo)
    session.establish()

    assert session.url == "http://127.0.0.1:9999/"
    assert any("local data node" in error for error in session.node_errors)
    session.stop()
