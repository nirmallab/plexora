"""How long a connection running inside a scheduler's job has left.

Slurm does not warn anybody. It ends the allocation, the tunnel goes with it,
and the first sign is a tile that will not load an hour into a session. So the
clock has to be shown, and to be shown it has to be derived -- nothing here can
ask the scheduler, and there is no field on a saved profile that holds a
deadline.

Three facts, each from the only place that has it:

- **How long was asked for** comes out of the profile's own `srun` line
  (`recipes.srun_seconds`). It is a free-text field of somebody's site
  arguments, and Slurm's `-t` is genuinely ambiguous -- a bare number is
  MINUTES, and it is the day separator that makes the colon groups hours.
- **When the job started** is observed: the moment establishment moves off
  `waiting_for_job`, which is the announce saying the job is running. A proxy,
  and one that errs EARLY by the fraction of a second srun spends exec'ing.
- **Where it is kept** is both the session and the node registry entry, because
  they have different lifetimes: a data node outlives the process that started
  it, so after a restart the tunnel is up and the session is gone.

The surfaces that show it are tests/js/remote_globe_probe.mjs (the navbar
panel), settings_remotes_probe.mjs (the card) and session_expiry_probe.mjs (the
dialog that interrupts).
"""

import time

import pytest

import plexora
from plexora.server.models import nodes as node_registry
from plexora.server.models import recipes, remote_sessions
from plexora.server.models import remotes as remote_store


@pytest.fixture
def client():
    return plexora.app.test_client()


@pytest.fixture(autouse=True)
def _no_sessions_left_running():
    """The registry is module state, and a leaked session outlives its test."""
    yield
    with remote_sessions._REGISTRY_LOCK:
        remote_sessions._SESSIONS.clear()


def a_remote(name="gpu", srun="-p gpu -t 4:00:00", **kwargs):
    fields = {"target": "me@login.cluster.edu", "local_node": False,
              "srun": srun}
    fields.update(kwargs)
    return remote_store.Remote(name=name, **fields)


def a_session(remote, kind=remote_sessions.KIND_NODE):
    """A session object with nothing running under it.

    The constructor spawns nothing -- `start()` is what does -- so the phase
    callbacks below drive exactly the transitions establishment would.
    """
    session = remote_sessions.RemoteSession(remote, kind=kind)
    with remote_sessions._REGISTRY_LOCK:
        remote_sessions._SESSIONS[
            remote_sessions._key(kind, remote.name)] = session
    return session


# -- reading a walltime off somebody's srun line -----------------------------


@pytest.mark.parametrize("text,seconds", [
    # The shape every preset writes, and the one everybody types.
    ("4:00:00", 4 * 3600),
    # A bare number is MINUTES. This is the one that would silently be read as
    # seconds -- turning half an hour into half a minute -- by anything that
    # treated `-t` as a plain duration.
    ("30", 30 * 60),
    ("30:00", 30 * 60),
    ("1-0", 86400),
    # Past the day separator the groups are hours-first: `1-2` is a day and two
    # HOURS, where a bare `2` would have been two minutes.
    ("1-2", 86400 + 2 * 3600),
    ("1-2:30", 86400 + 2 * 3600 + 30 * 60),
    ("1-2:30:15", 86400 + 2 * 3600 + 30 * 60 + 15),
])
def test_every_shape_slurm_accepts_is_read_the_way_slurm_reads_it(text, seconds):
    assert recipes.walltime_seconds(text) == seconds


@pytest.mark.parametrize("text", ["", None, "UNLIMITED", "infinite", "0",
                                  "0:00:00", "nonsense", "1:2:3:4", "x-1"])
def test_anything_without_a_deadline_in_it_has_no_clock(text):
    """All of these come out the same way on purpose. What a countdown must
    never do is invent a deadline: somebody told they have twenty minutes left
    on a job that is not on a clock will save and reconnect for nothing, while
    somebody told nothing simply carries on."""
    assert recipes.walltime_seconds(text) is None


def test_the_line_a_profile_stores_is_read_the_same_way_the_form_wrote_it():
    """One function over `split_srun`, so a page cannot end up parsing `-t`
    slightly differently from the form that composed it."""
    assert recipes.srun_seconds("-p interactive -t 4:00:00 -c 16 --mem 128G") \
        == 4 * 3600
    assert recipes.srun_seconds("-p interactive") is None
    # None is "no scheduler at all"; "" is "srun with the site's defaults".
    # Neither carries a walltime, and neither is an error.
    assert recipes.srun_seconds(None) is None
    assert recipes.srun_seconds("") is None


