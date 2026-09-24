"""Tiles for a layer that is not the reference image.

`data_model` holds ONE open datasource, in module globals: `_loaded_source`,
`load_generation`, the channel list, the pyramid. That is the right shape for
what it does -- one project is open in one viewer, and the warm tile path is
0.005 s because nothing has to be looked up. It is the wrong shape for a scene
with several image layers, and rewriting it would mean rewriting the hottest
path in the application for a capability nobody asked for: the requirement is N
LAYERS of one project, not N projects.

So this bypasses it, which is a documented move rather than a new one --
`figure_builder/server/render.py` says it outright: *"Nothing here goes through
data_model... a render that called load_datasource would evict their session to
draw a figure."* Same reason, same answer.

**This is cheap because the tile reader is already parameterized; only the
lookup was global.** `data_model.read_tile(pyramid, channel, level, tile, tw, th)`
is pure over the pyramid it is handed, and so are `_zarr_level`,
`quantization_window_of` and `encode_tile_array`. All four are reused unchanged;
what is new here is a small keyed cache of open pyramids and the route that
reads through it.

The reference image's tile path is NEVER touched. `test_layer_sources.py`
monkeypatches `data_model.load_datasource` with a counter and asserts zero calls
while serving a wall of layer tiles, because a comment saying so would not hold.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict

from plexora.server.models.project import Project, config_generation
from plexora.server.utils import colormaps

#: How many layer pyramids stay open. Each pins a file handle and whatever the
#: reader keeps behind it, and a scene with more than a handful of image layers
#: registered at once is not a thing anybody has. Evicting the least recently
#: used one costs a reopen, which is milliseconds.
MAX_OPEN_LAYERS = 8

_open_layers: "OrderedDict[tuple, OpenLayer]" = OrderedDict()
_lock = threading.RLock()


class OpenLayer:
    """One layer's pyramid, open and ready to slice.

    The same `(channels, zarray, metadata)` triple `ImageProvider.open()`
    already returns, kept together for the same reason it returns them together:
    they come from one file handle and one pyramid walk.
    """

    __slots__ = ("layer", "channels", "overview", "metadata", "generation",
                 "windows", "stats", "gmm")

    def __init__(self, layer, channels, overview, metadata, generation):
        self.layer = layer
        self.channels = channels
        self.overview = overview
        self.metadata = metadata
        self.generation = generation
        #: Per channel: what `window_of` has already scanned for. See that
        #: function -- the scan reads every pixel of a full-resolution plane
        #: and `layer_tile` used to ask for one per TILE.
        self.windows = {}
        #: Per channel: the stats packet and the GMM packet the channel panel
        #: asks for. Held here rather than in a module dict so they die with
        #: the pyramid they describe -- the key already carries the config
        #: generation, so a re-registered layer cannot serve the old fit.
        self.stats = {}
        self.gmm = {}

    @property
    def tile_width(self):
        return self.layer.tile_width or 1024

    @property
    def tile_height(self):
        return self.layer.tile_height or self.tile_width


def resolve_layer_provider(project, layer):
    """The provider that serves one layer's pixels.

    Sits beside `resolve_providers` and branches on `layer.binding` exactly as
    that branches on `project.resources` -- so a layer on an HPC node reaches
    the same NodeImageProvider, and no new provider class exists. A scene can
    perfectly well have its morphology image on a node and its transcripts here.
    """
    from plexora.server.providers.local import LocalImageProvider

    binding = layer.binding if (layer.binding and layer.binding.is_node) else None
    if binding is None:
        return LocalImageProvider(
            path=layer.src,
            pyramid=layer.pyramid,
            rgb=bool((layer.render or {}).get("rgb")),
        )

    from plexora.server.providers.node import NodeImageProvider

    return NodeImageProvider(binding)


def open_layer(project_name, layer_id):
    """This layer's pyramid, opened once and kept.

    Keyed on the config generation as well as the ids, so re-registering a layer
    with a corrected transform or a rebuilt pyramid does not go on serving the
    old one -- and so an unrelated project being loaded does NOT evict this,
    which is what would happen if the key were `data_model.load_generation`.

    Returns None for a layer that has no pixels of its own: the centroid layer,
    a points layer, or a layer the project no longer has.
    """
    generation = config_generation()
    key = (project_name, layer_id, generation)
    with _lock:
        found = _open_layers.get(key)
        if found is not None:
            _open_layers.move_to_end(key)
            return found

    try:
        project = Project.load(project_name)
    except KeyError:
        # A project nobody registered. None rather than propagating, so the
        # route answers 404 -- "no such layer" and "no such project" are the
        # same answer to a tile request, and a 500 would look like this server
        # is broken rather than like the url is wrong.
        return None
    layer = project.layer(layer_id) if project else None
    if layer is None or layer.kind != "image" or not layer.src:
        return None

    provider = resolve_layer_provider(project, layer)
    channels, overview, metadata = provider.open()
    opened = OpenLayer(layer, channels, overview, metadata, generation)

    with _lock:
        _open_layers[key] = opened
        _open_layers.move_to_end(key)
        while len(_open_layers) > MAX_OPEN_LAYERS:
            _open_layers.popitem(last=False)
        # Every other generation of this same layer is stale by definition.
        for stale in [k for k in _open_layers
                      if k[0] == project_name and k[1] == layer_id and k[2] != generation]:
            _open_layers.pop(stale, None)
    return opened


def forget(project_name=None, layer_id=None):
    """Drop open layers. Everything when called with nothing.

    For teardown and for the tests; the generation in the key already handles
    the ordinary invalidation, so nothing in the request path calls this.
    """
    with _lock:
        for key in [k for k in _open_layers
                    if (project_name is None or k[0] == project_name)
                    and (layer_id is None or k[1] == layer_id)]:
            _open_layers.pop(key, None)


def window_of(opened, channel_num):
    """(qmin, qmax) for one channel of an open layer, scanned once.

    `data_model.quantization_window_of` reads EVERY PIXEL of the channel's
    full-resolution plane -- that is what makes the ceiling trustworthy -- and
    `layer_tile` called it per tile. One pan over a layer is dozens of tiles,
    so the answer that takes a full-plane scan was being recomputed dozens of
    times for the same number. The reference image's path has always cached
    this (`_quantization_cache`); this is the same cache for a layer, living on
    the open pyramid so it is evicted with it.

    None for a channel that has no window: a segmentation plane, or an RGB
    layer whose bytes are already the picture.
    """
    from plexora.server.models import data_model

    if channel_num is None or channel_num == "rgb":
        return None
    if channel_num in opened.windows:
        return opened.windows[channel_num]
    window = data_model.quantization_window_of(opened.channels, channel_num)
    with _lock:
        opened.windows[channel_num] = window
    return window


def _channel_plane(project_name, layer_id, channel):
    """(opened, channel_num, window) for a channel-panel request, or None.

    The one gate both packets below go through, so "which layers have channel
    controls" is answered in one place: an image layer, a channel that names an
    index, and an overview big enough to hold it.
    """
    from plexora.server.models import data_model

    opened = open_layer(project_name, layer_id)
    if opened is None:
        return None
    channel_num, is_segmentation = data_model._parse_channel(str(channel))
    if is_segmentation or channel_num is None or channel_num == "rgb":
        return None
    overview = opened.overview
    if overview is None or getattr(overview, "ndim", 0) < 3:
        return None
    if not 0 <= int(channel_num) < overview.shape[0]:
        return None
    window = window_of(opened, channel_num)
    if not window:
        return None
    return opened, int(channel_num), window


def layer_channel_stats(project_name, layer_id, channel):
    """One layer channel's stats packet -- the same shape the reference
    image's `/get_image_channel_stats` returns, built the same way.

    This is what makes a registered layer's channel controls the SAME controls
    the reference image has rather than a second set that looks like them. The
    sidebar needs qmin/qmax before it can display even a saved range (its
    slider works in the byte domain the server quantized into), and
    vmin_hint/vmax_hint are what stop a freshly enabled channel being drawn
    near-black for the second the GaussianMixture fit takes.

    `channel_stats_of` is pure over the overview plane and the window, so this
    reuses it exactly -- no second implementation to drift.

    None for a layer with no channel planes (points, RGB, segmentation), which
    the route turns into a 404.
    """
    from plexora.server.models import data_model

    found = _channel_plane(project_name, layer_id, channel)
    if found is None:
        return None
    opened, channel_num, (qmin, qmax) = found
    if channel_num in opened.stats:
        return opened.stats[channel_num]
    packet = data_model.channel_stats_of(opened.overview[channel_num], qmin, qmax)
    with _lock:
        opened.stats[channel_num] = packet
    return packet


def layer_channel_gmm(project_name, layer_id, channel):
    """One layer channel's GaussianMixture packet, as `/get_channel_gmm`
    returns it for the reference image.

    Slow -- 0.2 to 1.9 s -- and cached for the life of the open pyramid for
    that reason. The fit is deterministic, so a second call would spend a
    second arriving at the number it already has.
    """
    from plexora.server.models import data_model

    found = _channel_plane(project_name, layer_id, channel)
    if found is None:
        return None
    opened, channel_num, (qmin, qmax) = found
    if channel_num in opened.gmm:
        return opened.gmm[channel_num]
    packet = data_model.channel_gmm_of(opened.overview[channel_num], qmin, qmax)
    with _lock:
        opened.gmm[channel_num] = packet
    return packet


def parse_style(raw):
    """A layer's colour and window from the query string, or None.

    `{color: (r, g, b), lo: int, hi: int}`. None -- no `color` given -- means
    "serve this tile the way a channel tile is served", which is the grey
    uint16 the GL colorize pass takes, and is what keeps this route's original
    behaviour byte for byte.

    A colour IS the reason this route colourises at all, and WHICH layers ask
    for one is now a narrow set. A layer whose pixels are channel planes is
    drawn through the client's GL colorize pass like any reference channel --
    it asks for no colour, so its tiles are the plain quantized plane, and its
    colour and window are free to change. What still arrives coloured is what
    that pass has nothing to do with: an rgb layer, and a points layer's
    density raster, both of which the browser composites directly. Doing it
    here costs one LUT lookup and a multiply on top of the quantization a
    channel tile already pays -- see `encode_tile_array`'s measurements.
    """
    raw = raw or {}
    colour = str(raw.get("color") or "").strip().lstrip("#")
    if len(colour) == 3:
        colour = "".join(c * 2 for c in colour)
    if len(colour) != 6:
        return None
    try:
        rgb = tuple(int(colour[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None

    def number(key):
        try:
            return int(float(raw.get(key)))
        except (TypeError, ValueError):
            return None

    def names(key):
        return [part for part in str(raw.get(key) or "").split(",") if part]

    def colours(key):
        found = []
        for part in names(key):
            hexed = part.strip().lstrip("#")
            if len(hexed) == 3:
                hexed = "".join(c * 2 for c in hexed)
            if len(hexed) != 6:
                continue
            try:
                found.append(tuple(int(hexed[i:i + 2], 16) for i in (0, 2, 4)))
            except ValueError:
                continue
        return found

    def quality(key):
        try:
            return int(float(raw.get(key)))
        except (TypeError, ValueError):
            return None

    def fraction(key, fallback):
        try:
            return min(1.0, max(0.0, float(raw.get(key))))
        except (TypeError, ValueError):
            return fallback

    return {
        "color": rgb,
        "lo": number("lo"),
        "hi": number("hi"),
        # A points layer's density, per gene. Present only for a transcript
        # layer, and the reason it is a LIST rather than one layer per gene:
        # a selection is a handful of genes with a colour each, and one tiled
        # layer per gene would mean N requests, N decodes and N world items
        # per pan for a picture that is their sum. See `density_rgb_tile`.
        "genes": names("genes"),
        "colors": colours("colors"),
        # Named `minq` and not `q`: `q` is already this route's encoding
        # quality parameter, and the collision would have been silent -- a
        # quality slider that quietly switched the tiles to lossless.
        "minq": quality("minq"),
        # -- the density map's own three controls ------------------------
        # How coarse the bins are, in LAYER PIXELS. The panel asks the user
        # for microns and converts, because a bin size is a physical size --
        # "40 by 40 microns" is a sentence about tissue and "148 pixels" is
        # one about this particular scan.
        "bin": number("bin"),
        # Which colour ramp, when the density is one field rather than a
        # gene per colour. Absent means the per-gene composite, which is a
        # different question and not a different palette -- see
        # `transcript_tiles.density_ramp_tile`.
        "ramp": (str(raw.get("ramp") or "").strip().lower() or None),
        # The window, as FRACTIONS of the automatic one. Fractions because
        # the count that means "dense" quadruples with every zoom level, and
        # a threshold set at one zoom has to keep its meaning at the next.
        "dlo": fraction("dlo", 0.0),
        "dhi": fraction("dhi", 1.0),
        # Counts through log1p before the window. For a bin layer, where a
        # 2 micron square holds one or two molecules and an islet holds
        # hundreds, a linear stretch shows the islet and nothing else.
        "log": str(raw.get("log") or "").strip().lower() in ("1", "true", "on", "yes"),
    }


def _colourise(quantized, rgb):
    """A uint8 grey tile as interleaved RGB in one colour.

    Multiplicative rather than a palette lookup: the layer is composited with
    `lighter` on top of whatever is under it, exactly as a fluorescence channel
    is, and multiplying the intensity by the colour is what that blend expects.
    Integer arithmetic throughout -- a float pass over a 1024x1024 tile is 4 MB
    of allocation per tile per pan for a result that is rounded back to bytes.
    """
    import numpy as np

    out = np.empty(quantized.shape + (3,), dtype=np.uint8)
    wide = quantized.astype(np.uint16)
    for index, component in enumerate(rgb):
        out[..., index] = (wide * int(component) // 255).astype(np.uint8)
    return out


#: How many times the layer's AVERAGE local density a bin has to hold to
#: saturate. Chosen rather than measured, and the choice is the point: a
#: density raster stretched to its own tile's maximum re-stretches as you pan,
#: so a sparse corner looks as busy as a tumour edge. Stretching to a multiple
#: of the mean instead is stable everywhere, stable across zoom (see
#: `density_window`) and costs nothing to compute.
DENSITY_STRETCH = 8


def density_window(manifest, level):
    """The upper end of a density layer's contrast window at one zoom level.

    Derived rather than stored, and that is deliberate. The obvious thing --
    record one `density_max` at build time -- cannot be right, because a
    density bin at level L covers 4^L times the area a level-0 bin does and
    therefore holds about 4^L times as many transcripts. One number would make
    every zoomed-out view black. This scales with the level, which is what
    makes the picture look the same as you zoom.

    Everything it reads (`point_count`, `width`, `height`) is already in the
    manifest, so a build does not have to do an extra pass over 50 million
    points to answer it.
    """
    return max(1, int(round(density_scale(manifest, level))))


def density_scale(manifest, level, bin_pixels=None) -> float:
    """The same window, UNROUNDED, for a bin of a given size.

    A count is an integer and a window over counts may as well be one, which
    is why `density_window` rounds -- but a bin at full zoom holds a
    hundredth of a molecule on average, and rounding that up to 1 is what made
    a density map at 40x a field of saturated white dots. The coloured path
    smooths its raster into a real local density (see
    `transcript_tiles._smooth`) and compares it against this.

    `bin_pixels` is how many IMAGE pixels across one bin is. Left out it is
    2^level, which is one bin per level pixel and is what every density tile
    used before the bin-size control existed. It matters because the whole
    point of the window is "eight times the average bin", and making the bins
    four times bigger puts four times as many molecules in each of them --
    without this, choosing 40-micron bins would black the picture out.
    """
    points = float(manifest.get("point_count") or 0)
    area = float(manifest.get("width") or 0) * float(manifest.get("height") or 0)
    if points <= 0 or area <= 0:
        return 1.0
    per_pixel = points / area
    span = float(bin_pixels) if bin_pixels else float(2 ** int(level))
    return DENSITY_STRETCH * per_pixel * (span * span)


def _points_tile(project_name, layer, level, tile, quality, style):
    """A points layer's density raster, coloured.

    Core draws no modality. What makes this a *transcript* density is the
    manifest the transcripts plugin's build wrote, which this reads by name
    through `transcript_tiles` -- a core module, because a tile cache of
    (gene, x, y) records is a spatial primitive and not a vendor's format.
    Without a manifest there is nothing built yet and the answer is 404, which
    is what the layer's card turns into "Preparing...".
    """
    from plexora.server.models import data_model, transcript_tiles

    if (layer.render or {}).get("pointKind") == "bin":
        # A counted grid (Visium HD) rather than a scatter. Dispatched on the
        # render hint and not on a modality, so core names no vendor.
        return _bins_tile(project_name, layer, level, tile, quality, style)

    stored = transcript_tiles.read_manifest(project_name, layer.id)
    if not stored:
        return None
    tile_x, tile_y = (int(part) for part in
                      str(tile).replace(".png", "").split("_"))
    tile_size = int(stored.get("tile_size") or transcript_tiles.DEFAULT_TILE_SIZE)
    min_q = (style or {}).get("minq")
    low = (style or {}).get("dlo") or 0.0
    high = (style or {}).get("dhi")
    high = 1.0 if high is None else high
    # What was actually binned, which is what was asked for unless the level
    # is too coarse to draw it -- and the contrast ceiling has to be the one
    # for the bins on screen, or the picture is stretched against a bin that
    # was never drawn. The ceiling goes as the bin's AREA, so getting this
    # wrong by the 10% the old rounding introduced moved every colour by 20%
    # as the viewer crossed a level.
    requested = (style or {}).get("bin")
    bin_pixels = transcript_tiles.bin_pixels_for(tile_size, int(level), requested)
    ceiling = density_scale(stored, int(level), bin_pixels)

    ramp_name = (style or {}).get("ramp")
    if ramp_name and colormaps.is_ramp(ramp_name):
        # ONE FIELD, READ OFF A RAMP. The genes are summed before the colour,
        # so the colour is a quantity rather than an identity -- which is the
        # question a heat map answers and the one a per-gene composite does
        # not. It covers the layer edge to edge: an empty bin is the ramp's
        # bottom colour, so zero is a value and not a hole, and the client
        # composites this one `source-over` rather than with the `lighter`
        # the per-gene path wants.
        indices = _selected_indices(stored, style)
        if indices is not None and not indices:
            indices = None
        return data_model.encode_tile_array(
            transcript_tiles.density_ramp_tile(
                project_name, layer.id, int(level), tile_x, tile_y,
                indices=indices, ramp=colormaps.ramp(ramp_name),
                # THE SELECTION'S OWN SHARE, for the same reason
                # `_density_groups` scales each gene's: the ceiling is a
                # multiple of the average bin over the WHOLE panel, and the
                # field being coloured holds only the genes that are on.
                # Three genes out of 480 reach two levels out of 255 against
                # the panel's window, which is the ramp's dark end
                # everywhere -- a flat purple wash over the whole slide that
                # reads as "this ramp is broken" rather than as "rescaled
                # wrong".
                ceiling=ceiling * _selection_share(stored, indices),
                tile_size=tile_size, min_q=min_q,
                bin_pixels=requested, low=low, high=high),
            False, quality)

    groups = _density_groups(stored, style, ceiling)
    if groups:
        # Several genes, each in its own colour, summed into one tile. One
        # request instead of one per gene, and each gene gets its own contrast
        # window -- a rare gene and an abundant one share a tile and must not
        # share a stretch, or the rare one is invisible.
        return data_model.encode_tile_array(
            transcript_tiles.density_rgb_tile(
                project_name, layer.id, int(level), tile_x, tile_y,
                groups=groups, tile_size=tile_size, min_q=min_q,
                bin_pixels=requested, low=low, high=high),
            False, quality)

    counts = transcript_tiles.density_tile(
        project_name, layer.id, int(level), tile_x, tile_y,
        tile_size=tile_size, min_q=min_q, bin_pixels=requested)

    hi = style.get("hi") if style else None
    if hi is None:
        hi = density_window(stored, int(level))
    lo = (style.get("lo") if style else None) or 0
    span = max(1, int(hi) - int(lo))
    quantized = data_model._quantize_to_uint8(counts, int(lo), span)
    if style:
        return data_model.encode_tile_array(
            _colourise(quantized, style["color"]), False, quality)
    # No colour asked for: the grey uint16 raster, which is what a channel tile
    # is and what `density_tile`'s docstring promises.
    return data_model.encode_tile_array(
        counts, False, quality, qmin=int(lo), qmax=int(lo) + span)


def _bins_tile(project_name, layer, level, tile, quality, style):
    """A bin layer's tile: pooled counts, coloured, RGBA drawn source-over.

    `(payload, mimetype, revision)`; the revision rides the ETag because the
    tile is derived per request from a store that can be rebuilt without the
    project record changing. None when there is no store yet, which the
    layer's card turns into "Preparing...".
    """
    from plexora.server.models import bin_tiles, data_model

    manifest = bin_tiles.read_manifest(project_name, layer.id)
    if not manifest:
        return None
    stats = bin_tiles.read_stats(project_name, layer.id)
    tile_x, tile_y = (int(part) for part in
                      str(tile).replace(".png", "").split("_"))
    style = style or {}
    # `bin` is in GRID SQUARES for a bin layer (2 means 4 micron squares on a
    # 2 micron grid); for a transcript layer it is layer pixels. The layer
    # decides what its own control means.
    pooling = bin_tiles.effective_pooling(manifest, int(level), style.get("bin"))
    names = style.get("genes") or [bin_tiles.TOTAL]
    rows = bin_tiles.gene_indices(manifest, names)
    if not rows:
        rows = [int(manifest["total_index"])]
    low = style.get("dlo") or 0.0
    high = style.get("dhi")
    high = 1.0 if high is None else high
    ramp_name = style.get("ramp")
    if ramp_name and colormaps.is_ramp(ramp_name):
        rgba = bin_tiles.ramp_tile(
            project_name, layer.id, manifest, stats, int(level), tile_x, tile_y,
            genes=rows, ramp=colormaps.ramp(ramp_name), pooling=pooling,
            low=low, high=high, log=bool(style.get("log")))
    else:
        colours = style.get("colors") or [style.get("color") or (255, 255, 255)]
        groups = [(row, colours[i % len(colours)]) for i, row in enumerate(rows)]
        rgba = bin_tiles.rgb_tile(
            project_name, layer.id, manifest, stats, int(level), tile_x, tile_y,
            groups=groups, pooling=pooling, low=low, high=high,
            log=bool(style.get("log")))
    payload, mimetype = data_model.encode_tile_array(rgba, False, quality)
    return payload, mimetype, bin_tiles.revision(manifest)


def _selected_indices(stored, style):
    """The gene indices a style names, or None for "the whole panel".

    None and an empty list are different answers and the ramp path needs
    both: nothing selected means draw everything (which is what a density map
    with no gene picked is for), and names that resolved to nothing means the
    user asked for genes this panel has not got.
    """
    from plexora.server.models import transcript_tiles

    names = (style or {}).get("genes") or []
    if not names:
        return None
    return transcript_tiles.gene_indices(stored, names)


def _selection_share(stored, indices) -> float:
    """What fraction of the layer's molecules these genes are.

    One, for the whole panel. Never zero: a gene the manifest has no count
    for would otherwise divide a contrast window to nothing.
    """
    if indices is None:
        return 1.0
    counts = stored.get("gene_counts") or []
    total = float(stored.get("point_count") or 0)
    if not total:
        return 1.0
    picked = sum(float(counts[i]) for i in indices if i < len(counts))
    return max(picked / total, 1e-6)


def _density_groups(stored, style, ceiling):
    """`[(indices, rgb, hi)]` for a per-gene density request, or `[]`.

    Each group gets its own contrast window, scaled from that gene's own share
    of the layer: the ceiling stretches to a multiple of the MEAN local
    density, and the mean for one gene out of 480 is not the mean for the
    panel. Without this a rare gene drawn beside an abundant one is a black
    tile, which reads as "not expressed" rather than as "rescaled wrong".

    Takes the ceiling rather than deriving it, because it now depends on the
    bin size as well as the level and the caller is the one holding both.
    """
    names = (style or {}).get("genes") or []
    colours = (style or {}).get("colors") or []
    if not names or not colours:
        return []
    vocabulary = {name: index
                  for index, name in enumerate(stored.get("genes") or [])}
    counts = stored.get("gene_counts") or []
    total = float(stored.get("point_count") or 0) or 1.0
    window = float(ceiling)

    groups = []
    for position, name in enumerate(names):
        index = vocabulary.get(name)
        if index is None:
            continue
        rgb = colours[position % len(colours)]
        share = (float(counts[index]) / total) if index < len(counts) else 1.0
        # A float, and deliberately not floored at 1: at full zoom the mean
        # count per bin is a hundredth of a molecule, and a window of 1 is
        # what turned a density map into a field of saturated dots.
        groups.append(([index], rgb, max(window * share, 1e-6)))
    return groups


def _layer_of(project_name, layer_id):
    """(project, layer) for a tile request, or (None, None)."""
    try:
        project = Project.load(project_name)
    except KeyError:
        return None, None
    return project, (project.layer(layer_id) if project else None)


def layer_tile(project_name, layer_id, channel, level, tile, quality="fast",
               style=None):
    """One tile of one layer, encoded exactly as a channel tile is.

    @param style - `parse_style`'s dict, or None for the uncoloured uint16 a
        channel tile carries. None is what a layer with channel controls asks
        for: those tiles are colourised on the client, in the same GL pass the
        reference image's channels go through. A style is for what that pass
        does not touch -- an rgb layer, a density raster.
    @returns (payload, mimetype, etag) or None when there is nothing to serve.
    """
    from plexora.server.models import data_model

    # Cheap, and it decides which of two entirely different readers runs. A
    # points layer has no pyramid to open, so it must not reach `open_layer`
    # -- whose contract (images only) is what `test_layer_sources` pins.
    project, layer = _layer_of(project_name, layer_id)
    if layer is None:
        return None
    if layer.kind == "points":
        served = _points_tile(project_name, layer, level, tile, quality, style)
        if served is None:
            return None
        payload, mimetype = served[0], served[1]
        revision = served[2] if len(served) > 2 else None
        etag = (f'"{config_generation()}-{project_name}-{layer_id}-density-'
                f'{level}-{tile}-{quality}-{_style_key(style)}'
                f'{"-" + revision if revision else ""}"')
        return payload, mimetype, etag

    opened = open_layer(project_name, layer_id)
    if opened is None:
        return None

    channel_num, is_segmentation = data_model._parse_channel(str(channel))
    pixels = data_model.read_tile(
        opened.channels, channel_num, level, tile,
        opened.tile_width, opened.tile_height)

    qmin = qmax = None
    if not is_segmentation and channel_num is not None:
        # Through the cache, not straight at data_model: the call it wraps
        # scans every pixel of the full-resolution plane, and this runs once
        # per tile.
        window = window_of(opened, channel_num)
        if window:
            qmin, qmax = window

    if style and not is_segmentation and channel_num is not None:
        low = style["lo"] if style["lo"] is not None else (qmin or 0)
        high = style["hi"] if style["hi"] is not None else (qmax or 65535)
        span = max(1, int(high) - int(low))
        pixels = _colourise(
            data_model._quantize_to_uint8(pixels, int(low), span), style["color"])
        # Interleaved RGB from here on, which `encode_tile_array` recognises by
        # shape and sends down its brightfield branch -- no window, no second
        # quantization, and no GL pass on the other end.
        payload, mimetype = data_model.encode_tile_array(pixels, False, quality)
    else:
        payload, mimetype = data_model.encode_tile_array(
            pixels, is_segmentation, quality, qmin=qmin, qmax=qmax)
    # The config generation, not load_generation: this tile is a function of the
    # project record and the file it names, and nothing about which datasource
    # happens to be open in the viewer.
    etag = (f'"{opened.generation}-{project_name}-{layer_id}-{channel}-{level}'
            f'-{tile}-{quality}-{_style_key(style)}"')
    return payload, mimetype, etag


def _style_key(style):
    """The style as one short token for the ETag.

    In the ETag rather than left out: the same url with a different colour is a
    different picture, and a browser that reused the cached one would show the
    old colour until a reload.
    """
    if not style:
        return "plain"
    colour = "".join(f"{c:02x}" for c in style["color"])
    key = f"{colour}-{style['lo']}-{style['hi']}"
    # The gene selection and its colours are part of the picture, so they are
    # part of the identity of the tile. Hashed rather than spelled out: forty
    # gene names in an ETag is a header longer than some tiles.
    extra = (tuple(style.get("genes") or ()), tuple(style.get("colors") or ()),
             style.get("minq"), style.get("bin"), style.get("ramp"),
             style.get("dlo"), style.get("dhi"), style.get("log"))
    if any(extra):
        key += f"-{hashlib.sha1(repr(extra).encode()).hexdigest()[:12]}"
    return key
