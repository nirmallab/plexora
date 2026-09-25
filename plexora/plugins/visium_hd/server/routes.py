"""The visium_hd plugin's HTTP surface.

    GET  /plugins/visium_hd/manifest   the gene vocabulary and the grid
    GET  /plugins/visium_hd/stats      each selected gene's automatic window
    GET  /plugins/visium_hd/bin        counts in the square under the cursor
    POST /plugins/visium_hd/build      turn the bin matrix into a bin store
    GET  /plugins/visium_hd/status     how far along that build is
    GET/POST /plugins/visium_hd/state  the panel's own selection, per project
    POST /plugins/visium_hd/groups     gene groups read out of the user's file

The tiles do not appear here. A bin layer is drawn through core's
`/generated/layer/...` route from core's bin store (`bin_tiles`), because a
counted grid is a rendering primitive and not a vendor's format -- which is
also what gets it into Figure Builder's export for free. What is here is what
interprets the data: which file it came from, which genes exist, which ones
the user picked and in what colour.
"""

from __future__ import annotations

import json

from flask import Blueprint, jsonify, request

from plexora import api
from plexora.plugins.visium_hd import VERSION

visium_hd_bp = Blueprint(
    "visium_hd", __name__,
    template_folder="../templates",
    static_folder="../static",
    static_url_path="/static",
)

#: Preparing a bin layer, as bands of one bar. Reading the 2 micron matrix
#: (870 million entries, 11 GB compressed) is about a third of the work and
#: writing the store tiles and their pooled levels is the rest.
BIN_STAGES = {
    "read": (0, 35, "Reading the bin matrix"),
    "tiling": (35, 95, "Building bin tiles"),
    "stats": (95, 99, "Measuring genes"),
}


def build_layer(project, layer, stage, report):
    """Turn this layer's Space Ranger matrix into a bin store. Run by `layer_jobs`."""
    from plexora.plugins.visium_hd.server import tenx
    from plexora.server.models import bin_tiles

    source = layer.src
    if not source:
        raise ValueError(f"{layer.id} has no source matrix to build from.")
    render = dict(layer.render or {})
    shape = render.get("gridShape") or [layer.height, layer.width]
    rows, columns = int(shape[0]), int(shape[1])
    names, ids = tenx.read_features(source)
    expected = bin_tiles.expected_manifest(
        source, columns=columns, rows=rows,
        bin_um=float(render.get("binMicrons") or 2.0),
        microns_per_pixel=render.get("micronsPerPixel"), layer_id=layer.id)
    stored = bin_tiles.read_manifest(project.name, layer.id)
    if bin_tiles.is_current(stored, expected):
        return
    manifest = bin_tiles.build(
        project.name, layer.id, genes=names, gene_ids=ids,
        blocks=tenx.iter_blocks(source), block_count=tenx.block_count(source),
        expected=expected, stage=stage, progress=report)
    return manifest


def _layer_of(project, layer_id):
    layer = project.layer(layer_id) if project else None
    if layer is None or layer.kind != "points":
        return None
    return layer


def _args():
    return (request.args.get("datasource") or "",
            request.args.get("layer") or "bins")


@visium_hd_bp.route("/manifest")
def manifest():
    """This layer's vocabulary and grid, from the derived manifest.

    Never inlined into `/config`: eighteen thousand gene names would be
    downloaded on every viewer boot of every project, including the ones
    without a Visium run in them.
    """
    from plexora.server.models import bin_tiles

    datasource, layer_id = _args()
    stored = bin_tiles.read_manifest(datasource, layer_id)
    if not stored:
        return jsonify({"status": "missing", "genes": []})
    if int(stored.get("version") or 0) != bin_tiles.CACHE_VERSION:
        return jsonify({"status": "stale", "genes": [], "layer": layer_id})
    keys = ("genes", "gene_ids", "gene_counts", "total_count", "bin_count",
            "columns", "rows", "bin_um", "microns_per_pixel", "supersample",
            "tile_size", "level_count", "width", "height", "store_poolings",
            "hist_poolings", "total_index", "saturated_records", "nnz")
    answer = {key: stored.get(key) for key in keys}
    answer.update({"status": "ready", "layer": layer_id,
                   "revision": bin_tiles.revision(stored), "version": VERSION})
    return jsonify(answer)


@visium_hd_bp.route("/stats")
def stats():
    """`{gene: {window, p99, max, sum, nnz}}` at one pooling, for the legend.

    The same `auto_window` the tile path stretches against, at the same
    requested square size, so the numbers printed beside the colour bar are
    the numbers the colours mean at every zoom.
    """
    from plexora.server.models import bin_tiles

    datasource, layer_id = _args()
    stored = bin_tiles.read_manifest(datasource, layer_id)
    table = bin_tiles.read_stats(datasource, layer_id)
    if not stored or table is None:
        return jsonify({})
    # Normalised exactly as the tiles normalise it, so the legend's numbers
    # are the window every zoom level stretches against.
    pooling = bin_tiles.requested_pooling(request.args.get("bin"))
    poolings = list(stored.get("hist_poolings") or [1])
    names = [n for n in (request.args.get("genes") or "").split(",") if n]
    out = {}
    for name in names:
        rows = bin_tiles.gene_indices(stored, [name])
        if not rows:
            continue
        row = rows[0]
        column = poolings.index(pooling) if pooling in poolings else len(poolings) - 1
        nnz, total, p99, peak = (float(v) for v in table[row, column])
        out[name] = {"window": bin_tiles.auto_window(stored, table, row, pooling),
                     "p99": p99, "max": peak, "sum": total, "nnz": nnz}
    return jsonify(out)


