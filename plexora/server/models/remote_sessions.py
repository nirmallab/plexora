"""Live SSH connections, owned by the app rather than by a terminal.

`plexora connect` and the Settings page do the same thing and cannot do it the
same way. The command runs in a terminal, blocks until Ctrl+C, and prompts on
its own tty. A request handler has to return a response in the next second or
two, while the ssh processes it just started keep running for the rest of the
afternoon and a later request asks how they are getting on. So this module
holds them: one `connect.Session` per saved profile, each driven on its own
daemon thread, each with a state a poll can read.

**Nothing here blocks a request.** `start()` returns as soon as the thread is
running; the page polls `status()`. That is not only for responsiveness -- an
srun connection legitimately waits fifteen minutes in a queue, and a route
that waited with it would hold a Waitress worker for the duration and time out
in the browser long before the job started.

**The states are the sentences the user reads.** "connecting" and "tunneling"
are different waits with different causes, and "waiting_for_job" is not a
problem at all even though it is by far the longest. Collapsing them into a
spinner is what made the old flow feel broken whenever it was merely slow.

**Secrets.** A password reaches ssh through `plexora/askpass.py` and lives in
`_prompt.answer` for the moment between the user typing it and the helper
collecting it. It is not stored, not logged, and not in `status()`. The ssh
output that IS shown is redacted on the way out, because Stage C's node
announce puts a token on stdout.

**One connection asks more than once.** A cluster connection is three ssh
authentications, not one -- the job, the login node again as a jump host, then
the compute node -- so a site that authenticates by password asked the same
question three times for one press of Connect. A repeatable answer is
therefore kept in `_secrets` for the length of ESTABLISHMENT and given again,
and the person types once however many hops the site has.

Two guards are what make that safe rather than merely convenient. Only a
standing secret is ever replayed -- never a one-time code, never a host-key
confirmation (`prompt_secret_kind`, which treats wording it does not recognise
as unrepeatable). And only the first time a given ssh asks a given question:
one process asking the same words twice means the answer it got was refused,
so the cached one is dropped and the person is asked instead. It has to be the
process and not the wording, because the job and the jump hop authenticate to
the same login node and so ask identically -- `askpass.asking_process` is what
tells them apart. That caps a mistyped password at one silent retry rather
than one per hop, which at a site with a lockout policy is the difference
between a typo and a locked account. The window closes the moment the
connection is up or has failed, so nothing here holds a password for the life
of a session.
"""

from __future__ import annotations

import atexit
import os
import re
import secrets
import stat
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from plexora import askpass, connect
from plexora.server.models import recipes

#: A connection attempt holds two ssh processes and a thread. Three at once is
#: already an unusual thing to be doing deliberately; more than that is a stuck
#: page retrying, and the cost of not capping it is a Waitress process with
#: forty ssh children.
MAX_CONNECTING = 3

#: How much of each ssh's output to keep. Enough to hold a stack of
#: authentication failures plus whatever the remote shell said on the way out,
#: which is where the actionable line almost always is.
LOG_LINES = 200

#: Only for a profile whose machine does not exist until Plexora asks for one
#: -- today that is the Google Cloud preset. It is a state of its own because
#: it is minutes long, because it SPENDS MONEY, and because it happens before
#: any ssh at all: a page showing "opening an SSH connection" while Compute
#: Engine builds a VM would be describing something that has not started.
STATE_PREPARING_COMPUTE = "preparing_compute"
STATE_CONNECTING = "connecting"
STATE_AUTHENTICATING = "authenticating"
#: Between signing in and installing anything: the connection is up, and the
#: data it exists to read is being mounted onto the far machine. Its own state
#: for the same reason installing is -- a first mount installs Cloud Storage
#: FUSE and builds a Python environment, which is minutes of silence that
#: otherwise reads as a login that hung.
STATE_MOUNTING_DATA = "mounting_data"
#: Only for a profile with "install or update Plexora" switched on, and it is a
#: state rather than something done quietly beside the connection because it is
#: minutes long, it writes to the far machine, and it is the step most likely
#: to be the one that failed. A pip pulling numpy and zarr onto a cold shared
#: home directory looks exactly like a hung login unless the page says which of
#: the two it is.
STATE_INSTALLING = "installing"
STATE_WAITING_FOR_JOB = "waiting_for_job"
STATE_TUNNELING = "tunneling"
#: Split out from tunnelling because the two waits fail for opposite reasons and
#: are told apart by nothing else: "tunnelling" is over in a second or it is
#: being asked for a password, whereas THIS one is a forward that opened without
#: complaint and carries nothing -- which on a cluster that refuses ssh into a
#: compute node is silent, and used to be indistinguishable from a slow start.
STATE_WAITING_FOR_APP = "waiting_for_app"
STATE_CONNECTED = "connected"
STATE_FAILED = "failed"
STATE_EXITED = "exited"

#: Every state meaning "this connection is on its way up". One tuple, because it
#: is read in four places and a state missing from one of them is a bug that
#: shows only under load -- or, worse, silently.
OPENING_STATES = (STATE_PREPARING_COMPUTE, STATE_CONNECTING,
                  STATE_AUTHENTICATING, STATE_MOUNTING_DATA, STATE_INSTALLING,
                  STATE_WAITING_FOR_JOB, STATE_TUNNELING, STATE_WAITING_FOR_APP)

