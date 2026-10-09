"""Physical spacing along each axis, and where each number came from.

A volume's z spacing is the one fact every 3-D measurement depends on and the
one most often missing: a confocal OME-TIFF states it, a Fiji stack may or may
not, a folder of PNG planes never does. So spacing is carried with its
*evidence* -- `source` -- and every consumer that needs a physical z asks
`volumetric_ok()` rather than reading a number that might be a default.

The rule is the one `project.normalize_pixel_size` states for x/y: a missing
calibration is never replaced by a conventional value. `assumed` exists only
for the one place a unitless ratio is still useful -- drawing a section pane
proportionally -- and it says so wherever it is shown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

#: Canonical axis order, OME-NGFF's: time, channel, then space slowest first.
CANONICAL = ("t", "c", "z", "y", "x")

#: Where a spacing value came from, strongest evidence first.
#:
#: - metadata: the file stated it (OME PhysicalSizeZ, ImageJ `spacing`, an
#:   NGFF `scale`, DICOM DistanceBetweenFocalPlanes, IMS extents).
#: - manual: somebody typed it for this project.
#: - assumed: nothing stated it and a ratio of 1 is being used for DISPLAY
#:   only (a section pane's proportions). Never used for a measurement.
#: - missing: nothing stated it and nothing is being assumed.
SPACING_SOURCES = ("metadata", "manual", "assumed", "missing")

#: Sources a physical measurement may be computed from.
TRUSTED_SOURCES = ("metadata", "manual")

DEFAULT_UNIT = "µm"

#: Unit spellings files use, normalised to the one the viewer prints.
_UNIT_ALIASES = {
    "um": "µm", "µm": "µm", "μm": "µm", "micron": "µm", "microns": "µm",
    "micrometer": "µm", "micrometre": "µm", "micrometers": "µm",
    "micrometres": "µm", "nm": "nm", "nanometer": "nm", "nanometre": "nm",
    "mm": "mm", "millimeter": "mm", "millimetre": "mm",
    "s": "s", "sec": "s", "second": "s", "seconds": "s",
    "ms": "ms", "millisecond": "ms", "min": "min", "minute": "min",
    "minutes": "min", "h": "h", "hour": "h", "hours": "h",
}

#: How many microns one of each length unit is.
_MICRONS = {"µm": 1.0, "nm": 1e-3, "mm": 1e3}


def normalize_unit(unit: Any, default: str = DEFAULT_UNIT) -> str:
    text = str(unit or "").strip()
    if not text:
        return default
    return _UNIT_ALIASES.get(text.lower(), _UNIT_ALIASES.get(text, text))


def to_microns(value: float, unit: str) -> float | None:
    """`value` in microns, or None for a unit that is not a length."""
    factor = _MICRONS.get(normalize_unit(unit))
    return None if factor is None else float(value) * factor


@dataclass(frozen=True)
class AxisSpacing:
    """One axis's step: a value, its unit, and the evidence for it."""

    value: float | None = None
    unit: str = DEFAULT_UNIT
    source: str = "missing"

    @property
    def known(self) -> bool:
        return self.value is not None and self.value > 0 and self.source in TRUSTED_SOURCES

    def microns(self) -> float | None:
        if self.value is None or not self.value > 0:
            return None
        return to_microns(self.value, self.unit)

    def to_dict(self) -> dict:
        return {"value": self.value, "unit": self.unit, "source": self.source}

    @classmethod
    def from_mapping(cls, raw: Any, *, default_source: str = "manual") -> "AxisSpacing | None":
        """The `{value, unit, source}` a project stores, or None when unusable."""
        if not isinstance(raw, Mapping):
            return None
        try:
            value = float(raw.get("value"))
        except (TypeError, ValueError):
            return None
        if not value > 0:
            return None
        source = str(raw.get("source") or "").strip()
        if source not in SPACING_SOURCES or source in ("assumed", "missing"):
            source = default_source
        return cls(value=value, unit=normalize_unit(raw.get("unit")), source=source)


MISSING = AxisSpacing()


