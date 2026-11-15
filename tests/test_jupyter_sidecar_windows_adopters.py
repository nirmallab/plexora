"""Sidecar adopters on Windows (audit lead N12).

`jupyter._process_alive` answered True for every pid on Windows, so an adopter
was never pruned and a sidecar adopted once kept `_has_live_adopter` True for
ever: the kernel that spawned it never terminated it.
"""
import os
import types

from plexora import jupyter
from plexora.server.models import server_records


def _windows(monkeypatch):
    # Only the two modules' view of `os`: patching os.name itself breaks
    # pathlib for the rest of the process.
    fake = types.SimpleNamespace(name="nt", getpid=os.getpid)
    monkeypatch.setattr(server_records, "os", fake)
    monkeypatch.setattr(jupyter, "os", fake)


def test_a_dead_adopter_on_windows_is_not_live(monkeypatch):
    _windows(monkeypatch)
    monkeypatch.setattr(server_records, "_alive_windows", lambda pid: pid == 111)
    assert jupyter._process_alive(111) is True
    assert jupyter._process_alive(222) is False


def test_spawner_may_stop_a_sidecar_whose_adopter_died(monkeypatch):
    key = ("data", "base", None, "127.0.0.1")
    registry = {jupyter._registry_key(key): {"port": 1, "pid": os.getpid(),
                                             "adopters": [222]}}
    monkeypatch.setattr(jupyter, "_read_registry", lambda: registry)
    _windows(monkeypatch)
    monkeypatch.setattr(server_records, "_alive_windows", lambda pid: False)
    assert jupyter._has_live_adopter(key) is False