#: What each state says on the page. One sentence, in the second person, naming
#: what is being waited for rather than what the code is doing.
PHRASES = {
    STATE_PREPARING_COMPUTE: "Preparing the Compute Engine VM — looking for "
                             "it, then starting or creating it. A new VM "
                             "takes a minute or two.",
    STATE_CONNECTING: "Opening an SSH connection…",
    STATE_AUTHENTICATING: "Waiting for you to answer the login prompt…",
    STATE_MOUNTING_DATA: "Mounting your Cloud Storage bucket and checking it "
                         "can be read. A first connection also sets Plexora "
                         "up on the VM, which takes a few minutes.",
    STATE_INSTALLING: "Installing or updating Plexora over there. A first "
                      "install pulls its dependencies onto that machine and "
                      "can take a few minutes.",
    STATE_WAITING_FOR_JOB: "Waiting for the scheduler to allocate a node. "
                           "This can take a while on a busy queue.",
    STATE_TUNNELING: "Opening the tunnel to the remote host…",
    STATE_WAITING_FOR_APP: "Waiting for Plexora to answer through the tunnel…",
    STATE_CONNECTED: "Connected.",
    STATE_FAILED: "Could not connect.",
    STATE_EXITED: "The connection has ended.",
}

#: Anything that looks like a secret in a line we are about to show. The node
#: announce prints `token=…` by design (it travels inside the ssh channel), and
#: the log tail is the one place it would escape that channel.
_REDACT = re.compile(r"((?:token|password|passwd|secret)\s*[=:]\s*)(\S+)",
                     re.IGNORECASE)

_PERMISSION_DENIED = ("Permission denied", "Too many authentication failures",
                      "No supported authentication methods")
_HOST_KEY = ("Host key verification failed", "REMOTE HOST IDENTIFICATION HAS CHANGED")


def redact(line):
    return _REDACT.sub(lambda m: m.group(1) + "…", str(line))


#: Questions whose answer must never be given twice, checked BEFORE anything
#: else. ssh asks everything through the same door -- a password, a Duo push, a
#: host-key confirmation -- so the wording is the only thing there is to tell
#: them apart by, and the two mistakes are not symmetrical. Refusing to reuse
#: something reusable costs one extra typing; reusing something one-time
#: replays a code that cannot work twice, or accepts an unknown host key on
#: somebody's behalf. So this is deliberately greedy.
_ONE_TIME_PROMPT = re.compile(
    r"\b(duo|passcode|one[- ]?time|otp|token|push|verification\s+code)\b"
    r"|yes/no|fingerprint",
    re.IGNORECASE)

#: `you@host's password:`, `Password:`, `Password for you@host:` -- the same
#: standing secret however the far end words it, which is why the host is not
#: part of the key: one account on one cluster has one password, and the hops
#: that ask for it are the login node and a compute node of the same cluster.
_PASSWORD_PROMPT = re.compile(r"\bpassword\b[^:]*:\s*$", re.IGNORECASE)

#: `Enter passphrase for key '/home/you/.ssh/id_ed25519':` -- keyed by the file
#: it names, because two keys are two different secrets and a passphrase given
#: for one is not an answer for the other.
_PASSPHRASE_PROMPT = re.compile(
    r"\bpassphrase for key\s+'(?P<key>[^']*)'", re.IGNORECASE)


def prompt_secret_kind(text):
    """Which standing secret this prompt asks for, or None for "ask again".

    None is the answer for two different questions and they come to the same
    thing: one whose answer is true only once -- a Duo push, a code, a
    `(yes/no)` -- and one worded in a way this does not recognise. Unrecognised
    has to fall on the None side, because a site with novel wording is exactly
    the site where a guess about what may be replayed would be wrong.
    """
    text = str(text or "").strip()
    if _ONE_TIME_PROMPT.search(text):
        return None
    match = _PASSPHRASE_PROMPT.search(text)
    if match:
        return f"passphrase:{match.group('key')}"
    if _PASSWORD_PROMPT.search(text):
        return "password"
    return None


@dataclass
class _Prompt:
    """One question ssh asked, on its way to the browser and back."""

    id: str
    text: str
    answer: str | None = None
    cancelled: bool = False
    #: Answered from what the user already typed, without reaching the page at
    #: all. Never a reason to render anything -- `status()` omits an answered
    #: prompt entirely -- but it is what the log line and the tests read.
    reused: bool = False
    #: Set when the answer is available, so the askpass helper's poll can stop.
    ready: threading.Event = field(default_factory=threading.Event)


#: A connection that starts Plexora on the far side and tunnels the browser to
#: it. The original meaning of "connect", and still what the Remote servers
#: page's Connect button does.
KIND_VIEWER = "viewer"
#: A connection that starts a data NODE on the far side and leaves the viewer
#: here. What a data form's Remote option opens: the project, the database and
#: the browser stay on this machine, and only the bytes of one file come over.
#: Same profile, same login, same askpass -- different thing at the far end.
KIND_NODE = "node"


def _key(kind, name):
    """The registry key. Viewer sessions keep the bare name they always had.

    Both kinds can be live for one profile at once and mean different things,
    so they cannot share a slot -- but the viewer's key stays unprefixed so
    that nothing which already looked a session up by name has to learn about
    kinds to keep working.
    """
    return str(name) if kind == KIND_VIEWER else f"{kind}:{name}"


