"""The website's static files (plexora/client/static_build.py): what Cloudflare
Pages serves at viewer.plexoraapp.com.

Pins the parts a deploy depends on and nothing in a browser would show: the
site is cross-origin isolated on every path (SharedArrayBuffer), the service
worker may control the whole site, every path that is not a file is the
loader, `shell.json` names where the Python bundle is, a preview asks not to
be indexed, and no file is over Pages' 25 MiB.
"""

import json
import re
from fnmatch import fnmatchcase
from pathlib import Path

import pytest

from plexora import __version__
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
    assert "/client/home.html\n  ! Cache-Control\n  Cache-Control: no-cache" in (
        tmp_path / "_headers").read_text(encoding="utf-8")


def test_a_nodes_build_has_no_home_page(built):
    # webapp.package_build serves the app under a path a root page does not fit.
    out, record = built
    assert not (out / "client" / "home.html").exists() and record["home"] is None
    assert "home" not in json.loads((out / "client" / "shell.json").read_text(encoding="utf-8"))



# -- what a returning visitor's browser keeps (`KEPT`, `FRESH`) --------------------------

CLIENT_JS = Path(static_build.__file__).resolve().parent / "src" / "js"


def _rules(headers):
    rules = {}
    for block in headers.strip().split("\n\n"):
        path, *lines = block.split("\n")
        rules[path] = [line.strip() for line in lines]
    return rules


def _fresh(path):
    return any(fnmatchcase(path, pattern) for pattern in static_build.FRESH)


def test_the_sites_files_are_kept_and_checked_in_the_background(built):
    """A warm visit runs what the browser has and asks the CDN in the
    background, except where a file's address does not change with it."""
    out, _ = built
    rules = _rules((out / "_headers").read_text(encoding="utf-8"))
    for path in ("/client/*", "/plugins/*"):
        assert rules[path] == [f"Cache-Control: {static_build.KEPT}"]
    for path in static_build.FRESH:
        assert rules[path] == ["! Cache-Control", "Cache-Control: no-cache"], path
    assert rules["/index.html"] == ["Cache-Control: no-cache"]
    assert len(rules) <= 100  # Pages' limit


def test_the_loader_carries_shell_json(built):
    """services/shell.js reads it from the page: one round trip less."""
    out, _ = built
    loader = (out / "index.html").read_text(encoding="utf-8")
    block = loader.split('<script type="application/json" id="plexora-shell">', 1)[1].split("</script>", 1)[0]
    assert json.loads(block) == json.loads((out / "client" / "shell.json").read_text(encoding="utf-8"))
    assert loader.index('id="plexora-shell"') < loader.index("services/shell.js")


def test_the_loaders_own_files_carry_the_sites_version(built):
    out, _ = built
    stamp = f"?v={__version__}"
    for page in ("index.html", "selfcheck.html"):
        text = (out / page).read_text(encoding="utf-8")
        files = re.findall(r'(?:src|href)="(client/src/[^"]+\.(?:js|css))', text)
        assert files, page
        for found in re.findall(r'(?:src|href)="(client/src/[^"]+\.(?:js|css)[^"]*)"', text):
            assert found.endswith(stamp), (page, found)


def _module(source: Path, target: str) -> Path:
    path = (source.parent / target).resolve()
    return path if path.suffix else path.with_name(path.name + ".js")


def _site_path(source: Path, target: str) -> str:
    resolved = _module(source, target)
    return "/client/" + resolved.relative_to(CLIENT_JS.parents[1]).as_posix()


IMPORTS = re.compile(r"""^\s*(?:import|export)\b[^;'"]*?\bfrom\s+['"](\.{1,2}/[^'"]+)['"]""", re.M)
#: webpack's entries (client/webpack*.config.js): what they import is in a
#: bundle in dist/, never fetched at its own address.
WEBPACK_ENTRIES = ("vendor.js", "ssh/sshEntry.js", "workers/tiffWorker.js")


def _bundled() -> set:
    todo = [CLIENT_JS / entry for entry in WEBPACK_ENTRIES]
    seen = set()
    while todo:
        source = todo.pop().resolve()
        if source in seen or not source.is_file():
            continue
        seen.add(source)
        todo += [_module(source, target) for target in IMPORTS.findall(source.read_text(encoding="utf-8"))]
    return seen


def test_every_file_reached_at_an_address_without_a_version_is_fresh():
    """A worker, a script a worker imports, a module imported by name: their
    addresses carry no version, so a kept copy would outlive a release. A
    vendored library's folder names its version (`external/<name>-<version>/`)."""
    bundled = _bundled()
    assert len(bundled) > 10
    reached = set()
    for source in CLIENT_JS.rglob("*.js"):
        text = source.read_text(encoding="utf-8")
        if source.resolve() not in bundled:
            for target in IMPORTS.findall(text):
                reached.add((source.name, _site_path(source, target)))
        for target in re.findall(r'importScripts\("(\.{1,2}/[^"]+)"\)', text):
            reached.add((source.name, _site_path(source, target)))
        # Workers' addresses, however a file spells them.
        for found in re.findall(r'["`]/?(client/(?:src/js/workers|dist)/[^"`?]+\.m?js)["`]', text):
            reached.add((source.name, "/" + found))
    assert len(reached) > 10
    versioned = re.compile(r"^/client/external/[A-Za-z][\w.-]*-\d[\w.-]*/")
    missing = sorted(f"{where} -> {path}" for where, path in reached
                     if not _fresh(path) and not versioned.match(path))
    assert missing == []
