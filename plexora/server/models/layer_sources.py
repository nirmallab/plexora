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


def layer_tile(project_name, layer_id, channel, level, tile, quality="fast"):
    """One tile of one layer, encoded exactly as a channel tile is.

    @returns (payload, mimetype, etag) or None when there is nothing to serve.
    """
    from plexora.server.models import data_model

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

    payload, mimetype = data_model.encode_tile_array(
        pixels, is_segmentation, quality, qmin=qmin, qmax=qmax)
    # The config generation, not load_generation: this tile is a function of the
    # project record and the file it names, and nothing about which datasource
    # happens to be open in the viewer.
    etag = f'"{opened.generation}-{project_name}-{layer_id}-{channel}-{level}-{tile}-{quality}"'
    return payload, mimetype, etag
