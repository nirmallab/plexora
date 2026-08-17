# Backend readiness check for the navbar's Tools dropdown: a datasource can
# only open a tool once it has real feature data (not just a quick-view stub
# CSV). If it doesn't, send the user to the upload page to attach the
# missing piece, prefilled from what's already registered, then return them
# here once that's done -- see page_routes.py's upload_page() for the other
# half of that handoff.
from plexora import app, get_config
from plexora.server import plugins as plugin_registry
from plexora.server.routes.page_routes import template_data
from flask import redirect, jsonify, render_template


#: What opening a tool should do, given a datasource.
#:
#: OPEN     -- everything it needs is there.
#: ATTACH   -- installed and compatible, but an input is missing. Recoverable,
#:             so hand off to the upload page and come back. This is why the
#:             Tools menu lists compatible-but-not-ready plugins at all: hiding
#:             them hides the only route to making them work.
#: FALLBACK -- unknown datasource, uninstalled tool, or permanently
#:             incompatible. Stale and bookmarked links land here, so it must
#:             not error.
OPEN, ATTACH, FALLBACK = "open", "attach", "fallback"


def _resolve(datasource, tool_name):
    """(outcome, plugin) for opening `tool_name` on `datasource`.

    Whether a tool applies is the plugin's own declaration (Plugin.requires),
    not a rule core hardcodes -- core used to test `image_kind == 'rgb'`
    directly, which only ever encoded what gating in particular could not
    handle.
    """
    entry = get_config().get(datasource)
    plugin = plugin_registry.find(app, tool_name)
    if not entry or plugin is None or not plugin.requires.applies_to(entry):
        return FALLBACK, None
    if plugin.requires.missing_from(entry):
        return ATTACH, plugin
    return OPEN, plugin


@app.route('/<string:datasource>/tools/<string:tool_name>')
def open_tool(datasource, tool_name):
    base_url = app.config.get('PLEXORA_BASE_URL', '')
    outcome, _ = _resolve(datasource, tool_name)

    if outcome == ATTACH:
        return redirect(f"{base_url}/upload_page?attach_to={datasource}&return_tool={tool_name}")
    if outcome == FALLBACK:
        return redirect(f"{base_url}/{datasource}")
    return redirect(f"{base_url}/{datasource}?tool={tool_name}")


@app.route('/<string:datasource>/tools/<string:tool_name>/panel')
def tool_panel(datasource, tool_name):
    """Fetched by toolLoader.js the first time a tool is opened mid-session
    (plain viewer already loaded, no navigation) -- mirrors open_tool()'s
    checks above, but returns JSON (panel HTML fragments + script URLs) for
    client-side injection instead of a redirect, so the viewer/OpenSeadragon
    instance already on the page is never torn down.
    """
    base_url = app.config.get('PLEXORA_BASE_URL', '')
    outcome, plugin = _resolve(datasource, tool_name)

    if outcome == ATTACH:
        return jsonify({
            "redirect": f"{base_url}/upload_page?attach_to={datasource}&return_tool={tool_name}",
        })
    if outcome == FALLBACK:
        return jsonify({"redirect": f"{base_url}/{datasource}"}), 400

    data = template_data(datasource=datasource, active_tool=tool_name)
    fragments = {
        slot: render_template(template_path, data=data)
        for slot, template_path in plugin.panels.items()
    }
    return jsonify({
        "fragments": fragments,
        "scripts": plugin.asset_urls("scripts", base_url),
        "styles": plugin.asset_urls("styles", base_url),
    })
