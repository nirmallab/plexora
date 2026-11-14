"""Disconnecting a connection that is still being opened ends it; it does not fail it.

"Stop connecting" (or Disconnect) pressed while a connection is still on its
way up is somebody giving up on it, not the connection breaking. Ending the
ssh processes is what makes establishment give up, and the error establishment
raises when its process dies is a consequence of the button, not a diagnosis:
the page must not say "Could not connect." with a cause nobody hit, and the
rented machine must get the ending the profile asks for on a Disconnect, not
the narrower rule kept for a failed connection.
"""

from __future__ import annotations

from plexora.server.models import remote_sessions
from plexora.server.models import remotes as remote_store
from tests.test_remote_connect import (  # noqa: F401 - fixtures
    GCLOUD_RECORD,
    FakeProcess,
    _no_sessions_left_running,
    a_cloud_remote,
    a_remote,
    compute,
    ssh,
    wait_for,
)


def test_disconnecting_while_waiting_for_the_app_ends_the_session_without_an_error(ssh):
    """The far Plexora has not answered yet when the user gives up. stop()
    kills the ssh, establishment sees its process die and raises -- and that
    raise must not turn a deliberate disconnect into a failed connection."""
    ssh.healthy = False  # the remote Plexora never answers its health check
    process = FakeProcess(block=True)
    ssh.queue.append(process)
    session = remote_sessions.start(a_remote(), askpass_url=None)
    assert wait_for(
        lambda: session.state == remote_sessions.STATE_WAITING_FOR_APP)

    session.stop()
    session._thread.join(10)

    assert not session._thread.is_alive()
    assert process.terminated is True
    status = session.status()
    assert status["state"] == remote_sessions.STATE_EXITED, status["error"]
    assert status["error"] is None, status["error"]


def test_disconnecting_a_cloud_connection_still_opening_applies_the_profile_ending(
        ssh, compute, plexora_data_root):
    """A VM that was already running is stopped on Disconnect when the profile
    says Stop. The establishing thread, seeing its ssh killed, must not take the
    teardown onto the after-failure rule (which leaves a reused VM running).

    The interleaving is forced: stop() is held, just before it gives the
    machine back, until the establishing thread has finished."""
    compute.action = "reused"
    ssh.healthy = False
    ssh.queue.append(FakeProcess(["PLEXORA_MOUNT_DONE"], block=True))
    record = dict(GCLOUD_RECORD, on_exit="stop")
    remote_store.save(a_cloud_remote(extra={"gcloud": record}))
    session = remote_sessions.start(remote_store.get("gcp"), askpass_url=None)
    assert wait_for(
        lambda: session.state == remote_sessions.STATE_WAITING_FOR_APP)

    # The last thing stop() does before releasing the VM; let the establishing
    # thread run its own ending first.
    session._end_unfinished_siblings = lambda: session._thread.join(10)
    session.stop()

    assert not session._thread.is_alive()
    assert [one["vm_name"] for one in compute.stopped] == ["plexora-gcp"], (
        session.status(200)["log"])
    assert session.state == remote_sessions.STATE_EXITED
