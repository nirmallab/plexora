"""The app a data node serves, under the path a portal serves the node at.

`plexora node serve --app` serves the website's static build at `/app/`.
Behind Open OnDemand that is `/rnode/<host>/<port>/app/` on the portal's
address, and OOD strips `/rnode/<host>/<port>` before the request reaches the
node -- so the node cannot see the prefix, and the page must work it out:

- the loader names its files relative to `<base href>` (client/static_build.py),
  and the node writes the base for each address it answers -- `../` once per
  folder below `/app/`, or absolute when the prefix is known (`--url-prefix`,
  `X-Forwarded-Prefix`); the page reads its base path back from it
  (services/shell.js `BASE`);
- Python renders the app's pages with `base_url` = that path (browser/boot.py),
  so every link stays under it;
- every answer is cross-origin isolated, as the website is (plan D5), and the
  site's service worker may control the app's root.

tests/browser/ood.mjs drives the same in Chrome through a stand-in portal.
"""

from __future__ import annotations

import os
import re

import pytest

from plexora.client import static_build
from plexora.server.node import app as node_app
from plexora.server.node import webapp
from plexora.server.providers.http import TOKEN_HEADER

TOKEN = "prefix-test-token"
PREFIX = "/rnode/compute-3/8642"


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """The real static build, as `node serve --app` serves it."""
    out = tmp_path_factory.mktemp("static")
    static_build.build(out, py="https://assets.example.org/py/0.0.0/")
    return out


def _client(built, **options):
    return node_app.create_node_app([], TOKEN, dynamic=True, app_dir=built, **options).test_client()


def _base(response):
    found = re.search(rb'<base href="([^"]*)">', response.data)
    return found.group(1).decode() if found else None


def test_the_loader_names_its_files_from_the_base(built):
    loader = (built / "index.html").read_text(encoding="utf-8")
    assert loader.count(static_build.BASE_HREF) == 1
    # Every file the loader loads is relative: under any base, never "/client".
    assert not re.search(r'(?:src|href)="/(?!")', loader.replace(static_build.BASE_HREF, ""))
    assert 'src="client/src/js/services/shell.js?v=' in loader


@pytest.mark.parametrize("path, base", [
    ("/app/", "./"),
    ("/app/connect", "./"),
    ("/app/settings", "./"),
    ("/app/tonsil", "./"),
    ("/app/project/tonsil", "../"),
    ("/app/project/tonsil/edit", "../../"),
])
def test_behind_an_unknown_prefix_the_base_is_relative(built, path, base):
    """OOD's rnode strips its prefix: `../` per folder below /app/ resolves
    to the app's root under whatever the browser's address is."""
    response = _client(built).get(path)
    assert response.status_code == 200, path
    assert _base(response) == base
    assert response.headers["Cache-Control"] == "no-cache"


def test_a_known_prefix_makes_the_base_absolute(built):
    configured = _client(built, url_prefix=PREFIX)
    assert _base(configured.get("/app/project/tonsil")) == f"{PREFIX}/app/"
    forwarded = _client(built).get("/app/settings", headers={"X-Forwarded-Prefix": PREFIX})
    assert _base(forwarded) == f"{PREFIX}/app/"
    # A malformed forwarded prefix is ignored, never written into the page.
    odd = _client(built).get("/app/settings", headers={"X-Forwarded-Prefix": '/a/"><script>'})
    assert _base(odd) == "./"


def test_the_base_resolves_to_the_apps_root_under_the_portal(built):
    """What the browser makes of it: the address it shows, plus the base."""
    from urllib.parse import urljoin

    client = _client(built)
    for path in ("/app/", "/app/connect", "/app/project/tonsil/edit"):
        shown = f"https://ood.example.org{PREFIX}{path}"
        root = urljoin(shown, _base(client.get(path)))
        assert root == f"https://ood.example.org{PREFIX}/app/", path
        assert urljoin(root, "client/src/js/services/shell.js").endswith(
            f"{PREFIX}/app/client/src/js/services/shell.js")


def test_every_answer_is_cross_origin_isolated(built):
    client = _client(built)
    for path in ("/app/", "/app/client/src/js/services/shell.js", "/app/client/shell.json",
                 "/app/client/src/css/main.css", "/app/client/nothing-here.js"):
        response = client.get(path)
        assert response.headers["Cross-Origin-Opener-Policy"] == "same-origin", path
        assert response.headers["Cross-Origin-Embedder-Policy"] == "require-corp", path
        assert response.headers["Cross-Origin-Resource-Policy"] == "same-origin", path
        assert response.headers["X-Content-Type-Options"] == "nosniff", path


def test_the_service_worker_may_control_the_apps_root(built):
    response = _client(built).get("/app/" + webapp.RELAY)
    assert response.status_code == 200
    assert response.headers["Service-Worker-Allowed"] == "/"
    assert response.headers["Content-Type"].startswith("text/javascript")


def test_the_data_routes_still_need_the_token(built):
    client = _client(built, url_prefix=PREFIX)
    assert client.get("/node/v1/hello").status_code == 403
    assert client.get("/node/v1/hello", headers={TOKEN_HEADER: TOKEN}).status_code == 200


def test_the_website_build_is_at_the_root(built):
    """Pages serves the same loader at every path: its base is the site's root."""
    from urllib.parse import urljoin

    loader = (built / "index.html").read_text(encoding="utf-8")
    base = re.search(r'<base href="([^"]*)">', loader).group(1)
    assert urljoin("https://viewer.plexoraapp.com/project/tonsil/edit", base) == "https://viewer.plexoraapp.com/"


def test_the_website_python_renders_links_under_the_base(monkeypatch):
    """boot.environment hands the app its base_url: a page's links, scripts
    and styles are under the prefix."""
    from plexora.browser import boot

    base = f"{PREFIX}/app"
    saved = {name: os.environ.get(name) for name in
             ("PLEXORA_DATA_PATH", "PLEXORA_TELEMETRY", "PLEXORA_ALLOWED_ORIGINS", "PLEXORA_WORK",
              "PLEXORA_BACKEND", "PLEXORA_NO_NUMBA", "BIOCOGNIA_DIR", "BIOCOGNIA_NO_HEARTBEAT",
              "PLEXORA_BASE_URL")}
    try:
        boot.environment("https://ood.example.org", base_url=base, data_root=os.environ.get(
            "PLEXORA_DATA_PATH", "/data"))
        assert os.environ["PLEXORA_BASE_URL"] == base
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    import plexora

    monkeypatch.setitem(plexora.app.config, "PLEXORA_BASE_URL", base)
    html = plexora.app.test_client().get("/settings").get_data(as_text=True)
    assert f'src="{base}/client/src/js/services/handoff.js' in html
    assert f'href="{base}/settings"' in html
    assert not re.search(r'src="/client/', html)
