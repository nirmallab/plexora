"""Attaching a node resource to one project must not evict another.

`nodes.attach_table/attach_image/attach_segmentation` (and `detach`) end in
`_reload`, which used to call `load_datasource(name, reload=True)` whatever
was open. This process has ONE loaded project, so importing a dataset from a
node, or an agent attaching a mask to sample B, swapped the user's open sample
A out from under the viewer. Now only the project that is loaded (or a process
with nothing loaded at all -- the CLI, a script) is re-read.
"""

import pytest

from plexora import nodes
from plexora.server.models import data_model


@pytest.fixture
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(data_model, "load_datasource",
                        lambda name, reload=False, **_: seen.append((name, reload)))
    monkeypatch.setattr(nodes.Project, "load", classmethod(lambda cls, name: name))
    monkeypatch.setattr(data_model, "loaded_scope", lambda name: name)
    return seen


def test_another_open_project_is_left_loaded(calls, monkeypatch):
    monkeypatch.setattr(data_model, "_loaded_source", "user-sample-a")
    assert nodes._reload("sample-b") == "sample-b"
    assert calls == []


def test_the_open_project_is_reloaded(calls, monkeypatch):
    monkeypatch.setattr(data_model, "_loaded_source", "sample-b")
    nodes._reload("sample-b")
    assert calls == [("sample-b", True)]


def test_a_process_with_nothing_loaded_still_loads(calls, monkeypatch):
    monkeypatch.setattr(data_model, "_loaded_source", None)
    nodes._reload("sample-b")
    assert calls == [("sample-b", True)]