class RemoteSession:
    """One saved profile's connection, from spawn to teardown."""

    def __init__(self, remote, *, askpass_url=None, auth_token=None,
                 timeout=None, kind=KIND_VIEWER, allow_origin=None,
                 register=None, unregister=None):
        self.remote = remote
        self.name = remote.name
        self.kind = kind
        #: Node sessions only: the origin the browser will use, the callable
        #: that records the node once it answers, and the one that takes it
        #: back off the map when this session ends on its own. All supplied by
        #: the route, which is the only place that knows any of them.
        self._allow_origin = allow_origin
        self._register = register
        self._unregister = unregister
        self.state = STATE_CONNECTING
        #: What establishment last said it was doing, kept separately because a
        #: prompt covers it over for as long as the question is on screen and
        #: this is what there is to go back to once it is answered.
        self._phase_state = STATE_CONNECTING
        self.error = None
        #: A fix for this failure that the page can offer as a button rather
        #: than as a paragraph, as a short key -- today only
        #: `gcloud.RECOVERY_STANDARD`. Empty for every failure whose answer is
        #: not one unambiguous edit, which is nearly all of them: guessing one
        #: wrong means offering to change somebody's configuration on a hunch.
        self.recovery = ""
        self.started_at = time.time()
        #: How long the scheduler was asked for, in seconds, or None for a
        #: connection that is not on a clock at all -- a login node, or an
        #: `srun` line with no `-t`. Read from the PROFILE, because that is
        #: what the request was made with; nothing here can ask Slurm.
        self.time_limit = recipes.srun_seconds(remote.srun)
        #: When the allocation began, as this process observed it. None until
        #: it does -- and for the whole life of a session with no time limit,
        #: which is what stops a countdown appearing where there is no clock.
        self.job_started_at = None
        self.lines = []
        self.session = None
        self.nonce = secrets.token_urlsafe(24)

        self._askpass_url = askpass_url
        self._auth_token = auth_token
        self._timeout = timeout
        self._prompt = None
        #: Standing secrets the user has typed during THIS establishment,
        #: keyed by what they answer (`prompt_secret_kind`). Emptied the moment
        #: establishment ends, either way -- see `_forget_secrets_locked`.
        self._secrets = {}
        #: Every question already put, as `(which ssh asked, the wording)`.
        #: The pair rather than the wording alone because the job and the jump
        #: hop authenticate to the SAME host and so ask the same thing word for
        #: word -- while one ssh asking twice is the whole of how a refused
        #: answer is noticed here, ssh not saying so but simply asking again.
        self._asked = set()
        #: False once there is nothing left to open, which is what stops a
        #: later prompt -- a rekey hours in -- from being answered silently.
        self._reuse = True
        self._lock = threading.Lock()
        self._thread = None
        self._helper_dir = None
        self._stopping = False
        #: What `_prepare_compute` did to get the VM -- "created", "started",
        #: "reused" -- or None for a profile that does not rent one. The
        #: teardown reads it: a machine this attempt brought up is ours to put
        #: back, and one that was already running when we arrived is not.
        self._compute_action = None
        #: Teardown reaches the VM from two directions (stop() and
        #: _tidy_after_end) and must reach it once.
        self._compute_released = False

    # -- what the page reads ----------------------------------------------

    @property
    def url(self):
        session = self.session
        return session.open_url if session is not None else None

    @property
    def node_name(self):
        """What the node this session registers is called on the map.

        The profile's `node_name` when it has one, its own name otherwise --
        the same fallback `Remote.as_node_kwargs` applies, kept in one place so
        that the name a status reports and the name written into nodes.json
        cannot drift apart.
        """
        return getattr(self.remote, "node_name", None) or self.name

    @property
    def expires_at(self):
        """When the allocation runs out, as an epoch, or None if unknown.

        None the moment this session stops being live, and that is not a
        detail. Disconnecting stops a session but deliberately KEEPS its record
        -- the final state and the last of its log are the only account of what
        happened, and dropping them on stop would take the answer away exactly
        when somebody is looking for it. A deadline computed from
        `job_started_at + time_limit` alone knows nothing about that: it goes on
        counting down for a connection the user closed, on an allocation that
        was very likely cancelled with it, and every surface asking "how long is
        left" was faithfully drawing a clock for a job that no longer exists.

        Live means on its way up or up. Registration happens inside
        establishment, so `_register_node` still gets a deadline to write into
        nodes.json; only `failed` and `exited` fall through to None.
        """
        if not self.time_limit or self.job_started_at is None:
            return None
        if self.state not in (*OPENING_STATES, STATE_CONNECTED):
            return None
        return self.job_started_at + self.time_limit

    def _start_the_clock_locked(self):
        """Note when the allocation began. Once, and only when there is one.

        The best moment this process can observe, and it is a proxy rather than
        the truth: Slurm starts counting when it starts the job, and what we
        see is establishment moving off `waiting_for_job` -- the announce that
        says the job is running and names the machine it landed on. The gap
        between those is the fraction of a second srun spends exec'ing Plexora,
        so a countdown built on this runs very slightly EARLY. That is the
        right direction for the one error it can make: a warning that comes
        a second sooner than it had to costs nothing, and one that comes after
        the job has already gone is not a warning.

        Idempotent, because both callers are legitimate: a queued job passes
        through `waiting_for_job` and is stamped on the way out of it, and a
        connection that never queued at all is stamped when it lands.
        """
        if self.time_limit and self.job_started_at is None:
            self.job_started_at = time.time()

    def status(self, log_lines=25):
        session = self.session
        with self._lock:
            prompt = self._prompt
            pending = None
            if prompt is not None and not prompt.ready.is_set():
                pending = {"id": prompt.id, "text": prompt.text}
            return {
                "name": self.name,
                "kind": self.kind,
                "phase": self._phrase(),
                # Which node this session put on the map, for a form waiting to
                # browse it. Only a node session has one, and only once it has
                # answered -- until then there is nothing to point a field at.
                #
                # `node_name`, not `name`: the profile is called one thing and
                # the node it registers may be called another (`as_node_kwargs`
                # passes `node_name or name` through, and that is what lands in
                # nodes.json and in `managed_by`). Reporting the profile name
                # here would hand a form a node identifier that resolves to
                # nothing the moment the two differ.
                "node": (self.node_name if self.kind == KIND_NODE
                         and self.state == STATE_CONNECTED else None),
                "state": self.state,
                # The clock, in two forms because they answer different
                # questions. `time_limit` is what was ASKED FOR and never
                # changes; `time_left` is what is left at the moment of this
                # response, which is what a countdown starts from -- sent as a
                # duration rather than a deadline so that a browser whose clock
                # disagrees with this machine's still counts down correctly.
                "time_limit": self.time_limit,
                "time_left": self.time_left,
                "error": self.error,
                # Beside the error and never instead of it: the sentence is
                # the account of what happened, and this is only whether one
                # of its clauses can be a button.
                "recovery": self.recovery,
                "url": self.url,
                "prompt": pending,
                # Which data nodes this connection set up, and which it could
                # not. Reported separately from `error` on purpose: a node that
                # failed to register leaves a viewer that opens with one layer
                # missing, which is a note rather than a failed connection.
                "data_nodes": list(getattr(session, "data_nodes", []) or []),
                "node_errors": list(getattr(session, "node_errors", []) or []),
                "log": [redact(line) for line in self.lines[-log_lines:]],
            }

    @property
    def time_left(self):
        """Seconds until the allocation ends, floored at zero, or None.

        Zero is a real answer and is not None: it means this connection is out
        of time, which is a thing to say. None means there is no clock.
        """
        expires = self.expires_at
        if expires is None:
            return None
        return max(0, int(round(expires - time.time())))

    def _phrase(self):
        """The sentence for this state, in the words this KIND needs.

        One state, two waits of very different length. A viewer answering
        through a tunnel is seconds; a data node's first start pulls Plexora's
        whole dependency stack off a shared filesystem, which on a cluster runs
        into minutes. The generic line reads as a hang for the second, so it
        says how long is normal rather than leaving somebody to guess.
        """
        if (self.kind == KIND_NODE
                and self.state == STATE_WAITING_FOR_APP):
            return ("Starting the data node over there… a first start can take "
                    "a few minutes while it loads.")
        return PHRASES.get(self.state, "")

    # -- running -----------------------------------------------------------

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"plexora-connect-{self.name}")
        self._thread.start()
        return self

    def _echo(self, line):
        """Collect ssh's output. Only the log -- the phase arrives separately."""
        with self._lock:
            self.lines.append(str(line))
            del self.lines[:-LOG_LINES]

    def _on_phase(self, phase):
        """Follow establishment's own account of what it is waiting for.

        A callback rather than reading the echoed lines, because the line
        announcing the longest wait -- a queued job -- is only printed five
        seconds in, and a page that took its cue from that would spend those
        five seconds saying it was doing something else.

        A prompt outranks all of this: while ssh is waiting on a password, what
        is being waited for is the user, whatever the transport is doing. It is
        recorded even so -- see `_phase_state`, which is what the page returns
        to once the question has been answered.
        """
        mapped = {"installing": STATE_INSTALLING,
                  "mounting_data": STATE_MOUNTING_DATA,
                  "waiting_for_job": STATE_WAITING_FOR_JOB,
                  "tunneling": STATE_TUNNELING,
                  "starting": STATE_TUNNELING,
                  "waiting_for_app": STATE_WAITING_FOR_APP}.get(phase)
        if mapped is None:
            return
        with self._lock:
            if (self._phase_state == STATE_WAITING_FOR_JOB
                    and mapped != STATE_WAITING_FOR_JOB):
                # Off the queue and into the job: the allocation is running,
                # and this is the moment its clock started.
                self._start_the_clock_locked()
            self._phase_state = mapped
            if self.state not in (STATE_AUTHENTICATING, STATE_FAILED):
                self.state = mapped

    def _register_node(self, name, endpoint, token, browser_endpoint=None,
                       managed_by=None):
        """Put the node on the map, carrying this session's deadline with it.

        The deadline has to be written where the NODE is recorded, because that
        is the pair that survives: a data node outlives the process that
        started it, so after a Plexora restart the tunnel is still up and this
        session object is gone. That is exactly the state in which somebody
        most needs to know how long is left, and nothing else on screen would
        be able to tell them.

        By the time a node announces, the job it is running in has been
        allocated -- `waiting_for_job` is behind us -- so the clock is already
        stamped. See `_start_the_clock_locked`.
        """
        if self._register is None:
            return None
        return self._register(name, endpoint, token,
                              browser_endpoint=browser_endpoint,
                              managed_by=managed_by,
                              expires_at=self.expires_at)

    def _build(self, env):
        common = dict(
            echo=self._echo,
            on_phase=self._on_phase,
            env=env,
            # No controlling terminal, so ssh cannot decide to prompt on the
            # console this server was started from -- where nobody is looking,
            # and where it would hang until the timeout.
            detach=True,
            timeout=self._timeout,
        )
        if self.kind == KIND_NODE:
            return connect.NodeSession(
                self.remote.target,
                allow_origin=self._allow_origin,
                register=self._register_node,
                **common,
                **self.remote.as_node_kwargs(),
            )
        return connect.Session(self.remote.target, **common,
                               **self.remote.as_session_kwargs())

    def _prepare_compute(self):
        """Have the machine, before there is anything to ssh to.

        Only for a profile that rents its machine rather than naming one. Runs
        on this thread, inside the same try/except as establishment, so a
        quota refusal or a disabled API arrives on the page as the sentence
        Google gave -- through exactly the path an ssh failure takes, and with
        the same teardown behind it.

        Every rung of the ladder is echoed into this session's log, because
        the three outcomes cost wildly different amounts: reusing a running VM
        is free and instant, starting a stopped one is a minute, and creating
        one is a machine somebody is now paying for. `redact()` covers the
        lines on the way out like any others.
        """
        from plexora import gcloud

        record = self.remote.gcloud
        with self._lock:
            self._phase_state = STATE_PREPARING_COMPUTE
            if self.state not in (STATE_AUTHENTICATING, STATE_FAILED):
                self.state = STATE_PREPARING_COMPUTE
        action = gcloud.ensure_instance(record, echo=self._echo)
        with self._lock:
            self._compute_action = action

    def _release_compute(self, after_failure=False):
        """Give the rented machine back. Every ending comes through here.

        The billing story of this preset is decided in this method, because a
        VM nobody stops is a VM somebody pays for. There were five ways a
        session could end and only one of them -- the Disconnect button -- used
        to consult the profile at all; the other four (a failed connect, a
        dropped network, a quit app, a crash) left a 16-core machine running
        with nothing left in the process that would ever notice.

        Two rules, and they differ on purpose:

        - **After a failure, put back only what this attempt brought up, and
          only by stopping it.** A VM created or started for a connection that
          then failed has never carried a session and never will; stopping it
          is cleaning up our own mess, and it happens even when the profile
          says to leave VMs running, because "leave it running" means "leave
          my session's machine up", not "keep paying for a machine that failed
          to connect". A VM that was already running when we arrived is left
          exactly as we found it.

          **Never deleted, even when the profile says Delete.** A machine that
          failed to connect is the one machine still worth reading: the reason
          is in `/var/log/plexora-startup.log` and `/tmp/plexora-gcsfuse-
          install.log` on its disk, and the next connection prints both. A
          teardown that deleted it would be destroying the evidence for the
          bug that had just been hit, which is a very fast way to make a
          failure permanent.
        - **After a normal end, do what the profile says** -- leave, stop, or
          delete, from `gcloud.exit_action`.

        Never deletes a machine the user already runs: `exit_action` refuses to
        return Delete for one, `gcloud.profile` will not store it, and
        `delete_instance` checks the label on the instance itself. Three
        refusals for one mistake, because it is the one mistake here that
        cannot be undone.

        Never fatal, and never blocking: this runs on a dying session thread
        and inside an atexit handler, where an exception is noise and a
        ninety-second wait for Google to confirm is worse than useless.
        """
        record = self.remote.gcloud
        if not record:
            return
        try:
            from plexora import gcloud
        except Exception:             # noqa: BLE001 - an ending, not a step
            return

        with self._lock:
            if self._compute_released:
                return
            self._compute_released = True
            action = self._compute_action
        rented = record.get("vm_source", "plexora") != "existing"
        wanted = gcloud.exit_action(record)
        if after_failure:
            if not rented or action not in ("created", "started"):
                return
            wanted = gcloud.EXIT_STOP
            why = ("the VM this connection just created"
                   if action == "created" else
                   "the VM this connection just started")
        else:
            if wanted == gcloud.EXIT_LEAVE:
                return
            why = "the VM"
        # A profile has two possible sessions -- the viewer and the data node
        # -- and they share one machine. Whichever ends first must not switch
        # the other one's floor off.
        if self._other_live_session():
            self._echo("  Leaving the VM running: another Plexora session is "
                       "still using it.")
            return
        try:
            if wanted == gcloud.EXIT_DELETE:
                gcloud.delete_instance(record, block=False)
                self._echo(
                    "  Deleting the VM, as this connection is set to. Nothing "
                    "keeps billing; your bucket is untouched, and connecting "
                    "again builds a new machine.")
                return
            gcloud.stop_instance(record, block=False)
            self._echo(f"  Stopping {why} so it stops billing. Reconnecting "
                       f"starts it again in under a minute.")
        except Exception as exc:      # noqa: BLE001 - reported, never raised
            verb = "delete" if wanted == gcloud.EXIT_DELETE else "stop"
            self._echo(f"  Could not {verb} the VM automatically: {exc}. Do it "
                       f"in Settings or in the Google Cloud console so it does "
                       f"not keep billing.")

    def _other_live_session(self):
        """Is the profile's *other* session (viewer vs node) still up?"""
        alive = OPENING_STATES + (STATE_CONNECTED,)
        for session in all_sessions().values():
            if session is self or session.remote.name != self.remote.name:
                continue
            if session.state in alive:
                return True
        return False

    def _run(self):
        try:
            if self.remote.gcloud:
                self._prepare_compute()
            env = self._ssh_environment()
            self.session = self._build(env)
            self.session.establish()
        except BaseException as exc:  # noqa: BLE001 - reported, never raised
            self._fail(exc)
            # Not only the helper: establishment spawns real ssh processes
            # before it can fail -- under srun, a job holding an allocation and
            # a tunnel beside it -- and a failed connection's children serve
            # nobody. Left running, they held the Slurm job (and its walltime
            # bill) until the app exited.
            # On Google Cloud the equivalent of those children is a whole
            # machine: the VM is created BEFORE anything can be established,
            # so every failure after that point -- a refused mount, a bucket
            # without permission, a tunnel that never opened -- used to leave
            # one running that nothing would ever turn off. Hence the flag:
            # the teardown has to know this was a failure to apply the
            # narrower, stricter rule.
            self._tidy_after_end(after_failure=True)
            return

        with self._lock:
            if self.state != STATE_FAILED:
                self.state = STATE_CONNECTED
                # For a job that was allocated instantly and never announced a
                # queue wait. No-op for one that did -- see the method.
                self._start_the_clock_locked()
            # There is nothing left to open, so there is nothing left to hold a
            # credential for. This is the closing of the reuse window.
            self._forget_secrets_locked()

        # The helper deliberately outlives establishment and is removed in
        # stop(): ssh owns it for as long as it is running, and deleting the
        # program it has been told to exec would turn any later prompt -- a
        # rekey, a second hop -- into a failure with no explanation.

        # Then simply outlive the request that started us. This blocks for the
        # life of the connection, which is exactly what the thread is for.
        try:
            self.session.wait()
        except Exception:
            pass
        with self._lock:
            if self.state not in (STATE_FAILED,):
                self.state = STATE_EXITED
            stopping = self._stopping
        # `wait()` returning without stop() having been called means the
        # connection died on its own -- a walltime, a dropped network, a crash
        # on the far side -- and nothing else is going to run the teardown.
        # When stop() IS what unblocked the wait, the teardown has already run
        # (and for a disconnect, the route forgets the node itself).
        if not stopping:
            self._tidy_after_end()

    def _tidy_after_end(self, after_failure=False):
        """Teardown for a session nothing will press Disconnect on.

        Four things only stop() used to do, each of which a session that
        failed or exited on its own left behind:

        - The sibling watchers. Under srun the tunnel is a second ssh: the job
          leg exiting (which is what ends `wait()`) does not end it, and on a
          cluster that does not adopt compute-side ssh into the job it kept a
          local port listening and forwarding into refused connections.
        - The askpass helper's temp directory.
        - The node entry this session registered. The node ran inside the job
          that just ended, so the address on the map is dead -- and while the
          entry stood, `/resource_routing` kept offering it to browsers and
          `/resource_status` reported a project reading from it as fine. Only
          an entry THIS session put there (`session.registered`), through the
          route-supplied callable that re-checks `managed_by` -- an entry a
          terminal's own `plexora connect` owns is not this session's to take
          down.
        """
        try:
            if self.session is not None:
                self.session.stop()
        except Exception:
            pass
        self._cleanup_helper()
        if (self.kind == KIND_NODE and self._unregister is not None
                and getattr(self.session, "registered", None) is not None):
            try:
                self._unregister(self.node_name)
            except Exception:
                pass
        # And the rented machine. A connection that died on its own -- a
        # dropped network, a VM reboot, a laptop that slept -- reaches here
        # and nowhere else, and it is exactly the case where nobody is
        # watching to press Disconnect.
        self._release_compute(after_failure=after_failure)

    def _fail(self, exc):
        with self._lock:
            self.state = STATE_FAILED
            self.error = self._diagnose(exc)
            # Only what the raiser attached. Nothing here reads the sentence
            # back to work out what went wrong -- a recovery offered on a
            # substring match is a button that changes a saved profile because
            # two unrelated errors happened to share a word.
            self.recovery = str(getattr(exc, "recovery", "") or "")
            self._forget_secrets_locked()
            self._cancel_prompt_locked()

    def _diagnose(self, exc):
        """The one sentence worth putting in front of the user.

        ssh's own message is usually the honest answer, but four failures are
        common enough and unhelpful enough in raw form to be worth naming: a
        remote `plexora` that is not on a non-interactive PATH, a scheduler
        that refused the job, a rejected credential, and a changed host key.
        Each has a different fix and none of them is "try again".
        """
        lines = []
        if self.session is not None:
            for watched in self.session.watchers:
                watched.drain(timeout=1)
                lines += watched.lines
        text = str(exc) or exc.__class__.__name__

        # Before anything below reads the output: a step that already knew
        # what failed has said so, and everything below this line is guesswork
        # over substrings. The mount step prints apt's own log when an install
        # fails; that log said `gpg: not found`, and this function told
        # somebody their remote PATH was wrong about a connection whose PATH
        # was fine and whose VM was missing a package.
        if getattr(exc, "diagnosed", False):
            return text

        # Before the missing-command check: argparse's refusal carries "usage:"
        # and a subcommand list, which trips the same markers a shell's "not
        # found" does -- and would send somebody editing a PATH that is right.
        stale = connect.unsupported_remote_flag(lines)
        if stale:
            return (
                f"The Plexora installed on {self.remote.target} is too old for "
                f"this: it does not understand `{stale}`. Upgrade it there "
                f"(`pip install --upgrade plexora` in the environment "
                f"“Plexora command or environment” points at). Nothing on "
                f"this computer or in this saved server needs changing."
            )
        # Before the missing-command check, which a scheduler's refusal can
        # trip on its own words, and before the login banner reaches `text`:
        # a cluster greets every connection with one, and O2's is a paragraph
        # about lower-case usernames that has nothing to do with any of this.
        refusal = connect.scheduler_refusal(lines)
        if refusal:
            return refusal
        if connect.looks_like_missing_command(lines):
            return (
                f"The remote host could not run "
                f"{self.remote.remote_command!r}. A non-interactive SSH "
                f"session usually has a shorter PATH than a login shell -- set "
                f"“Plexora command or environment” to the environment "
                f"Plexora is installed in, e.g. "
                f"`/home/you/miniconda3/envs/plexora`."
            )
        if any(marker in line for line in lines for marker in _PERMISSION_DENIED):
            return ("The remote host rejected the login. Check the username, "
                    "and the password or key you are using for it.")
        if any(marker in line for line in lines for marker in _HOST_KEY):
            return ("SSH refused to continue because this host's key is not "
                    "the one it saw last time. That is worth checking with "
                    "your administrator before accepting it.")
        return text

    # -- the askpass handshake ---------------------------------------------

    def _ssh_environment(self):
        """A copy of this process's environment, with the relay wired in.

        `SSH_ASKPASS_REQUIRE=force` is what makes ssh use the helper even
        though it could theoretically find a terminal; it needs OpenSSH 8.4 or
        newer, which is every macOS and every current Linux. On older ssh the
        trigger is a set `DISPLAY` and no tty, so that is set too -- harmless
        where REQUIRE works, and the whole mechanism where it does not.
        """
        env = dict(os.environ)
        if not self._askpass_url:
            return env

        # Recorded before the helper is written, not returned for the caller to
        # store: a failure in between would otherwise leave a temp directory
        # that nothing knows about and stop() cannot remove.
        self._helper_dir = tempfile.mkdtemp(prefix="plexora-askpass-")
        helper = _write_helper(self._helper_dir)
        env.update({
            "SSH_ASKPASS": str(helper),
            "SSH_ASKPASS_REQUIRE": "force",
            "DISPLAY": env.get("DISPLAY") or ":0",
            askpass.ENV_URL: self._askpass_url,
            askpass.ENV_NONCE: self.nonce,
        })
        if self._auth_token:
            env[askpass.ENV_TOKEN] = self._auth_token
        return env

    def _cleanup_helper(self):
        directory, self._helper_dir = self._helper_dir, None
        if not directory:
            return
        try:
            for child in Path(directory).iterdir():
                child.unlink(missing_ok=True)
            Path(directory).rmdir()
        except OSError:
            pass

    def open_prompt(self, text, asker=None):
        """Record a question ssh is asking, or answer it from what was typed.

        One connection to a cluster authenticates three times and every one of
        them arrives here, so answering only the first is what turns three
        password boxes into one. The prompt object is returned either way and
        `collect` is unchanged: a reused answer is simply one that is ready
        before the page has been told there was a question.

        `asker` identifies the ssh process behind the question and is what
        makes the refusal check honest -- two hops to the same host ask the
        same words, so the wording alone cannot tell a second hop from a
        second attempt. None (Windows) falls back to the wording, which errs
        towards asking the person again.
        """
        text = str(text)
        prompt = _Prompt(id=secrets.token_urlsafe(12), text=text)
        asked = (str(asker or ""), text)
        with self._lock:
            kind = prompt_secret_kind(text)
            if asked in self._asked:
                # The same ssh asking the same thing twice: the answer it was
                # given was refused. Drop it rather than offer it again -- that
                # is what caps a typo at one retry instead of one per hop.
                self._secrets.pop(kind, None)
            self._asked.add(asked)
            secret = self._secrets.get(kind) if kind else None
            if secret is not None:
                prompt.reused = True
                prompt.answer = secret
                prompt.ready.set()
            elif self.state in OPENING_STATES:
                # Only a question somebody has to answer is worth changing the
                # state for. One answered from memory leaves the page saying
                # what it is actually waiting for -- usually the scheduler.
                self.state = STATE_AUTHENTICATING
            self._prompt = prompt
        if prompt.reused:
            # Outside the lock: `_echo` takes it, and it is not reentrant.
            # Logged because a credential used somewhere the user did not watch
            # it being used should still be visible afterwards. The trailing
            # colon goes because `redact` eats whatever follows one after the
            # word "password" -- correctly, on every other line but this.
            self._echo(f"  Answered “{text.strip().rstrip(':')}” with what "
                       f"you typed a moment ago.")
        return prompt

    def collect(self, prompt_id, timeout=0.0):
        """The answer for `prompt_id`, or None while nobody has typed one."""
        with self._lock:
            prompt = self._prompt
        if prompt is None or prompt.id != prompt_id:
            return None
        if timeout:
            prompt.ready.wait(timeout=timeout)
        if not prompt.ready.is_set():
            return None
        if prompt.cancelled:
            return False
        answer, prompt.answer = prompt.answer, None  # handed over exactly once
        return answer or ""

    def answer(self, text, prompt_id=None):
        with self._lock:
            prompt = self._prompt
            if prompt is None or prompt.ready.is_set():
                return False
            if prompt_id and prompt.id != prompt_id:
                return False
            prompt.answer = str(text)
            prompt.ready.set()
            kind = prompt_secret_kind(prompt.text)
            if kind and self._reuse:
                self._secrets[kind] = str(text)
            if self.state == STATE_AUTHENTICATING:
                # Back to whatever was actually happening, not to the beginning.
                # On a cluster the password is asked for AFTER the job has been
                # submitted, so resetting to "connecting" here left the page
                # claiming to be opening an SSH connection for the whole wait in
                # the queue -- which is the longest and most anxious part of it,
                # and the one the wording exists to explain.
                self.state = self._phase_state
            return True

    def _forget_secrets_locked(self):
        """Drop everything typed, and stop keeping anything else.

        Called when establishment ends, whichever way it ended. After this a
        prompt goes to the person again -- a rekey, a hop this session makes an
        hour from now -- which is the right answer: the reuse exists to get one
        connection open, not to hold a password for the afternoon.
        """
        self._reuse = False
        self._secrets.clear()

    def _cancel_prompt_locked(self):
        prompt = self._prompt
        if prompt is not None and not prompt.ready.is_set():
            prompt.cancelled = True
            prompt.answer = None
            prompt.ready.set()

    # -- teardown ----------------------------------------------------------

    def stop(self):
        with self._lock:
            self._stopping = True
            self._forget_secrets_locked()
            self._cancel_prompt_locked()
        if self.session is not None:
            self.session.stop()
        self._cleanup_helper()
        with self._lock:
            if self.state not in (STATE_FAILED,):
                self.state = STATE_EXITED
        # Deliberately after the state is set, so `_other_live_session` on the
        # sibling session sees this one as finished rather than as a reason to
        # keep the machine up. This is the path the Disconnect button takes,
        # and also the one `atexit` takes when the app quits with a connection
        # still open -- which is why the profile's switch is consulted here
        # and not only in the HTTP route that used to own it.
        self._release_compute()


