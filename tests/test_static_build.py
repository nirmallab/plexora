"""The website's static files (plexora/client/static_build.py): what Cloudflare
Pages serves at viewer.plexoraapp.com.

Pins the parts a deploy depends on and nothing in a browser would show: the
site is cross-origin isolated on every path (SharedArrayBuffer), the service
worker may control the whole site, every path that is not a file is the
loader, `shell.json` names where the Python bundle is, a preview asks not to
be indexed, and no file is over Pages' 25 MiB.
"""

import json

import pytest

from plexora.client import static_build

PAGES_FILE_LIMIT = 25 << 20


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("static")
    record = static_build.build(out, py="https://assets.example/py/dev/abc/", noindex=True,
                                models="https://assets.example/models/")
    return out, record


def test_the_loader_answers_every_path(built):
    out, _ = built
    assert (out / "_redirects").read_text(encoding="utf-8") == "/* /index.html 200\n"
    assert "services/shell.js" in (out / "index.html").read_text(encoding="utf-8")


def test_every_path_is_cross_origin_isolated(built):
    out, _ = built
    headers = (out / "_headers").read_text(encoding="utf-8")
    everywhere = headers.split("\n\n")[0]
    assert everywhere.startswith("/*\n")
    for line in ("Cross-Origin-Opener-Policy: same-origin", "Cross-Origin-Embedder-Policy: require-corp",
                 "X-Robots-Tag: noindex"):
        assert f"  {line}\n" in everywhere + "\n"
    assert "/client/src/js/workers/backendRelay.js\n  Service-Worker-Allowed: /" in headers


def test_shell_names_the_python_bundle(built):
    out, record = built
    shell = json.loads((out / "client" / "shell.json").read_text(encoding="utf-8"))
    # And the relay a connection to a server goes through (docs/internal/
    # WEB_REMOTE_RELAY.md).
    assert shell == {"version": record["version"], "py": "https://assets.example/py/dev/abc/",
                     "models": "https://assets.example/models/", "relay": static_build.RELAY_URL}


def test_a_release_build_may_be_indexed(tmp_path):
    static_build.build(tmp_path)
    assert "X-Robots-Tag" not in (tmp_path / "_headers").read_text(encoding="utf-8")
    shell = json.loads((tmp_path / "client" / "shell.json").read_text(encoding="utf-8"))
    assert shell["py"].startswith("/py/") and shell["models"] == "/models/"
    assert "flags" not in shell  # a development build reads no switches


def test_the_shell_names_the_sites_switches_when_told(tmp_path):
    record = static_build.build(tmp_path, flags="https://assets.example/flags.json")
    shell = json.loads((tmp_path / "client" / "shell.json").read_text(encoding="utf-8"))
    assert shell["flags"] == record["flags"] == "https://assets.example/flags.json"


def test_what_pages_takes(built):
    out, _ = built
    files = [path for path in out.rglob("*") if path.is_file()]
    assert not [path for path in files if path.suffix in (".map", ".ai")]
    assert max(path.stat().st_size for path in files) < PAGES_FILE_LIMIT
    assert len(files) < 20_000
    assert (out / "client" / "src" / "js" / "workers" / "pythonWorker.js").is_file()
    assert (out / "client" / "dist" / "tiffWorker_bundle.js").is_file()


def test_google_sign_in_comes_back_to_a_page_of_the_site(built, tmp_path):
    """Plan L9: the OAuth client's redirect URI is `<site>/google-auth.html`,
    a file (not the loader), sent with no referrer and never cached; a build
    names its client id (and a stand-in's endpoint for a test) in shell.json,
    and a build without one names nothing."""
    out, _ = built
    page = (out / "google-auth.html").read_text(encoding="utf-8")
    assert 'src="client/src/js/services/googleAuthReturn.js"' in page
    assert '<meta name="referrer" content="no-referrer">' in page
    headers = (out / "_headers").read_text(encoding="utf-8")
    assert "/google-auth.html\n  Cache-Control: no-store\n  Referrer-Policy: no-referrer" in headers
    assert "google" not in json.loads((out / "client" / "shell.json").read_text(encoding="utf-8"))
    google = static_build.google_config("123-abc.apps.googleusercontent.com",
                                        "http://127.0.0.1:9/o/oauth2/v2/auth")
    static_build.build(tmp_path, google=google)
    shell = json.loads((tmp_path / "client" / "shell.json").read_text(encoding="utf-8"))
    assert shell["google"] == {"client_id": "123-abc.apps.googleusercontent.com",
                               "auth": "http://127.0.0.1:9/o/oauth2/v2/auth"}
    assert static_build.google_config(None) == {}
    with pytest.raises(ValueError):
        static_build.google_config("not a client id")


def test_a_first_visits_home_page_is_drawn_as_the_websites_python_draws_it(tmp_path):
    # The loader shows it at once on a first visit while Python starts
    # (services/shell.js `builtHome`); Python's own answer replaces it if it
    # differs. Matched the preview's Python byte for byte on 2026-11-07.
    record = static_build.build(tmp_path, py="https://assets.example/py/dev/abc/", home=True)
    page = (tmp_path / "client" / "home.html").read_text(encoding="utf-8")
    assert record["home"] == "client/home.html"
    assert json.loads((tmp_path / "client" / "shell.json").read_text(encoding="utf-8"))["home"] == "client/home.html"
    assert 'id="quick_view_go"' in page
    variables = json.loads(page.split('id="plexora-variables">', 1)[1].split("</script>", 1)[0])
    # A first visit's: the website's backend, at the site's root, nothing kept,
    # no licence -- nothing of the computer that built it.
    assert variables["backend"] == "browser" and variables["base_url"] == ""
    assert variables["datasources"] == [] and variables["client_node"] == ""
    assert variables["license"]["plan"] == "free" and variables["license"]["paid"] is False
    assert "quick-view-recent" not in page
    assert "/client/home.html\n  Cache-Control: no-cache" in (tmp_path / "_headers").read_text(encoding="utf-8")


def test_a_nodes_build_has_no_home_page(built):
    # webapp.package_build serves the app under a path a root page does not fit.
    out, record = built
    assert not (out / "client" / "home.html").exists() and record["home"] is None
    assert "home" not in json.loads((out / "client" / "shell.json").read_text(encoding="utf-8"))
