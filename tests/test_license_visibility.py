"""Not granted means not shown.

A Pro plugin the licence does not unlock is installed -- its routes refuse
with the `license` block, its `/panel` says `locked` -- but no page shows it:
no Tools-menu row, no launcher, no nav entry, no layer section, no scripts, and
no "Pro" badge anywhere. With the grant it is all there. Templates ask the
same question through `licensed()`, which never raises.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import plexora
from plexora.api.plugin import NavItem, Plugin
from plexora.server import plugins as plugin_registry
from plexora.server.routes import page_routes
from tests.agent_fixtures import make_synthetic_project

PRO = Plugin(name="proplug", label="Pro Plug", entitlement="plugin:proplug",
              scripts=("proplug.js",), launcher=True, icon="star",
              nav_items=(NavItem(menu="file", label="Pro Plug library", path="/library"),))


@pytest.fixture
def page(tmp_path, monkeypatch):
    """A viewer page for a synthetic 2-D project, with PRO installed beside
    whatever the app already installed."""
    make_synthetic_project(tmp_path)
    app = plexora.app
    monkeypatch.setitem(app.config, plugin_registry._CONFIG_KEY,
                        [*plugin_registry.installed(app), PRO])
    with app.test_client() as client:
        yield lambda url="/synth": client.get(url)


def test_unlocked_is_true_for_a_free_plugin_and_follows_the_grant(license_issuer):
    assert plugin_registry.unlocked(Plugin(name="freeplug", label="Free Plug"))
    assert not plugin_registry.unlocked(PRO)
    license_issuer.install(license_issuer.issue(ent=["plugin:proplug"]))
    assert plugin_registry.unlocked(PRO)


def test_unlocked_reads_a_licensing_failure_as_locked(monkeypatch):
    from plexora import licensing

    def broken(_entitlement):
        raise RuntimeError("damaged licence file")

    monkeypatch.setattr(licensing, "allows", broken)
    assert not plugin_registry.unlocked(PRO)
    assert plugin_registry.unlocked(Plugin(name="freeplug", label="Free Plug"))
    assert page_routes.licensed("ai") is False and page_routes.licensed(None) is True


def test_the_four_lookups_leave_out_a_locked_plugin(page, license_issuer):
    from plexora.server.models.project import Project

    app = plexora.app
    project = Project.load("synth")
    section = SimpleNamespace(name="prolayer", entitlement="plugin:proplug",
                              is_layer_section=True,
                              requires=SimpleNamespace(applies_to=lambda _p: True))

    def lookups():
        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(app.config, plugin_registry._CONFIG_KEY,
                       [*plugin_registry.installed(app), section])
            sections = [p.name for p in plugin_registry.layer_sections_for(app, project)]
        return ({p.name for p in plugin_registry.tools_for(app, project)},
                {p.name for p in plugin_registry.ready_tools(app, project)},
                sections,
                {item["label"] for item in plugin_registry.nav_items(app)})

    tools, ready, sections, nav = lookups()
    assert "proplug" not in tools and "proplug" not in ready
    assert "prolayer" not in sections and "Pro Plug library" not in nav
    # Still installed: routes, /panel and capability discovery need it.
    assert plugin_registry.find(app, "proplug") is PRO

    license_issuer.install(license_issuer.issue(ent=["plugin:proplug"]))
    tools, ready, sections, nav = lookups()
    assert "proplug" in tools and "proplug" in ready
    assert "prolayer" in sections and "Pro Plug library" in nav


def test_a_free_page_carries_no_trace_of_a_locked_plugin(page):
    html = page().get_data(as_text=True)
    assert 'data-tool="proplug"' not in html
    assert "Pro Plug" not in html
    assert "proplug.js" not in html
    assert "plx-pro-badge" not in html and "viewer-launcher-pro" not in html


def test_a_stale_tool_link_to_a_locked_plugin_renders_the_plain_viewer(page):
    html = page("/synth?tool=proplug").get_data(as_text=True)
    assert "proplug.js" not in html
    assert '"active_tool": ""' in html or "'active_tool': ''" in html or \
        '"active_tool":""' in html


def test_with_the_grant_the_page_offers_it(page, license_issuer):
    license_issuer.install(license_issuer.issue(ent=["plugin:proplug"]))
    html = page().get_data(as_text=True)
    assert 'data-tool="proplug"' in html
    assert "Pro Plug library" in html
    assert "plx-pro-badge" not in html
    assert "proplug.js" in page("/synth?tool=proplug").get_data(as_text=True)


def test_the_typed_url_is_still_refused(page):
    from plexora.server.routes import tool_routes

    reply = page("/synth/tools/proplug/panel")
    if reply.status_code == 403:
        assert reply.get_json()["locked"]["entitlement"] == "plugin:proplug"
    else:  # the synthetic project may not satisfy _resolve's requirements
        assert tool_routes._locked(PRO)
    assert page("/synth/tools/proplug").status_code == 302


def test_licensed_is_a_template_global(page, license_issuer):
    from flask import render_template_string

    with plexora.app.test_request_context("/"):
        assert render_template_string("{{ licensed('ai') }}") == "False"
        assert render_template_string("{{ licensed(None) }}") == "True"
    license_issuer.install(license_issuer.issue(ent=["ai"]))
    with plexora.app.test_request_context("/"):
        assert render_template_string("{{ licensed('ai:qc:session') }}") == "True"
        assert render_template_string("{{ licensed('volume') }}") == "False"