def _write_helper(directory):
    """An executable ssh can run, wrapping this interpreter around askpass.py.

    SSH_ASKPASS has to name a program, not a command line, so `python
    askpass.py` cannot be expressed there directly. Two lines of shell can.
    Written 0700 inside a private temp directory: it names nothing secret, but
    it is a program this process will be asked to execute.
    """
    script_path = Path(askpass.__file__).resolve()
    directory = Path(directory)
    if os.name == "nt":
        helper = directory / "plexora-askpass.bat"
        helper.write_text(
            f'@echo off\r\n"{sys.executable}" "{script_path}" %*\r\n',
            encoding="utf-8",
        )
        return helper
    helper = directory / "plexora-askpass"
    helper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{script_path}" "$@"\n',
        encoding="utf-8",
    )
    os.chmod(helper, stat.S_IRWXU)
    return helper


# -- the registry ----------------------------------------------------------


_SESSIONS = {}
_REGISTRY_LOCK = threading.Lock()


class ConnectionRefused(RuntimeError):
    """Asked for something the manager will not do -- with a reason to show."""


def get(name, kind=KIND_VIEWER):
    with _REGISTRY_LOCK:
        return _SESSIONS.get(_key(kind, name))


def all_sessions():
    with _REGISTRY_LOCK:
        return dict(_SESSIONS)


