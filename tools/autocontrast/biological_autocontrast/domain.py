"""IntensityDomain: what a channel's raw numbers mean, and the one transform
between raw units and the model's canonical [0, 1] space.

    x_norm = (x - offset) / (full_scale - offset)
    x_raw  = offset + x_norm * (full_scale - offset)

`full_scale` comes from metadata (effective bits) or the dtype -- never from the
observed maximum, because a dim channel's maximum says nothing about the
camera's range, and normalising by it is exactly what makes a dim channel look
bright. Floats are the exception: they have no nominal range, so theirs is
sampled and flagged with a low confidence.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

import numpy as np

from biological_autocontrast.config import DOMAIN_VERSION


@dataclass
class IntensityDomain:
    storage_dtype: str
    effective_bits: int | None
    bits_source: str  # ome:SignificantBits | tiff:BitsPerSample | user | dtype_nominal | sampled
    offset: float
    full_scale: float
    full_scale_source: str
    normalization_method: str  # fixed_scale | sampled_range
    normalization_confidence: float
    raw_min: float | None = None
    raw_max: float | None = None
    sample_level: int | None = None
    sample_fraction: float | None = None
    finite_fraction: float = 1.0
    nan_fraction: float = 0.0
    inf_fraction: float = 0.0
    saturated_fraction: float = 0.0
    zero_fraction: float = 0.0
    warnings: list = field(default_factory=list)
    version: str = DOMAIN_VERSION

    @property
    def span(self) -> float:
        return float(self.full_scale) - float(self.offset)

    def to_dict(self, prefix="") -> dict:
        out = dataclasses.asdict(self)
        out["warnings"] = list(self.warnings)
        return {f"{prefix}{k}": v for k, v in out.items()}

    @classmethod
    def from_dict(cls, data: dict, prefix="") -> "IntensityDomain":
        names = {f.name for f in dataclasses.fields(cls)}
        values = {k[len(prefix):]: v for k, v in data.items()
                  if k.startswith(prefix) and k[len(prefix):] in names}
        warnings = values.get("warnings")
        if isinstance(warnings, str):
            import json

            values["warnings"] = json.loads(warnings) if warnings else []
        elif warnings is None:
            values["warnings"] = []
        else:
            values["warnings"] = list(warnings)
        return cls(**values)


def nominal_bits(dtype) -> int | None:
    dtype = np.dtype(dtype)
    return dtype.itemsize * 8 if dtype.kind in "ui" else None


def detect(image, plane, level, *, assume_bits=None) -> IntensityDomain:
    """The domain of one channel plane. `plane` is that channel's overview
    (raw, any dtype) and only feeds the fractions, the warnings and -- for a
    float image -- the sampled range."""
    dtype = np.dtype(image.dtype)
    plane = np.asarray(plane)
    warnings = []
    if dtype.kind == "b":
        raise ValueError("boolean images have no intensity domain")

    size = max(1, plane.size)
    if dtype.kind == "f":
        finite_mask = np.isfinite(plane)
        nan_fraction = float(np.isnan(plane).sum()) / size
        inf_fraction = float(np.isinf(plane).sum()) / size
    else:
        finite_mask = None
        nan_fraction = inf_fraction = 0.0
    finite = plane[finite_mask] if finite_mask is not None else plane.ravel()
    finite_fraction = finite.size / size
    raw_min = float(finite.min()) if finite.size else None
    raw_max = float(finite.max()) if finite.size else None

    if dtype.kind in "ui":
        nominal = nominal_bits(dtype)
        bits, source, confidence = None, None, None
        if assume_bits:
            bits, source, confidence = int(assume_bits), "user", 1.0
        elif image.significant_bits and 0 < image.significant_bits <= nominal:
            bits, source, confidence = int(image.significant_bits), "ome:SignificantBits", 1.0
        elif image.bits_per_sample and 0 < image.bits_per_sample < nominal:
            bits, source, confidence = int(image.bits_per_sample), "tiff:BitsPerSample", 0.9
        else:
            bits, source, confidence = nominal, "dtype_nominal", 0.7
            if nominal > 8:
                warnings.append(f"no effective bit depth in the metadata; using the "
                                f"{dtype.name} range (0..{2 ** nominal - 1})")
        if dtype.kind == "i":
            bits = min(bits, nominal - 1)
            confidence = min(confidence, 0.6)
            if raw_min is not None and raw_min < 0:
                warnings.append("signed image with negative pixels; they display as black")
        full_scale = float(2 ** bits - 1)
        if raw_max is not None and raw_max > full_scale and source != "dtype_nominal":
            warnings.append(f"pixels reach {raw_max:g}, above the {bits}-bit range "
                            f"({source}); falling back to the dtype range")
            bits = nominal - (1 if dtype.kind == "i" else 0)
            full_scale = float(2 ** bits - 1)
            source, confidence = "dtype_nominal", 0.5
        offset = 0.0
        saturated = float((finite >= full_scale).sum()) / size if finite.size else 0.0
        zero = float((finite == 0).sum()) / size if finite.size else 0.0
        return IntensityDomain(
            storage_dtype=dtype.name, effective_bits=bits, bits_source=source,
            offset=offset, full_scale=full_scale,
            full_scale_source=f"2^{bits}-1 ({source})", normalization_method="fixed_scale",
            normalization_confidence=confidence, raw_min=raw_min, raw_max=raw_max,
            sample_level=int(level), sample_fraction=_fraction(image, level),
            finite_fraction=finite_fraction, nan_fraction=nan_fraction,
            inf_fraction=inf_fraction, saturated_fraction=saturated, zero_fraction=zero,
            warnings=warnings)

    # Float: no nominal range. Offset is where negatives start (never above 0),
    # full scale the 99.99th percentile, both sampled -- and said so.
    if finite.size:
        low, high = np.percentile(finite.astype(np.float64), [0.01, 99.99])
    else:
        low, high = 0.0, 1.0
    offset = float(min(0.0, low))
    full_scale = float(high) if high > offset else offset + 1.0
    warnings.append(f"floating-point image: its range was sampled from pyramid level "
                    f"{level}, so windows compare only roughly with other images")
    if nan_fraction or inf_fraction:
        warnings.append(f"{nan_fraction + inf_fraction:.2%} of sampled pixels are NaN/inf; "
                        "they are masked, never shown as signal")
    if offset < 0:
        warnings.append(f"negative values down to {raw_min:g}; offset set to {offset:g}")
    saturated = float((finite >= full_scale).sum()) / size if finite.size else 0.0
    zero = float((finite == 0).sum()) / size if finite.size else 0.0
    return IntensityDomain(
        storage_dtype=dtype.name, effective_bits=None, bits_source="sampled",
        offset=offset, full_scale=full_scale, full_scale_source="sampled p99.99",
        normalization_method="sampled_range", normalization_confidence=0.4,
        raw_min=raw_min, raw_max=raw_max, sample_level=int(level),
        sample_fraction=_fraction(image, level), finite_fraction=finite_fraction,
        nan_fraction=nan_fraction, inf_fraction=inf_fraction,
        saturated_fraction=saturated, zero_fraction=zero, warnings=warnings)


def _fraction(image, level) -> float:
    h0, w0 = image.level_shape(0)
    h, w = image.level_shape(level)
    return float(h * w) / float(max(1, h0 * w0))


def normalize(plane, domain: IntensityDomain):
    """(float32 canonical plane, finite mask). Non-finite pixels become 0 and are
    False in the mask; nothing is rescaled per patch or per plane."""
    plane = np.asarray(plane)
    values = (plane.astype(np.float64) - domain.offset) / domain.span
    finite = np.isfinite(values)
    values = np.where(finite, values, 0.0)
    np.clip(values, 0.0, 2.0, out=values)
    return values.astype(np.float32), finite


def to_norm(value, domain: IntensityDomain) -> float:
    return (float(value) - domain.offset) / domain.span


def to_raw(value, domain: IntensityDomain) -> float:
    return domain.offset + float(value) * domain.span
