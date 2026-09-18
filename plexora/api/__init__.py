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
    "manifest",
    "project_data",
    "store",
    "table_operation",
    "table_stream",
]
