"""Small z-stacks and time series in every format the volume layer reads.

Every writer fills plane (t, c, z) with `value(t, c, z, y, x)`, so a test can
say which plane a tile came from by looking at it: the thousands digit is the
channel, the hundreds the frame, the tens the plane, and a small (y + x)
ripple keeps a projection's max, mean and min apart.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import tifffile


def value(t, c, z, y, x):
    return (c + 1) * 1000 + t * 100 + z * 10 + (np.add.outer(y, x) % 7)


def stack(frames=1, channels=3, depth=5, height=96, width=80, dtype=np.uint16):
    """(T, C, Z, Y, X) filled with `value`."""
    y = np.arange(height)
    x = np.arange(width)
    out = np.empty((frames, channels, depth, height, width), dtype=dtype)
    for t in range(frames):
        for c in range(channels):
            for z in range(depth):
                out[t, c, z] = value(t, c, z, y, x)
    return out


def write_ome_volume(path, *, frames=1, channels=3, depth=5, height=96, width=80,
                     physical_z=2.0, physical_xy=0.5, levels=1, tile=None,
                     names=None, axes=None):
    """An OME-TIFF of `stack(...)`, optionally pyramidal (SubIFDs).

    Returns `(path, stack)` with the stack always (T, C, Z, Y, X), whatever
    `axes` wrote."""
    full = data = stack(frames, channels, depth, height, width)
    metadata = {"axes": "TCZYX"}
    if physical_xy:
        metadata.update(PhysicalSizeX=physical_xy, PhysicalSizeY=physical_xy)
    if physical_z:
        metadata["PhysicalSizeZ"] = physical_z
    if names:
        metadata["Channel"] = {"Name": list(names)}
    if axes == "CZYX":
        data = data[0]
        metadata["axes"] = "CZYX"
    elif axes == "ZYX":
        data = data[0, 0]
        metadata["axes"] = "ZYX"
    path = Path(path)
    kwargs = {"tile": tile} if tile else {}
    with tifffile.TiffWriter(path, bigtiff=True, ome=True) as writer:
        if levels > 1:
            writer.write(data, subifds=levels - 1, metadata=metadata, **kwargs)
            for level in range(1, levels):
                step = 2 ** level
                writer.write(data[..., ::step, ::step], subfiletype=1, **kwargs)
        else:
            writer.write(data, metadata=metadata, **kwargs)
    return path, full


def write_imagej_volume(path, *, frames=1, channels=3, depth=5, height=64, width=48,
                        spacing=2.0):
    """An ImageJ hyperstack (TZCYX on disk) of `stack(...)`."""
    data = stack(frames, channels, depth, height, width)
    ordered = np.transpose(data, (0, 2, 1, 3, 4))  # T, Z, C, Y, X
    metadata = {"axes": "TZCYX", "unit": "um"}
    if spacing:
        metadata["spacing"] = spacing
    tifffile.imwrite(path, ordered, imagej=True, metadata=metadata,
                     resolution=(2.0, 2.0))
    return Path(path), data


def write_ngff_volume(path, *, frames=1, channels=3, depth=5, height=96, width=80,
                      levels=2, z_scale=2.0, chunks_z=1, version="0.4"):
    """An OME-Zarr image of `stack(...)` with axes tczyx (or czyx at frames=1).

    Returns `(path, stack)` with the stack as written (czyx at frames=1)."""
    import zarr

    data = stack(frames, channels, depth, height, width)
    axes = "tczyx" if frames > 1 else "czyx"
    if frames == 1:
        data = data[0]
    zarr_format = 2 if str(version).startswith("0.4") else 3
    group = zarr.open_group(str(path), mode="w", zarr_format=zarr_format)
    datasets = []
    current = data
    for level in range(levels):
        chunks = [1] * (current.ndim - 2) + [min(64, current.shape[-2]),
                                             min(64, current.shape[-1])]
        chunks[axes.index("z")] = chunks_z
        array = group.create_array(str(level), shape=current.shape, dtype=current.dtype,
                                   chunks=tuple(chunks))
        array[:] = current
        step = 2 ** level
        scale = []
        for axis in axes:
            scale.append(0.5 * step if axis in "xy" else (z_scale if axis == "z" else 1.0))
        datasets.append({"path": str(level),
                         "coordinateTransformations": [{"type": "scale", "scale": scale}]})
        current = current[..., ::2, ::2]
    types = {"t": "time", "c": "channel", "z": "space", "y": "space", "x": "space"}
    axis_meta = []
    for axis in axes:
        entry = {"name": axis, "type": types[axis]}
        if axis in "xyz":
            entry["unit"] = "micrometer"
        if axis == "t":
            entry["unit"] = "second"
        axis_meta.append(entry)
    payload = {"multiscales": [{"version": str(version), "axes": axis_meta,
                                "datasets": datasets}]}
    if zarr_format == 2:
        group.attrs.update(payload)
    else:
        group.attrs.update({"ome": dict(payload, version=str(version))})
    return Path(path), data


def _attr(text):
    """IMS attributes are arrays of single bytes."""
    return np.array([bytes([b]) for b in str(text).encode()], dtype="S1")


def write_ims(path, *, frames=1, channels=2, depth=6, height=96, width=80,
              levels=2, step_xy=0.5, step_z=2.0, names=None, pad=8, compression=None,
              shuffle=False, chunks=None):
    """An Imaris 5.5 file of `stack(...)`, with chunk padding like Imaris writes."""
    import h5py

    data = stack(frames, channels, depth, height, width)
    with h5py.File(str(path), "w") as handle:
        info = handle.create_group("DataSetInfo/Image")
        for key, value_ in (("X", width), ("Y", height), ("Z", depth), ("Unit", "um"),
                            ("ExtMin0", 0), ("ExtMin1", 0), ("ExtMin2", 0),
                            ("ExtMax0", width * step_xy), ("ExtMax1", height * step_xy),
                            ("ExtMax2", depth * step_z)):
            info.attrs[key] = _attr(value_)
        for c in range(channels):
            group = handle.create_group(f"DataSetInfo/Channel {c}")
            group.attrs["Name"] = _attr((names or [f"ch{i}" for i in range(channels)])[c])
        if frames > 1:
            time = handle.create_group("DataSetInfo/TimeInfo")
            for t in range(frames):
                time.attrs[f"TimePoint{t + 1}"] = _attr(f"2024-01-01 00:00:{t * 30:02d}.000")
        current = data
        for level in range(levels):
            _, _, d, h, w = current.shape
            for t in range(frames):
                for c in range(channels):
                    group = handle.create_group(
                        f"DataSet/ResolutionLevel {level}/TimePoint {t}/Channel {c}")
                    padded = np.zeros((d, h + pad, w + pad), dtype=current.dtype)
                    padded[:, :h, :w] = current[t, c]
                    shape = chunks or (4, 32, 32)
                    group.create_dataset("Data", data=padded,
                                         chunks=(min(d, shape[0]), min(h + pad, shape[1]),
                                                 min(w + pad, shape[2])),
                                         compression=compression, shuffle=shuffle)
                    group.attrs["ImageSizeX"] = _attr(w)
                    group.attrs["ImageSizeY"] = _attr(h)
                    group.attrs["ImageSizeZ"] = _attr(d)
            current = current[..., ::2, ::2]
    return Path(path), data
