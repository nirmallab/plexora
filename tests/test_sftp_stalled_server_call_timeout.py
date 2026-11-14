"""An SFTP server that stops answering while ssh stays up.

A scratch filesystem can stall `sftp-server` for minutes behind a live ssh
link. Every call on such a connection must still give up after the client's
call timeout ("did not answer within ... seconds") -- including the calls made
after earlier ones already timed out, whose requests never got a reply.
"""

import os
import threading

from plexora.server.utils import sftp


class _Sink:
    """A process stdin that takes everything and answers nothing."""

    def write(self, data):
        return len(data)

    def flush(self):
        pass

    def close(self):
        pass


class _StalledServer:
    """A far side that is up (poll() is None) but never replies."""

    def __init__(self):
        read_fd, self._write_fd = os.pipe()
        self.stdin = _Sink()
        self.stdout = os.fdopen(read_fd, "rb", buffering=0)

    def poll(self):
        return None

    def hang_up(self):
        if self._write_fd is not None:
            os.close(self._write_fd)
            self._write_fd = None


def test_calls_after_earlier_timeouts_still_time_out_instead_of_hanging():
    server = _StalledServer()
    client = sftp.SFTPClient(server, label="stalled", call_timeout=0.05)
    outcome = {}

    def one_more_call():
        try:
            client.stat("/n/scratch/x.tif")
        except BaseException as exc:  # noqa: BLE001 -- recorded for the assert
            outcome["error"] = exc

    try:
        # As many calls as the client lets be outstanding, each timing out
        # because the server never answers.
        timed_out = 0
        for _ in range(sftp._MAX_IN_FLIGHT):
            try:
                client.stat("/n/scratch/x.tif")
            except TimeoutError:
                timed_out += 1
        assert timed_out == sftp._MAX_IN_FLIGHT

        # The next call must fail the same way, not wait forever for a
        # request slot that a timed-out request still holds.
        caller = threading.Thread(target=one_more_call, daemon=True)
        caller.start()
        caller.join(timeout=5)
        assert not caller.is_alive(), (
            "a call on a stalled connection blocked past its call timeout "
            "(alive=%s)" % client.alive)
        assert isinstance(outcome.get("error"), (TimeoutError, ConnectionError))
    finally:
        # Ending the stream fails everything pending, which frees the slots
        # and any caller still blocked on one.
        server.hang_up()
        client.close()
        caller_thread = locals().get("caller")
        if caller_thread is not None:
            caller_thread.join(timeout=5)
        server.stdout.close()