@dataclass(frozen=True)
class Spacing:
    """Spacing along x, y, z (and t, for a time series)."""

    x: AxisSpacing = MISSING
    y: AxisSpacing = MISSING
    z: AxisSpacing = MISSING
    t: AxisSpacing | None = None

    def volumetric_ok(self) -> bool:
        """Whether a physical 3-D measurement may be made: x, y and z all
        stated or typed, and all lengths."""
        return all(axis.known and axis.microns() is not None
                   for axis in (self.x, self.y, self.z))

    def anisotropy(self) -> float:
        """z step over x step, in the same unit -- 1.0 when either is unknown.

        For DRAWING only (a section pane's row height). The 1.0 fallback is
        what `assumed` means; `anisotropy_source` says which it was.
        """
        xs, zs = self.x.microns(), self.z.microns()
        if xs and zs:
            return zs / xs
        return 1.0

    @property
    def anisotropy_source(self) -> str:
        if self.x.microns() and self.z.microns():
            # The weaker of the two pieces of evidence is the evidence.
            return max((self.x.source, self.z.source),
                       key=lambda s: SPACING_SOURCES.index(s))
        return "assumed"

    def to_dict(self) -> dict:
        out = {"x": self.x.to_dict(), "y": self.y.to_dict(), "z": self.z.to_dict()}
        if self.t is not None:
            out["t"] = self.t.to_dict()
        return out


def spacing_from(physical: Mapping[str, Any] | None, *, pixel_size: Mapping | None = None,
                 pixel_size_z: Mapping | None = None,
                 frame_interval: Mapping | None = None,
                 depth: int = 1, frames: int = 1) -> Spacing:
    """The effective spacing: what the project says over what the file says.

    `physical` is the reader's metadata payload (`physical_size_x/_y/_z` with
    `_unit` siblings, `time_increment`); `pixel_size`/`pixel_size_z`/
    `frame_interval` are the project's own `{value, unit, source}` records.
    A project value wins because typing it is what overriding means -- the
    same rule `data_model._with_pixel_size` applies to x/y.
    """
    physical = physical or {}

    def stated(key):
        try:
            value = float(physical.get(key))
        except (TypeError, ValueError):
            return None
        if not value > 0:
            return None
        return AxisSpacing(value=value, unit=normalize_unit(physical.get(f"{key}_unit")),
                           source="metadata")

    manual_xy = AxisSpacing.from_mapping(pixel_size)
    x = manual_xy or stated("physical_size_x") or MISSING
    y = manual_xy or stated("physical_size_y") or (x if x.source != "missing" else MISSING)
    z = AxisSpacing.from_mapping(pixel_size_z) or stated("physical_size_z")
    if z is None:
        z = MISSING
    t = None
    if frames > 1:
        t = AxisSpacing.from_mapping(frame_interval)
        if t is None:
            try:
                value = float(physical.get("time_increment"))
            except (TypeError, ValueError):
                value = 0.0
            t = (AxisSpacing(value=value,
                             unit=normalize_unit(physical.get("time_increment_unit"), "s"),
                             source="metadata")
                 if value > 0 else AxisSpacing(unit="s"))
    if depth <= 1 and z.source == "missing":
        # A single plane has no z step to be missing.
        z = MISSING
    return Spacing(x=x, y=y, z=z, t=t)


@dataclass(frozen=True)
class Axes:
    """The axes a volume HAS, in canonical order (a subset of t,c,z,y,x)."""

    names: tuple[str, ...] = ("c", "y", "x")
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        order = [name for name in CANONICAL if name in self.names]
        if tuple(order) != tuple(self.names):
            raise ValueError(f"axes {self.names!r} are not in t,c,z,y,x order")

    @property
    def spelled(self) -> str:
        return "".join(self.names)

    @classmethod
    def of(cls, depth: int, frames: int, channels: int = 1) -> "Axes":
        names = []
        if frames > 1:
            names.append("t")
        names.append("c")
        if depth > 1:
            names.append("z")
        names += ["y", "x"]
        return cls(tuple(names))
