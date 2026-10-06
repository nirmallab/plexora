"""Plexora's plugin API -- the only surface a plugin may import.

    from plexora import api

    data = api.project_data("my_project")
    data.image.channel_names
    data.table.markers
    data.schema.x

`api.dataset(...)` is the same function under its old name, kept as an alias:
a Dataset in Plexora is now the folder a cohort of projects lives in
(`plexora.create_dataset`), and one word could not be both.

Anything not re-exported here is an implementation detail and will change
without notice. In particular `plexora.server.models.data_model` is off limits:
it carries mutable module-level globals under a load lock and two adjacent
loaders whose names differ by one underscore. The handles in `dataset` call the
correct one on a plugin's behalf.

First-party plugins (gating today) import from here and nowhere else. That is
deliberate -- it is what keeps this file honest, since a gap in the API becomes
a gap in the shipped product rather than something only outside authors hit.
"""

from plexora.api.dataset import (
    # The compatibility aliases, exported beside the names they alias so a
    # plugin written against either reads the same. See api/dataset.py.
    Dataset,
    DatasetSchema,
    ImageHandle,
    ImageSource,
    MetadataColumn,
    ProjectData,
    SegHandle,
    TableHandle,
    TableSource,
    dataset,
    project_data,
)
from plexora.api.http import json_response
# The descriptor a plugin declares itself with. Re-exported so a plugin's
# whole import surface is `plexora.api`; `plexora.api.plugin` stays importable
# because the bundled plugins (and plugins in the wild) import from it.
from plexora.api.plugin import (
    LAYER_SECTION_SLOT,
    NavItem,
    Plugin,
    Requirement,
    Requires,
    layer_requirement,
    normalize_shortcut,
    requirement,
)
from plexora.api.store import PluginStore, store
from plexora.server.models import manifest
from plexora.server.models.adapters.anndata_adapter import _deduplicate_names
from plexora.server.providers.base import (
    ResourceLocator,
    ResourceNotLocal,
    ResourceUnavailable,
)
from plexora.server.providers.operations import table_operation, table_stream

#: Multiplexed panels routinely re-stain the same marker across cycles, so
#: duplicate names are ordinary rather than exceptional. Suffixes them the way
#: anndata's own var_names_make_unique() does.
deduplicate_names = _deduplicate_names

def notify_viewers(project, plugin, kind, payload=None) -> int:
    """Tell every open viewer tab on a project to redraw.

    Call this from a plugin route that changed a project's state some other
    way than the tab that is asking for it -- an import finishing, a batch
    operation -- so every other open tab on that project picks up the change
    too, instead of showing stale state until its next reload.

    Args:
        project (str): The project's name.
        plugin (str): This plugin's own name, so a tab's listener can tell
            which plugin the event belongs to.
        kind (str): The event's type, in whatever vocabulary this plugin
            uses for its own events.
        payload (dict, optional): Data to deliver with the event. Must
            survive `json.dumps`. `{}` if omitted.

    Returns:
        int: How many open tabs were told.
    """
    # The tab-side listener is the browser's `plexora:agent-state-changed`
    # custom event, dispatched by its event-polling loop; a plugin's own JS
    # registers for it to know when to re-fetch and redraw.
    from plexora.server.models import dataset_events, viewer_sessions

    # `core`/`dataset.changed`: the table changed underneath -- refresh this
    # server's caches before any tab re-fetches on hearing of it.
    dataset_events.before_publish(project, plugin, kind, payload or {})
    return viewer_sessions.publish(project, plugin, kind, payload or {}, origin="server")


def layers(project, *, kind=None, modality=None) -> list:
    """Every layer of one sample's spatial scene, optionally filtered.

    The server-side counterpart of the browser's `ctx.layers.find`, and the
    same vocabulary: `kind` is the rendering strategy core owns (one of
    `"image"`, `"labels"`, `"points"`, `"shapes"`); `modality` is what the
    data MEANS, which a plugin declares for itself -- a transcripts tool asks
    for `modality="transcripts"` and does not care that it happens to be
    drawn as points.

    Includes the synthesized layers -- the reference image, the segmentation
    mask, the cell centroids -- alongside any registered ones, since "what is
    in this sample" means all of it.

    Args:
        project (Project): The project record to read layers from, e.g.
            `api.project_data(name).project`.
        kind (str, optional): Keep only layers rendered this way. None (the
            default) keeps every kind.
        modality (str, optional): Keep only layers of this modality. None
            (the default) keeps every modality.

    Returns:
        list: The matching layers, bottom of the stack first.
    """
    found = list(project.all_layers)
    if kind:
        found = [layer for layer in found if layer.kind == kind]
    if modality:
        found = [layer for layer in found if layer.modality == modality]
    return found


def layer(project, layer_id):
    """Look up one layer by id.

    Args:
        project (Project): The project record to read the layer from.
        layer_id (str): The layer's id, synthesized ids (the reference image,
            the mask, the centroids) included.

    Returns:
        The matching layer, in the same shape `layers()` returns, or None if
        this project has no layer with that id.
    """
    return project.layer(layer_id)


def sample(project) -> dict:
    """Summarize what this sample is, for a header or a panel title.

    Deliberately small and derived: a plugin that wants the layer objects
    themselves calls `layers()` instead.

    Args:
        project (Project): The project record to summarize.

    Returns:
        dict: `name`, the sorted `modalities` present across its layers,
        `bundles` (where groups of layers came from), the `reference` layer's
        id, and whether the sample is `blank` (no image file at all).
    """
    return {
        "name": project.name,
        "modalities": sorted({layer.modality for layer in project.all_layers
                              if layer.modality}),
        "bundles": [dict(bundle) for bundle in project.bundles],
        "reference": project.reference_layer.id,
        "blank": project.image.is_blank,
    }


__all__ = [
    "Dataset",
    "DatasetSchema",
    "ImageHandle",
    "ImageSource",
    "LAYER_SECTION_SLOT",
    "MetadataColumn",
    "NavItem",
    "Plugin",
    "PluginStore",
    "ProjectData",
    "Requirement",
    "Requires",
    "ResourceLocator",
    "ResourceNotLocal",
    "ResourceUnavailable",
    "SegHandle",
    "TableHandle",
    "TableSource",
    "dataset",
    "deduplicate_names",
    "json_response",
    "layer",
    "layer_requirement",
    "layers",
    "manifest",
    "normalize_shortcut",
    "notify_viewers",
    "project_data",
    "requirement",
    "sample",
    "store",
    "table_operation",
    "table_stream",
]
