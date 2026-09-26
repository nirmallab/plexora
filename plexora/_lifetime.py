"""How a Plexora process ends when nobody is typing Ctrl+C at it.

Three launchers need this and none of them has a terminal: the desktop shell,
which holds the server's stdin and closes it to say "quit"; a data node
started over ssh, which is told the same thing the same way; and File > Quit in
the page, which arrives as a request on a worker thread.

The one clean stop Waitress has is a KeyboardInterrupt on the main thread:
`BaseWSGIServer.run()` catches it, closes the dispatcher and returns, and then
`atexit` runs -- which is where ssh tunnels are closed, the remote-store index
is flushed and `nodes.json` is tidied. `os._exit` from a worker thread skips
every one of those. So `request_shutdown` raises that same interrupt in the main
thread, exactly as Ctrl+C would, and keeps `os._exit` only as the backstop for
a teardown that wedges.

Leaf module, stdlib only: `cli.py` and the node app import it before anything
heavy, and it must never be the reason a process fails to start.
"""

from __future__ import annotations

import _thread
import io
import os
import signal
import sys
import threading

#: Seconds between asking the main thread to stop and giving up on it. Long
#: enough for an ssh session to say goodbye; short enough that a Quit which
#: hangs is still visibly a Quit.
DEFAULT_GRACE = 10.0

_shutdown_lock = threading.Lock()
_shutdown_requested = False


def ensure_std_streams():
    """Give a process started without a console somewhere to write.

    A GUI-launched child on Windows can come up with `sys.stdout` or
    `sys.stderr` set to None, and the first `print()` anywhere -- a startup
    notice, a warning from a dependency -- raises AttributeError from deep
    inside something unrelated. Pointing them at devnull costs nothing and
    turns that into silence. The streams that do exist are made UTF-8, because
    a path with a non-ASCII character is ordinary on a lab share and the
    console code page on Windows is not.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, io.UnsupportedOperation):
                pass
    if sys.stdin is None:
        sys.stdin = open(os.devnull, "r", encoding="utf-8")


def flush_std():
    """Flush stdout and stderr, whatever state they are in.

    Called on the way out, which is exactly when the far end of a pipe may
    already have gone: a closed stream or a None one is not worth a traceback
    in a process that is ending anyway.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and not stream.closed:
                stream.flush()
        except Exception:
            pass


def watch_stdin(on_eof, *, stream=None, name="plexora-stdin-watch"):
    """Call `on_eof()` once stdin reaches end-of-file. Returns the thread.

    The parent holding the other end of the pipe is the lifetime: when it
    closes it deliberately, or dies and the OS closes it, `readline()` returns
    the empty string and this fires. Lines that do arrive are ignored -- the
    channel is a lifeline, not a command stream. Returns None when there is no
    stdin to watch.
    """
    stream = sys.stdin if stream is None else stream
    if stream is None:
        return None

    def watch():
        try:
            while stream.readline():
                pass
        except Exception:
            pass
        on_eof()

    thread = threading.Thread(target=watch, name=name, daemon=True)
    thread.start()
    return thread


def request_shutdown(reason="", *, grace=DEFAULT_GRACE,
                     interrupt=_thread.interrupt_main, exit_fn=os._exit,
                     log=None):
    """Stop this process the way Ctrl+C would, from any thread. Idempotent.

    Raises KeyboardInterrupt in the main thread, which Waitress's `run()`
    treats as its clean stop: the accept loop is on a select with a one-second
    timeout, so the interrupt lands within a second, `run()` returns, the
    caller's `finally` blocks run and so does `atexit`. A daemon timer
    hard-exits after `grace` seconds in case something in that teardown hangs,
    so Quit always means quit.

    Returns True for the call that started the shutdown and False for every
    later one.
    """
    global _shutdown_requested
    with _shutdown_lock:
        if _shutdown_requested:
            return False
        _shutdown_requested = True

    if log is not None and reason:
        try:
            log(f"Stopping: {reason}.")
        except Exception:
            pass

    def backstop():
        flush_std()
        exit_fn(0)

    timer = threading.Timer(grace, backstop)
    timer.daemon = True
    timer.start()
    interrupt()
    return True


def shutdown_requested():
    return _shutdown_requested


def _reset_for_tests():
    global _shutdown_requested
    with _shutdown_lock:
        _shutdown_requested = False


def install_signal_handlers(signals=None):
    """Make the polite stop signals behave like Ctrl+C.

    SIGTERM is what a process manager, `kill`, or the desktop shell's second
    attempt sends; Python's default for it is to die without running `atexit`.
    SIGHUP (POSIX) is a closed terminal and SIGBREAK (Windows) is Ctrl+Break or
    a console closing. All of them become the KeyboardInterrupt that Waitress
    already stops cleanly on. Only callable from the main thread, which is the
    only place the launchers call it; elsewhere it quietly does nothing.
    """
    if threading.current_thread() is not threading.main_thread():
        return []
    if signals is None:
        signals = [signal.SIGTERM]
        for name in ("SIGHUP", "SIGBREAK"):
            if hasattr(signal, name):
                signals.append(getattr(signal, name))

    def handler(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")

    installed = []
    for signum in signals:
        try:
            signal.signal(signum, handler)
            installed.append(signum)
        except (OSError, ValueError):
            pass
    return installed


def exit_when_stdin_closes(log=print):
    """End this process when whoever launched it closes the channel.

    The lifetime tie for a data node with no terminal. Normally `ssh -t` gives
    the far side a pty, and a connection dropping -- a closed lid, a lost
    network, Ctrl+C -- lands as a SIGHUP that ends the node with it. A Windows
    node cannot be given one: asking Windows sshd for a pty gets a ConPTY,
    which wraps the startup line the node prints and breaks the registration it
    exists to carry. So the channel is watched directly instead: when ssh goes,
    stdin reaches EOF, and this ends the process the same way the signal would
    have.

    `os._exit` rather than `request_shutdown`: a node holds no database and
    writes its manifest as it goes, so there is nothing to unwind for, and the
    far end is gone -- nobody is waiting to see a clean exit.
    """
    def on_eof():
        try:
            log("The connection that started this node closed; stopping.")
        except Exception:
            pass
        flush_std()
        os._exit(0)

    return watch_stdin(on_eof)
