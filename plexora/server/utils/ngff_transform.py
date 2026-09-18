"""Where a layer sits, read out of what the file says.

Plexora asserted the identity everywhere. `ome_zarr.physical_metadata` read only
`type == "scale"` and threw the translation away; `spatialdata_adapter` said a
store needing a transform "would have to grow explicit support here"; the ROI
adapter hardcoded `Identity()`. For a single image that is harmless -- the image
IS the coordinate system. For a scene it is the whole problem: a morphology
image, an H&E section and a transcript table only mean anything together if each
one's own space is put into a shared one.

This module reads NGFF `coordinateTransformations` and turns them into the one
representation the rest of Plexora uses:

    [a, b, c, d, e, f]   with   x' = a*x + c*y + e
                                y' = b*x + d*y + f

Canvas/SVG order, not a numpy 2x3, because the client's hot path is
`ctx.transform(...layer.transform)` and a conversion there would run per overlay
per frame. `LayerSpec.transform` stores exactly this.

**No spatialdata import.** The coordinate systems of a SpatialData store are
plain JSON in each element's `.zattrs` / `zarr.json`, and a core build must not
grow a dependency to read JSON. `spatialdata` may well be installed -- the
adapter uses it -- but nothing here needs it, and the boundary test exists to
keep that true.

**Absent is not identity.** A layer whose transform could not be read gets None,
and the viewer says "aligned by assumption" rather than claiming a registration
nobody performed. That sentence is the first thing that makes today's silent
behaviour visible.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path

#: Six numbers that change nothing.
IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

#: How far a decomposed affine may stray from a similarity transform before the
#: viewer refuses it. OpenSeadragon's TiledImage offers x/y/width/degrees/flipped
#: -- translation, UNIFORM scale, rotation and flip -- and nothing else, so shear
#: and anisotropic scale have no way to be drawn. One part in ten thousand: a
#: rotation written out as an affine by any sane writer lands well inside it, and
#: a genuine shear is nowhere near.
TOLERANCE = 1e-4


def compose(outer: Sequence[float], inner: Sequence[float]) -> tuple[float, ...]:
    """`outer` after `inner` -- the affine that applies `inner` then `outer`.

    Matrix order, which is the order that trips people up: composing "layer to
    global" with "global to reference" means reference_from_global AFTER
    layer_to_global, so the layer's own transform is the INNER one.
    """
    a1, b1, c1, d1, e1, f1 = outer
    a2, b2, c2, d2, e2, f2 = inner
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def invert(t: Sequence[float]) -> tuple[float, ...] | None:
    """The inverse, or None where the affine collapses the plane."""
    a, b, c, d, e, f = t
    det = a * d - b * c
    if not det or not math.isfinite(det):
        return None
    return (d / det, -b / det, -c / det, a / det,
            (c * f - d * e) / det, (b * e - a * f) / det)


def apply(t: Sequence[float], x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = t
    return (a * x + c * y + e, b * x + d * y + f)


def is_identity(t: Sequence[float] | None, tol: float = 1e-9) -> bool:
    if t is None:
        return True
    return all(abs(v - w) <= tol for v, w in zip(t, IDENTITY))


# -- NGFF coordinateTransformations ----------------------------------------


def _axis_names(axes) -> list[str]:
    if not isinstance(axes, Sequence) or isinstance(axes, (str, bytes)):
        return []
    out = []
    for axis in axes:
        if isinstance(axis, Mapping):
            out.append(str(axis.get("name", "")).lower())
        else:
            out.append(str(axis).lower())
    return out


def _xy_positions(names: Sequence[str]) -> tuple[int, int] | None:
    """Where x and y sit in an axis list, or None when the store does not say.

    NGFF orders axes slowest-first (t, c, z, y, x), so a `scale` array is in that
    order too and picking the wrong entry silently scales a layer by a channel
    count. Named rather than assumed for exactly that reason.
    """
    if "x" not in names or "y" not in names:
        return None
    return names.index("x"), names.index("y")


def transform_list(transforms, names: Sequence[str]) -> tuple[float, ...]:
    """One NGFF `coordinateTransformations` list, flattened to an affine.

    Handles `scale`, `translation` and `affine`; `identity` is a no-op and
    anything else is skipped, because an unknown transform type is a claim this
    module cannot evaluate and pretending it was the identity would place the
    layer somewhere nobody asked for while reporting success.

    Applied in list order, which is what the spec says: the first entry acts on
    the array coordinates and each later one acts on the previous result.
    """
    positions = _xy_positions(names)
    if positions is None:
        return IDENTITY
    xi, yi = positions
    out = IDENTITY
    if not isinstance(transforms, Sequence) or isinstance(transforms, (str, bytes)):
        return out
    for entry in transforms:
        if not isinstance(entry, Mapping):
            continue
        kind = entry.get("type")
        if kind == "scale":
            values = entry.get("scale")
            if isinstance(values, Sequence) and len(values) == len(names):
                try:
                    sx, sy = float(values[xi]), float(values[yi])
                except (TypeError, ValueError):
                    continue
                out = compose((sx, 0.0, 0.0, sy, 0.0, 0.0), out)
        elif kind == "translation":
            values = entry.get("translation")
            if isinstance(values, Sequence) and len(values) == len(names):
                try:
                    tx, ty = float(values[xi]), float(values[yi])
                except (TypeError, ValueError):
                    continue
                out = compose((1.0, 0.0, 0.0, 1.0, tx, ty), out)
        elif kind == "affine":
            matrix = _affine_xy(entry.get("affine"), xi, yi, len(names))
            if matrix is not None:
                out = compose(matrix, out)
    return out


def _affine_xy(matrix, xi: int, yi: int, ndim: int) -> tuple[float, ...] | None:
    """The x/y block of an NGFF affine, as six numbers.

    NGFF writes an affine as `ndim` rows of `ndim + 1` columns (the homogeneous
    row is implied). Taking the x and y rows and the x, y and translation columns
    is the whole of the extraction; every other axis is left alone, which is the
    right answer for a 2-D viewer reading a 3-D or 5-D store.
    """
    if not isinstance(matrix, Sequence) or isinstance(matrix, (str, bytes)):
        return None
    rows = list(matrix)
    # Some writers flatten it. Reshape when the length says so.
    if rows and not isinstance(rows[0], Sequence):
        if len(rows) != ndim * (ndim + 1):
            return None
        rows = [rows[i * (ndim + 1):(i + 1) * (ndim + 1)] for i in range(ndim)]
    if len(rows) < max(xi, yi) + 1:
        return None
    try:
        rx = [float(v) for v in rows[xi]]
        ry = [float(v) for v in rows[yi]]
    except (TypeError, ValueError):
        return None
    if len(rx) < ndim + 1 or len(ry) < ndim + 1:
        return None
    # a = dx/dx, c = dx/dy, e = dx; b = dy/dx, d = dy/dy, f = dy
    return (rx[xi], ry[xi], rx[yi], ry[yi], rx[ndim], ry[ndim])


def element_transform(attrs: Mapping, system: str = "global") -> tuple[float, ...] | None:
    """A SpatialData element's map into one named coordinate system.

    `attrs` is the element's own `.zattrs` / `zarr.json` attributes as plain
    JSON. SpatialData records, per element, a list of transformations each naming
    the `output` coordinate system it lands in -- so this is a lookup by name,
    not an assumption that the first one is the one wanted.

    None when the element does not declare that system at all, which is
    information: it means the two are not registered against each other, and
    saying so beats returning the identity and calling them aligned.
    """
    if not isinstance(attrs, Mapping):
        return None

    # SpatialData nests its own block; a plain NGFF image does not.
    block = attrs.get("spatialdata_attrs")
    transforms = None
    if isinstance(block, Mapping):
        transforms = block.get("transform") or block.get("transformations")
    if transforms is None:
        transforms = attrs.get("coordinateTransformations")
    if transforms is None:
        multiscale = _first_multiscale(attrs)
        if multiscale is not None:
            transforms = multiscale.get("coordinateTransformations")

    names = _axis_names(_axes_of(attrs))
    if not names:
        # A points or shapes element records its axes in the spatialdata block.
        names = _axis_names(block.get("axes") if isinstance(block, Mapping) else None)
    if not names:
        # Last resort: the very common 2-D case, stated rather than guessed at
        # per call site.
        names = ["y", "x"]

    if isinstance(transforms, Mapping):
        # `{"global": [...]}` -- the shape spatialdata writes when an element
        # lands in several systems.
        entry = transforms.get(system)
        if entry is None:
            return None
        return _normalize(transform_list(_as_list(entry), names))

    if not isinstance(transforms, Sequence) or isinstance(transforms, (str, bytes)):
        return None

    # A list. Entries that name an output belong to one system each; entries
    # that name none are plain NGFF and apply unconditionally.
    named = [t for t in transforms if isinstance(t, Mapping) and t.get("output")]
    if named:
        chosen = [t for t in named if _system_name(t.get("output")) == system]
        if not chosen:
            return None
        return _normalize(transform_list(chosen, names))
    return _normalize(transform_list(transforms, names))


def _as_list(entry):
    if isinstance(entry, Mapping):
        return [entry]
    if isinstance(entry, Sequence) and not isinstance(entry, (str, bytes)):
        return list(entry)
    return []


def _system_name(output):
    if isinstance(output, Mapping):
        return output.get("name")
    return output


def _axes_of(attrs: Mapping):
    multiscale = _first_multiscale(attrs)
    if isinstance(multiscale, Mapping) and multiscale.get("axes") is not None:
        return multiscale.get("axes")
    return attrs.get("axes")


def _first_multiscale(attrs: Mapping):
    multiscales = attrs.get("multiscales")
    if isinstance(multiscales, Sequence) and multiscales and isinstance(multiscales[0], Mapping):
        return multiscales[0]
    return None


def _normalize(t: Sequence[float] | None) -> tuple[float, ...] | None:
    if t is None:
        return None
    values = tuple(float(v) for v in t)
    if not all(math.isfinite(v) for v in values):
        return None
    return values


def layer_transform(reference_attrs: Mapping, layer_attrs: Mapping,
                    system: str = "global") -> tuple[float, ...] | None:
    """Where a layer sits in the REFERENCE layer's pixel grid.

    Both elements state where they land in a shared system; the composition is
    `inverse(reference -> system)` after `layer -> system`. That is the whole of
    registration, and it is why the shared system never has to be the one the
    viewer draws in: it only has to be one both elements name.

    None when either element does not reach that system, or when the reference's
    own transform does not invert.
    """
    layer_to = element_transform(layer_attrs, system)
    ref_to = element_transform(reference_attrs, system)
    if layer_to is None or ref_to is None:
        return None
    back = invert(ref_to)
    if back is None:
        return None
    return compose(back, layer_to)


def read_attrs(path) -> dict:
    """One zarr node's attributes, v2 or v3, as plain JSON.

    Tried in the order a reader should: `zarr.json` (v3) carries them under
    `attributes`, `.zattrs` (v2) IS them. Returns {} for a node that has neither,
    which is a node with nothing to say rather than an error.
    """
    root = Path(path)
    v3 = root / "zarr.json"
    if v3.is_file():
        try:
            doc = json.loads(v3.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        attrs = doc.get("attributes")
        return dict(attrs) if isinstance(attrs, Mapping) else {}
    v2 = root / ".zattrs"
    if v2.is_file():
        try:
            doc = json.loads(v2.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return dict(doc) if isinstance(doc, Mapping) else {}
    return {}


# -- decomposition, and what OpenSeadragon can actually draw ----------------


def decompose(t: Sequence[float]) -> dict:
    """An affine as the parts a renderer has controls for.

    OpenSeadragon's TiledImage takes x, y, width, degrees and flipped -- so a
    layer can be translated, scaled uniformly, rotated and mirrored, and nothing
    else. This says what an affine asks for; `supported_by_osd` says whether the
    answer is drawable.

    `flipped` is read off a negative determinant, which is what a mirror IS. It
    is reported separately from the rotation because OSD applies it separately,
    and because a flip composed into an angle reads as a 180-degree turn of a
    mirrored image -- a different picture.
    """
    a, b, c, d, e, f = (float(v) for v in t)
    det = a * d - b * c
    flipped = det < 0
    if flipped:
        # Mirror about the y axis first, so what is left is a rotation. Undoing
        # it here is what keeps `rotation` an angle rather than an angle plus a
        # reflection nobody can separate later.
        a, b = -a, -b
    scale_x = math.hypot(a, b)
    rotation = math.degrees(math.atan2(b, a)) if scale_x else 0.0
    shear = (a * c + b * d) / (scale_x ** 2) if scale_x else 0.0
    scale_y = (a * d - b * c) / scale_x if scale_x else 0.0
    return {
        "scale_x": scale_x,
        "scale_y": scale_y,
        "rotation": rotation,
        "shear": shear,
        "translate_x": e,
        "translate_y": f,
        "flipped": flipped,
    }


def unsupported_reason(t: Sequence[float] | None, tol: float = TOLERANCE) -> str | None:
    """Why OpenSeadragon cannot draw this transform, or None when it can.

    Refused loudly at registration rather than approximated, because the failure
    mode of approximating is a layer that looks plausible and is wrong by a few
    microns everywhere -- which is exactly the error a registration exists to
    remove. The escape hatch for an anisotropic layer is to resample it on the
    way in (ome_zarr.build_extension already resamples and writes); there is no
    escape hatch for shear, and there is also no real dataset that needs one.
    """
    if t is None:
        return None
    parts = decompose(t)
    if not math.isfinite(parts["scale_x"]) or not parts["scale_x"]:
        return "degenerate"
    if abs(parts["shear"]) > tol:
        return "shear"
    ratio = abs(parts["scale_y"]) / abs(parts["scale_x"])
    if abs(ratio - 1.0) > tol:
        return "anisotropic"
    return None


def supported_by_osd(t: Sequence[float] | None, tol: float = TOLERANCE) -> bool:
    return unsupported_reason(t, tol) is None