@visium_hd_bp.route("/bin")
def square():
    """The counts of the selected genes in the square under the cursor.

    `x`/`y` are GRID coordinates (the client inverts the layer transform), so
    this is a lookup and not a registration.
    """
    from plexora.server.models import bin_tiles

    datasource, layer_id = _args()
    stored = bin_tiles.read_manifest(datasource, layer_id)
    if not stored:
        return jsonify({})
    try:
        column = int(float(request.args.get("x")))
        row = int(float(request.args.get("y")))
        pooling = max(1, int(request.args.get("bin") or 1))
    except (TypeError, ValueError):
        return jsonify({"error": "x and y are required"}), 400
    if not (0 <= column < stored["columns"] and 0 <= row < stored["rows"]):
        return jsonify({"inside": False})
    names = [n for n in (request.args.get("genes") or "").split(",") if n]
    names = names or [bin_tiles.TOTAL]
    rows = bin_tiles.gene_indices(stored, names)
    counts = bin_tiles.square_at(datasource, layer_id, stored, column, row,
                                 rows + [int(stored["total_index"])], pooling)
    by_name = {}
    for name, index in zip(names, rows):
        by_name[name] = counts.get(index, 0)
    return jsonify({
        "inside": True, "column": (column // pooling) * pooling,
        "row": (row // pooling) * pooling,
        "size_um": float(stored.get("bin_um") or 2.0) * pooling,
        "counts": by_name, "total": counts.get(int(stored["total_index"]), 0)})


@visium_hd_bp.route("/build", methods=["POST"])
def build():
    """Ask for this layer's bin store to be built, through core's job registry."""
    from plexora.server.models import layer_jobs
    from plexora.server.models.project import Project

    body = request.get_json(silent=True) or {}
    datasource = str(body.get("datasource") or "")
    layer_id = str(body.get("layer") or "bins")
    if not datasource:
        return jsonify({"error": "datasource is required"}), 400
    try:
        project = Project.load(datasource)
    except KeyError:
        return jsonify({"error": f"unknown project {datasource!r}"}), 404
    layer = _layer_of(project, layer_id)
    if layer is None or not layer.src:
        return jsonify({"error": f"{datasource} has no bin layer "
                                 f"{layer_id!r} with a source matrix"}), 404
    return jsonify(layer_jobs.start(
        datasource, layer_id,
        lambda p, l, stage, report: build_layer(p, l, stage, report),
        stages=BIN_STAGES)), 202


@visium_hd_bp.route("/status")
def status():
    from plexora.server.models import bin_tiles, layer_jobs

    datasource, layer_id = _args()
    state = layer_jobs.get(datasource, layer_id)
    if state is None:
        stored = bin_tiles.read_manifest(datasource, layer_id)
        return jsonify({"status": "ready" if stored else "missing"})
    return jsonify(state)


@visium_hd_bp.route("/state", methods=["GET", "POST"])
def state():
    """The panel's own state for one project: genes, colours, bin size, window.

    Stored as the JSON the client posted and handed back unread -- the shape
    is the panel's, and core has no business knowing it.
    """
    datasource = (request.args.get("datasource")
                  or (request.get_json(silent=True) or {}).get("datasource")
                  or "")
    if not datasource:
        return jsonify({"error": "datasource is required"}), 400
    store = api.store(datasource, "visium_hd")
    if request.method == "GET":
        blob = store.get_state()
        if not blob:
            return jsonify({})
        try:
            return jsonify(json.loads(blob.decode("utf-8")))
        except (ValueError, UnicodeDecodeError):
            return jsonify({})
    body = request.get_json(silent=True) or {}
    body.pop("datasource", None)
    store.put_state(json.dumps(body).encode("utf-8"))
    return jsonify({"saved": True})


@visium_hd_bp.route("/groups", methods=["POST"])
def groups():
    """Read gene groups out of a table the user has, against this bin layer.

    The same read the Transcripts layer's `/groups` does -- core's
    `server/utils/gene_groups.py`, behind core's gene-group dialog -- matched
    against THIS layer's vocabulary: the genes in its bin store's manifest.
    A marker list written for a Xenium panel works here unchanged, which is
    the point of the two panels sharing one gene list.
    """
    from plexora.server.models import bin_tiles
    from plexora.server.routes.import_routes import trim_filepath_quotes
    from plexora.server.utils import gene_groups

    datasource = (request.form.get("datasource") or "").strip()
    layer_id = (request.form.get("layer") or "bins").strip()
    if not datasource:
        return jsonify({"error": "datasource is required"}), 400
    stored = bin_tiles.read_manifest(datasource, layer_id) or {}
    body, status = gene_groups.answer(request.files, request.form,
                                      stored.get("genes") or [],
                                      trim=trim_filepath_quotes)
    return jsonify(body), status
