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


def _has_real_feature_data(entry):
    """Whether a datasource has a real feature table attached, vs. a
    quick-view registration with no feature data at all.

    has_feature_data is written explicitly by every registration path now
    (True for register_datasource/register_anndata_datasource/save_config,
    False for register_image_datasource/register_rgb_datasource -- see
    datasource.py), so this always resolves from the key directly for any
    config written since. Datasources registered before that flag existed
    have no such key; the fallback below covers only those -- it detects the
    now-removed stub CSV a quick-view registration used to write, by its
    fixed filename (quick_view_points.csv).
    """
    if 'has_feature_data' in entry:
        return entry['has_feature_data']
    feature_data = entry.get('featureData') or [{}]
    src = feature_data[0].get('src', '')
    return not src.endswith('quick_view_points.csv')


def _usable_plugin(datasource, tool_name):
    """The named plugin, if it is installed here and this datasource can
    actually use it. Returns (plugin, entry) or (None, entry).

    Whether a tool applies is the plugin's own declaration (Plugin.requires),
    not a rule core hardcodes -- core used to test `image_kind == 'rgb'`
    directly, which only ever encoded what gating in particular could not
    handle.
    """
    entry = get_config().get(datasource)
    if not entry:
        return None, None
    plugin = plugin_registry.find(app, tool_name)
    if plugin is None or not plugin.requires.satisfied_by(entry):
        return None, entry
    return plugin, entry


@app.route('/<string:datasource>/tools/<string:tool_name>')
def open_tool(datasource, tool_name):
    base_url = app.config.get('PLEXORA_BASE_URL', '')
    plugin, entry = _usable_plugin(datasource, tool_name)

    # Unknown datasource, or a tool that isn't installed here / doesn't apply
    # -- fall back to the plain viewer rather than erroring (covers stale or
    # bookmarked Tools links).
    if plugin is None:
        # A plugin that only needs a feature table the project lacks is
        # recoverable: send the user to attach one and come back.
        if entry and plugin_registry.find(app, tool_name) and _has_real_feature_data(entry) is False:
            return redirect(f"{base_url}/upload_page?attach_to={datasource}&return_tool={tool_name}")
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
    plugin, entry = _usable_plugin(datasource, tool_name)

    if plugin is None:
        if entry and plugin_registry.find(app, tool_name) and not _has_real_feature_data(entry):
            return jsonify({
                "redirect": f"{base_url}/upload_page?attach_to={datasource}&return_tool={tool_name}",
            })
        return jsonify({"redirect": f"{base_url}/{datasource}"}), 400

    data = template_data(datasource=datasource, active_tool=tool_name)
    fragments = {
        slot: render_template(template_path, data=data)
        for slot, template_path in plugin.panels.items()
    }
    return jsonify({
        "fragments": fragments,
        "scripts": plugin.asset_urls("scripts"),
        "styles": plugin.asset_urls("styles"),
    })
