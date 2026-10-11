"""Volumes: z-stacks and time series as first-class images.

The volume is the canonical object; a plane, a slab projection and an
orthogonal section are views of it, each served through the same
`[channel, rows, cols]` pyramid contract the rest of Plexora already speaks.

    axes.py         spacing per axis, with its evidence
    viewspec.py     the one grammar naming a view (`z12`, `z4-19max`, `xzy1200`)
    model.py        `Volume` (what it is) and `VolumeSource` (how to read it)
    plane_view.py   a view as a pyramid: planes, slabs, sections
    reduce.py       projection operators
    provenance.py   what every 2-D picture of a volume carries with it
    sources/        one reader per format (TIFF, OME-Zarr, IMS, in-memory)

This package never imports `data_model`; `server/models/views.py` is where
the loaded project's volume meets the tile route.
"""

from plexora.server.volume.axes import AxisSpacing, Spacing, spacing_from
from plexora.server.volume.model import LevelInfo, Volume, VolumeSource
from plexora.server.volume.provenance import Provenance, provenance_for
from plexora.server.volume.viewspec import BadView, ViewSpec, canonical_token

__all__ = [
    "AxisSpacing",
    "BadView",
    "LevelInfo",
    "Provenance",
    "Spacing",
    "ViewSpec",
    "Volume",
    "VolumeSource",
    "canonical_token",
    "provenance_for",
    "spacing_from",
]
