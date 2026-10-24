"""Synthetic multiplex images for the tests and for smoke runs.

One canonical scene in [0, 1] (nuclei, sparse bright cells, a dim diffuse
stain, an empty channel, all inside a tissue ellipse), written at any storage
type. The uint16 twin of the uint8 file is that file x 257 exactly, so the two
must give the same canonical windows.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

NAMES = ("DNA", "CD3", "Dim", "Empty")


def scene(seed=0, size=512, n_cells=220):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    c = size / 2
    tissue = ((xx - c) / (0.42 * size)) ** 2 + ((yy - c) / (0.36 * size)) ** 2 <= 1
    out = np.zeros((4, size, size), dtype=np.float32)
    out[0] = 0.004 + 0.02 * tissue
    out[1] = 0.003 + 0.015 * tissue
    out[2] = 0.002 + 0.03 * tissue * (0.6 + 0.4 * np.sin(xx / 37.0) ** 2)
    out[3] = 0.0005
    ys, xs = np.nonzero(tissue)
    pick = rng.choice(ys.size, n_cells, replace=False)
    r = max(3, size // 90)
    for k, i in enumerate(pick):
        cy, cx = ys[i], xs[i]
        y0, y1, x0, x1 = max(0, cy - r), min(size, cy + r + 1), max(0, cx - r), min(size, cx + r + 1)
        disk = (yy[y0:y1, x0:x1] - cy) ** 2 + (xx[y0:y1, x0:x1] - cx) ** 2 <= r * r
        out[0, y0:y1, x0:x1][disk] = rng.uniform(0.25, 0.45)
        if k % 8 == 0:
            out[1, y0:y1, x0:x1][disk] = rng.uniform(0.5, 0.8)
        out[2, y0:y1, x0:x1][disk] += 0.03
    out += rng.normal(0, 0.002, out.shape).astype(np.float32)
    return np.clip(out, 0, 1), tissue


def quantize(canonical, dtype, bits):
    top = 2 ** bits - 1
    return np.clip(np.rint(canonical * top), 0, top).astype(dtype)


def write_tiff(path, planes, *, names=NAMES, levels=3, tile=(128, 128), pixel_size=0.65,
               significant_bits=None, ome=True):
    import tifffile

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"axes": "CYX" if planes.ndim == 3 else "ZCYX",
            "Channel": {"Name": list(names)[:planes.shape[-3]]}}
    if pixel_size:
        meta.update({"PhysicalSizeX": pixel_size, "PhysicalSizeXUnit": "µm",
                     "PhysicalSizeY": pixel_size, "PhysicalSizeYUnit": "µm"})
    if significant_bits:
        meta["SignificantBits"] = int(significant_bits)
    with tifffile.TiffWriter(path, ome=ome) as handle:
        handle.write(planes, subifds=levels - 1, tile=tile, metadata=meta)
        for level in range(1, levels):
            f = 2 ** level
            handle.write(planes[..., ::f, ::f], tile=tile, subfiletype=1)
    return path


def write_ngff_stack(path, planes_czyx, *, names=NAMES, levels=2, pixel_size=0.65):
    """A minimal NGFF 0.4 (zarr v2) c,z,y,x store."""
    import zarr

    path = Path(path)
    group = zarr.open_group(str(path), mode="w", zarr_format=2)
    datasets = []
    current = planes_czyx
    for level in range(levels):
        array = group.create_array(str(level), shape=current.shape, dtype=current.dtype,
                                   chunks=(1, 1, 64, 64))
        array[:] = current
        s = 2 ** level
        datasets.append({"path": str(level), "coordinateTransformations": [
            {"type": "scale", "scale": [1.0, 1.0, pixel_size * s, pixel_size * s]}]})
        current = current[..., ::2, ::2]
    group.attrs["multiscales"] = [{
        "version": "0.4", "name": path.stem,
        "axes": [{"name": "c", "type": "channel"}, {"name": "z", "type": "space", "unit": "micrometer"},
                 {"name": "y", "type": "space", "unit": "micrometer"},
                 {"name": "x", "type": "space", "unit": "micrometer"}],
        "datasets": datasets}]
    group.attrs["omero"] = {"channels": [{"label": n} for n in list(names)[:planes_czyx.shape[0]]]}
    return path


def make_all(folder, seed=0, size=512) -> dict:
    """Every fixture the tests and README name. Returns {name: path}."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    canon, _tissue = scene(seed, size)
    u8 = quantize(canon, np.uint8, 8)
    out = {
        "synth8": write_tiff(folder / "synth8.ome.tif", u8),
        "synth16": write_tiff(folder / "synth16.ome.tif", u8.astype(np.uint16) * 257),
        "synth12": write_tiff(folder / "synth12.ome.tif", quantize(canon, np.uint16, 12),
                              significant_bits=12),
        "synth14": write_tiff(folder / "synth14.ome.tif", quantize(canon, np.uint16, 14),
                              significant_bits=14),
        "synth12_nometa": write_tiff(folder / "synth12_nometa.ome.tif",
                                     quantize(canon, np.uint16, 12)),
    }
    floats = canon * 1000.0 - 100.0
    floats[1, :4, :4] = np.nan
    floats[2, -3:, -3:] = np.inf
    out["synthf"] = write_tiff(folder / "synthf.ome.tif", floats.astype(np.float32))
    dark = quantize(canon, np.uint16, 16)
    dark[1] = np.rint(canon[1] / max(canon[1].max(), 1e-6) * 350).astype(np.uint16)
    out["synth_dark"] = write_tiff(folder / "synth_dark.ome.tif", dark)
    # per-channel effective ranges in one container: ch0 12-bit, ch1 14-bit, ch2 full
    mixed = np.stack([quantize(canon[0], np.uint16, 12), quantize(canon[1], np.uint16, 14),
                      quantize(canon[2], np.uint16, 16), quantize(canon[3], np.uint16, 16)])
    out["synth_mixed"] = write_tiff(folder / "synth_mixed.ome.tif", mixed)
    blank = np.zeros_like(u8)
    blank += np.uint8(1)
    out["synth_blank"] = write_tiff(folder / "synth_blank.ome.tif", blank)
    stack = np.stack([quantize(np.clip(canon * (0.4 + 0.15 * z), 0, 1), np.uint16, 16)
                      for z in range(5)], axis=1)
    out["stack"] = write_ngff_stack(folder / "stack.zarr", stack)
    return {k: str(v) for k, v in out.items()}
