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
`_pending.answer` for the moment between the user typing it and the helper
collecting it. It is not stored, not logged, and not in `status()`. The ssh
output that IS shown is redacted on the way out, because Stage C's node
announce puts a token on stdout.
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

#: A connection attempt holds two ssh processes and a thread. Three at once is
#: already an unusual thing to be doing deliberately; more than that is a stuck
#: page retrying, and the cost of not capping it is a Waitress process with
#: forty ssh children.
MAX_CONNECTING = 3

#: How much of each ssh's output to keep. Enough to hold a stack of
#: authentication failures plus whatever the remote shell said on the way out,
#: which is where the actionable line almost always is.
LOG_LINES = 200

STATE_CONNECTING = "connecting"
STATE_AUTHENTICATING = "authenticating"
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
OPENING_STATES = (STATE_CONNECTING, STATE_AUTHENTICATING, STATE_WAITING_FOR_JOB,
                  STATE_TUNNELING, STATE_WAITING_FOR_APP)

#: What each state says on the page. One sentence, in the second person, naming
#: what is being waited for rather than what the code is doing.
PHRASES = {
    STATE_CONNECTING: "Opening an SSH connection…",
    STATE_AUTHENTICATING: "Waiting for you to answer the login prompt…",
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


@dataclass
class _Prompt:
    """One question ssh asked, on its way to the browser and back."""

    id: str
    text: str
    answer: str | None = None
    cancelled: bool = False
    #: Set when the answer is available, so the askpass helper's poll can stop.
    ready: threading.Event = field(default_factory=threading.Event)


class RemoteSession:
    """One saved profile's connection, from spawn to teardown."""

    def __init__(self, remote, *, askpass_url=None, auth_token=None,
                 timeout=None):
        self.remote = remote
        self.name = remote.name
        self.state = STATE_CONNECTING
        #: What establishment last said it was doing, kept separately because a
        #: prompt covers it over for as long as the question is on screen and
        #: this is what there is to go back to once it is answered.
        self._phase_state = STATE_CONNECTING
        self.error = None
        self.started_at = time.time()
        self.lines = []
        self.session = None
        self.nonce = secrets.token_urlsafe(24)

        self._askpass_url = askpass_url
        self._auth_token = auth_token
        self._timeout = timeout
        self._prompt = None
        self._lock = threading.Lock()
        self._thread = None
        self._helper_dir = None
        self._stopping = False

    # -- what the page reads ----------------------------------------------

    @property
    def url(self):
        session = self.session
        return session.open_url if session is not None else None

    def status(self, log_lines=25):
        session = self.session
        with self._lock:
            prompt = self._prompt
            pending = None
            if prompt is not None and not prompt.ready.is_set():
                pending = {"id": prompt.id, "text": prompt.text}
            return {
                "name": self.name,
                "state": self.state,
                "phase": PHRASES.get(self.state, ""),
                "error": self.error,
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
        mapped = {"waiting_for_job": STATE_WAITING_FOR_JOB,
                  "tunneling": STATE_TUNNELING,
                  "starting": STATE_TUNNELING,
                  "waiting_for_app": STATE_WAITING_FOR_APP}.get(phase)
        if mapped is None:
            return
        with self._lock:
            self._phase_state = mapped
            if self.state not in (STATE_AUTHENTICATING, STATE_FAILED):
                self.state = mapped

    def _run(self):
        try:
            env = self._ssh_environment()
            self.session = connect.Session(
                self.remote.target,
                echo=self._echo,
                on_phase=self._on_phase,
                env=env,
                # No controlling terminal, so ssh cannot decide to prompt on
                # the console this server was started from -- where nobody is
                # looking, and where it would hang until the timeout.
                detach=True,
                timeout=self._timeout,
                **self.remote.as_session_kwargs(),
            )
            self.session.establish()
        except BaseException as exc:  # noqa: BLE001 - reported, never raised
            self._fail(exc)
            self._cleanup_helper()
            return

        with self._lock:
            if self.state != STATE_FAILED:
                self.state = STATE_CONNECTED

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

    def _fail(self, exc):
        with self._lock:
            self.state = STATE_FAILED
            self.error = self._diagnose(exc)
            self._cancel_prompt_locked()

    def _diagnose(self, exc):
        """The one sentence worth putting in front of the user.

        ssh's own message is usually the honest answer, but three failures are
        common enough and unhelpful enough in raw form to be worth naming: a
        remote `plexora` that is not on a non-interactive PATH, a rejected
        credential, and a changed host key. Each has a different fix and none
        of them is "try again".
        """
        lines = []
        if self.session is not None:
            for watched in self.session.watchers:
                watched.drain(timeout=1)
                lines += watched.lines
        text = str(exc) or exc.__class__.__name__

        if connect.looks_like_missing_command(lines):
            return (
                f"The remote host could not run "
                f"{self.remote.remote_command!r}. A non-interactive SSH "
                f"session usually has a shorter PATH than a login shell -- set "
                f"“How to start Plexora over there” to the environment "
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

    def open_prompt(self, text):
        """Record a question ssh is asking. Returns its id."""
        prompt = _Prompt(id=secrets.token_urlsafe(12), text=str(text))
        with self._lock:
            self._prompt = prompt
            if self.state in OPENING_STATES:
                self.state = STATE_AUTHENTICATING
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
            if self.state == STATE_AUTHENTICATING:
                # Back to whatever was actually happening, not to the beginning.
                # On a cluster the password is asked for AFTER the job has been
                # submitted, so resetting to "connecting" here left the page
                # claiming to be opening an SSH connection for the whole wait in
                # the queue -- which is the longest and most anxious part of it,
                # and the one the wording exists to explain.
                self.state = self._phase_state
            return True

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
            self._cancel_prompt_locked()
        if self.session is not None:
            self.session.stop()
        self._cleanup_helper()
        with self._lock:
            if self.state not in (STATE_FAILED,):
                self.state = STATE_EXITED


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


def get(name):
    with _REGISTRY_LOCK:
        return _SESSIONS.get(name)


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


def start(remote, *, askpass_url=None, auth_token=None, timeout=None):
    """Begin connecting to `remote`, or say why not."""
    with _REGISTRY_LOCK:
        existing = _SESSIONS.get(remote.name)
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
                                auth_token=auth_token, timeout=timeout)
        _SESSIONS[remote.name] = session
    return session.start()


def stop(name):
    session = get(name)
    if session is None:
        return False
    session.stop()
    return True


def forget(name):
    """Drop a finished session's record. Live ones are stopped first."""
    session = get(name)
    if session is not None:
        session.stop()
    with _REGISTRY_LOCK:
        _SESSIONS.pop(name, None)


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
