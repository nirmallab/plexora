"""Plugin-level licensing: a Paid plugin, a mixed plugin, and the rule that
every plugin shipped today is untouched by any of it."""

import pytest
from flask import Blueprint, Flask, jsonify

import plexora
from plexora.api.plugin import Plugin
from plexora.server import plugins as plugin_registry


def _app_with(plugin, blueprint):
    app = Flask(__name__)
    plugin_registry._guard_licensed_routes(plugin, blueprint)
    app.register_blueprint(blueprint, url_prefix=plugin.url_prefix)
    return app.test_client()


def _blueprint(name):
    bp = Blueprint(name, __name__, static_folder=None)

    @bp.route("/analyze", methods=["POST"])
    def analyze():
        return jsonify(success=True, ran="analyze")

    @bp.route("/view")
    def view():
        return jsonify(success=True, ran="view")

    @bp.route("/static/<path:filename>", endpoint="static")
    def static_file(filename):
        return "/* js */", 200

    return bp


def test_every_shipped_plugin_is_free():
    for name in plugin_registry.available_names():
        plugin = plugin_registry.load(name)
        if plugin is not None:
            assert plugin.entitlement is None, name
            assert not plugin.endpoint_entitlements, name
            assert "entitlement" not in plugin.describe(), name


def test_a_malformed_plugin_entitlement_is_refused():
    with pytest.raises(ValueError):
        Plugin(name="bad", label="Bad", entitlement="Plugin Bad")
    with pytest.raises(ValueError):
        Plugin(name="bad", label="Bad", endpoint_entitlements={"x": "not valid"})


def test_a_paid_plugins_routes_answer_403_on_free_but_its_assets_load():
    plugin = Plugin(name="paidplug", label="Paid Plug", entitlement="plugin:paidplug")
    client = _app_with(plugin, _blueprint("paidplug"))
    refused = client.post("/plugins/paidplug/analyze")
    assert refused.status_code == 403
    body = refused.get_json()
    assert body["success"] is False
    assert body["license"]["code"] == "license_required"
    assert body["license"]["entitlement"] == "plugin:paidplug"
    assert body["license"]["plan_required"] == "paid"
    assert client.get("/plugins/paidplug/view").status_code == 403
    assert client.get("/plugins/paidplug/static/app.js").status_code == 200


def test_a_paid_plugins_routes_work_on_paid(license_issuer):
    license_issuer.install(license_issuer.issue(ent=["ai", "plugin:paidplug"]))
    plugin = Plugin(name="paidplug", label="Paid Plug", entitlement="plugin:paidplug")
    client = _app_with(plugin, _blueprint("paidplug"))
    assert client.post("/plugins/paidplug/analyze").get_json()["ran"] == "analyze"


def test_paid_alone_does_not_unlock_a_separately_sold_plugin(paid_license):
    plugin = Plugin(name="paidplug", label="Paid Plug", entitlement="plugin:paidplug")
    client = _app_with(plugin, _blueprint("paidplug"))
    assert client.post("/plugins/paidplug/analyze").status_code == 403


def test_a_mixed_plugin_guards_only_its_paid_endpoint():
    plugin = Plugin(name="mixed", label="Mixed", endpoint_entitlements={"analyze": "ai"})
    client = _app_with(plugin, _blueprint("mixed"))
    assert client.post("/plugins/mixed/analyze").status_code == 403
    assert client.get("/plugins/mixed/view").get_json()["ran"] == "view"


def test_a_mixed_plugin_on_paid(paid_license):
    plugin = Plugin(name="mixed", label="Mixed", endpoint_entitlements={"analyze": "ai"})
    client = _app_with(plugin, _blueprint("mixed"))
    assert client.post("/plugins/mixed/analyze").status_code == 200


def test_a_free_plugin_gets_no_guard_at_all():
    plugin = Plugin(name="freeplug", label="Free Plug")
    bp = _blueprint("freeplug")
    before = dict(bp.before_request_funcs)
    client = _app_with(plugin, bp)
    assert bp.before_request_funcs == before
    assert client.post("/plugins/freeplug/analyze").status_code == 200


def test_a_paid_plugin_is_described_with_its_entitlement():
    plugin = Plugin(name="paidplug", label="Paid Plug", entitlement="plugin:paidplug")
    assert plugin.describe()["entitlement"] == "plugin:paidplug"
    from plexora.server.routes.page_routes import _tool_entry

    assert _tool_entry(plugin)["locked"] is True
    assert "locked" not in _tool_entry(Plugin(name="freeplug", label="Free Plug"))


# -- the tool panel -------------------------------------------------------------------

@pytest.fixture
def client():
    # A plain test client, as the rest of the suite uses: no TESTING flag,
    # which would change error handling for every test after this one.
    with plexora.app.test_client() as test_client:
        yield test_client


def _fake_resolve(monkeypatch, plugin):
    from plexora.server.routes import tool_routes

    monkeypatch.setattr(tool_routes, "_resolve", lambda ds, tool: (tool_routes.OPEN, plugin, object()))


def test_a_locked_tool_panel_says_locked_and_sends_nothing_to_mount(client, monkeypatch):
    plugin = Plugin(name="paidplug", label="Paid Plug", entitlement="plugin:paidplug",
                    scripts=("paid.js",))
    _fake_resolve(monkeypatch, plugin)
    reply = client.get("/anything/tools/paidplug/panel")
    assert reply.status_code == 403
    body = reply.get_json()
    assert set(body) == {"locked"}
    assert body["locked"] == {"entitlement": "plugin:paidplug", "plan_required": "paid",
                              "tool": "paidplug", "label": "Paid Plug",
                              "summary": "A Paid Plexora feature.", "state": "free"}


def test_an_unlocked_tool_panel_is_served(client, monkeypatch, license_issuer):
    license_issuer.install(license_issuer.issue(ent=["plugin:paidplug"]))
    plugin = Plugin(name="paidplug", label="Paid Plug", entitlement="plugin:paidplug",
                    scripts=("paid.js",))
    _fake_resolve(monkeypatch, plugin)
    body = client.get("/anything/tools/paidplug/panel").get_json()
    assert "locked" not in body and body["scripts"]


def test_the_plain_link_to_a_locked_tool_goes_back_to_the_viewer(client, monkeypatch):
    plugin = Plugin(name="paidplug", label="Paid Plug", entitlement="plugin:paidplug")
    _fake_resolve(monkeypatch, plugin)
    reply = client.get("/demo/tools/paidplug")
    assert reply.status_code == 302
    assert reply.headers["Location"].endswith("/demo")
