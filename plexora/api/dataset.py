"""The dataset a plugin is handed.

Every plugin receives image data. It may additionally receive segmentation and
a feature table (CSV or AnnData today; SpatialData later), plus metadata naming
which columns hold the cell id, image id and coordinates. Anything a plugin
needs beyond that, it must collect itself.

Two rules make this contract durable:

**Plugins read roles, never column names.** `schema.x` resolves to whatever the
import wizard recorded in `featureData[0]['xCoordinate']`. A plugin that hard-
codes `"X"` breaks on the next dataset; one that reads `schema.x` does not.

**Plugins never touch data_model directly.** This module does, and it is core
code, so it is free to. That inversion is the whole point: `data_model` holds
mutable module-level globals mutated under a load lock, with two confusingly
adjacent loaders -- `_ensure_loaded()` warms the feature table/BallTree while
`ensure_loaded()` warms the image pyramid and returns load_generation. Handing
that surface to third parties would freeze it forever and invite the exact race
its own comments warn about. Handles below call the right one and expose
neither.

Adding SpatialData means adding an adapter under server/models/adapters/ that
produces the same NormalizedDatasource. No plugin learns it happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from plexora import get_config
from plexora.server.models import data_model

# featureData[0] keys the import wizard writes today, in role order. image_id is
# deliberately absent: nothing in the upload form collects it yet (gating asks
# the user to type a column name at AnnData-save time), so it resolves to None
# until the wizard grows the field. Plugins must treat it as optional.
_ROLE_KEYS = {
    "cell_id": ("idField",),
    "x": ("xCoordinate",),
    "y": ("yCoordinate",),
    "celltype": ("celltype",),
    "image_id": ("imageId", "imageid", "image_id"),
}


@dataclass(frozen=True)
class DatasetSchema:
    """Role -> column name, resolved from the import wizard's own record.

    Cheap and load-free: this reads config only, never the table. Marker
    discovery needs the data itself and lives on TableHandle.markers.
    """

    cell_id: str | None = None
    x: str | None = None
    y: str | None = None
    celltype: str | None = None
    image_id: str | None = None
    #: Roles added to the contract later land here rather than forcing a
    #: dataclass change that would break every plugin's constructor call.
    extra: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_config(cls, entry: Mapping[str, Any]) -> "DatasetSchema | None":
        feature_data = (entry or {}).get("featureData") or []
        if not feature_data:
            return None
        spec = feature_data[0] or {}
        resolved = {}
        for role, keys in _ROLE_KEYS.items():
            resolved[role] = next((spec[k] for k in keys if spec.get(k)), None)
        known = {k for keys in _ROLE_KEYS.values() for k in keys}
        extra = {k: v for k, v in spec.items() if k not in known and isinstance(v, str)}
        return cls(**resolved, extra=extra)


class ImageHandle:
    """The one input every plugin is guaranteed."""

    def __init__(self, name: str, entry: Mapping[str, Any]):
        self._name = name
        self._entry = entry or {}

    @property
    def channels(self) -> list[dict]:
        """Real image channels, excluding the 'Area' placeholder that only
        exists when segmentation was registered."""
        return [c for c in self._entry.get("imageData", []) if c.get("fullname") != "Area"]

    @property
    def channel_names(self) -> list[str]:
        return [c["fullname"] for c in self.channels]

    @property
    def kind(self) -> str | None:
        return self._entry.get("image_kind")

    @property
    def size(self) -> tuple[int | None, int | None]:
        return self._entry.get("width"), self._entry.get("height")

    @property
    def max_level(self) -> int | None:
        return self._entry.get("maxLevel")

    @property
    def tile_size(self) -> tuple[int | None, int | None]:
        return self._entry.get("tileWidth"), self._entry.get("tileHeight")

    def stats(self, channel: str) -> dict:
        """Per-channel intensity statistics, including the vmin/vmax hints the
        viewer uses for immediate display before the full GMM fit lands."""
        return data_model.get_image_channel_stats(channel, self._name)

    def quantization_window(self, channel: str) -> tuple:
        """(qmin, qmax) from FULL-RESOLUTION data. Deliberately split from the
        GMM fit so callers that only need the byte-domain window do not pay the
        ~1 s GaussianMixture cost."""
        return data_model.get_channel_quantization_window(channel, self._name)


class SegHandle:
    """Segmentation mask, when the project has one."""

    def __init__(self, name: str, entry: Mapping[str, Any]):
        self._name = name
        self._entry = entry or {}

    @property
    def available(self) -> bool:
        return bool(self._entry.get("segmentation"))

    @property
    def pending(self) -> bool:
        """True while the background outline-generation job is still running."""
        return self._entry.get("segmentation_status") == "pending"

    def centroid_manifest(self) -> dict:
        return data_model.get_centroid_manifest(self._name)

    def centroid_tiles(self, level, tiles, gates=None, max_points=None):
        return data_model.get_centroid_tiles(self._name, level, tiles, gates, max_points)


class TableHandle:
    """The feature table, whatever it was imported from.

    Every method warms the table first, so callers never reason about load
    order or touch data_model's globals.
    """

    def __init__(self, name: str, entry: Mapping[str, Any]):
        self._name = name
        self._entry = entry or {}

    @property
    def available(self) -> bool:
        return bool(self._entry.get("has_feature_data", True)) and bool(
            self._entry.get("featureData")
        )

    @property
    def source_kind(self) -> str:
        """'csv' when unset -- the format every datasource used before the
        adapter dispatch existed."""
        return self._entry.get("data_type", "csv")

    def frame(self):
        """The whole table as a polars DataFrame (None if this project has no
        feature data)."""
        data_model._ensure_loaded(self._name)
        return data_model.get_datasource_df()

    def describe(self) -> dict:
        """Per-column summary stats plus a 50-bin histogram. Cached per
        datasource by data_model."""
        return data_model.get_datasource_description(self._name)

    @property
    def markers(self) -> list[str]:
        """Columns a plugin can meaningfully threshold or plot: the numeric
        feature columns for which a value histogram could actually be built.

        A structural channel like DNA is commonly a real image channel with no
        feature column, so this is NOT the same list as image.channel_names --
        conflating the two is a long-standing source of bugs here.
        """
        description = self.describe()
        return [name for name, info in description.items() if info.get("histogram")]

    def columns(self, names) -> dict:
        """Numeric numpy views of the named columns, cached one set at a time
        so repeated range queries reuse the same arrays."""
        data_model._ensure_loaded(self._name)
        return data_model.get_filter_columns(self._name, list(names))

    def range_mask(self, gates: Mapping[str, tuple], mode: str = "and"):
        """Boolean mask over rows for {column: (low, high)} ranges. `mode` is
        'and' (every gate must match) or 'or' (any)."""
        return data_model.apply_range_mask(self.columns(gates.keys()), gates, mode)

    def ids_matching(self, gates: Mapping[str, tuple], mode: str = "and") -> list:
        """Cell ids whose rows satisfy the gates, in table order."""
        frame = self.frame()
        if frame is None or not gates:
            return []
        return frame["id"].to_numpy()[self.range_mask(gates, mode)].tolist()


@dataclass(frozen=True)
class Dataset:
    """Everything the host offers a plugin about one project."""

    name: str
    image: ImageHandle
    segmentation: SegHandle
    table: TableHandle
    schema: DatasetSchema | None
    config: Mapping[str, Any]

    @property
    def source_kind(self) -> str | None:
        return self.table.source_kind if self.table.available else None

    def cached(self, key, compute):
        """Memoize an expensive derived value against this datasource.

        Entries are dropped when the datasource reloads, so a plugin cannot
        serve a result derived from data that has since changed underneath it.
        Intended for genuinely costly work -- a mixture-model fit, a spatial
        index -- not for ordinary lookups.

        `key` is namespaced per datasource here, so plugins do not have to
        remember to include the project name and cannot collide across
        projects.
        """
        return data_model.gmm_cache_get_or_set((self.name, key), compute)


def dataset(name: str) -> Dataset:
    """Build the handle set for a datasource. Raises KeyError if unknown.

    Construction is cheap -- it reads config only. Nothing is loaded from disk
    until a handle method is actually called.
    """
    entry = get_config().get(name)
    if entry is None:
        raise KeyError(f"Unknown datasource: {name!r}")
    return Dataset(
        name=name,
        image=ImageHandle(name, entry),
        segmentation=SegHandle(name, entry),
        table=TableHandle(name, entry),
        schema=DatasetSchema.from_config(entry),
        config=entry,
    )
