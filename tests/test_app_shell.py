"""Every page can be served as a fragment, and a fragment is the same page.

appRouter.js keeps the live viewer alive across internal navigation by fetching
the destination's CONTENT and rendering it beside the viewer instead of letting
the browser replace the document. That only works if the server can hand back
the content on its own -- which is what `X-Plexora-Fragment: 1` and
_fragment.html are for.

Two properties matter, and both are easy to break by accident:

  - **A fragment is the page's own content, not a second rendering of it.** The
    same template, the same route, the same `data`. If the two ever diverge, the
    user sees one thing when they navigate and another when they route, and only
    one of those paths gets tested by hand.
  - **A request without the header is untouched.** A bookmark, a hard reload, a
    browser with JavaScript off, `curl`, and every other test in this suite all
    take that path, and it has to stay the whole document.

The layout is chosen by a context processor rather than by an argument, so no
route knows any of this exists -- which is also why this test walks the real
routes rather than calling a helper.
"""

import json
import re

import pytest

import plexora
from tests.helpers import ALL_CONFIRMED, csv_spec, project

FRAGMENT = {"X-Plexora-Fragment": "1"}

#: Every page a user can reach from a live viewer without changing project.
#: `/demo` is in here deliberately: the viewer's own URL has to render as a
#: fragment too, because a redirect can land on it (see showPage).
PAGES = (
    "/",
    "/upload_page",
    "/open_project",
    "/settings",
    "/demo",
    "/edit_config/demo",
    "/plugins/figure_builder/figures",
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    csv_path = tmp_path / "cells.csv"
    csv_path.write_text("CellID,X_centroid,Y_centroid,CD3\n1,0,0,5\n", encoding="utf-8")
    record = project(
        "demo",
        dataset=csv_spec(csv_path, markers=["CD3"],
                         metadata=["CellID", "X_centroid", "Y_centroid"]),
        confirmed=ALL_CONFIRMED,
    )
    (tmp_path / "config.json").write_text(
        json.dumps({"demo": record.to_entry()}), encoding="utf-8")
    return plexora.app.test_client()


def _body(html):
    """What the document actually shows, with the shell stripped off."""
    match = re.search(r"<body[^>]*>(.*)</body>", html, re.S)
    assert match, "the full page has no <body>"
    return match.group(1)


#: A <link> or a plugin <script src> at the very start or end of a fragment.
#: Both are assets base.html renders into <head> on a full page, so both have to
#: be lifted out before the middle can be compared against <body>.
_LEADING_LINK = re.compile(r"\s*(<link\b[^>]*>)")
_TRAILING_SCRIPT = re.compile(r"(<script\b[^>]*\bsrc=[^>]*>\s*</script>)\s*$")


def _split(fragment):
    """A fragment's head assets and its body content, as the router sorts them.

    Three things arrive in one string: the plugin's own stylesheets and
    `{% block style %}` at the front, `{% block content %}` in the middle, and
    the plugin's scripts at the back. Only the middle belongs in <body>; the
    router lifts the rest into <head>, which is where the full page renders
    them.
    """
    body = fragment
    assets = []
    while True:
        match = _LEADING_LINK.match(body)
        if not match:
            break
        assets.append(match.group(1))
        body = body[match.end():]
    while True:
        match = _TRAILING_SCRIPT.search(body)
        if not match:
            break
        assets.append(match.group(1))
        body = body[:match.start()]
    return assets, body.strip()


@pytest.mark.parametrize("path", PAGES)
def test_a_page_without_the_header_is_still_the_whole_document(client, path):
    response = client.get(path)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "<body" in html
    assert "<nav" in html
    # The navbar, the asset tags and the shell all come from base.html; a
    # template that lost its `{% extends layout %}` default would drop the lot
    # and still return 200.
    assert "plexora_page_host" in html


@pytest.mark.parametrize("path", PAGES)
def test_a_fragment_is_content_without_the_shell(client, path):
    response = client.get(path, headers=FRAGMENT)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    # Nothing the shell already has. The router inserts this into a document
    # that is holding a live viewer; a second <body> or a second navbar would
    # be inserted right next to the first.
    assert "<body" not in html
    assert "<nav id=\"topBar\"" not in html
    assert "plexora_page_host" not in html


@pytest.mark.parametrize("path", PAGES)
def test_a_fragment_carries_the_same_content_as_the_page(client, path):
    """The one property that makes routing safe rather than merely fast.

    Compared as a containment rather than an equality because the full document
    wraps the content in a body that also holds the navbar and the page host --
    but every element of the fragment has to be in there, in order, or the two
    renderings have diverged.

    The pieces are checked against the two places they land: assets into <head>,
    content into <body>. Checking the fragment as one string against <body>
    would fail on every page that has a stylesheet, for a reason that is the
    router working correctly.
    """
    assets, content = _split(client.get(path, headers=FRAGMENT).get_data(as_text=True))
    full = client.get(path).get_data(as_text=True)
    assert content, f"{path} rendered an empty fragment"
    assert content in _body(full), f"{path} renders different content as a fragment"
    for asset in assets:
        assert asset in full, f"{path} declares an asset the full page does not"


def test_a_fragment_brings_its_own_stylesheet(client):
    """`{% block style %}` is outside `{% block content %}`, so a fragment that
    rendered only the content would arrive unstyled -- and the router has
    nothing else to learn the page's stylesheet from."""
    html = client.get("/open_project", headers=FRAGMENT).get_data(as_text=True)
    assert "openProject.css" in html


def test_a_fragment_brings_its_own_scripts(client):
    """A page's controller is loaded at the bottom of its content block. The
    router re-creates these tags, because markup inserted as innerHTML never
    runs -- but only if they are in the fragment to begin with."""
    html = client.get("/open_project", headers=FRAGMENT).get_data(as_text=True)
    assert "openProjectPage.js" in html


def test_a_plugin_page_brings_the_plugin_with_it(client):
    """The failure this guards against is the quiet kind.

    Figure Builder's library and canvas are whole pages, and their controllers
    are in the PLUGIN's script list -- which base.html renders into <head>, so a
    fragment that emitted only the two blocks would drop them. The page then
    arrives as its static markup and looks completely correct: heading, tabs,
    search box, all in the template. Nothing ever loads and no button works.
    """
    path = "/plugins/figure_builder/figures"
    full = client.get(path).get_data(as_text=True)
    fragment = client.get(path, headers=FRAGMENT).get_data(as_text=True)
    for asset in ("figureLibrary.js", "figureBuilderApi.js", "figure_builder.css"):
        assert asset in full, f"{asset} is not on the full page either"
        assert asset in fragment, f"the fragment dropped {asset}"


def test_a_core_page_brings_no_plugin_assets(client):
    """The other half: the plugin asset lists are empty on a core page, so this
    costs a core-only build nothing and names no plugin.

    Asset tags only. The Open Project page legitimately carries a plugin's nav
    entry -- a Figures tab, pointing into figure_builder's namespace -- and that
    is the plugin contract working, not an asset leaking in.
    """
    fragment = client.get("/open_project", headers=FRAGMENT).get_data(as_text=True)
    assets, _ = _split(fragment)
    for asset in assets:
        assert "/plugins/" not in asset, f"a core page pulled in {asset}"


def test_the_viewer_page_declares_its_datasource_on_the_body(client):
    """How appRouter.js decides whether this document has a live viewer worth
    preserving, and which project it is showing."""
    html = client.get("/demo").get_data(as_text=True)
    assert 'data-plexora-datasource="demo"' in html


@pytest.mark.parametrize("path", ("/", "/open_project", "/settings"))
def test_a_page_with_no_viewer_says_so(client, path):
    """The router's own off switch. Empty here means "nothing to preserve", and
    every link on the page keeps its ordinary browser behaviour."""
    html = client.get(path).get_data(as_text=True)
    assert 'data-plexora-datasource=""' in html


def test_the_shell_loads_the_router_and_the_page_registry(client):
    """Both are core globals every page controller and both figure-builder pages
    call at their top level, so they have to be on every page -- including the
    ones that will never route."""
    html = client.get("/open_project").get_data(as_text=True)
    assert "services/pageBoot.js" in html
    assert "services/appRouter.js" in html
    # pageBoot must not be deferred: the controllers below call
    # PlexoraPage.register while they are being parsed.
    assert re.search(r'pageBoot\.js[^"]*"[^>]*type="text/javascript"', html)
