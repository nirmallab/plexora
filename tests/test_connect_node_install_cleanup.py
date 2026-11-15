"""`connect_node` stops its ssh when the chained install fails (audit lead N16).

The ssh was spawned, then `_await_install` ran before the watcher was in
`_ACTIVE` and outside the try/finally: an install that timed out raised past
both, and the ssh (with the half-done install over there) was left running.
"""
import pytest

from tests.test_connect import FakeProcess, connect_mod, rig  # noqa: F401


def test_a_failed_install_stops_the_ssh(rig, monkeypatch):
    process = FakeProcess([])
    rig.queue.append(process)
    monkeypatch.setattr(connect_mod, "_begin_install", lambda shim: "pip install plexora")

    def timed_out(shim, watched, line):
        raise connect_mod.ConnectError("Installing Plexora did not finish", diagnosed=True)

    monkeypatch.setattr(connect_mod, "_await_install", timed_out)

    with pytest.raises(connect_mod.ConnectError):
        connect_mod.connect_node("me@host", ["image:t=/scratch/t.ome.tif"],
                                 echo=rig.echo, install=True)

    assert process.terminated
    assert connect_mod._ACTIVE == []
