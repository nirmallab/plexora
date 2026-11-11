"""Hand-written OME-Zarr stores for the tests.

Deliberately not built with `ome-zarr-py` or `spatialdata`: the point of most of
these fixtures is a store shape those writers do NOT produce -- a single level,
a non-halving pyramid, NGFF 0.4's flat `.zattrs` against 0.5's nested `"ome"`
key, dataset paths that are not "0".."n". Writing the metadata by hand is also
what keeps the reader's tests honest about the spec rather than about one
library's interpretation of it.

`test_spatialdata_image_import.py` is where a real SpatialData writer is
exercised, precisely because that one is about interoperating with it.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import zarr

#: NGFF axis types, so a fixture only has to name its axes.
_AXIS_TYPES = {"t": "time", "c": "channel", "z": "space", "y": "space", "x": "space"}


def write_ngff(path, shape=(3, 256, 256), levels=3, version="0.4", axes="cyx",
               labels=None, names=None, unit="micrometer", scale=0.325,
               dtype="uint16", factor=2, seed=0):
    """Write an OME-Zarr image and return its path.

    `version` picks the metadata layout: "0.4" writes zarr v2 with the NGFF keys
    at the top of `.zattrs`, "0.5" writes zarr v3 with them nested under `"ome"`
    in `zarr.json`. `names` overrides the array names (the reader must follow
    `datasets[].path` rather than assuming them). `factor` is the downsample
    step -- pass something other than 2 to build a pyramid the viewer's
    `size >> level` tile source cannot use. `labels` writes `omero` channel
    names; leaving it None writes no `omero` block at all.
    """
    path = Path(path)
    zarr_format = 2 if str(version).startswith("0.4") else 3
    group = zarr.open_group(str(path), mode="w", zarr_format=zarr_format)
    names = list(names or [str(i) for i in range(levels)])
    axis_index = {axis: i for i, axis in enumerate(axes)}

    rng = np.random.default_rng(seed)
    current = rng.integers(1, 4000, size=shape).astype(dtype)
    datasets = []
    for level in range(levels):
        step = factor ** level
        array = group.create_array(
            names[level], shape=current.shape, dtype=dtype,
            chunks=tuple(min(int(d), 64) for d in current.shape))
        array[:] = current
        datasets.append({
            "path": names[level],
            "coordinateTransformations": [{
                "type": "scale",
                "scale": [scale * step if axis in "xy" else 1.0 for axis in axes],
            }],
        })
        window = [slice(None)] * len(shape)
        window[axis_index["y"]] = slice(None, None, factor)
        window[axis_index["x"]] = slice(None, None, factor)
        current = current[tuple(window)]

    axis_meta = []
    for axis in axes:
        entry = {"name": axis, "type": _AXIS_TYPES[axis]}
        if axis in "xyz" and unit:
            entry["unit"] = unit
        axis_meta.append(entry)

    payload = {"multiscales": [{"version": str(version), "axes": axis_meta,
                                "datasets": datasets}]}
    if labels is not None:
        payload["omero"] = {"channels": [{"label": label} for label in labels]}

    if zarr_format == 2:
        group.attrs.update(payload)
    else:
        group.attrs.update({"ome": dict(payload, version=str(version))})
    return path


def _container_format(version):
    # Real stores are homogeneous -- SpatialData writes zarr v3 throughout,
    # bioformats2raw v2 -- so the container follows its elements' NGFF version.
    return 2 if str(version).startswith("0.4") else 3


def write_spatialdata_like(path, elements=("morphology",), version="0.4", **kwargs):
    """A store shaped like SpatialData's: image elements under `images/`.

    Enough to exercise `resolve_image_path`'s SpatialData branch without the
    dependency; the real writer is used in test_spatialdata_image_import.py.
    """
    path = Path(path)
    zarr_format = _container_format(version)
    zarr.open_group(str(path), mode="w", zarr_format=zarr_format)
    zarr.open_group(str(path / "images"), mode="w", zarr_format=zarr_format)
    for element in elements:
        write_ngff(path / "images" / element, version=version, **kwargs)
    return path


def write_bioformats2raw_like(path, series=("0",), version="0.4", **kwargs):
    """A store shaped like bioformats2raw's: numbered series groups at the root."""
    path = Path(path)
    root = zarr.open_group(str(path), mode="w",
                           zarr_format=_container_format(version))
    root.attrs.update({"bioformats2raw.layout": 3})
    for name in series:
        write_ngff(path / name, version=version, **kwargs)
    return path