def find_by_nonce(nonce):
    """The session a helper's nonce belongs to, or None.

    Compared against every live session rather than looked up, because the
    nonce is the credential: a dictionary keyed by it would be a lookup table
    an attacker could probe one entry at a time.
    """
    if not nonce:
        return None
    for session in all_sessions().values():
        if secrets.compare_digest(str(nonce), session.nonce):
            return session
    return None


def start(remote, *, askpass_url=None, auth_token=None, timeout=None,
          kind=KIND_VIEWER, allow_origin=None, register=None, unregister=None):
    """Begin connecting to `remote`, or say why not."""
    key = _key(kind, remote.name)
    with _REGISTRY_LOCK:
        existing = _SESSIONS.get(key)
        if existing is not None and existing.state in (
                *OPENING_STATES, STATE_CONNECTED):
            raise ConnectionRefused(
                f"“{remote.name}” is already connected or connecting.")
        connecting = sum(1 for session in _SESSIONS.values()
                         if session.state in OPENING_STATES)
        if connecting >= MAX_CONNECTING:
            raise ConnectionRefused(
                f"{MAX_CONNECTING} connections are already being opened. Wait "
                f"for one to finish, or disconnect it.")
        session = RemoteSession(remote, askpass_url=askpass_url,
                                auth_token=auth_token, timeout=timeout,
                                kind=kind, allow_origin=allow_origin,
                                register=register, unregister=unregister)
        _SESSIONS[key] = session
    if existing is not None:
        # Outside the lock -- stopping waits on processes. A failed or exited
        # session's stop() is usually a no-op by now (its own teardown ran),
        # but replacing the record without it leaked its watcher entries into
        # connect._ACTIVE for the life of the app, and any child process that
        # happened to survive its session with them.
        try:
            existing.stop()
        except Exception:
            pass
    return session.start()


def stop(name, kind=KIND_VIEWER):
    session = get(name, kind)
    if session is None:
        return False
    session.stop()
    return True


def forget(name, kind=KIND_VIEWER):
    """Drop a finished session's record. Live ones are stopped first."""
    session = get(name, kind)
    if session is not None:
        session.stop()
    with _REGISTRY_LOCK:
        _SESSIONS.pop(_key(kind, name), None)


def _shut_down_all():
    for session in list(all_sessions().values()):
        try:
            session.stop()
        except Exception:
            pass


# connect.py registers its own handler for the watchers it tracks; this one
# covers a session whose ssh never reached _ACTIVE, and is idempotent either
# way because stopping a stopped process is a no-op.
atexit.register(_shut_down_all)
