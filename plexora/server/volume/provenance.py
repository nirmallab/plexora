"""What a 2-D picture of a volume is, attached to everything that leaves the volume layer.

A maximum projection looks like a plane. A cell that is bright in a projection
may be two cells at different depths; two markers overlapping in one may never
touch in 3-D. None of that is visible in the pixels, so it has to travel with
them: every tile, region read, capture, figure panel and agent render made
from a volume carries a `Provenance`, and the label that a person reads
("MIP z 8–20 · 13 planes") is derived from it rather than typed separately.

`exact` is False for a coarse-level projection tile, which is `op(pooled
planes)` rather than `pooled(op(planes))` -- right for display, biased low for
a max. Anything that measures asks for exact reads (level 0, or a pinned
projection), never for a display tile.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from plexora.server.volume.reduce import PERCENTILE_METHOD
from plexora.server.volume.viewspec import ViewSpec

#: The warning every derived (non-plane) picture carries, verbatim.
PROJECTION_WARNING = ("A projection combines planes: overlap in it is not "
                      "colocalization, and objects in it are not 3-D objects.")
SECTION_WARNING = ("A section is one row of every plane; its vertical axis is z, "
                   "drawn at the stated z spacing.")


@dataclass(frozen=True)
class Provenance:
    dataset: str
    view: str
    view_spec: Mapping[str, Any]
    level: int
    channels: tuple[str, ...]
    spacing: Mapping[str, Any]
    exact: bool
    produced_by: str
    label: str
    depth: int = 1
    frames: int = 1
    percentile_method: str | None = None
    warnings: tuple[str, ...] = ()
    source_key: str | None = None
    at: str = field(default_factory=lambda: _dt.datetime.now(_dt.timezone.utc)
                    .replace(microsecond=0).isoformat())

    def to_dict(self) -> dict:
        out = asdict(self)
        out["channels"] = list(self.channels)
        out["warnings"] = list(self.warnings)
        return out


def provenance_for(dataset: str, spec: ViewSpec, *, level: int = 0,
                   channels=(), volume=None, produced_by: str = "tile",
                   exact: bool | None = None, source_key: str | None = None) -> Provenance:
    depth = int(getattr(volume, "depth", 1) or 1)
    frames = int(getattr(volume, "frames", 1) or 1)
    spacing = volume.spacing.to_dict() if volume is not None else {}
    if exact is None:
        exact = spec.kind != "slab" or level == 0
    warnings = []
    if spec.kind == "slab":
        warnings.append(PROJECTION_WARNING)
    elif spec.kind == "section":
        warnings.append(SECTION_WARNING)
        z = spacing.get("z") or {}
        if z.get("source") not in ("metadata", "manual"):
            warnings.append("This image states no z spacing, so the section is "
                            "drawn one row per plane per pixel (assumed).")
    return Provenance(
        dataset=str(dataset),
        view=spec.token(),
        view_spec=spec.to_dict(),
        level=int(level),
        channels=tuple(str(c) for c in channels),
        spacing=spacing,
        exact=bool(exact),
        produced_by=produced_by,
        label=spec.label(depth, frames) if not (volume is None or volume.is_planar) else "",
        depth=depth,
        frames=frames,
        percentile_method=(PERCENTILE_METHOD
                           if spec.kind == "slab" and spec.op in ("pct", "med") else None),
        warnings=tuple(warnings),
        source_key=source_key,
    )
