"""Reconnecting while the last session's VM is still stopping, or being deleted.

Disconnect stops (or deletes) a rented VM with `--async`, so for a minute or
so after it Compute Engine reports the machine as STOPPING. Connecting again
in that minute must wait for the stop to settle and then start the machine
(or create a new one, if the stop was a delete) -- not probe ssh on a machine
that is shutting down for seven minutes and then blame the IAP role.

Driven through the same `_RUNNER` seam as tests/test_gcloud.py, with a fake
clock so the ssh deadline is reached without waiting for it.
"""

import json

import pytest

from plexora import gcloud


CFG = {
    "project": "my-project",
    "zone": "us-east1-b",
    "vm_name": "plexora-gcp",
    "machine_type": "e2-highmem-16",
    "bucket": "my-imaging-bucket",
    "mount_path": "~/plexora-data",
    "boot_disk_gb": 50,
}


class StoppingVmRunner:
    """A gcloud whose VM reports STOPPING, then settles on `settles_to`.

    `settles_to` is a status ("TERMINATED") or None for a VM that a delete
    removed. ssh answers only once the VM has been started or created by this
    end, which is what a machine in the middle of shutting down does.
    """

    def __init__(self, settles_to, stopping_reads=1):
        self.settles_to = settles_to
        self.stopping_reads = stopping_reads
        self.described = 0
        self.up = False
        self.calls = []

    def __call__(self, argv, timeout=None):
        self.calls.append(" ".join(argv))
        joined = " ".join(argv)
        if "auth list" in joined:
            return 0, json.dumps([{"account": "aj@example.com"}]), ""
        if "firewall-rules list" in joined:
            return 0, json.dumps([
                {"direction": "INGRESS", "sourceRanges": [gcloud.IAP_RANGE],
                 "allowed": [{"IPProtocol": "tcp", "ports": ["22"]}]}]), ""
        if "instances describe" in joined:
            self.described += 1
            if self.up:
                status = "RUNNING"
            elif self.described <= self.stopping_reads:
                status = "STOPPING"
            else:
                status = self.settles_to
            if status is None:
                return 1, "", "ERROR: The resource was not found"
            return 0, json.dumps({
                "status": status, "labels": {"plexora": "managed"},
                "tags": {"items": ["plexora"]},
                "networkInterfaces": [
                    {"accessConfigs": [{"natIP": "203.0.113.7"}]}]}), ""
        if "instances start" in joined or "instances create" in joined:
            self.up = True
            return 0, "", ""
        if "PLEXORA_STARTUP_DONE" in joined:
            return (0, "PLEXORA_STARTUP_DONE\n", "") if self.up \
                else (255, "", "connection refused")
        if "compute ssh" in joined:
            return (0, "PLEXORA_SSH_OK\n", "") if self.up \
                else (255, "", "connection refused")
        return 0, "", ""


@pytest.fixture
def fake_clock(monkeypatch):
    """Ten seconds pass on every read, so a 420 s deadline ends in ~42 probes."""
    now = {"t": 0.0}

    def tick():
        now["t"] += 10.0
        return now["t"]

    monkeypatch.setattr(gcloud, "_now", tick)
    monkeypatch.setattr(gcloud, "_sleep", lambda seconds: None)
    monkeypatch.setattr(gcloud, "_which", lambda name: "/usr/bin/" + name)
    return now


def test_a_vm_still_stopping_from_the_last_disconnect_is_started_again(
        monkeypatch, fake_clock):
    """Disconnect stopped the VM with --async; Connect a moment later sees
    STOPPING. The ladder must wait for the stop to land and start the VM,
    returning "started" -- not wait out SSH_READY_TIMEOUT against a machine
    that is shutting down and then raise the IAP-role sentence."""
    fake = StoppingVmRunner(settles_to="TERMINATED")
    monkeypatch.setattr(gcloud, "_RUNNER", fake)

    said = []
    action = gcloud.ensure_instance(CFG, echo=said.append)

    assert any("instances start plexora-gcp" in call for call in fake.calls)
    assert action == "started"


def test_a_vm_still_being_deleted_from_the_last_disconnect_is_created_again(
        monkeypatch, fake_clock):
    """With on_exit=delete the VM reads STOPPING while the delete runs and is
    then gone. The rented ladder must create a new one and return "created"."""
    fake = StoppingVmRunner(settles_to=None)
    monkeypatch.setattr(gcloud, "_RUNNER", fake)

    action = gcloud.ensure_instance(CFG, echo=lambda line: None)

    assert any("instances create plexora-gcp" in call for call in fake.calls)
    assert action == "created"
