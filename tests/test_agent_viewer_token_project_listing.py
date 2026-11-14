"""Picking a viewer tab by project from the MCP process, on a server that
needs a token (the desktop app always has one).

The agent's link adds `?token=` to every request; a project filter must be
sent alongside it, not glued into the path, or the server sees no token and
refuses the listing.
"""

import threading
from urllib.parse import parse_qs, urlsplit

import pytest

import plexora
from plexora.agent import registry, viewer
from plexora.agent.attach import ServerLink
from plexora.server.models import viewer_sessions as vs
from tests.agent_fixtures import make_synthetic_project

TOKEN = "s3cret-token"


@pytest.fixture
def token_server(tmp_path, monkeypatch):
    from werkzeug.serving import make_server

    make_synthetic_project(tmp_path)
    registry.discover([])
    vs._reset_for_tests()
    monkeypatch.setitem(plexora.app.config, "PLEXORA_AUTH_TOKEN", TOKEN)
    server = make_server("127.0.0.1", 0, plexora.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield ServerLink(f"http://127.0.0.1:{server.server_port}/", TOKEN)
    server.shutdown()
    vs._reset_for_tests()


def test_listing_viewers_by_project_sends_the_token_as_its_own_parameter(monkeypatch):
    link = ServerLink("http://127.0.0.1:1/", TOKEN)
    seen = []

    def capture(method, path, body=None, *, timeout=None, **query):
        seen.append(link.url(path, **query))
        return 200, {"success": True, "sessions": []}

    monkeypatch.setattr(link, "request", capture)
    viewer.RemoteViewerControl(link).list_sessions("LSP 1")
    query = parse_qs(urlsplit(seen[0]).query)
    assert query.get("token") == [TOKEN], seen[0]
    assert query.get("project") == ["LSP 1"], seen[0]


def test_a_token_protected_server_lists_the_tab_open_on_a_project(token_server):
    view_id = vs.register(project="synth", client="browser")["view_id"]
    control = viewer.RemoteViewerControl(token_server)
    # Unscoped listing works: the token is the only query parameter.
    assert [s["view_id"] for s in control.list_sessions()] == [view_id]
    # Scoped by project, the same tab must be found, not refused.
    assert [s["view_id"] for s in control.list_sessions("synth")] == [view_id]
    assert viewer.resolve_view(control, None, "synth")["view_id"] == view_id
