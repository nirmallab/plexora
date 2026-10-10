"""Where the loaded project's volume meets the tile route.

`data_model` holds the loaded image as module globals; `plexora.server.volume`
never imports it. This module is the seam: it resolves a `view=` token against
the loaded volume and hands back the pyramid that serves it, so the tile path,
the stats routes and the node API each make one call and change nothing else.

The default view (an empty token, or the token of the default plane) is
`data_model.channels` itself -- not a copy and not a wrapper -- which is what
keeps a planar project's tiles byte-identical and a volume's default plane
free.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

from plexora.server.models import data_model
from plexora.server.volume.plane_view import plane_pyramid
from plexora.server.volume.viewspec import BadView, ViewSpec

#: Derived (slab/section) pyramids kept built. They hold no pixels -- every
#: read is computed -- so this only saves rebuilding a handful of objects.
_MAX_VIEWS = 16

_views: OrderedDict = OrderedDict()
_lock = threading.Lock()


def current_source(datasource: str):
    """The loaded project's `VolumeSource`, or None for a planar image."""
    data_model.ensure_loaded(datasource)
    return data_model.volume_source


def is_volumetric(datasource: str) -> bool:
    source = current_source(datasource)
    return source is not None and not source.volume.is_planar


def resolve(datasource: str, token) -> ViewSpec | None:
    """The view `token` names on this project's image, or None for "the image".

    BadView for a token on an image that has one plane, a malformed token, or
    one naming planes the image does not have -- the route turns it into a
    400 rather than serving some other picture under that URL.
    """
    text = "" if token is None else str(token).strip()
    source = current_source(datasource)
    if source is None or source.volume.is_planar:
        if text:
            raise BadView(_no_views(datasource))
        return None
    return ViewSpec.parse(text, source.volume)


def _no_views(datasource: str) -> str:
    """Why this image has no views: one plane, or -- for a volume on a data
    node -- a node that could not describe it as one (see providers/node.py)."""
    providers = data_model.get_current_providers()
    refused = getattr(getattr(providers, "image", None), "views_refused", None)
    return refused or "this image has a single plane, so it has no views to choose from"


def canonical(datasource: str, token) -> str:
    """The one spelling of `token` for cache keys and ETags ("" = default)."""
    spec = resolve(datasource, token)
    if spec is None or not str(token or "").strip():
        return ""
    return spec.token()


def is_default(datasource: str, spec: ViewSpec | None) -> bool:
    if spec is None:
        return True
    source = data_model.volume_source
    return source is not None and spec == source.volume.default_view


def plane_pyramid_for(datasource: str, token):
    """The `(C, Y, X)` pyramid serving `token` (see module docstring)."""
    spec = resolve(datasource, token)
    if spec is None or is_default(datasource, spec):
        return data_model.channels
    source = data_model.volume_source
    if spec.kind == "plane":
        return source.plane_pyramid(spec.t, spec.z)
    key = (data_model.load_generation, datasource, spec.token())
    with _lock:
        cached = _views.get(key)
        if cached is not None:
            _views.move_to_end(key)
            return cached
    built = plane_pyramid(source, spec, served=True)
    with _lock:
        _views[key] = built
        _views.move_to_end(key)
        while len(_views) > _MAX_VIEWS:
            _views.popitem(last=False)
    return built


def window_class_for(datasource: str, token) -> str | None:
    """Which quantization window class a tile of `token` uses, or None for a
    planar image (whose window is the plain per-channel one)."""
    spec = resolve(datasource, token)
    if spec is None:
        if is_volumetric(datasource):
            return "volume"
        return None
    return spec.window_class()


def volume_info(datasource: str) -> dict | None:
    """The `/volume/<ds>/info` payload, or None for a planar image."""
    source = current_source(datasource)
    if source is None:
        return None
    return source.volume.to_dict()


def forget() -> None:
    with _lock:
        _views.clear()
