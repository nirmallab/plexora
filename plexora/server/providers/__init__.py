"""Resolving a project's three scientific resources to the things that serve them.

`resolve_providers(project)` is the one place that turns "what this project is"
into "where each piece of it lives". Everything downstream holds providers, not
paths, which is what lets a project be split across machines without any of its
readers knowing.

The default is unanimous and silent: a project with no `resources` block in its
config entry -- which is every project that existed before this package -- gets
three local providers and a `has_remote` of False, and every dispatch guard in
`data_model` short-circuits on that one boolean.
"""

from __future__ import annotations

from dataclasses import dataclass

from plexora.server.providers.base import (
    LOCAL,
    NODE,
    NODE_SCHEME,
    RESOURCE_KINDS,
    Fingerprint,
    ImageProvider,
    NodeVersionMismatch,
    ResourceError,
    ResourceLocator,
    ResourceMoved,
    ResourceNotLocal,
    ResourceUnavailable,
    SegmentationProvider,
    TableProvider,
    is_node_locator,
    node_locator,
)
from plexora.server.providers.operations import (
    UnknownOperation,
    registered_operations,
    run_table_operation,
    run_table_stream,
    table_operation,
    table_stream,
)


@dataclass(frozen=True)
class ProviderSet:
    """The three providers for one project, resolved together.

    Together rather than one at a time because the interesting questions are
    cross-resource: whether anything at all is remote (the boolean every guard
    tests), and whether the table and the mask agree about how many cells there
    are. Resolving them separately would mean three config reads per load.
    """

    table: object | None = None
    image: object | None = None
    segmentation: object | None = None

    @property
    def has_remote(self) -> bool:
        """Whether any resource is served by a node.

        This is the value that becomes `data_model._remote`, and the reason the
        single-server path costs nothing: it is False for every project that
        has no `resources` block, so no guard ever looks further.
        """
        return any(
            provider is not None and not getattr(provider, "is_local", True)
            for provider in (self.table, self.image, self.segmentation)
        )

    @property
    def remote_nodes(self) -> list[str]:
        """Which nodes this project depends on, deduplicated, in kind order."""
        seen = []
        for provider in (self.image, self.segmentation, self.table):
            if provider is None or getattr(provider, "is_local", True):
                continue
            node = provider.locator.node
            if node and node not in seen:
                seen.append(node)
        return seen

    def get(self, kind: str):
        if kind not in RESOURCE_KINDS:
            raise KeyError(f"Unknown resource kind: {kind!r}")
        return getattr(self, kind)


#: What a project with nothing resolved yet looks like. Never used as a
#: fallback for a real project -- `resolve_providers` always produces the local
#: three -- but it is the correct value for `_providers` before the first load.
EMPTY = ProviderSet()


def resolve_providers(project) -> ProviderSet:
    """Which provider serves each of this project's resources.

    Reads the project record only: no file is opened and no node is contacted,
    because this runs inside `load_datasource`'s lock and a network probe there
    would block every tile request behind a sleeping laptop. A node provider
    discovers unreachability on its first real call, which is where the caller
    can degrade rather than fail the load.
    """
    # Imported here rather than at module scope: `local` reaches back into
    # data_model for the frame computations it shares with the node side, and
    # data_model imports this package. By the time this is called, both are
    # fully initialized.
    from plexora.server.providers.local import (
        LocalImageProvider,
        LocalSegmentationProvider,
        LocalTableProvider,
    )

    bindings = getattr(project, "resources", None) or {}

    def binding(kind):
        entry = bindings.get(kind)
        return entry if entry is not None and entry.is_node else None

    image_binding = binding("image")
    seg_binding = binding("segmentation")
    table_binding = binding("table")

    if image_binding or seg_binding or table_binding:
        from plexora.server.providers.node import (
            NodeImageProvider,
            NodeSegmentationProvider,
            NodeTableProvider,
        )

    # The tile grid and the channel names are the PROJECT's, recorded centrally
    # when the image was registered. A node knows only what position a plane
    # sits at, which is exactly what lets `upload_channels` rename a whole panel
    # without the node hearing about it.
    tile_size = (project.image.tile_width or 1024, project.image.tile_height or 1024)

    if image_binding:
        image = NodeImageProvider(image_binding).with_channels(
            project.image.channel_names, *tile_size)
    else:
        image = LocalImageProvider(project.image.src)

    if seg_binding:
        segmentation = NodeSegmentationProvider(seg_binding).with_tile_size(*tile_size)
    else:
        segmentation = LocalSegmentationProvider(project.segmentation.derived)
    if table_binding:
        table = NodeTableProvider(table_binding, project.dataset)
    elif project.has_table:
        table = LocalTableProvider(project.dataset, project.name)
    else:
        table = None

    return ProviderSet(table=table, image=image, segmentation=segmentation)


__all__ = [
    "EMPTY",
    "Fingerprint",
    "ImageProvider",
    "LOCAL",
    "NODE",
    "NODE_SCHEME",
    "NodeVersionMismatch",
    "ProviderSet",
    "RESOURCE_KINDS",
    "ResourceError",
    "ResourceLocator",
    "ResourceMoved",
    "ResourceNotLocal",
    "ResourceUnavailable",
    "SegmentationProvider",
    "TableProvider",
    "UnknownOperation",
    "is_node_locator",
    "node_locator",
    "registered_operations",
    "resolve_providers",
    "run_table_operation",
    "run_table_stream",
    "table_operation",
    "table_stream",
]
