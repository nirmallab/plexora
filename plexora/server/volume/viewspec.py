"""Which 2-D picture of a volume is wanted: a plane, a slab projection, or a section.

One small grammar, spelled the same way everywhere a view has to be named --
a tile URL's `view=` parameter, a cache key, an ETag, the project's stored
default, a provenance record, an agent's input. There is deliberately no
second spelling: `plexora.api.representation` exposes the same thing as a
typed union for agents and converts through `token()` / `ViewSpec.parse()`.

    token := ["t" INT] ( "z" INT                     a plane            z12, t3z12
                       | "z" INT "-" INT OP         a slab projection  z4-19max
                       | "z" OP                     the whole stack    zmax
                       | "xzy" INT | "yzx" INT )    a section          xzy1234, yzx5678
           | "t" INT                                a plane of a 2-D time series
           | ""                                     the image's default view
    OP    := max | min | mean | med | std | sum | "p" NUMBER        (p95, p99.5)

**Slab bounds are inclusive at both ends**: `z8-20max` is planes 8..20, which
is what the viewer prints ("MIP z 8–20"). Section indices are in full-
resolution image pixels, like every other coordinate the viewer exchanges.

`sum` parses -- an export or a voxel tool may ask for it -- but is never served
as a tile: summed planes overflow the 16-bit tile encoders (`served`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

#: Projection operators, in the order the viewer lists them.
OPS = ("max", "mean", "min", "std", "med", "sum")
#: Operators a tile may be served for. `p<q>` percentiles are also served.
SERVED_OPS = ("max", "mean", "min", "std", "med")

#: Words the viewer prints for each operator.
OP_LABELS = {"max": "MIP", "mean": "Mean", "min": "Min", "std": "Std. dev.",
             "med": "Median", "sum": "Sum"}

KINDS = ("plane", "slab", "section")
SECTION_AXES = ("xz", "yz")

_NUMBER = r"\d+(?:\.\d+)?"
_OP = rf"(?:max|min|mean|med|std|sum|p{_NUMBER})"
_TOKEN = re.compile(
    rf"^(?:t(?P<t>\d+))?"
    rf"(?:"
    rf"z(?P<z0>\d+)-(?P<z1>\d+)(?P<op>{_OP})"
    rf"|z(?P<whole>{_OP})"
    rf"|z(?P<z>\d+)"
    rf"|(?P<axis>xz|yz)(?P<across>[yx])(?P<index>\d+)"
    rf")?$")

#: Longest token accepted, so a URL parameter cannot make the parser do work.
MAX_TOKEN = 48


class BadView(ValueError):
    """A view token that does not parse, or names planes the image lacks."""


@dataclass(frozen=True)
class ViewSpec:
    kind: str = "plane"
    t: int = 0
    z: int = 0
    z0: int | None = None
    z1: int | None = None
    op: str | None = None
    q: float | None = None
    axis: str | None = None
    index: int = 0
    #: True for the image's default view (an empty token). Not part of
    #: equality-by-meaning: `canonical()` resolves it against a volume.
    default: bool = False

    # -- parsing ---------------------------------------------------------

    @classmethod
    def parse(cls, token: Any, volume: Any = None) -> "ViewSpec":
        """The view a token names, clipped to `volume` when one is given.

        `volume` is anything with `depth`, `frames`, `width`, `height` and a
        `default_view` (a `Volume`). Without it the token is only checked for
        form. Raises BadView for anything malformed or out of range -- never
        silently substitutes another view, because a picture of the wrong
        planes captioned as the right ones is the failure this module exists
        to prevent.
        """
        text = "" if token is None else str(token).strip()
        if len(text) > MAX_TOKEN:
            raise BadView("view token is too long")
        if not text:
            if volume is not None and getattr(volume, "default_view", None) is not None:
                return volume.default_view
            return cls(default=True)
        match = _TOKEN.match(text)
        if match is None:
            raise BadView(f"{text!r} is not a view (expected e.g. z12, z4-19max, zmax, xzy1200)")
        t = int(match.group("t") or 0)
        if match.group("axis"):
            axis = match.group("axis")
            across = match.group("across")
            if (axis, across) not in (("xz", "y"), ("yz", "x")):
                raise BadView(f"{text!r}: an XZ section is placed at a y, a YZ section at an x")
            spec = cls(kind="section", t=t, axis=axis, index=int(match.group("index")))
        elif match.group("z0") is not None:
            op, q = _op(match.group("op"))
            z0, z1 = int(match.group("z0")), int(match.group("z1"))
            if z1 < z0:
                z0, z1 = z1, z0
            spec = cls(kind="slab", t=t, z0=z0, z1=z1, op=op, q=q)
        elif match.group("whole") is not None:
            op, q = _op(match.group("whole"))
            spec = cls(kind="slab", t=t, z0=0, z1=None, op=op, q=q)
        elif match.group("z") is not None:
            spec = cls(kind="plane", t=t, z=int(match.group("z")))
        else:
            # "t3" alone: a plane of a 2-D time series (or the default plane
            # of a volume at that frame, resolved against the volume below).
            spec = cls(kind="plane", t=t, z=-1)
        return spec.clipped(volume) if volume is not None else spec

    def clipped(self, volume) -> "ViewSpec":
        """This view checked against a volume's extent; BadView when outside."""
        if getattr(volume, "color", False) and self.kind != "plane":
            raise BadView("a colour slide's focal planes are shown one at a time; they are "
                          "not combined into a projection or cut into sections")
        depth = max(1, int(getattr(volume, "depth", 1) or 1))
        frames = max(1, int(getattr(volume, "frames", 1) or 1))
        if not 0 <= self.t < frames:
            raise BadView(f"t {self.t} is outside this image's {frames} frame(s)")
        if self.kind == "plane":
            z = self.z
            if z < 0:
                default = getattr(volume, "default_view", None)
                z = default.z if default is not None and default.kind == "plane" else depth // 2
            if not 0 <= z < depth:
                raise BadView(f"z {z} is outside this image's {depth} plane(s)")
            return replace(self, z=z, default=False)
        if self.kind == "slab":
            if depth < 2:
                raise BadView("a projection needs more than one plane")
            z1 = depth - 1 if self.z1 is None else self.z1
            z0 = self.z0 or 0
            if z0 >= depth:
                raise BadView(f"z {z0} is outside this image's {depth} plane(s)")
            return replace(self, z0=z0, z1=min(z1, depth - 1), default=False)
        if self.kind == "section":
            if depth < 2:
                raise BadView("a section needs more than one plane")
            limit = int(getattr(volume, "height" if self.axis == "xz" else "width", 0) or 0)
            if limit and not 0 <= self.index < limit:
                raise BadView(f"{'y' if self.axis == 'xz' else 'x'} {self.index} "
                              f"is outside the image")
            return replace(self, default=False)
        return self

    # -- spelling --------------------------------------------------------

    def token(self) -> str:
        if self.default:
            return ""
        prefix = f"t{self.t}" if self.t else ""
        if self.kind == "plane":
            return f"{prefix}z{self.z}" if self.z >= 0 else (prefix or "")
        if self.kind == "slab":
            op = self.op_token
            if self.z1 is None and not self.z0:
                return f"{prefix}z{op}"
            return f"{prefix}z{self.z0}-{self.z1}{op}"
        if self.kind == "section":
            across = "y" if self.axis == "xz" else "x"
            return f"{prefix}{self.axis}{across}{self.index}"
        raise BadView(f"unknown view kind {self.kind!r}")

    @property
    def op_token(self) -> str:
        if self.op == "pct":
            q = self.q if self.q is not None else 50.0
            return f"p{q:g}"
        return self.op or ""

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return self.token() or "<default>"

    # -- meaning ---------------------------------------------------------

    @property
    def is_plane(self) -> bool:
        return self.kind == "plane"

    @property
    def served(self) -> bool:
        """Whether a tile may be served for this view (see module docstring)."""
        if self.kind != "slab":
            return True
        return self.op == "pct" or self.op in SERVED_OPS

    @property
    def planes(self) -> tuple[int, int] | None:
        """(first, last) plane this view reads, inclusive, or None for a section."""
        if self.kind == "plane":
            return (self.z, self.z)
        if self.kind == "slab":
            return (self.z0 or 0, self.z1 if self.z1 is not None else -1)
        return None

    def window_class(self) -> str:
        """Which quantization window a tile of this view is encoded with.

        One window per channel for every plane, slab and section of a volume,
        so a pixel value means the same byte whichever plane is on screen; a
        standard-deviation projection lives in a different range and gets its
        own (see data_model.get_channel_quantization_window).
        """
        return "std" if self.kind == "slab" and self.op == "std" else "volume"

    def stats_key(self) -> str:
        """The key a channel's display statistics are cached under.

        Every plane shares one (contrast defaults are per channel, not per
        plane); each slab has its own because a projection's distribution is
        its own; sections borrow the planes'.
        """
        if self.kind == "slab":
            return self.token()
        return "plane"

    def label(self, depth: int | None = None, frames: int | None = None) -> str:
        """What the viewer and every capture caption print for this view."""
        prefix = f"t {self.t + 1}/{frames} · " if frames and frames > 1 else ""
        if self.kind == "plane":
            of = f" / {depth}" if depth else ""
            return f"{prefix}z {self.z}{of}"
        if self.kind == "slab":
            name = (f"{self.q:g}th percentile" if self.op == "pct"
                    else OP_LABELS.get(self.op or "", self.op or ""))
            last = self.z1 if self.z1 is not None else (depth - 1 if depth else "end")
            return f"{prefix}{name} z {self.z0 or 0}–{last}"
        if self.kind == "section":
            where = "y" if self.axis == "xz" else "x"
            return f"{prefix}{self.axis.upper()} @ {where} {self.index:,}"
        return prefix.rstrip(" ·")

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"kind": self.kind, "t": self.t, "token": self.token()}
        if self.kind == "plane":
            out["z"] = self.z
        elif self.kind == "slab":
            out.update(z0=self.z0, z1=self.z1, op=self.op)
            if self.op == "pct":
                out["q"] = self.q
        elif self.kind == "section":
            out.update(axis=self.axis, index=self.index)
        return out

    @classmethod
    def from_dict(cls, raw: Any, volume: Any = None) -> "ViewSpec":
        """The typed spelling (`{kind, t, z | z0,z1,op,q | axis,index}`)."""
        if isinstance(raw, str) or raw is None:
            return cls.parse(raw, volume)
        if not isinstance(raw, dict):
            raise BadView("a view is a token or an object with a kind")
        if raw.get("token") is not None and len(raw) == 1:
            return cls.parse(raw["token"], volume)
        kind = raw.get("kind", "plane")
        if kind not in KINDS:
            raise BadView(f"unknown view kind {kind!r}")
        try:
            t = int(raw.get("t") or 0)
            if kind == "plane":
                spec = cls(kind="plane", t=t, z=int(raw.get("z", -1)))
            elif kind == "slab":
                op = str(raw.get("op") or "max")
                q = raw.get("q")
                if op in ("pct", "percentile"):
                    op = "pct"
                    q = float(q if q is not None else 50.0)
                    if not 0 <= q <= 100:
                        raise BadView("a percentile is between 0 and 100")
                elif op.startswith("p") and op not in OPS:
                    op, q = _op(op)
                elif op not in OPS:
                    raise BadView(f"unknown projection {op!r}")
                z0 = int(raw.get("z0", 0) or 0)
                z1 = raw.get("z1")
                z1 = None if z1 is None else int(z1)
                if z1 is not None and z1 < z0:
                    z0, z1 = z1, z0
                spec = cls(kind="slab", t=t, z0=z0, z1=z1, op=op,
                           q=float(q) if op == "pct" else None)
            else:
                axis = str(raw.get("axis") or "")
                if axis not in SECTION_AXES:
                    raise BadView("a section's axis is xz or yz")
                spec = cls(kind="section", t=t, axis=axis, index=int(raw.get("index", 0)))
        except (TypeError, ValueError) as exc:
            if isinstance(exc, BadView):
                raise
            raise BadView(f"view: {exc}") from None
        return spec.clipped(volume) if volume is not None else spec


def _op(text: str) -> tuple[str, float | None]:
    if text.startswith("p"):
        q = float(text[1:])
        if not 0 <= q <= 100:
            raise BadView("a percentile is between 0 and 100")
        return "pct", q
    return text, None


def parse(token: Any, volume: Any = None) -> ViewSpec:
    return ViewSpec.parse(token, volume)


def canonical_token(token: Any, volume: Any = None) -> str:
    """The one spelling of `token` against `volume` (cache keys, ETags)."""
    return ViewSpec.parse(token, volume).token()