def write_plate_like(path, wells=("B/2", "B/3"), fields=("0",), version="0.4",
                     **kwargs):
    """A store shaped like an HCS plate's: fields under `<row>/<col>/`.

    Two index layers -- `plate.wells[].path` at the root, `well.images[].path`
    in each well -- above ordinary multiscale images. Plates also carry
    bioformats2raw's layout stamp, which the fixture reproduces because that
    overlap is exactly what the resolver has to order its branches around.
    """
    path = Path(path)
    zarr_format = _container_format(version)
    root = zarr.open_group(str(path), mode="w", zarr_format=zarr_format)
    rows = sorted({well.split("/")[0] for well in wells})
    columns = sorted({well.split("/")[1] for well in wells})
    root.attrs.update({
        "bioformats2raw.layout": 3,
        "plate": {
            "version": str(version),
            "rows": [{"name": row} for row in rows],
            "columns": [{"name": column} for column in columns],
            "wells": [{"path": well,
                       "rowIndex": rows.index(well.split("/")[0]),
                       "columnIndex": columns.index(well.split("/")[1])}
                      for well in wells],
            "field_count": len(fields),
        },
    })
    for row in rows:
        zarr.open_group(str(path / row), mode="w", zarr_format=zarr_format)
    for well in wells:
        group = zarr.open_group(str(path / well), mode="w",
                                zarr_format=zarr_format)
        group.attrs.update({"well": {"images": [{"path": field, "acquisition": 0}
                                                for field in fields]}})
        for field in fields:
            write_ngff(path / well / field, version=version, **kwargs)
    return path


def write_legacy_v2(path, shape=(2, 64, 48), chunks=(1, 32, 32), dtype=">u1",
                    nested=False, separator_key=False, missing=(), levels=1,
                    consolidated=False, version="0.2", fill_value=0,
                    zarray_extra=None, seed=0):
    """An NGFF 0.1-0.3 image as zarr-python 2 era writers left it, written by hand.

    zarr-python 3 cannot write these (that is the point): `dtype` is written
    verbatim (">u1" is what old bioformats2raw wrote for 8-bit images),
    `nested` stores chunks as "0/0/0" the way NestedDirectoryStore did before
    zarr 2.8, and `separator_key=False` leaves `dimension_separator` out of
    `.zarray`, as those versions did. `missing` lists level-0 chunk coordinates
    left unwritten (read back as `fill_value`); `"all"` writes none.
    `zarray_extra` is merged into every `.zarray` (a key mapped to None is
    removed). Chunks are uncompressed, C order. Returns (path, level-0 pixels).
    """
    import json

    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    (path / ".zgroup").write_text(json.dumps({"zarr_format": 2}))
    rng = np.random.default_rng(seed)
    np_dtype = np.dtype(dtype)
    if np_dtype.kind == "b":
        data = rng.integers(0, 2, size=shape).astype(np_dtype)
    else:
        data = rng.integers(1, 120, size=shape).astype(np_dtype)
    current = data
    datasets = []
    consolidated_meta = {".zgroup": {"zarr_format": 2}}
    for level in range(levels):
        name = str(level)
        meta = {"zarr_format": 2, "shape": list(current.shape), "chunks": list(chunks),
                "dtype": dtype, "compressor": None, "fill_value": fill_value,
                "order": "C", "filters": None}
        if separator_key:
            meta["dimension_separator"] = "/" if nested else "."
        for key, value in (zarray_extra or {}).items():
            if value is None:
                meta.pop(key, None)
            else:
                meta[key] = value
        array_dir = path / name
        array_dir.mkdir(exist_ok=True)
        (array_dir / ".zarray").write_text(json.dumps(meta))
        consolidated_meta[f"{name}/.zarray"] = meta
        grid = [range(-(-s // c)) for s, c in zip(current.shape, chunks)]
        for coords in np.ndindex(*[len(g) for g in grid]):
            if level == 0 and (missing == "all" or tuple(coords) in set(map(tuple, missing))):
                continue
            block = np.zeros(chunks, dtype=np_dtype)
            window = tuple(slice(i * c, min((i + 1) * c, s))
                           for i, c, s in zip(coords, chunks, current.shape))
            piece = current[window]
            block[tuple(slice(0, n) for n in piece.shape)] = piece
            key = ("/" if nested else ".").join(str(i) for i in coords)
            target = array_dir / key
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(block.tobytes(order="C"))
        datasets.append({"path": name})
        current = current[..., ::2, ::2]
    axes_note = {"0.1": None, "0.2": None, "0.3": ["c", "y", "x"][-len(shape):]}
    multiscale = {"version": version, "datasets": datasets}
    if axes_note.get(version):
        multiscale["axes"] = axes_note[version]
    attrs = {"multiscales": [multiscale]}
    (path / ".zattrs").write_text(json.dumps(attrs))
    consolidated_meta[".zattrs"] = attrs
    if consolidated:
        (path / ".zmetadata").write_text(json.dumps(
            {"zarr_consolidated_format": 1, "metadata": consolidated_meta}))
    return path, data
