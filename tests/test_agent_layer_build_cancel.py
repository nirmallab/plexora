"""Cancelling surface_create or pin_projection while its layer builds ends the
job 'cancelled', not 'failed' with internal_error (audit ai-agent-core-7):
the wait loops raise the jobs runner's JobCancelled, not InterruptedError."""

from types import SimpleNamespace

import pytest

from plexora.agent.jobs import JobCancelled


class _Call:
    def __init__(self):
        self.session = SimpleNamespace(project=lambda name: SimpleNamespace(name=name))
        self.notify = None

    def cancelled(self):
        return True

    def progress(self, *args, **kwargs):
        pass


def _pending(monkeypatch):
    from plexora.server.models import layer_jobs

    monkeypatch.setattr(layer_jobs, "get", lambda *a, **k: {"status": "pending"})


def test_cancelling_surface_create_is_a_cancellation(monkeypatch):
    from plexora.agent.core import surface
    from plexora.server.surface import store

    _pending(monkeypatch)
    cancelled = []
    monkeypatch.setattr(store, "create", lambda *a, **k: {"layer": "s1", "created": True})
    monkeypatch.setattr(store, "cancel", lambda *a: cancelled.append(a))
    inp = SimpleNamespace(project="p", type="label_surfaces", channel=None, iso=None, t=0,
                          quality=None, min_size=None, label=None, smoothing=None,
                          sigma_um=0, background_um=0, box=None, planes=None, ids=None,
                          replace=False, color=None)

    with pytest.raises(JobCancelled):
        surface.surface_create(_Call(), inp)
    assert cancelled == [("p", "s1")]


def test_cancelling_pin_projection_is_a_cancellation(monkeypatch):
    from plexora.agent.core import volume
    from plexora.server.volume import pin

    _pending(monkeypatch)
    cancelled = []
    monkeypatch.setattr(pin, "pin", lambda *a, **k: {"layer": "p1", "created": True})
    monkeypatch.setattr(pin, "cancel", lambda *a: cancelled.append(a))
    inp = SimpleNamespace(project="p", view={}, replace=False)

    with pytest.raises(JobCancelled):
        volume.pin_projection(_Call(), inp)
    assert cancelled == [("p", "p1")]
