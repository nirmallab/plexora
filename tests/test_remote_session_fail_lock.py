"""`RemoteSession._fail` diagnoses outside the session lock (audit lead N14).

`_diagnose` drains each watcher, which joins its reader thread -- and that
thread's next step is `_echo`, which takes the same lock. Held across the
drain, every drain ran out its timeout and the lines it waited for (the
"command not found" the diagnosis reads) were never added.
"""
import threading
import types

from plexora.server.models import remote_sessions
from tests.test_remote_connect import a_remote


def test_a_reader_still_echoing_is_drained_before_the_diagnosis():
    session = remote_sessions.RemoteSession(a_remote(), askpass_url="http://x/_askpass")
    seen = {}

    class Watcher:
        lines = []

        def drain(self, timeout=2):
            def pump():
                session._echo("  [ssh] bash: plexora: command not found")
                self.lines.append("bash: plexora: command not found")

            reader = threading.Thread(target=pump, daemon=True)
            reader.start()
            reader.join(timeout=timeout)
            seen["caught_up"] = not reader.is_alive()

    session.session = types.SimpleNamespace(watchers=[Watcher()])
    session._fail(RuntimeError("ssh exited"))

    assert seen["caught_up"]
    assert session.state == remote_sessions.STATE_FAILED
