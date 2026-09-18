"""The transcripts plugin's HTTP surface.

Three routes, and the split between them is the split between what interprets
the data and what renders it:

    GET  /plugins/transcripts/manifest   what genes exist, how many of each
    GET  /plugins/transcripts/points     the molecules in a rectangle
    POST /plugins/transcripts/build      turn a vendor file into tiles

The tiles a LAYER is drawn from do not appear here. Density is a uint16 raster
that goes through core's `/generated/layer/...` route, because it is a channel
like any other -- which is what makes the low-zoom transcript view cost no new
client rendering code and survive into Figure Builder's export for free.

Wire format for the points is the binary convention the rest of Plexora already
uses (data_routes' `get_all_cells`): a numpy structured array, gzipped, served as
octet-stream with the record count in a header. JSON for 2 million points is 60
MB of ASCII and a second of parsing.
"""

from __future__ import annotations

import gzip
import json

from flask import Blueprint, Response, jsonify, request

from plexora import api
from plexora.plugins.transcripts import VERSION

transcripts_bp = Blueprint(
    "transcripts", __name__,
    template_folder="../templates",
    static_folder="../static",
    static_url_path="/static",
)

#: The most points one request may return. Past this the client is being handed
#: more than it can draw in a frame, and the honest answer is a sample plus a
#: header saying so -- see `X-Transcript-Truncated`.
MAX_POINTS_PER_REQUEST = 2_000_000

#: The stages of preparing a transcript layer, as bands of one bar. Named here
#: because they are this modality's phases -- reading a 6 GB parquet is most of
#: the work and tiling the rest -- and handed to core, which owns how a bar is
#: drawn and how a job is run. The plugin never starts a thread of its own any
#: more: `layer_jobs` does, and its record is what `/import/status` reports
#: alongside the mask's.
TRANSCRIPT_STAGES = {
    "read": (0, 45, "Reading the transcript file"),
    "index": (45, 55, "Indexing genes"),
    "tiling": (55, 97, "Building transcript tiles"),
}


def build_layer(project, layer, stage, report):
    """Turn this layer's vendor file into tiles. Run by `layer_jobs`.

    The body of what `/build` used to run on a thread of its own, unchanged in
    what it does and changed entirely in who calls it: the importer starts it
    when a Xenium run is registered, and the panel's button starts it when
    somebody asks again. One job record either way, so a user who opens the
    panel while the import is still building sees the import's progress rather
    than starting a second build of the same file.
    """
    from plexora.plugins.transcripts.server import xenium
    from plexora.server.models import transcript_tiles

    source = layer.src
    if not source:
        raise ValueError(f"{layer.id} has no source file to build from.")

    stage("read")
    pixel_size = (project.image.pixel_size or {}).get("value")
    genes, gene_index, x, y = xenium.read_transcripts(
        source, pixel_size=pixel_size,
        min_qv=(layer.render or {}).get("min_qv"),
        progress=lambda key, done, total: report(done, total))
    stage("index")
    stage("tiling")
    expected = transcript_tiles.expected_manifest(
        source,
        width=project.image.width or int(x.max() or 1) + 1,
        height=project.image.height or int(y.max() or 1) + 1,
        tile_size=project.image.tile_width or transcript_tiles.DEFAULT_TILE_SIZE,
        layer_id=layer.id)
    transcript_tiles.build(
        project.name, layer.id, genes=genes, gene_index=gene_index,
        x=x, y=y, expected=expected, progress=report)


def _layer_of(project, layer_id):
    layer = project.layer(layer_id) if project else None
    if layer is None or layer.kind != "points":
        return None
    return layer


@transcripts_bp.route("/manifest")
def manifest():
    """This layer's gene vocabulary and grid.

    Served from the derived manifest rather than from `config.json`, and that is
    a rule rather than an implementation detail: `/config` reaches the client on
    every viewer boot for every project the user has, and a 300-gene vocabulary
    inlined there would be downloaded on every page load of every project,
    including the ones with no transcripts at all.
    """
    from plexora.server.models import transcript_tiles

    datasource = request.args.get("datasource") or ""
    layer_id = request.args.get("layer") or ""
    stored = transcript_tiles.read_manifest(datasource, layer_id)
    if not stored:
        return jsonify({"status": "missing", "genes": []})
    return jsonify({
        "status": "ready",
        "layer": layer_id,
        "genes": stored.get("genes") or [],
        "point_count": stored.get("point_count", 0),
        "tile_size": stored.get("tile_size"),
        "columns": stored.get("columns"),
        "rows": stored.get("rows"),
        "width": stored.get("width"),
        "height": stored.get("height"),
        # What the client's LOD estimate reads. Without it the viewer would have
        # to fetch a tile to find out how many points are in it -- which is
        # fetching the thing it is trying to decide whether to fetch.
        "tile_counts": stored.get("tile_counts") or [],
        "version": VERSION,
    })


