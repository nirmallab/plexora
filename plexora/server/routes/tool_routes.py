# Backend readiness check for the navbar's Tools dropdown: a datasource can
# only open a tool once it has real feature data (not just a quick-view stub
# CSV). If it doesn't, send the user to the upload page to attach the
# missing piece, prefilled from what's already registered, then return them
# here once that's done -- see page_routes.py's upload_page() for the other
# half of that handoff.
from plexora import app, get_config
from plexora.server.modules.registry import TOOL_LABELS, TOOL_PANEL_TEMPLATES, TOOL_SCRIPTS
from plexora.server.routes.page_routes import template_data
from flask import redirect, jsonify, render_template


def _has_real_feature_data(entry):
    """Whether a datasource has a real feature table attached, vs. only the
    synthetic 1-row stub CSV a quick-view registration writes.

    Prefers the explicit has_feature_data flag (set at registration time by
    register_image_datasource/register_rgb_datasource -- see datasource.py).
    Datasources registered before that flag existed have no such key, so as
    a fallback, detect the stub by its synthesized filename directly -- it's
    always named exactly quick_view_points.csv (see _write_stub_point_csv).
    """
    if 'has_feature_data' in entry:
        return entry['has_feature_data']
    feature_data = entry.get('featureData') or [{}]
    src = feature_data[0].get('src', '')
    return not src.endswith('quick_view_points.csv')


@app.route('/<string:datasource>/tools/<string:tool_name>')
def open_tool(datasource, tool_name):
    base_url = app.config.get('PLEXORA_BASE_URL', '')
    entry = get_config().get(datasource)
    installed_module = app.config.get('PLEXORA_ACTIVE_MODULE', '')

    # Unknown datasource, unknown/unlabeled tool, or a tool that isn't the
    # module actually installed on this process -- fall back to the plain
    # viewer rather than erroring (covers stale/bookmarked Tools links).
    if not entry or tool_name not in TOOL_LABELS or tool_name != installed_module:
        return redirect(f"{base_url}/{datasource}")

    # Thresholding is inapplicable to a flat RGB image (no channels to gate on).
    if entry.get('image_kind') == 'rgb':
        return redirect(f"{base_url}/{datasource}")

    if _has_real_feature_data(entry):
        return redirect(f"{base_url}/{datasource}?tool={tool_name}")

    return redirect(f"{base_url}/upload_page?attach_to={datasource}&return_tool={tool_name}")


@app.route('/<string:datasource>/tools/<string:tool_name>/panel')
def tool_panel(datasource, tool_name):
    """Fetched by toolLoader.js the first time a tool is opened mid-session
    (plain viewer already loaded, no navigation) -- mirrors open_tool()'s
    checks above, but returns JSON (panel HTML fragments + script URLs) for
    client-side injection instead of a redirect, so the viewer/OpenSeadragon
    instance already on the page is never torn down.
    """
    base_url = app.config.get('PLEXORA_BASE_URL', '')
    entry = get_config().get(datasource)
    installed_module = app.config.get('PLEXORA_ACTIVE_MODULE', '')

    if (
        not entry
        or tool_name not in TOOL_LABELS
        or tool_name != installed_module
        or entry.get('image_kind') == 'rgb'
    ):
        return jsonify({"redirect": f"{base_url}/{datasource}"}), 400

    if not _has_real_feature_data(entry):
        return jsonify({
            "redirect": f"{base_url}/upload_page?attach_to={datasource}&return_tool={tool_name}",
        })

    data = template_data(datasource=datasource, active_tool=tool_name)
    fragments = {
        slot: render_template(template_path, data=data)
        for slot, template_path in TOOL_PANEL_TEMPLATES.get(tool_name, {}).items()
    }
    return jsonify({
        "fragments": fragments,
        "scripts": TOOL_SCRIPTS.get(tool_name, []),
    })
