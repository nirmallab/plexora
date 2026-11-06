"""`/app/` on a data node: the viewer's static build, served beside the data.

Public code, served without the token -- and nothing else about the node
changes: `/node/v1/*` is exactly as guarded as before, `/` says as little as
before, and a node not started with `--app` has no `/app/` at all.
"""

from __future__ import annotations

import pytest

from plexora.server.node import app as node_app
from plexora.server.node import webapp
from plexora.server.providers.http import TOKEN_HEADER

TOKEN = "app-test-token"


@pytest.fixture
def build(tmp_path):
    """A small stand-in for `plexora/client/dist/static/`."""
    root = tmp_path / "static"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>Plexora</title>")
    (root / "assets" / "main.js").write_text("export const ok = 1;\n")
    (root / "assets" / "worker.mjs").write_text("self.onmessage = () => {};\n")
    (root / "assets" / "app.css").write_text("body { margin: 0 }\n")
    (root / "assets" / "core.wasm").write_bytes(b"\0asm\1\0\0\0")
    (root / "manifest.json").write_text("{}")
    (root / "icon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    (root / "docs").mkdir()
    (root / "docs" / "index.html").write_text("docs page")
    (root / ".secret").write_text("not for serving")
    (tmp_path / "outside.txt").write_text("outside the build")
    return root


def _client(**options):
    return node_app.create_node_app([], TOKEN, dynamic=True, **options).test_client()


def _features(client):
    return client.get("/node/v1/hello", headers={TOKEN_HEADER: TOKEN}).get_json()["features"]


def test_off_by_default(build):
    client = _client()
    for path in ("/app", "/app/", "/app/assets/main.js", "/app/connect"):
        assert client.get(path).status_code == 404, path
    assert "app" not in _features(client)


def test_asked_for_but_not_built_is_off_too(tmp_path):
    """A wheel without the build: no routes, and `/hello` does not claim one."""
    empty = tmp_path / "static"
    empty.mkdir()
    client = _client(app_dir=empty)
    assert client.get("/app/").status_code == 404
    assert "app" not in _features(client)


def test_the_build_is_served_without_the_token(build):
    client = _client(app_dir=build)
    assert "app" in _features(client)

    index = client.get("/app/")
    assert index.status_code == 200
    assert index.data.startswith(b"<!doctype html>")
    assert index.headers["Content-Type"] == "text/html; charset=utf-8"
    # Revalidated every time: it names the assets of one particular build.
    assert index.headers["Cache-Control"] == "no-cache"
    assert index.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize(("path", "content_type"), [
    ("/app/assets/main.js", "text/javascript; charset=utf-8"),
    ("/app/assets/worker.mjs", "text/javascript; charset=utf-8"),
    ("/app/assets/app.css", "text/css; charset=utf-8"),
    ("/app/assets/core.wasm", "application/wasm"),
    ("/app/manifest.json", "application/json"),
    ("/app/icon.svg", "image/svg+xml; charset=utf-8"),
])
def test_content_types_do_not_depend_on_the_machine(build, path, content_type):
    """Windows' registry often says `.js` is text/plain, and a module script
    served as that does not run; `.wasm` must be application/wasm to compile
    while it streams."""
    answer = _client(app_dir=build).get(path)
    assert answer.status_code == 200
    assert answer.headers["Content-Type"] == content_type


def test_a_page_route_falls_back_to_the_index(build):
    client = _client(app_dir=build)
    for path in ("/app/connect", "/app/tonsil/view", "/app/a/b/c"):
        answer = client.get(path)
        assert answer.status_code == 200, path
        assert answer.data.startswith(b"<!doctype html>"), path


def test_a_missing_asset_is_a_404_not_the_index(build):
    """HTML handed back for a script moves the error somewhere worse."""
    client = _client(app_dir=build)
    assert client.get("/app/assets/gone.js").status_code == 404
    assert client.get("/app/gone.wasm").status_code == 404


def test_nothing_outside_the_build_or_hidden_in_it_is_served(build):
    client = _client(app_dir=build)
    assert client.get("/app/.secret").status_code == 404
    assert client.get("/app/..%2Foutside.txt").status_code == 404
    assert client.get("/app/assets/..%2F..%2Foutside.txt").status_code == 404


def test_a_directory_is_its_index(build):
    client = _client(app_dir=build)
    answer = client.get("/app/docs/")
    assert answer.status_code == 200 and answer.data == b"docs page"
    moved = client.get("/app/docs")
    assert moved.status_code == 302
    assert moved.headers["Location"] == "docs/"


def test_the_data_routes_still_need_the_token(build):
    client = _client(app_dir=build)
    assert client.get("/node/v1/hello").status_code == 403
    assert client.get("/node/v1/health").status_code == 403
    assert client.get("/node/v1/hello", headers={TOKEN_HEADER: TOKEN}).status_code == 200
    # And the token in a query string under /app/ opens nothing more.
    assert client.get(f"/app/?t={TOKEN}").status_code == 200
    assert client.get("/node/v1/hello?t=wrong").status_code == 403


def test_the_root_still_says_nothing(build):
    answer = _client(app_dir=build).get("/").get_json()
    assert answer["plexora_node"] is True
    assert set(answer) == {"plexora_node", "api_version", "message"}


def test_app_without_a_slash_redirects_relatively_when_no_prefix_is_known(build):
    """Relative, so it resolves under any proxy prefix without knowing it."""
    moved = _client(app_dir=build).get("/app?x=1")
    assert moved.status_code == 302
    assert moved.headers["Location"] == "app/?x=1"
    assert moved.headers["Cache-Control"] == "no-store"


def test_a_configured_prefix_is_put_in_front(build):
    moved = _client(app_dir=build, url_prefix="/rnode/c42/8642/").get("/app")
    assert moved.headers["Location"] == "/rnode/c42/8642/app/"


def test_a_forwarded_prefix_is_used_when_none_was_configured(build):
    client = _client(app_dir=build)
    moved = client.get("/app", headers={"X-Forwarded-Prefix": "/rnode/c42/8642"})
    assert moved.headers["Location"] == "/rnode/c42/8642/app/"
    # One that could leave the site is ignored, not followed.
    for header in ("//evil.example", "/\\evil.example", "/a/../b", "https://evil.example"):
        moved = client.get("/app", headers={"X-Forwarded-Prefix": header})
        assert moved.headers["Location"] == "app/", header


@pytest.mark.parametrize("prefix", ["/a/../b", "/a\\b", "https://x.example/p", "/a b"])
def test_a_bad_url_prefix_stops_the_node(build, prefix):
    with pytest.raises(node_app.NodeStartupError):
        node_app.create_node_app([], TOKEN, dynamic=True, app_dir=build,
                                 url_prefix=prefix)


@pytest.mark.parametrize(("value", "cleaned"), [
    (None, ""), ("", ""), ("/", ""), ("rnode/c42/8642/", "/rnode/c42/8642"),
    ("/user/me/proxy/8642", "/user/me/proxy/8642"),
])
def test_prefixes_are_cleaned_like_the_viewer_s(value, cleaned):
    assert webapp.clean_prefix(value) == cleaned


def test_serve_node_app_uses_the_shipped_build(build, monkeypatch, tmp_path):
    """`--app` reads the build from inside the package; with none there the
    node says so and starts without it."""
    import waitress

    from plexora.server.models import data_model

    served = {}
    monkeypatch.setattr(data_model, "prime_hot_code", lambda log=None: None)
    monkeypatch.setattr(node_app, "warm_resources", lambda *a, **k: None)
    monkeypatch.setattr(waitress, "serve", lambda app, **kw: served.update(app=app))

    lines = []
    monkeypatch.setattr(webapp, "DEFAULT_DIR", build)
    node_app.serve_node([], token=TOKEN, port=8642, node_id="app-test",
                        dynamic=True, manifest=tmp_path / "m.json",
                        serve_app=True, log=lambda line="", **kw: lines.append(str(line)))
    client = served["app"].test_client()
    assert client.get("/app/").status_code == 200
    assert "app" in _features(client)
    assert any("/app/" in line for line in lines)

    lines.clear()
    monkeypatch.setattr(webapp, "DEFAULT_DIR", tmp_path / "not-built")
    node_app.serve_node([], token=TOKEN, port=8642, node_id="app-test",
                        dynamic=True, manifest=tmp_path / "m.json",
                        serve_app=True, log=lambda line="", **kw: lines.append(str(line)))
    client = served["app"].test_client()
    assert client.get("/app/").status_code == 404
    assert "app" not in _features(client)
    assert any("no web app build" in line for line in lines)