@transcripts_bp.route("/points")
def points():
    """The molecules inside a rectangle, for the genes asked for.

    `genes` is a comma-separated list of NAMES, resolved to indices here, so the
    client never has to know that the file stores an index. A name this panel
    does not carry is skipped rather than refused: a saved view naming a gene a
    re-imported run no longer has should draw the rest.
    """
    from plexora.server.models import transcript_tiles

    datasource = request.args.get("datasource") or ""
    layer_id = request.args.get("layer") or ""
    stored = transcript_tiles.read_manifest(datasource, layer_id)
    if not stored:
        return Response(b"", mimetype="application/octet-stream",
                        headers={"X-Transcript-Record-Count": "0"})

    names = [n for n in (request.args.get("genes") or "").split(",") if n]
    indices = transcript_tiles.gene_indices(stored, names) if names else None
    if names and not indices:
        # Every name was unknown. Nothing, rather than everything: the user
        # asked for specific genes, and drawing the whole panel instead is the
        # opposite of what they asked for.
        return Response(b"", mimetype="application/octet-stream",
                        headers={"X-Transcript-Record-Count": "0"})

    bounds = {
        "minX": float(request.args.get("minX", 0)),
        "minY": float(request.args.get("minY", 0)),
        "maxX": float(request.args.get("maxX", stored.get("width", 0))),
        "maxY": float(request.args.get("maxY", stored.get("height", 0))),
    }
    cap = min(MAX_POINTS_PER_REQUEST,
              int(request.args.get("max", MAX_POINTS_PER_REQUEST) or MAX_POINTS_PER_REQUEST))

    records = transcript_tiles.read_region(
        datasource, layer_id, bounds, genes=indices, max_points=cap)

    payload = gzip.compress(records.tobytes("C"))
    return Response(payload, mimetype="application/octet-stream", headers={
        "Content-Encoding": "gzip",
        "Content-Length": str(len(payload)),
        "X-Transcript-Record-Count": str(len(records)),
        # Said out loud rather than left for the client to infer from the count:
        # a view that quietly drew a sample and looked sparse would be read as
        # "this gene is not expressed here".
        "X-Transcript-Truncated": "1" if len(records) >= cap else "0",
        "Cache-Control": "private, max-age=0",
    })


@transcripts_bp.route("/build", methods=["POST"])
def build():
    """Ask for this layer's tiles to be built.

    The work itself is `build_layer`, run by core's `layer_jobs` -- a daemon
    thread and a polled status, exactly as before, but ONE record that
    `/import/status` reports alongside the mask's. Which matters because the
    importer also starts this build: somebody who opens the panel while an
    import is still running now sees that build's progress instead of starting
    a second one over the same cache directory.
    """
    from plexora.server.models import layer_jobs
    from plexora.server.models.project import Project

    body = request.get_json(silent=True) or {}
    datasource = str(body.get("datasource") or "")
    layer_id = str(body.get("layer") or "transcripts")
    if not datasource:
        return jsonify({"error": "datasource is required"}), 400

    try:
        project = Project.load(datasource)
    except KeyError:
        return jsonify({"error": f"unknown project {datasource!r}"}), 404

    layer = _layer_of(project, layer_id)
    if layer is None or not layer.src:
        # The source comes off the LAYER now, not off the request body. A
        # transcript file the project has never heard of is not something to
        # build tiles for under this project's name -- registering it is what
        # the import flow is for, and it is one call away.
        return jsonify({"error": f"{datasource} has no transcript layer "
                                 f"{layer_id!r} with a source file"}), 404

    return jsonify(layer_jobs.start(
        datasource, layer_id,
        lambda p, l, stage, report: build_layer(p, l, stage, report),
        stages=TRANSCRIPT_STAGES)), 202


@transcripts_bp.route("/status")
def status():
    """This layer's build state, from the one job registry.

    Kept as its own route rather than folded into `/import/status`: the panel
    asks about ONE layer and gets one answer, and a plugin that had to parse a
    whole-sample document to find itself would be coupled to a shape it does
    not own.
    """
    from plexora.server.models import layer_jobs, transcript_tiles

    datasource = request.args.get("datasource") or ""
    layer_id = request.args.get("layer") or "transcripts"
    state = layer_jobs.get(datasource, layer_id)
    if state is None:
        stored = transcript_tiles.read_manifest(datasource, layer_id)
        return jsonify({"status": "ready" if stored else "missing"})
    return jsonify(state)