# -- when the clock starts ---------------------------------------------------


def test_the_clock_starts_when_the_job_does_not_when_connecting_does():
    """Queue time is not allocation time. A job that waits a quarter of an hour
    in the queue still gets its whole walltime, and a clock started at Connect
    would have counted the wait against it."""
    session = a_session(a_remote())
    assert session.time_limit == 4 * 3600
    assert session.time_left is None      # nothing has been allocated yet

    session._on_phase("waiting_for_job")
    assert session.time_left is None, "queueing is not the job running"

    session._on_phase("tunneling")
    assert session.time_left == pytest.approx(4 * 3600, abs=2)


def test_a_job_that_never_queued_is_stamped_when_it_lands():
    """An allocation that comes back instantly announces no queue wait at all,
    and would otherwise have no clock for its whole life."""
    session = a_session(a_remote())
    with session._lock:
        session._start_the_clock_locked()

    assert session.time_left == pytest.approx(4 * 3600, abs=2)


def test_the_clock_is_stamped_once_and_not_restarted():
    """A reconnecting tunnel can pass through these phases again, and a clock
    that restarted would hand somebody a fresh four hours on a job that is
    three hours old."""
    session = a_session(a_remote())
    session._on_phase("waiting_for_job")
    session._on_phase("tunneling")
    first = session.job_started_at

    session._on_phase("waiting_for_job")
    session._on_phase("tunneling")

    assert session.job_started_at == first


def test_a_connection_that_is_not_in_a_job_has_no_clock_at_all():
    """A login node, or an srun line with no `-t`. There is no deadline to
    show and nothing must appear to have one."""
    session = a_session(a_remote(srun=None))
    session._on_phase("waiting_for_job")
    session._on_phase("tunneling")

    assert session.time_limit is None
    assert session.time_left is None
    assert session.expires_at is None
    assert session.status(log_lines=0)["time_left"] is None


def test_what_the_page_reads_is_a_duration_not_a_deadline():
    """Sent as "how long is left right now" so a browser whose clock disagrees
    with this machine's still counts down correctly."""
    session = a_session(a_remote())
    session._on_phase("waiting_for_job")
    session._on_phase("tunneling")

    status = session.status(log_lines=0)

    assert status["time_limit"] == 4 * 3600
    assert status["time_left"] == pytest.approx(4 * 3600, abs=2)


def test_a_job_past_its_walltime_reads_zero_rather_than_negative():
    """Zero is a real answer and is not None -- it means out of time, which is
    a thing to say."""
    session = a_session(a_remote(srun="-t 1"))
    session.job_started_at = time.time() - 600

    assert session.time_left == 0


# -- and where it is kept, so it survives this process ------------------------


def test_the_deadline_is_written_onto_the_node_entry():
    """A data node outlives the process that started it: after a restart the
    tunnel is up, the session is gone, and this entry is the only thing left
    that knows there is a clock."""
    from plexora import nodes as node_api

    deadline = time.time() + 900
    node_api.register_node("gpu-data", "http://127.0.0.1:41000", token="t",
                           verify=False, managed_by="connect:gpu-data",
                           expires_at=deadline)

    entry = node_registry.get("gpu-data")
    assert entry.expires_at == pytest.approx(deadline)
    assert entry.time_left == pytest.approx(900, abs=2)


def test_a_node_with_no_job_behind_it_carries_no_deadline():
    from plexora import nodes as node_api

    node_api.register_node("plain", "http://127.0.0.1:41000", token="t",
                           verify=False)

    assert node_registry.get("plain").expires_at is None
    assert node_registry.get("plain").time_left is None


def test_a_session_carries_its_deadline_into_the_registry():
    """The wrapper is what joins the two: the route's callback records the
    node, and only the session knows when the job it is running in ends."""
    recorded = {}

    def record(name, endpoint, token, *, browser_endpoint=None,
               managed_by=None, expires_at=None):
        recorded.update(name=name, expires_at=expires_at)

    session = a_session(a_remote())
    session._register = record
    session._on_phase("waiting_for_job")
    session._on_phase("tunneling")

    session._register_node("gpu-data", "http://127.0.0.1:41000", "t")

    assert recorded["name"] == "gpu-data"
    assert recorded["expires_at"] == pytest.approx(session.expires_at)


# -- what the surfaces read --------------------------------------------------


def test_data_places_carries_what_a_live_session_says(client, plexora_data_root):
    remote = remote_store.save(a_remote())
    session = a_session(remote)
    session._on_phase("waiting_for_job")
    session._on_phase("tunneling")

    place = next(p for p in client.get("/data_places").get_json()["places"]
                 if p["id"] == "gpu")

    assert place["time_limit"] == 4 * 3600
    assert place["time_left"] == pytest.approx(4 * 3600, abs=2)


