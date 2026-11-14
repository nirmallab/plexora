"""A session whose tunnel dies is not still connected.

Under srun a connection is two ssh processes: the job leg, which holds the
allocation and the remote Plexora, and the tunnel, which is the only way the
browser reaches it. The tunnel can end on its own while the job lives -- three
missed keepalives on the compute-node hop, a site's idle-session killer ending
the `-N` session, the compute node's sshd dropping the forward. From that moment
every request to the local port is refused, so the session must stop saying
"Connected": it ends, gives the allocation back, and takes its node off the map,
exactly as it does when the job leg is the one that ends.
"""

from __future__ import annotations

from plexora.server.models import remote_sessions
from tests.test_remote_connect import (  # noqa: F401 - fixtures
    FakeProcess,
    _no_sessions_left_running,
    a_remote,
    ssh,
    wait_for,
)


def test_a_node_session_whose_tunnel_dies_ends_and_takes_its_node_off_the_map(ssh):
    """The job is still running, but nothing reaches the node any more: the
    session ends, the job is stopped, and the dead address leaves nodes.json."""
    forgotten = []
    job = FakeProcess(
        ["[plexora-node] host=127.0.0.1 port=41000 node_id=ab token=s3cr3t "
         "hostname=compute-9"],
        block=True)
    tunnel = FakeProcess(block=True)
    ssh.queue.extend([job, tunnel])
    session = remote_sessions.start(
        a_remote(srun="-t 1:00:00"), askpass_url=None,
        kind=remote_sessions.KIND_NODE,
        allow_origin="http://127.0.0.1:8000",
        register=lambda name, *args, **extra: name,
        unregister=forgotten.append)
    assert wait_for(lambda: session.state == remote_sessions.STATE_CONNECTED)

    tunnel.returncode = 255  # the tunnel ssh gives up; the job does not

    assert wait_for(lambda: session.state != remote_sessions.STATE_CONNECTED), (
        f"still {session.status()['state']!r} with a dead tunnel "
        f"(job alive: {job.returncode is None})")
    assert wait_for(lambda: job.terminated)
    assert wait_for(lambda: forgotten == ["hpc"])


def test_a_viewer_session_whose_tunnel_dies_ends_and_frees_the_profile(ssh):
    """The viewer's URL answers nothing once its tunnel is gone. The session
    must stop offering it, give the job back, and let the profile connect
    again instead of refusing as 'already connected'."""
    job = FakeProcess(["[plexora-remote] node=compute-9 port=41000"],
                      block=True)
    tunnel = FakeProcess(block=True)
    ssh.queue.extend([job, tunnel])
    session = remote_sessions.start(
        a_remote(srun="-t 1:00:00"), askpass_url=None)
    assert wait_for(lambda: session.state == remote_sessions.STATE_CONNECTED)

    tunnel.returncode = 255

    assert wait_for(lambda: session.state != remote_sessions.STATE_CONNECTED), (
        f"still {session.status()['state']!r}, offering {session.status()['url']}"
        f" with a dead tunnel")
    assert wait_for(lambda: job.terminated)
