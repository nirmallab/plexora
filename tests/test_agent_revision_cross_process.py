"""revision.json's bumps hold between processes (audit lead flow-user-state-9).

The viewer server and `plexora mcp serve` both bump the counters a cached
read is keyed on. With a thread-only lock, a bump in another process that
read the file before this one wrote it rolls the counter back to a token an
earlier read is already cached under -- and that stale read is served again.
Here a real second process is paused between its read and its write.
"""

import os
import subprocess
import sys
import textwrap

from plexora.agent import revision

SECOND_PROCESS = textwrap.dedent("""
    import sys, time
    from plexora.agent import revision

    real = revision._read

    def paused(file):
        data = real(file)
        print(str(revision.path()), flush=True)  # read, nothing written yet
        time.sleep(1.5)
        return data

    revision._read = paused
    revision.bump("p")
    print("done", flush=True)
""")


def test_a_bump_racing_another_process_is_not_lost():
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    other = subprocess.Popen([sys.executable, "-c", SECOND_PROCESS], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        first = other.stdout.readline().strip()
        assert first == str(revision.path()), (first, other.stderr.read())
        revision.bump("p")                    # while the other holds what it read
        assert other.stdout.readline().strip() == "done"
    finally:
        other.wait(timeout=30)
    assert revision.token("p") == "g0.p2"
    assert revision.token() == "a2"
