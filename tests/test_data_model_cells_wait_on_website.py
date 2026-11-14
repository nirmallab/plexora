"""On the website, a request that reads cells before the queued table read has
run reads them rather than freezing Python.

A table of `BACKGROUND_TABLE_BYTES` or more is read behind the viewer
(`data_model._load_table_behind`). On the website that read is a queued task
(`_work.spawn`), which the worker runs only between requests
(plexora/browser/cooperative.py). A request that reads cells before then --
the gating sidebar's consistency report, a plugin route calling
`get_datasource_df` -- waits for the read in `_await_table`. There is one
thread, so the wait has to run the queued read itself (`_work.wait_event`
does exactly that); an untimed `Event.wait` can never be woken, and every
later request hangs behind it until the tab is reloaded.
"""

import threading

import pytest

from plexora import _work
from plexora.server.models import data_model
from tests.test_nearest_cell import _register, root  # noqa: F401 -- the fixture


@pytest.fixture
def website(monkeypatch):
    monkeypatch.setenv("PLEXORA_WORK", "cooperative")
    _work._reset_for_tests()
    yield
    _work._reset_for_tests()


def test_a_cells_request_before_the_queued_table_read_reads_the_cells(root, website, monkeypatch):
    monkeypatch.setattr(data_model, "BACKGROUND_TABLE_BYTES", 1)
    _register(root, "web_big", [5.0, 20.0, 40.0], [5.0, 20.0, 40.0])

    # The project's own load request: the table read is queued behind it.
    with _work.boundary():
        data_model.load_datasource("web_big")
    assert data_model.table_pending("web_big")
    assert _work.pending(background=False) >= 1
    event = data_model._table_pending[data_model.loaded_scope("web_big")]

    # The next request reads cells before the worker has had a quiet moment
    # to run the queued read. Run on a thread of its own only so that a hang
    # fails this test instead of freezing the run.
    answer = {}

    def request():
        try:
            with _work.boundary():
                answer["frame"] = data_model.get_datasource_df()
        except BaseException as exc:  # noqa: BLE001 -- reported below
            answer["error"] = exc

    thread = threading.Thread(target=request, daemon=True, name="audit-cells-request")
    thread.start()
    thread.join(15)
    hung = thread.is_alive()
    if hung:
        # Let the stuck wait go, so the thread does not outlive the test.
        event.set()
        thread.join(5)

    assert not hung, (
        "the cells request never returned: _await_table waited on an Event only "
        f"the queued table read sets, and that read is still queued "
        f"(pending={_work.pending(background=False)}, "
        f"table_pending={data_model.table_pending('web_big')})")
    assert "error" not in answer, answer.get("error")
    assert answer["frame"] is not None and len(answer["frame"]) == 3
