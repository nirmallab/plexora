"""A job that srun started and that then failed is not a job srun refused.

Slurm prints `srun: error: <node>: task 0: Exited with exit code N` whenever
the command it launched exits non-zero -- after the allocation, after the
command ran. That line shares the prefix srun uses when it turns a job down,
but it says the opposite: the scheduler did its part, and what failed is the
program on the compute node. A `plexora` missing from the job's PATH, or a
Plexora refusing to choose between two data directories, must still be named
as that, not reported as the scheduler refusing the job and sent off to edit
partition or walltime settings that were right.

The module is loaded straight off disk, the way tests/test_connect.py loads it.
"""

import importlib.util
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "plexora_connect_srun_task_exit_under_test",
        ROOT / "plexora" / "connect.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


connect_mod = _load()

SCHEDULER_SAID_NO = "The scheduler would not start the job"


class FakeProcess:
    """Enough of Popen for _Watched: output to drain, and a liveness answer."""

    def __init__(self, lines=(), dead_with=None):
        self.stdout = iter([line + "\n" for line in lines])
        self.stdin = None
        self.returncode = dead_with

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        if self.returncode is None:
            self.returncode = -15

    def kill(self):
        self.terminate()


@pytest.fixture
def rig(monkeypatch):
    calls = types.SimpleNamespace(spawned=[], echoed=[], queue=[])

    def popen(argv, **kwargs):
        calls.spawned.append(argv)
        return calls.queue.pop(0) if calls.queue else FakeProcess()

    def urlopen(url, timeout=None):
        raise OSError("refused")

    monkeypatch.setattr(connect_mod, "_popen", popen)
    monkeypatch.setattr(connect_mod, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(connect_mod, "_urlopen", urlopen)
    monkeypatch.setattr(connect_mod, "_open_browser", lambda url: None)
    monkeypatch.setattr(connect_mod, "_sleep", lambda seconds: None)
    monkeypatch.setattr(connect_mod, "_ACTIVE", [])
    monkeypatch.setattr(connect_mod, "pick_ports",
                        lambda local=None, remote=None, **kw: (local or 9999,
                                                               remote or 9999))
    calls.echo = calls.echoed.append
    return calls


#: What Slurm prints when the job was allocated and the program it was asked
#: to run is not on the compute node's PATH.
ALLOCATED_THEN_NOT_FOUND = [
    "srun: job 4242 queued and waiting for resources",
    "srun: job 4242 has been allocated resources",
    "slurmstepd: error: execve(): plexora: No such file or directory",
    "srun: error: compute-a-16: task 0: Exited with exit code 2",
]


def test_a_viewer_job_whose_plexora_is_not_on_the_path_is_told_about_the_path(rig):
    rig.queue = [FakeProcess(ALLOCATED_THEN_NOT_FOUND, dead_with=2)]

    session = connect_mod.Session("me@login", srun="-p short", echo=rig.echo,
                                  timeout=5, local_node=False)
    with pytest.raises(connect_mod.ConnectError) as caught:
        session.establish()

    message = str(caught.value)
    assert SCHEDULER_SAID_NO not in message
    assert "could not run" in message
    assert "--remote-command" in message


def test_a_viewer_job_that_refused_two_data_directories_says_so(rig):
    from plexora.paths import CONFLICT_MARKER

    rig.queue = [FakeProcess([
        "srun: job 4242 has been allocated resources",
        CONFLICT_MARKER,
        "  /n/scratch/users/a/ajn16 (the connection's suggestion, 3 projects)",
        "Plexora will not choose between them.",
        "srun: error: compute-a-16: task 0: Exited with exit code 2",
    ], dead_with=2)]

    session = connect_mod.Session("me@login", srun="-p short", echo=rig.echo,
                                  timeout=5, local_node=False)
    with pytest.raises(connect_mod.ConnectError) as caught:
        session.establish()

    message = str(caught.value)
    assert SCHEDULER_SAID_NO not in message
    assert CONFLICT_MARKER in message


def test_a_node_job_whose_plexora_is_not_on_the_path_is_told_about_the_path(rig):
    rig.queue = [FakeProcess([
        "srun: job 4242 has been allocated resources",
        "bash: plexora: command not found",
        "srun: error: compute-a-16: task 0: Exited with exit code 127",
    ], dead_with=127)]

    session = connect_mod.NodeSession("me@login", srun="-p gpu", echo=rig.echo,
                                      timeout=5)
    with pytest.raises(connect_mod.ConnectError) as caught:
        session.establish()

    message = str(caught.value)
    assert SCHEDULER_SAID_NO not in message
    assert "could not run" in message


def test_a_real_refusal_is_still_read_as_one():
    """The fix must not blind the refusal reading: a job never allocated is
    still the scheduler's answer."""
    message = connect_mod.scheduler_refusal([
        "srun: error: Unable to allocate resources: Invalid partition name "
        "specified",
    ])
    assert message and SCHEDULER_SAID_NO in message
