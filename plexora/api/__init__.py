"""Plexora's plugin API -- the only surface a plugin may import.

    from plexora import api

    ds = api.dataset("my_project")
    ds.image.channel_names
    ds.table.markers
    ds.schema.x

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
    Dataset,
    DatasetSchema,
    ImageHandle,
    SegHandle,
    TableHandle,
    dataset,
)
from plexora.server.models.adapters.anndata_adapter import _deduplicate_names

#: Multiplexed panels routinely re-stain the same marker across cycles, so
#: duplicate names are ordinary rather than exceptional. Suffixes them the way
#: anndata's own var_names_make_unique() does.
deduplicate_names = _deduplicate_names

__all__ = [
    "Dataset",
    "DatasetSchema",
    "ImageHandle",
    "SegHandle",
    "TableHandle",
    "dataset",
    "deduplicate_names",
]
