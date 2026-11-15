"""Gating's read-modify-write locks hold between processes (audit flow-user-state-3).

`plexora mcp serve` is a second process running the same gating capabilities
against the same data root as the viewer: a gate, a provenance row or the
classifier state written by an agent is the same load-change-store the
browser's route does. Each lock below used to be a `threading` lock, which a
second process never sees, so the two could interleave and one write vanish.
A real second Python process takes the lock here; this process must wait.
"""

import os
import subprocess
import sys
import textwrap
import threading

import pytest

from plexora.plugins.gating.server import model, provenance
from plexora.plugins.gating.server.classifier import store

HOLDER = textwrap.dedent("""
    import sys, time
    from plexora.plugins.gating.server import model, provenance
    from plexora.plugins.gating.server.classifier import store

    kind, name = sys.argv[1], sys.argv[2]
    lock = {"gates": model._lock_for, "provenance": provenance._lock,
            "classifier": store.lock}[kind](name)
    with lock:
        print("held", flush=True)
        time.sleep(1.5)
""")

LOCKS = {"gates": model._lock_for, "provenance": provenance._lock, "classifier": store.lock}


@pytest.mark.parametrize("kind", sorted(LOCKS))
def test_a_lock_held_by_another_process_keeps_this_one_out(kind):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, kind, "race_project"], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    entered = threading.Event()

    def take():
        with LOCKS[kind]("race_project"):
            entered.set()

    try:
        assert holder.stdout.readline().strip() == "held", holder.stderr.read()
        thread = threading.Thread(target=take, daemon=True)
        thread.start()
        assert not entered.wait(0.7), "entered while another process held the lock"
    finally:
        _, err = holder.communicate(timeout=60)
    assert holder.returncode == 0, err
    assert entered.wait(10), "never entered once the other process let go"


@pytest.mark.parametrize("kind", sorted(LOCKS))
def test_the_lock_is_still_reentrant_within_a_process(kind):
    """`clear_gates` holds the gates lock and then provenance's; a capability
    holding provenance's may call `record` again. Neither may deadlock."""
    done = threading.Event()

    def nest():
        with LOCKS[kind]("race_project"):
            with LOCKS[kind]("race_project"):
                done.set()

    threading.Thread(target=nest, daemon=True).start()
    assert done.wait(10), "the lock deadlocked against itself"
