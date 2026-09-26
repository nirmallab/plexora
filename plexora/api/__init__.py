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
    """Tell every open viewer tab on `project` that `plugin`'s state changed.

    For a plugin route that changed state some other way than the tab that
    asked -- an import, a batch operation -- so the other tabs (and an agent
    watching) redraw instead of showing the old state. Returns how many tabs
    were told. The tab-side listener is `plexora:agent-state-changed`.
    """
    from plexora.server.models import viewer_sessions

    return viewer_sessions.publish(project, plugin, kind, payload or {}, origin="server")


def layers(project, *, kind=None, modality=None) -> list:
    """Every layer of one sample, optionally filtered.

    The server-side counterpart of `ctx.layers.find` in the browser, and the
    same vocabulary: `kind` is the rendering strategy core owns (one of
    `image`, `labels`, `points`, `shapes`) and `modality` is what the data
    MEANS, which a plugin owns. A transcripts tool asks for
    `modality="transcripts"` and does not care that it is drawn as points.

    Includes the synthesized layers -- the reference image, the mask, the
    centroids -- because a plugin asking "what is in this sample" means all of
    it, and those three are layers to everything except the storage.
    """
    found = list(project.all_layers)
    if kind:
        found = [layer for layer in found if layer.kind == kind]
    if modality:
        found = [layer for layer in found if layer.modality == modality]
    return found


def layer(project, layer_id):
    """One layer by id, synthesized ones included, or None."""
    return project.layer(layer_id)


def sample(project) -> dict:
    """What this sample is, as a plugin sees it.

    Deliberately small and derived: a plugin that wants the layer objects calls
    `layers()`, and this is the summary a panel puts in a header -- the name,
    where the data came from, and what modalities are present.
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
    "MetadataColumn",
    "PluginStore",
    "ProjectData",
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
    "layers",
    "manifest",
    "notify_viewers",
    "project_data",
    "sample",
    "store",
    "table_operation",
    "table_stream",
]
