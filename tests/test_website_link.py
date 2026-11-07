"""File > Open on the Website, in the desktop app (plexora/website_link.py):
the route and the menu row's rules, and that it is hidden until the website's
GA switch (its flags' `viewer: "on"`) says the website is open.
"""

import json

import pytest

import plexora
from plexora import links, paths, website_link


@pytest.fixture
def libraries(tmp_path, monkeypatch):
    """The user's own data root holding `here`, and the website's library
    (Documents/Plexora/projects) holding `on web`."""
    own = tmp_path / "own"
    library = tmp_path / "Documents" / "Plexora" / "projects"
    own.mkdir()
    library.mkdir(parents=True)
    (own / "config.json").write_text(json.dumps({"here": {}}), encoding="utf-8")
    (library / "config.json").write_text(json.dumps({"on web": {}}), encoding="utf-8")
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(own))
    monkeypatch.setenv("PLEXORA_LIBRARY_PATH", str(library))
    paths.reset()
    website_link.reset()
    yield
    website_link.reset()
    paths.reset()


@pytest.fixture
def desktop(monkeypatch):
    monkeypatch.setitem(plexora.app.config, "PLEXORA_DESKTOP", True)


def _flags(value):
    def fetch():
        if isinstance(value, Exception):
            raise value
        return value
    return fetch


def test_hidden_by_default_until_the_website_says_it_is_open(libraries, desktop):
    # Unreachable flags (offline, today's preview flags, a paused site): hidden.
    for flags in (OSError("offline"), {}, {"viewer": "preview"}, {"viewer": "desktop-only"},
                  ["not", "an", "object"], {"viewer": "ON"}):
        website_link.reset()
        assert website_link.describe(plexora.app, "on web", fetch=_flags(flags))["shown"] is False, flags
    website_link.reset()
    shown = website_link.describe(plexora.app, "on web", fetch=_flags({"viewer": "on"}))
    assert shown["shown"] is True


def test_the_real_read_is_of_the_websites_own_flags_and_fails_closed(libraries, desktop, monkeypatch):
    asked = []

    def urlopen(request, timeout):
        asked.append((request.full_url, timeout))
        raise OSError("no network in tests")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    assert website_link.website_open() is False
    assert asked == [(links.WEBSITE_FLAGS_URL, website_link.TIMEOUT_SECONDS)]
    assert links.WEBSITE_FLAGS_URL == "https://assets.plexoraapp.com/flags.json"


def test_only_the_desktop_apps_server_offers_it(libraries, monkeypatch):
    monkeypatch.setitem(plexora.app.config, "PLEXORA_DESKTOP", False)
    assert website_link.describe(plexora.app, "on web", fetch=_flags({"viewer": "on"}))["shown"] is False


def test_a_library_project_opens_at_its_address_and_any_other_says_where_it_must_be(libraries, desktop):
    on = _flags({"viewer": "on"})
    opened = website_link.describe(plexora.app, "on web", fetch=on)
    assert opened == {"shown": True, "in_library": True, "url": "https://viewer.plexoraapp.com/on%20web",
                      "library": "Documents › Plexora", "message": None}
    elsewhere = website_link.describe(plexora.app, "here", fetch=on)
    assert elsewhere["shown"] is True and elsewhere["url"] is None and not elsewhere["in_library"]
    assert elsewhere["message"] == "To open it on the website, keep the project in Documents › Plexora."
    assert website_link.describe(plexora.app, None, fetch=on)["shown"] is False


def test_the_data_root_being_the_library_counts(tmp_path, monkeypatch, desktop):
    library = tmp_path / "Documents" / "Plexora" / "projects"
    library.mkdir(parents=True)
    (library / "config.json").write_text(json.dumps({"mine": {}}), encoding="utf-8")
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(library))
    monkeypatch.setenv("PLEXORA_LIBRARY_PATH", str(library))
    paths.reset()
    try:
        assert website_link.in_library("mine")
    finally:
        paths.reset()


def test_the_flags_are_read_once_per_cache_window(libraries, desktop, monkeypatch):
    reads = []

    def fetch():
        reads.append(1)
        return {"viewer": "on"}

    for _ in range(3):
        assert website_link.website_open(fetch)
    assert len(reads) == 1
    clock = [1000.0]
    monkeypatch.setattr(website_link.time, "monotonic", lambda: clock[0])
    website_link.reset()
    website_link.website_open(fetch)
    clock[0] += website_link.CACHE_SECONDS + 1
    website_link.website_open(fetch)
    assert len(reads) == 3


def test_the_route_answers_the_menu_row(libraries, desktop, monkeypatch):
    client = plexora.app.test_client()
    monkeypatch.setattr(website_link, "_fetch_flags", lambda: {"viewer": "preview"})
    assert client.get("/desktop/website?datasource=on%20web").get_json()["shown"] is False
    website_link.reset()
    monkeypatch.setattr(website_link, "_fetch_flags", lambda: {"viewer": "on"})
    answer = client.get("/desktop/website?datasource=on%20web").get_json()
    assert answer["shown"] and answer["url"] == "https://viewer.plexoraapp.com/on%20web"


def test_the_row_is_rendered_hidden_and_only_for_the_desktop_app(libraries, monkeypatch):
    from flask import render_template

    from plexora.server.routes.page_routes import template_data

    with plexora.app.test_request_context("/"):
        data = template_data()
        data.update(datasource="on web", desktop=True)
        page = render_template("base.html", data=data)
        row = page[page.index('id="nav_open_website"'):]
        assert 'style="display: none"' in row[:200]
        data.update(desktop=False)
        assert 'id="nav_open_website"' not in render_template("base.html", data=data)
