"""`_Watched` keeps reading when the far side writes a byte it cannot decode.

ssh and the local data node write whatever their side's encoding produces: a
Windows workstation's Python writes cp1252, an older cluster's rc file may be
Latin-1, and the node prints paths the user chose ("Données"). The reader
thread is the only thing draining that pipe and the only thing that sets the
events every wait in connect.py blocks on, so one bad byte must not end it:
the announce after it still has to be found, the lines before it still have to
be kept for the failure message, and a reader that does stop for any reason
has to wake its waiters instead of leaving them to a deadline hours away.
"""

import subprocess
import sys
import threading

from plexora import connect


# Bytes that are invalid in strict UTF-8 (0xE9 alone, 0x9D alone) AND in
# strict cp1252 (0x9D is undefined there), so the test means the same thing
# whichever locale encoding the pipe would otherwise be decoded with.
_CHILD = (
    "import sys, time\n"
    "out = sys.stdout.buffer\n"
    "out.write(b'serving C:\\\\Donn\\xe9es\\\\run1 \\x9d\\n')\n"
    "out.write(b'[plexora-node] host=127.0.0.1 port=41000 node_id=ab token=s3cr3t\\n')\n"
    "out.flush()\n"
    "time.sleep(60)\n"
)


def test_a_byte_the_pipe_cannot_decode_does_not_hide_the_node_announce():
    """A non-UTF-8 path printed before the announce: the announce is still
    found, the line before it is still kept, and the reader is still running
    so the pipe keeps being drained."""
    watched = connect._Watched(
        [sys.executable, "-c", _CHILD], "node", echo=lambda _text: None,
        matchers={"node": connect.parse_node_announce})
    try:
        assert watched.events["node"].wait(timeout=10), (
            "the node announce never set its event; reader alive="
            f"{watched._thread.is_alive()}, lines={watched.lines!r}")
        assert watched.found.get("node"), (
            f"the announce was not parsed; lines={watched.lines!r}")
        assert watched.found["node"]["token"] == "s3cr3t"
        assert any("Donn" in line for line in watched.lines), watched.lines
        # The child is still running and may write more: somebody has to be
        # reading, or it blocks on its next write once the pipe fills.
        assert watched.alive
        assert watched._thread.is_alive()
    finally:
        watched.stop()
        watched.drain(timeout=5)


class _BrokenStream:
    """A stdout that yields one line and then fails mid-read."""

    def __init__(self):
        self._lines = iter(["[plexora-remote] starting\n"])

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self._lines)
        except StopIteration:
            raise OSError("the pipe broke under the reader") from None


class _StillRunning:
    def __init__(self):
        self.stdout = _BrokenStream()
        self.stdin = None
        self.returncode = None

    def poll(self):
        return None


def test_a_reader_that_dies_mid_stream_still_wakes_every_waiter(monkeypatch):
    """Whatever ends the reader thread, every event is set on its way out, so
    `_wait_for_announce`, `_wait_for_node` and `_await_install` see it now and
    not at their deadline."""
    monkeypatch.setattr(connect, "_popen", lambda *a, **k: _StillRunning())
    # The thread's own exception is not what this test is about.
    monkeypatch.setattr(threading, "excepthook", lambda _args: None)

    watched = connect._Watched(
        ["ssh"], "ssh", echo=lambda _text: None,
        matchers={"announce": connect.parse_announce,
                  "node": connect.parse_node_announce,
                  "installed": connect.parse_install_done})
    watched.drain(timeout=5)

    assert watched.lines == ["[plexora-remote] starting"]
    unset = [name for name, event in watched.events.items() if not event.is_set()]
    assert unset == [], f"events left unset after the reader died: {unset}"
