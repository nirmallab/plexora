"""The bridge between ssh's password prompt and a browser tab.

`plexora connect` in a terminal needs none of this: ssh has a tty, and it
prompts on it. The Settings page has the opposite problem. There the ssh
processes are children of a Flask server that nobody is looking at -- a prompt
written to its console would appear in a log file at best, and hang forever at
worst, which is exactly what "Connect" did before this existed on any site that
uses passwords, Duo, or a one-time code.

ssh has one supported way out of that, and this is it. Given `SSH_ASKPASS`
pointing at an executable and `SSH_ASKPASS_REQUIRE=force`, ssh runs the
program with the prompt as its argument and reads the answer from its stdout.
So the program is this file: it hands the prompt back to the Plexora that
spawned it over loopback, waits for the user to type the answer into the page,
prints it, and exits. Every kind of prompt travels the same way -- a password,
a Duo push code, a `yes/no` host-key confirmation -- because ssh asks all of
them through this one door.

**What the secret does and does not touch.** It arrives over loopback, is held
in one attribute of one session object, is handed to ssh's stdin-equivalent
once, and is dropped. It is never written to remotes.json, never logged, never
included in a status response, and never in a process argument. This module
prints it to stdout, which is a pipe ssh created and is reading -- that is the
interface, and it is the only place the value is written at all.

Standalone-loadable, stdlib-only, and for a sharper reason than cli.py's: ssh
spawns this as a bare program hundreds of milliseconds into a connection, and
`import plexora` would build a Flask app, register every Blueprint and import
tifffile to answer a password prompt.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

#: Where to hand the prompt back to. The base of the two askpass routes on the
#: Plexora that spawned the ssh -- always a loopback address.
ENV_URL = "PLEXORA_ASKPASS_URL"
#: A per-session one-time value. Loopback is not by itself an authorisation on
#: a shared machine, where every other account can also reach 127.0.0.1; this
#: is what makes a prompt belong to one connection attempt.
ENV_NONCE = "PLEXORA_ASKPASS_NONCE"
#: The app's own auth token, when it is running with one. Carried so the
#: askpass routes need no exemption from the guard -- see plexora/__init__.py,
#: where the rule is that nothing is exempt.
ENV_TOKEN = "PLEXORA_ASKPASS_TOKEN"
ENV_TIMEOUT = "PLEXORA_ASKPASS_TIMEOUT"

#: How long to wait for a person. Generous because it is measured against a
#: human reaching for a phone to read a six-digit code, and the cost of it
#: being too short is a failed cluster login.
DEFAULT_TIMEOUT = 180.0
POLL_SECONDS = 1.0

# Seams, rebound by tests. Same convention as connect.py.
_urlopen = urllib.request.urlopen
_sleep = time.sleep
_now = time.monotonic


class AskpassError(RuntimeError):
    pass


def _url(base, path, token=None, **query):
    url = f"{str(base).rstrip('/')}/{path.lstrip('/')}"
    if token:
        query = dict(query, token=token)
    return f"{url}?{urllib.parse.urlencode(query)}" if query else url


def _request(url, payload=None, timeout=10):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    with _urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", "replace")
    return json.loads(body) if body.strip() else {}


#: How far above this process to look for the ssh that wants an answer. Two
#: levels is the real chain -- python under cmd.exe under ssh.exe -- and the
#: spare ones are for a site whose ssh is itself wrapped. A cap rather than a
#: while loop because pids are reused, so a snapshot can contain a cycle.
ANCESTOR_DEPTH = 6

#: Which ancestor counts. Lower-cased image names. A jump hop is `ssh -W`, a
#: separate ssh.exe with its own pid, which is exactly right: it authenticates
#: separately and so is a separate asker.
ASKER_IMAGES = ("ssh.exe",)


def nearest_asker(pid, parents, images, depth=ANCESTOR_DEPTH):
    """Walk up `parents` to the first pid whose image is in `images`.

    Split out from the snapshot that fills it so the walk can be tested without
    one. `parents` maps a pid to `(parent pid, lower-cased image name)` -- the
    name belongs to the KEY, not to the parent, so a step up has to look the
    new pid up again to find out what it is.
    """
    for _ in range(depth):
        entry = parents.get(pid)
        if not entry:
            return None
        pid = entry[0]
        if not pid:
            return None
        ancestor = parents.get(pid)
        if ancestor is not None and ancestor[1] in images:
            return pid
    return None


def _windows_process_tree():
    """`{pid: (parent pid, image name)}` for every process, or None.

    Toolhelp32 rather than anything friendlier because this module is
    stdlib-only by contract and runs a few hundred milliseconds into an ssh
    connection -- one snapshot is a couple of milliseconds and needs no
    privileges, which matters because the ssh above us may be running as
    somebody this process cannot open a handle to.
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None

    class _Entry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            # The ANSI entry, to match Process32First below. The names this
            # looks for are ASCII, so the narrow variant loses nothing.
            ("szExeFile", ctypes.c_char * 260),
        ]

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD,
                                                      wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32First.argtypes = [wintypes.HANDLE,
                                            ctypes.POINTER(_Entry)]
        kernel32.Process32First.restype = wintypes.BOOL
        kernel32.Process32Next.argtypes = [wintypes.HANDLE,
                                           ctypes.POINTER(_Entry)]
        kernel32.Process32Next.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        TH32CS_SNAPPROCESS = 0x00000002
        invalid = ctypes.c_void_p(-1).value
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot == invalid:
            return None
        tree = {}
        try:
            entry = _Entry()
            entry.dwSize = ctypes.sizeof(_Entry)
            found = kernel32.Process32First(snapshot, ctypes.byref(entry))
            while found:
                tree[int(entry.th32ProcessID)] = (
                    int(entry.th32ParentProcessID),
                    entry.szExeFile.decode("mbcs", "replace").lower(),
                )
                found = kernel32.Process32Next(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
        return tree
    except Exception:
        # Every failure here means the same thing to the caller: no identity,
        # so treat a repeat as a refusal. Never worth failing a login over.
        return None


def asking_process():
    """Which ssh is asking, or None when that cannot be known.

    One connection to a cluster runs three ssh processes and two of them
    authenticate to the SAME host, so they ask the same question word for word.
    Plexora reuses an answer across hops but must not reuse one that a hop has
    already refused, and those two cases are only distinguishable by which
    process is asking -- identical text otherwise.

    The POSIX helper `exec`s this interpreter, so this process's parent IS the
    ssh that wants an answer, and it stays the same pid across that ssh's own
    retries. The Windows wrapper is a .bat and cannot exec, so the parent there
    is a transient cmd.exe -- a new pid every time, which would look like a new
    asker at every hop AND at every retry. Reading it as a new asker is the
    unsafe direction (a refused password replayed once per hop, at a site with
    a lockout policy), so Windows used to report nothing at all and take the
    safe reading instead: any repeat is a refusal. That is safe and it is also
    why "remembered for this connection" never survived the first hop there,
    since the job and the tunnel ask the login node the same words.

    So the cmd.exe is stepped over rather than trusted: ssh.exe is its parent
    and has the stable pid this wants. Anything unexpected on the way up --
    no snapshot, an ancestor that is not an ssh -- returns None and lands back
    on the safe reading, which is where Windows already was.
    """
    if os.name == "nt":
        tree = _windows_process_tree()
        if not tree:
            return None
        pid = nearest_asker(os.getpid(), tree, ASKER_IMAGES)
        return f"pid:{pid}" if pid else None
    try:
        return f"pid:{os.getppid()}"
    except OSError:
        return None


def ask(prompt, env=None):
    """Post `prompt` back to Plexora and block until somebody answers it.

    Returns the answer. Raises AskpassError when nobody does in time, when the
    user cancels, or when the session it belonged to has already gone -- all of
    which mean the same thing to ssh, which is that authentication failed.
    """
    env = os.environ if env is None else env
    base = env.get(ENV_URL)
    nonce = env.get(ENV_NONCE)
    if not base or not nonce:
        raise AskpassError(
            "not running under a Plexora connection (no askpass endpoint)")
    token = env.get(ENV_TOKEN) or None
    try:
        timeout = float(env.get(ENV_TIMEOUT) or DEFAULT_TIMEOUT)
    except ValueError:
        timeout = DEFAULT_TIMEOUT

    opened = _request(_url(base, "prompt", token),
                      {"nonce": nonce, "prompt": str(prompt),
                       "asker": asking_process()})
    prompt_id = opened.get("id")
    if not prompt_id:
        raise AskpassError(opened.get("error") or "Plexora refused the prompt")

    deadline = _now() + timeout
    poll = _url(base, "answer", token, nonce=nonce, id=prompt_id)
    while _now() < deadline:
        try:
            answer = _request(poll, timeout=10)
        except urllib.error.URLError as exc:
            # The Plexora that spawned us has gone. Nothing will ever answer.
            raise AskpassError(f"Plexora is no longer listening: {exc}") from exc
        state = answer.get("state")
        if state == "answered":
            return answer.get("answer") or ""
        if state == "cancelled":
            raise AskpassError("cancelled")
        _sleep(POLL_SECONDS)
    raise AskpassError("timed out waiting for the answer")


def main(argv=None):
    """ssh's contract: the answer on stdout, or a non-zero exit and no output.

    The prompt text arrives as argv[1] and is passed through verbatim rather
    than being parsed or prettified. It is written by whichever authentication
    method the site uses -- "Password:", "Duo two-factor login for you", "Enter
    passphrase for key ...", the whole host-key paragraph ending in
    "(yes/no)?" -- and only the user can tell which of those they are looking
    at. Rewriting it would be guessing on their behalf about the one thing
    they need to read exactly.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    prompt = argv[0] if argv else "Password:"
    try:
        sys.stdout.write(ask(prompt))
        sys.stdout.flush()
    except Exception as exc:
        # stderr, never stdout: ssh reads stdout as the answer, so a message
        # written there would be tried as a password.
        sys.stderr.write(f"plexora askpass: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
