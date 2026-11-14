"""Connecting a profile again after its earlier session has ended.

`remote_sessions.start()` keeps a finished session's record (Disconnect only
stops it, so the final state and log stay readable) and, when the same profile
is connected again, registers the new attempt and then stop()s the old record.
The old record's real stop() ends with `_end_unfinished_siblings`, which stops
every still-opening session of the same profile -- and the brand-new attempt is
exactly that. So the second Connect, or the page's "Try again", must still
connect: the old session's teardown is not somebody ending the new one.

tests/test_remote_connect.py pins that the old record is reaped, but replaces
its stop() with a lambda; these use the real one.
"""

from plexora.server.models import remote_sessions
from plexora.server.models import remotes as remote_store
from tests.test_remote_connect import (  # noqa: F401 - fixtures
    FakeProcess,
    _no_sessions_left_running,
    a_cloud_remote,
    a_remote,
    compute,
    ssh,
    wait_for,
)


def test_trying_again_after_a_failed_connection_connects(ssh):
    """The page's "Try again" after a failure starts the same profile again.
    It has to open a connection, not end as "The connection has ended." with
    nothing spawned."""
    ssh.healthy = False
    ssh.queue.append(FakeProcess(
        ["bash: plexora: command not found"], dead_with=127))
    first = remote_sessions.start(a_remote(), askpass_url=None)
    assert wait_for(lambda: first.state == remote_sessions.STATE_FAILED)
    spawned_before = len(ssh.spawned)

    ssh.healthy = True
    second = remote_sessions.start(a_remote(), askpass_url=None)

    assert wait_for(lambda: second.state == remote_sessions.STATE_CONNECTED), (
        f"second attempt ended as {second.state!r} after "
        f"{len(ssh.spawned) - spawned_before} new ssh spawn(s)")
    assert len(ssh.spawned) > spawned_before


def test_connecting_again_after_a_disconnect_connects(ssh):
    """Disconnect stops a session but keeps its record. Pressing Connect on
    the same profile afterwards must open a new connection."""
    first = remote_sessions.start(a_remote(), askpass_url=None)
    assert wait_for(lambda: first.state == remote_sessions.STATE_CONNECTED)
    remote_sessions.stop("hpc")
    assert first.state == remote_sessions.STATE_EXITED
    spawned_before = len(ssh.spawned)

    second = remote_sessions.start(a_remote(), askpass_url=None)

    assert wait_for(lambda: second.state == remote_sessions.STATE_CONNECTED), (
        f"second attempt ended as {second.state!r} after "
        f"{len(ssh.spawned) - spawned_before} new ssh spawn(s)")
    assert remote_sessions.get("hpc") is second


def test_retrying_a_cloud_profile_never_leaves_its_vm_up_with_no_session(
        ssh, compute, plexora_data_root):
    """A Google Cloud profile whose first attempt failed is tried again. The
    retry starts the stopped VM; it must then connect to it -- not stop short,
    sit in "preparing_compute" for ever, and leave the machine billing with
    nothing using it and nothing left that will stop it."""
    compute.fail = "Google Cloud refused the VM: quota."
    remote_store.save(a_cloud_remote())
    first = remote_sessions.start(remote_store.get("gcp"), askpass_url=None)
    assert wait_for(lambda: first.state == remote_sessions.STATE_FAILED)

    compute.fail = None
    compute.action = "started"
    ssh.queue.append(FakeProcess(["PLEXORA_MOUNT_DONE"], block=True))
    second = remote_sessions.start(remote_store.get("gcp"), askpass_url=None)

    connected = wait_for(
        lambda: second.state == remote_sessions.STATE_CONNECTED)
    second._thread.join(5)
    vm_started = len(compute.prepared)
    vm_stopped = len(compute.stopped)
    assert connected, (
        f"second attempt left in {second.state!r}; VM brought up "
        f"{vm_started} time(s), stopped {vm_stopped} time(s); "
        f"ssh spawned {len(ssh.spawned)} time(s)")
