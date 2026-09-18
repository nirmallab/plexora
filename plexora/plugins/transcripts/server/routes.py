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
import threading

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

#: Build jobs in flight, by (datasource, layer). A daemon thread plus a polled
#: status endpoint, like the segmentation job -- no Celery, no Redis, and no
#: async: this codebase has one concurrency model and this is it.
_jobs = {}
_jobs_lock = threading.Lock()


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
    """Turn a vendor transcript file into tiles, in the background.

    A daemon thread and a polled status, like the segmentation pyramid job: a
    10-100 million row parquet is minutes of work, and a request that held the
    connection open for it would time out on every proxy between here and the
    user.
    """
    from plexora.plugins.transcripts.server import xenium
    from plexora.server.models import transcript_tiles
    from plexora.server.models.project import Project

    body = request.get_json(silent=True) or {}
    datasource = str(body.get("datasource") or "")
    layer_id = str(body.get("layer") or "transcripts")
    source = str(body.get("source") or "")
    if not datasource or not source:
        return jsonify({"error": "datasource and source are both required"}), 400

    try:
        project = Project.load(datasource)
    except KeyError:
        return jsonify({"error": f"unknown project {datasource!r}"}), 404

    key = (datasource, layer_id)
    with _jobs_lock:
        running = _jobs.get(key)
        if running and running.get("status") == "running":
            return jsonify(running), 202
        state = {"status": "running", "stage": "reading", "done": 0, "total": 0}
        _jobs[key] = state

    def run():
        try:
            pixel_size = (project.image.pixel_size or {}).get("value")
            genes, gene_index, x, y = xenium.read_transcripts(
                source, pixel_size=pixel_size,
                min_qv=body.get("min_qv"),
                progress=lambda stage, done, total: state.update(
                    stage=stage, done=done, total=total))
            state.update(stage="tiling", done=0, total=0)
            expected = transcript_tiles.expected_manifest(
                source,
                width=project.image.width or int(x.max() or 1) + 1,
                height=project.image.height or int(y.max() or 1) + 1,
                tile_size=project.image.tile_width or transcript_tiles.DEFAULT_TILE_SIZE,
                layer_id=layer_id)
            written = transcript_tiles.build(
                datasource, layer_id, genes=genes, gene_index=gene_index,
                x=x, y=y, expected=expected,
                progress=lambda done, total: state.update(done=done, total=total))
            state.update(status="ready", stage="done",
                         point_count=written.get("point_count", 0),
                         gene_count=written.get("gene_count", 0))
        except xenium.TranscriptDependencyMissing as error:
            # Its own status, so the client can offer the install line rather
            # than showing a stack trace to somebody who cannot act on one.
            state.update(status="error", stage="dependency",
                         error=str(error), install=xenium.TranscriptDependencyMissing.INSTALL)
        except Exception as error:  # pragma: no cover - reported, not swallowed
            state.update(status="error", stage="failed", error=str(error))

    thread = threading.Thread(target=run, name=f"transcripts-{layer_id}", daemon=True)
    thread.start()
    return jsonify(state), 202


@transcripts_bp.route("/status")
def status():
    datasource = request.args.get("datasource") or ""
    layer_id = request.args.get("layer") or "transcripts"
    with _jobs_lock:
        state = _jobs.get((datasource, layer_id))
    if state is None:
        from plexora.server.models import transcript_tiles

        stored = transcript_tiles.read_manifest(datasource, layer_id)
        return jsonify({"status": "ready" if stored else "missing"})
    return jsonify(state)
