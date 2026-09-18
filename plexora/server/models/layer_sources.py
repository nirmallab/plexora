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

import threading
from collections import OrderedDict

from plexora.server.models.project import Project, config_generation

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

    __slots__ = ("layer", "channels", "overview", "metadata", "generation")

    def __init__(self, layer, channels, overview, metadata, generation):
        self.layer = layer
        self.channels = channels
        self.overview = overview
        self.metadata = metadata
        self.generation = generation

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


def parse_style(raw):
    """A layer's colour and window from the query string, or None.

    `{color: (r, g, b), lo: int, hi: int}`. None -- no `color` given -- means
    "serve this tile the way a channel tile is served", which is the grey
    uint16 the GL colorize pass takes, and is what keeps this route's original
    behaviour byte for byte.

    A colour IS the reason this route colourises at all. A registered layer is
    NOT in the GL pipeline: that pass is keyed on `config.imageData` indices,
    which a registered layer has no entry in, so its tiles are composited by
    the browser and have to arrive already coloured. Doing it here costs one
    LUT lookup and a multiply on top of the quantization a channel tile
    already pays -- see `encode_tile_array`'s measurements.
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

    return {"color": rgb, "lo": number("lo"), "hi": number("hi")}


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
    points = float(manifest.get("point_count") or 0)
    area = float(manifest.get("width") or 0) * float(manifest.get("height") or 0)
    if points <= 0 or area <= 0:
        return 1
    per_pixel = points / area
    return max(1, int(round(DENSITY_STRETCH * per_pixel * (4 ** int(level)))))


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

    stored = transcript_tiles.read_manifest(project_name, layer.id)
    if not stored:
        return None
    tile_x, tile_y = (int(part) for part in
                      str(tile).replace(".png", "").split("_"))
    counts = transcript_tiles.density_tile(
        project_name, layer.id, int(level), tile_x, tile_y,
        tile_size=int(stored.get("tile_size") or transcript_tiles.DEFAULT_TILE_SIZE))

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
        channel tile carries. A registered layer is outside the GL colorize
        pass, so this is where its colour is applied.
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
        payload, mimetype = served
        etag = (f'"{config_generation()}-{project_name}-{layer_id}-density-'
                f'{level}-{tile}-{quality}-{_style_key(style)}"')
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
        window = data_model.quantization_window_of(opened.channels, channel_num)
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
    return f"{colour}-{style['lo']}-{style['hi']}"
