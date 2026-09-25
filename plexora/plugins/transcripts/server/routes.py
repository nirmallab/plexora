"""The transcripts plugin's HTTP surface.

A few routes, and the split between them is the split between what interprets
the data and what renders it:

    GET  /plugins/transcripts/manifest   what genes exist, how many of each
    GET  /plugins/transcripts/points     one tile's molecules -- or, above
                                         level 0, what they merge into
    POST /plugins/transcripts/build      turn a vendor file into tiles
    GET  /plugins/transcripts/status     how far along that build is
    GET/POST /plugins/transcripts/state  the panel's own selection, per project

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
    # The LAYER's transform first. It is the registration the importer
    # composed from the run's own manifest, and it is what every other layer
    # in the sample is drawn through; `image.pixel_size` is a fallback that
    # was, on every Xenium import made before this, simply None -- so the
    # cache was built in microns, at a fifth of the slide's scale, and the
    # whole density raster sat in the top-left corner of the image.
    pixel_size = (project.image.pixel_size or {}).get("value")
    genes, gene_index, x, y, q = xenium.read_transcripts(
        source,
        transform=list(layer.transform) if layer.transform else None,
        pixel_size=pixel_size,
        vocabulary=_panel_for(layer),
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
        x=x, y=y, q=q, expected=expected, progress=report)


def _panel_for(layer):
    """The gene vocabulary this layer's run declares, or None.

    Off the BUNDLE, which is where the importer recorded the run directory.
    Without it the vocabulary is whatever gene names appear in the transcript
    table, which quietly omits every gene the panel targeted and found none
    of -- and "we looked and there are none" is a result the selector should
    be able to show.
    """
    from plexora.plugins.transcripts.server import xenium

    root = (layer.source or {}).get("root") if layer.source else None
    if not root:
        return None
    try:
        return xenium.read_gene_panel(root)
    except Exception:
        return None


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
    from plexora.server.models import layer_sources, transcript_tiles

    datasource = request.args.get("datasource") or ""
    layer_id = request.args.get("layer") or ""
    stored = transcript_tiles.read_manifest(datasource, layer_id)
    if not stored:
        return jsonify({"status": "missing", "genes": []})
    if int(stored.get("version") or 0) != transcript_tiles.CACHE_VERSION \
            or stored.get("record_dtype") != transcript_tiles.RECORD_DTYPE_NAME:
        # `stale`, not `ready`, and the difference is not cosmetic: a cache
        # written at the old 10-byte stride decoded at the new 11-byte one
        # does not come back slightly wrong, it comes back as noise -- every
        # point somewhere else on the slide, plausibly distributed, with
        # nothing on screen to say so. The panel rebuilds on this.
        return jsonify({"status": "stale", "genes": [],
                        "layer": layer_id, "version": VERSION})
    genes = stored.get("genes") or []
    return jsonify({
        "status": "ready",
        "layer": layer_id,
        "genes": genes,
        # How many molecules of each, in the same order. What the selector
        # shows beside a name -- the single most useful thing to know about a
        # gene before turning it on -- and what the client's level-of-detail
        # estimate scales a selection by, so one rare gene out of 480 is
        # drawn molecule by molecule rather than merged because the panel as
        # a whole is dense.
        "gene_counts": stored.get("gene_counts") or [],
        "point_count": stored.get("point_count", 0),
        "tile_size": stored.get("tile_size"),
        "columns": stored.get("columns"),
        "rows": stored.get("rows"),
        "width": stored.get("width"),
        "height": stored.get("height"),
        "units": stored.get("units") or "reference_pixels",
        # What the client's level-of-detail estimate reads. Without it the
        # viewer would have to fetch a tile to find out how many points are
        # in it -- which is fetching the thing it is trying to decide whether
        # to fetch.
        "tile_counts": stored.get("tile_counts") or [],
        # Bins across an aggregate tile, in each axis. Part of the contract
        # rather than a constant on each side: the client picks the level of
        # detail by working out how big a bin would be ON SCREEN, which it
        # cannot do without knowing how many of them a tile is cut into.
        "aggregate_bins": transcript_tiles.AGGREGATE_BINS,
        # -- what the density controls need to speak in real units --------
        #
        # MICRONS PER PIXEL, read off the project NOW rather than out of the
        # cache. A bin size is a physical size -- "40 by 40 microns" is a
        # sentence about tissue and "188 pixels" is one about this scan -- so
        # the panel asks in microns and converts. Live rather than stored
        # because somebody who sets the pixel size on the edit page should
        # get a correctly labelled slider without rebuilding a 19-million-row
        # cache, and null where the project has never been told: the panel
        # falls back to labelling the slider in pixels, which is honest.
        "pixel_size": _pixel_size(datasource),
        # The multiple of the average bin that saturates the colour ramp.
        # Sent rather than repeated in the client, because the legend under
        # the ramp claims a number of molecules and a legend that disagreed
        # with the picture would be worse than no legend.
        "density_stretch": layer_sources.DENSITY_STRETCH,
        "version": VERSION,
    })


def _pixel_size(datasource):
    """Microns per reference pixel for this project, or None."""
    from plexora.server.models.project import Project

    try:
        project = Project.load(datasource)
    except (KeyError, ValueError):
        return None
    value = (project.image.pixel_size or {}).get("value")
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _whole(value, default):
    """A query parameter as a non-negative integer, or `default`.

    Lenient rather than a 400: these arrive on every tile request from a
    viewer that is mid-zoom, and refusing one is a hole in the picture where
    the honest answer is "the level you asked for was not a number, here is
    the base one".
    """
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


#: An empty answer, in the shape a full one has.
def _no_points():
    return Response(b"", mimetype="application/octet-stream",
                    headers={"X-Transcript-Record-Count": "0"})


@transcripts_bp.route("/points")
def points():
    """The molecules in ONE TILE, or inside an arbitrary rectangle.

    `tile=<x>_<y>` is the mode the viewer uses and the reason panning is
    cheap. A tile is a fixed, addressable unit: the client keeps the ones it
    has, asks only for the ones that came into view, and the answer is
    immutable for as long as the cache is -- so it carries an ETag and a long
    `Cache-Control` and a pan back over ground already covered costs nothing.
    The rectangle mode it replaced refetched the whole viewport on every pan,
    including all the points that had not moved.

    `genes` is a comma-separated list of NAMES, resolved to indices here, so the
    client never has to know that the file stores an index. A name this panel
    does not carry is skipped rather than refused: a saved view naming a gene a
    re-imported run no longer has should draw the rest.

    `level` is the level of detail, and it is what keeps Points mode POINTS at
    every zoom. Level 0 is the molecules themselves; each level up covers four
    times the area in one tile and merges what is in it, per gene, into one
    record carrying a count and the centroid of the molecules it stands for
    (`transcript_tiles.aggregate_tile`). So a screenful is about the same
    number of requests and the same number of dots however far out the view
    is, and nothing ever quietly turns into a density raster.

    The quality score is NOT filtered here AT LEVEL 0. It rides in the record
    and the client discards below the threshold in its vertex shader, so
    moving the slider redraws without refetching -- which is the whole reason
    the byte is in the record. An aggregate has no such freedom: its count is
    a count of what survived the filter, so `minq` is applied before the merge
    and is part of what the tile is.
    """
    from plexora.server.models import transcript_tiles

    datasource = request.args.get("datasource") or ""
    layer_id = request.args.get("layer") or ""
    stored = transcript_tiles.read_manifest(datasource, layer_id)
    if not stored:
        return _no_points()

    names = [n for n in (request.args.get("genes") or "").split(",") if n]
    indices = transcript_tiles.gene_indices(stored, names) if names else None
    if names and not indices:
        # Every name was unknown. Nothing, rather than everything: the user
        # asked for specific genes, and drawing the whole panel instead is the
        # opposite of what they asked for.
        return _no_points()

    level = _whole(request.args.get("level"), 0)
    min_q = _whole(request.args.get("minq"), None)

    tile = (request.args.get("tile") or "").strip()
    if tile:
        try:
            tile_x, tile_y = (int(part) for part in tile.split("_"))
        except (TypeError, ValueError):
            return _no_points()
        if level > 0:
            records = transcript_tiles.aggregate_tile(
                datasource, layer_id, level, tile_x, tile_y, genes=indices,
                tile_size=stored.get("tile_size")
                or transcript_tiles.DEFAULT_TILE_SIZE,
                min_q=min_q)
        else:
            records = transcript_tiles.read_tile(
                datasource, layer_id, tile_x, tile_y, indices)
        # Everything the answer is a function of, so that a tile the browser
        # already holds is reused and a tile for a different selection or a
        # different threshold is not mistaken for it. The threshold is in the
        # key only above level 0, because that is the only place it changes
        # the answer -- putting it in unconditionally would throw away every
        # cached molecule tile on each tick of a slider that does not affect
        # them. How the aggregate is DERIVED rides along for the same reason
        # and under the same condition: it is computed per request rather
        # than stored, so `source_mtime_ns` cannot see a change to it, and
        # without this a browser holding these tiles under a year-long
        # max-age would go on drawing the old grain and the old dots.
        tag = f"{level}:{tile}:{','.join(names)}"
        if level > 0:
            tag = (f"{tag}:{min_q}:{transcript_tiles.AGGREGATE_BINS}"
                   f"r{transcript_tiles.AGGREGATE_REVISION}")
        return _points_response(records, cap=None, cacheable=True,
                                stored=stored, tag=tag)

    bounds = {
        "minX": float(request.args.get("minX", 0)),
        "minY": float(request.args.get("minY", 0)),
        "maxX": float(request.args.get("maxX", stored.get("width", 0))),
        "maxY": float(request.args.get("maxY", stored.get("height", 0))),
    }
    cap = min(MAX_POINTS_PER_REQUEST,
              int(request.args.get("max", MAX_POINTS_PER_REQUEST) or MAX_POINTS_PER_REQUEST))

    records = transcript_tiles.read_region(
        datasource, layer_id, bounds, genes=indices, max_points=cap,
        min_q=min_q)
    return _points_response(records, cap=cap, cacheable=False,
                            stored=stored, tag="")


def _points_response(records, *, cap, cacheable, stored, tag):
    """The binary wire format, with the caching a tile earns and a rectangle
    does not."""
    payload = gzip.compress(records.tobytes("C"))
    headers = {
        "Content-Encoding": "gzip",
        "Content-Length": str(len(payload)),
        "X-Transcript-Record-Count": str(len(records)),
        # Said out loud rather than left for the client to infer from the count:
        # a view that quietly drew a sample and looked sparse would be read as
        # "this gene is not expressed here".
        "X-Transcript-Truncated": "1" if cap and len(records) >= cap else "0",
        "X-Transcript-Record-Size": str(records.dtype.itemsize),
        "Cache-Control": "private, max-age=0",
    }
    if cacheable:
        # Keyed on what the cache was built from, so a rebuild invalidates
        # every tile at once and nothing else does.
        headers["ETag"] = (f'"{stored.get("source_mtime_ns")}-'
                           f'{stored.get("version")}-{tag}"')
        headers["Cache-Control"] = "private, max-age=31536000"
    return Response(payload, mimetype="application/octet-stream", headers=headers)


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


@transcripts_bp.route("/state", methods=["GET", "POST"])
def state():
    """The panel's own state for one project: genes, colours, groups, sliders.

    Per PROJECT and not per session. A gene selection is an analysis decision
    -- these forty genes, in these colours, grouped like this -- and somebody
    who set one up and came back the next day to find an empty panel would
    reasonably conclude the tool had lost their work. Kept in the plugin's own
    store, which is the same place every other plugin keeps its state, so it
    travels with the project and nothing in core has to know the shape of it.

    The shape is entirely the client's: stored as the JSON it posted and
    handed back unread. Core validating a plugin's state would be core knowing
    what a gene group is.
    """
    datasource = (request.args.get("datasource")
                  or (request.get_json(silent=True) or {}).get("datasource")
                  or "")
    if not datasource:
        return jsonify({"error": "datasource is required"}), 400
    store = api.store(datasource, "transcripts")

    if request.method == "GET":
        blob = store.get_state()
        if not blob:
            return jsonify({})
        try:
            return jsonify(json.loads(blob.decode("utf-8")))
        except (ValueError, UnicodeDecodeError):
            # State written by a version that shaped it differently. An empty
            # panel the user can fill in beats a 500 they cannot act on.
            return jsonify({})

    body = request.get_json(silent=True) or {}
    body.pop("datasource", None)
    store.put_state(json.dumps(body).encode("utf-8"))
    return jsonify({"saved": True})


@transcripts_bp.route("/groups", methods=["POST"])
def groups():
    """Read gene groups out of a table the user has, against this panel.

    The read is core's (`server/utils/gene_groups.py`), shared with the
    Visium HD layer's own `/groups`, because the dialog that posts here is
    core's too. What is this plugin's is the VOCABULARY: the genes in this
    transcript layer's manifest, which the file's names are matched against
    and given back in the spelling of.
    """
    from plexora.server.models import transcript_tiles
    from plexora.server.routes.import_routes import trim_filepath_quotes
    from plexora.server.utils import gene_groups

    datasource = (request.form.get("datasource") or "").strip()
    layer_id = (request.form.get("layer") or "transcripts").strip()
    if not datasource:
        return jsonify({"error": "datasource is required"}), 400
    stored = transcript_tiles.read_manifest(datasource, layer_id) or {}
    body, status = gene_groups.answer(request.files, request.form,
                                      stored.get("genes") or [],
                                      trim=trim_filepath_quotes)
    return jsonify(body), status

