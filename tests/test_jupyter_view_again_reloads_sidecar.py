"""Opening a second viewer on new in-memory data shows the new data.

`plexora.view(image=..., table=...)` called again in the same kernel -- the
notebook loop of annotate, call again, look -- re-registers the snapshots and
then reuses the sidecar the first call started. `plexora.view`'s docstring says
calling again with the same data refreshes that project, and
`register_memory_datasource`'s Note offers a new `plexora.view(...)` as a way to
see a change. The sidecar keys "which project is loaded" on the name, so unless
the second viewer asks it to reload, the new page's boot (`/init_database`)
finds the project already loaded and keeps serving the first round's table.

The sidecar here is `plexora.app` in this process, as in
tests/test_memory_datasource.py: an already-running sidecar is put where
`_start_server` finds one, and any request the viewer sends to its port is
answered by the Flask test client.
"""

import io
import urllib.request

import numpy as np
import pandas as pd
import pytest

import plexora
from plexora import jupyter
from plexora.notebook_env import resolve_display
from plexora.server.models import data_model

QUIET = {"log": lambda *args, **kwargs: None}
FAKE_PORT = 1  # Nothing listens here; requests to it go to the test client.
NAME = "viewagain_cells"


class _RunningSidecar:
    """A sidecar `_start_server` reuses: alive, on FAKE_PORT, loopback."""

    _plexora_port = FAKE_PORT
    _plexora_base_url = ""
    _plexora_token = None
    pid = 0

    def poll(self):
        return None


class _Response(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def sidecar(monkeypatch, plexora_data_root):
    """The in-process app, standing in for a sidecar that is already running."""
    client = plexora.app.test_client()
    resolved = resolve_display(False, None)
    key = jupyter._server_key(plexora_data_root.resolve(), resolved.server_base,
                              None, resolved.bind_host)
    monkeypatch.setitem(jupyter._SERVERS, key, _RunningSidecar())

    real_urlopen = urllib.request.urlopen
    prefix = f"http://127.0.0.1:{FAKE_PORT}"

    def urlopen(request, *args, **kwargs):
        url = request.full_url if isinstance(request, urllib.request.Request) else request
        if not str(url).startswith(prefix):
            return real_urlopen(request, *args, **kwargs)
        method = request.get_method() if isinstance(request, urllib.request.Request) else "GET"
        answer = client.open(str(url)[len(prefix):], method=method,
                             data=getattr(request, "data", None) or b"")
        return _Response(answer.status_code, answer.data)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return client


def _shown(client):
    """What a new viewer page's boot leaves the sidecar serving."""
    assert client.get(f"/init_database?datasource={NAME}").status_code == 200
    rows = data_model.get_datasource_df().height
    cd3 = data_model.get_filter_columns(NAME, ["CD3"])["CD3"].tolist()
    return rows, cd3


def test_calling_view_again_with_new_data_shows_the_new_data_in_the_reused_sidecar(
    sidecar, plexora_data_root
):
    image = np.zeros((2, 256, 256), dtype=np.uint8)
    first = pd.DataFrame({"X": [10.0, 60.0], "Y": [10.0, 60.0], "CD3": [1.0, 1.0]})
    second = pd.DataFrame({"X": [10.0, 60.0, 90.0], "Y": [10.0, 60.0, 90.0],
                           "CD3": [9.0, 9.0, 9.0]})

    viewer = plexora.view(NAME, image=image, table=first, x="X", y="Y",
                          proxy=False, data_dir=plexora_data_root, **QUIET)
    assert viewer._port == FAKE_PORT  # the running sidecar was reused
    assert _shown(sidecar) == (2, [1.0, 1.0])

    # Annotate, call again, look -- without viewer.refresh().
    again = plexora.view(NAME, table=second, x="X", y="Y",
                         proxy=False, data_dir=plexora_data_root, **QUIET)
    assert again._port == FAKE_PORT

    assert _shown(sidecar) == (3, [9.0, 9.0, 9.0])