def test_data_places_falls_back_to_the_registry_after_a_restart(
        client, plexora_data_root):
    """The case the registry copy exists for. No session at all -- which is
    what a restart leaves -- and the node still up and still on a clock."""
    from plexora import nodes as node_api

    remote_store.save(a_remote(name="gpu", node_name="gpu-data"))
    node_api.register_node("gpu-data", "http://127.0.0.1:41000", token="t",
                           verify=False, managed_by="connect:gpu-data",
                           expires_at=time.time() + 1800)

    place = next(p for p in client.get("/data_places").get_json()["places"]
                 if p["id"] == "gpu")

    assert place["node"] is None, "no session owns it"
    assert place["time_left"] == pytest.approx(1800, abs=2)


def test_a_hand_registered_node_does_not_lend_a_profile_its_clock(
        client, plexora_data_root):
    """Same ownership rule as everywhere else: without the `managed_by` marker
    a shared name is a coincidence, and reporting somebody else's deadline
    against this profile would be inventing one."""
    from plexora import nodes as node_api

    remote_store.save(a_remote(name="gpu", node_name="gpu-data"))
    node_api.register_node("gpu-data", "http://127.0.0.1:41000", token="t",
                           verify=False, expires_at=time.time() + 1800)

    place = next(p for p in client.get("/data_places").get_json()["places"]
                 if p["id"] == "gpu")

    assert place["time_left"] is None


# -- a clock belongs to a live allocation ------------------------------------
#
# Disconnecting stops a session and deliberately KEEPS its record: the final
# state and the last of its log are the only account of what happened, and
# throwing them away on stop would take the answer away at the moment somebody
# is looking for it. But a deadline computed from `job_started_at + time_limit`
# alone knows nothing about that -- so the globe went on counting down for a
# connection the user had closed, on an allocation that was very likely
# cancelled in the same breath.


def test_a_stopped_session_has_no_deadline(plexora_data_root):
    remote = remote_store.save(a_remote())
    session = a_session(remote)
    session._on_phase("waiting_for_job")
    session._on_phase("tunneling")
    assert session.time_left is not None, "precondition: it was on a clock"

    session.stop()

    assert session.expires_at is None
    assert session.time_left is None
    assert session.status()["time_left"] is None
    # What was ASKED FOR is a fact about the profile and does not stop being
    # true, which is why only one of the two goes away.
    assert session.status()["time_limit"] == 4 * 3600


def test_a_failed_session_has_no_deadline(plexora_data_root):
    """The other end. A connection that got its allocation and then broke is
    not counting down towards anything either."""
    remote = remote_store.save(a_remote())
    session = a_session(remote)
    session._on_phase("waiting_for_job")
    session._on_phase("tunneling")
    session.state = remote_sessions.STATE_FAILED

    assert session.time_left is None


def test_disconnecting_takes_the_countdown_with_it(client, plexora_data_root):
    """End to end, through the route the globe and the Settings cards read.

    Both halves have to go: the session's clock, and the node entry's copy of
    it -- `_forget_node` drops the entry, and without that the fallback would
    hand the same deadline straight back.
    """
    from plexora import nodes as node_api

    remote = remote_store.save(a_remote(name="gpu", node_name="gpu-data"))
    session = a_session(remote)
    session._on_phase("waiting_for_job")
    session._on_phase("tunneling")
    node_api.register_node("gpu-data", "http://127.0.0.1:41000", token="t",
                           verify=False, managed_by="connect:gpu-data",
                           expires_at=session.expires_at)

    def place():
        return next(p for p in client.get("/data_places").get_json()["places"]
                    if p["id"] == "gpu")

    assert place()["time_left"] is not None, "precondition: it was on a clock"

    client.post("/settings/remotes/gpu/disconnect", query_string={"kind": "node"})

    assert place()["time_left"] is None


def test_a_session_still_coming_up_keeps_its_clock(plexora_data_root):
    """The liveness test has to admit every state on the way up, not just
    `connected`. A data node's first start can take minutes -- which is time
    off the allocation -- and the clock has been running since the job was
    allocated, which is before any of that."""
    remote = remote_store.save(a_remote())
    session = a_session(remote)
    session._on_phase("waiting_for_job")
    session._on_phase("tunneling")

    for state in remote_sessions.OPENING_STATES:
        session.state = state
        assert session.time_left is not None, state
