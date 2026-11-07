"""The website's 3-D work against the desktop's: fixtures, operations, comparison.

    python tests/browser/volume_web.py make DIR [--scale test|bench]
    python tests/browser/volume_web.py ops NAME [--iso V] [--mask]  (PLEXORA_DATA_PATH set)
    python tests/browser/volume_web.py compare WEBSITE_DIR DESKTOP_DIR

- `make`: z-stacks as a user brings them -- an OME-TIFF stack (pyramidal,
  tiled, deflated) with a 3-D label mask beside it, an OME-Zarr folder, and
  Imaris files written as Imaris 10 does (multi-level, coarse levels halving
  z, chunk-padded, odd sizes): deflate uint16, LZ4 uint16, LZ4 uint8 (LZ4
  through hdf5plugin, which only this side has).
- `ops`: the 3-D operations no route runs synchronously -- an isosurface and
  the cell surfaces of a mask (surface/store.py's extraction, nets and mesh),
  and measuring the mask's objects (volume/objects.py) -- each timed, with a
  sha256 of what it made. The same source runs in the website's Python
  (tests/browser/volume.mjs sends `OPS` through `PlexoraPython.run`), here
  with numba, and here with PLEXORA_NO_NUMBA=1 (the Python the kernels fall
  back to, which is what the website runs until plan L5).
- `compare`: what tests/browser/volume.mjs saved of each arm's answers, URL
  by URL: the same bytes, or -- for a PNG tile, whose deflate stream differs
  between zlib-ng and zlib -- the same pixels; a JSON answer with the picked
  files' paths reduced to their names.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]

#: (channels, depth, height, width) per scale. Odd sizes throughout: no level
#: divides evenly, and Imaris pads every dataset to its chunks.
SIZES = {"test": (2, 19, 301, 257), "mid": (2, 32, 511, 509), "bench": (2, 64, 1023, 997)}
IMS_CHUNKS = {"test": (8, 64, 64), "mid": (16, 128, 128), "bench": (16, 128, 128)}


def _attr(text):
    return np.array([bytes([b]) for b in str(text).encode()], dtype="S1")


def cells(channels, depth, height, width, *, seed=5, count=None):
    """(C, Z, Y, X) uint16: blurred ellipsoid cells (a nucleus, a membrane
    shell, a marker on some), with noise -- texture a codec has to work on.
    And the (Z, Y, X) uint16 label mask of the nuclei."""
    from scipy import ndimage

    rng = np.random.default_rng(seed)
    count = count or max(8, int(height * width * depth / 60000))
    vol = np.zeros((3, depth, height, width), np.float32)
    labels = np.zeros((depth, height, width), np.uint16)
    for label in range(1, count + 1):
        cz, cy, cx = rng.uniform(0, depth), rng.uniform(0, height), rng.uniform(0, width)
        rz, ry, rx = rng.uniform(2.5, 5.0), rng.uniform(8, 16), rng.uniform(8, 16)
        z0, z1 = int(max(0, cz - rz - 1)), int(min(depth, cz + rz + 2))
        y0, y1 = int(max(0, cy - ry - 4)), int(min(height, cy + ry + 5))
        x0, x1 = int(max(0, cx - rx - 4)), int(min(width, cx + rx + 5))
        if z1 <= z0 or y1 <= y0 or x1 <= x0:
            continue
        gz, gy, gx = np.mgrid[z0:z1, y0:y1, x0:x1]
        d = ((gz - cz) / rz) ** 2 + ((gy - cy) / ry) ** 2 + ((gx - cx) / rx) ** 2
        inside = d <= 1
        vol[0, z0:z1, y0:y1, x0:x1] += inside * rng.uniform(2500, 6000)
        vol[1, z0:z1, y0:y1, x0:x1] += ((d > 1) & (d <= 1.6)) * rng.uniform(800, 2500)
        if rng.random() < 0.3:
            vol[2, z0:z1, y0:y1, x0:x1] += (d <= 1.4) * rng.uniform(1500, 5000)
        block = labels[z0:z1, y0:y1, x0:x1]
        block[inside & (block == 0)] = label
    for c in range(3):
        vol[c] = ndimage.gaussian_filter(vol[c], (1.0, 1.5, 1.5))
    vol += rng.normal(150, 30, vol.shape).astype(np.float32)
    return np.clip(vol[:channels], 0, 65535).astype(np.uint16), labels


def write_ome_tiff(path, data, *, levels=2, tile=128, names=None, z_um=2.0):
    import tifffile

    md = {"axes": "CZYX", "PhysicalSizeX": 0.5, "PhysicalSizeY": 0.5, "PhysicalSizeZ": z_um,
          "Channel": {"Name": list(names or [f"ch{c}" for c in range(data.shape[0])])}}
    with tifffile.TiffWriter(path, bigtiff=True, ome=True) as writer:
        writer.write(data, subifds=levels - 1, tile=(tile, tile), metadata=md, compression="zlib")
        for level in range(1, levels):
            step = 2 ** level
            writer.write(data[..., ::step, ::step], subfiletype=1, tile=(tile, tile),
                         compression="zlib")
    return path


def write_labels(path, labels):
    import tifffile

    tifffile.imwrite(path, labels, compression="zlib", metadata={"axes": "ZYX"})
    return path


def write_ome_zarr(path, data, *, levels=3, chunks=(1, 8, 64, 64), names=None, z_um=2.0):
    """An OME-Zarr 0.4 (zarr v2) image, (c, z, y, x), XY halving per level."""
    import zarr

    group = zarr.open_group(str(path), mode="w", zarr_format=2)
    datasets = []
    current = data
    for level in range(levels):
        level_chunks = tuple(min(c, s) for c, s in zip(chunks, current.shape))
        array = group.create_array(str(level), shape=current.shape, dtype=current.dtype,
                                   chunks=level_chunks)
        array[:] = current
        step = 2 ** level
        datasets.append({"path": str(level), "coordinateTransformations": [
            {"type": "scale", "scale": [1.0, z_um, 0.5 * step, 0.5 * step]}]})
        current = current[..., ::2, ::2]
    # Written as a file: zarr's attribute update replaces .zattrs twice at
    # once, which Windows refuses ("Access is denied").
    attrs = {
        "multiscales": [{"version": "0.4", "name": Path(path).stem, "datasets": datasets,
                         "axes": [{"name": "c", "type": "channel"},
                                  {"name": "z", "type": "space", "unit": "micrometer"},
                                  {"name": "y", "type": "space", "unit": "micrometer"},
                                  {"name": "x", "type": "space", "unit": "micrometer"}]}],
        "omero": {"channels": [{"label": n, "color": "FFFFFF", "active": True,
                                "window": {"start": 0, "end": 6000, "min": 0, "max": 65535}}
                               for n in (names or [f"ch{c}" for c in range(data.shape[0])])]},
    }
    (Path(path) / ".zattrs").write_text(json.dumps(attrs), encoding="utf-8", newline="\n")
    return path


def write_ims(path, data, *, compression, shuffle=False, chunks=(8, 64, 64), names=None,
              z_um=2.0, min_side=100):
    """An Imaris 5.5 file as Imaris 10 writes one: every level padded to its
    chunks, coarse levels halving XY (and Z once the voxels are about
    isotropic), until a side falls under `min_side`. `data` is (C, Z, Y, X)."""
    import h5py

    channels, depth, height, width = data.shape
    with h5py.File(str(path), "w") as handle:
        info = handle.create_group("DataSetInfo/Image")
        for key, value in (("X", width), ("Y", height), ("Z", depth), ("Unit", "um"),
                           ("ExtMin0", 0), ("ExtMin1", 0), ("ExtMin2", 0),
                           ("ExtMax0", width * 0.5), ("ExtMax1", height * 0.5),
                           ("ExtMax2", depth * z_um)):
            info.attrs[key] = _attr(value)
        for c in range(channels):
            handle.create_group(f"DataSetInfo/Channel {c}").attrs["Name"] = _attr(
                (names or [f"ch{i}" for i in range(channels)])[c])
        current, level, step_z, step_xy = data, 0, z_um, 0.5
        while True:
            _, d, h, w = current.shape
            for c in range(channels):
                group = handle.create_group(f"DataSet/ResolutionLevel {level}/TimePoint 0/Channel {c}")
                cz, cy, cx = (min(d, chunks[0]), min(h, chunks[1]), min(w, chunks[2]))
                padded = np.zeros((-(-d // cz) * cz, -(-h // cy) * cy, -(-w // cx) * cx),
                                  current.dtype)
                padded[:d, :h, :w] = current[c]
                group.create_dataset("Data", data=padded, chunks=(cz, cy, cx),
                                     compression=compression, shuffle=shuffle)
                for key, value in (("ImageSizeX", w), ("ImageSizeY", h), ("ImageSizeZ", d)):
                    group.attrs[key] = _attr(value)
            if min(h, w) // 2 < min_side:
                break
            current = current[..., ::2, ::2]
            step_xy *= 2
            if step_xy >= step_z and current.shape[1] > 1:
                current = current[:, ::2]
                step_z *= 2
            level += 1
    return path


def make(out, scale="test") -> dict:
    """Every fixture under `out`; returns {name: path}."""
    import hdf5plugin

    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    channels, depth, height, width = SIZES[scale]
    data, labels = cells(channels, depth, height, width)
    names = ["DNA", "Membrane", "Marker"][:channels]
    made = {"stack": write_ome_tiff(out / "stack.ome.tif", data, names=names,
                                    levels=2 if scale == "test" else 3),
            "mask": write_labels(out / "stack_labels.tif", labels),
            "zarr": write_ome_zarr(out / "stack.ome.zarr", data, names=names)}
    chunks = IMS_CHUNKS[scale]
    min_side = 64 if scale == "test" else 128
    made["ims_deflate"] = write_ims(out / "deflate_u16.ims", data, compression="gzip",
                                    shuffle=True, chunks=chunks, names=names, min_side=min_side)
    made["ims_lz4"] = write_ims(out / "lz4_u16.ims", data, compression=hdf5plugin.LZ4(),
                                chunks=chunks, names=names, min_side=min_side)
    made["ims_lz4_u8"] = write_ims(out / "lz4_u8.ims", (data >> 5).clip(0, 255).astype(np.uint8),
                                   compression=hdf5plugin.LZ4(), chunks=chunks, names=names,
                                   min_side=min_side)
    return {key: str(value) for key, value in made.items()}


# -- the operations, in either Python -------------------------------------------
#
# `OPS` is sent as source to the website's Python; keep it self-contained.

OPS = r'''
def volume_ops(name, iso=None, mask=False, objects=False):
    """The 3-D operations on project `name`: each one's ms and a sha256 of
    what it made. Nothing here goes through a route: these are the functions
    the routes' background jobs run (`@pro` guards aside)."""
    import hashlib, json, time
    import numpy as np
    from plexora.server.models.project import Project
    from plexora.server.surface import store, mesh as mesh_mod
    from plexora.server.utils import jit

    def sha(*arrays):
        digest = hashlib.sha256()
        for array in arrays:
            array = np.ascontiguousarray(array)
            digest.update(str(array.dtype).encode() + str(array.shape).encode())
            digest.update(array.tobytes())
        return digest.hexdigest()

    def mesh_sha(surface):
        # The topology (integers) and the vertex positions (floats) apart: a
        # float rounded otherwise in wasm moves one hash, not both.
        return {"topology": sha(surface.triangles, surface.object_id),
                "positions": sha(surface.positions),
                "vertices": int(surface.n_vertices)}

    # Time spent in each compiled kernel (plan L5's ports), measured at the
    # kernels Python calls -- never one a kernel calls, which numba resolves
    # at compile time.
    import importlib
    entries = {"plexora.server.surface.mesh": ("_face_normals_into", "_adjacency", "_umbrella_step",
                                               "_signed_volumes", "_object_sums", "_object_moments"),
               "plexora.server.surface.nets": ("_pool_runs", "_count", "_fill", "_triangulate"),
               "plexora.server.volume.measure": ("_bbox_kernel",)}
    spent, originals = {}, []

    def timer(key, fn):
        def timed(*args, **kwargs):
            started = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                spent[key] = spent.get(key, 0.0) + (time.perf_counter() - started) * 1000
        return timed

    for module_name, kernels in entries.items():
        module = importlib.import_module(module_name)
        for kernel in kernels:
            originals.append((module, kernel, getattr(module, kernel)))
            setattr(module, kernel, timer(f"{module_name.rsplit('.', 1)[1]}.{kernel}",
                                          getattr(module, kernel)))

    def kernels():
        taken = {k: round(v, 1) for k, v in sorted(spent.items(), key=lambda kv: -kv[1])}
        spent.clear()
        return taken

    out = {"numba": jit.enabled()}
    try:
        return _volume_ops(out, name, iso, mask, objects, kernels, sha, mesh_sha)
    finally:
        for module, kernel, fn in originals:
            setattr(module, kernel, fn)


def _volume_ops(out, name, iso, mask, objects, kernels, sha, mesh_sha):
    import time
    import traceback
    import numpy as np
    from plexora.server.models.project import Project
    from plexora.server.surface import store, mesh as mesh_mod

    def failed(key):
        kernels()
        out[key] = {"error": traceback.format_exc()[-1200:]}

    project = Project.find(name)
    channels = list(project.image.channel_names)
    source = store.open_volume_for(project)
    quiet = dict(stage=lambda *a: None, report=lambda *a, **k: None, cancelled=lambda: False)
    try:
        volume = source.volume
        out["shape"] = [volume.channels, volume.depth, volume.height, volume.width]
        if iso is None:
            coarse = volume.levels[-1]
            block = source.read_block(coarse.index, 0, 0, 0, volume.depth - 1,
                                      slice(0, coarse.height), slice(0, coarse.width))
            iso = float(np.percentile(block, 99))
        try:
            spec = store.SurfaceSpec.from_dict({"type": "isosurface", "channel": channels[0],
                                                "iso": iso, "smoothing": "medium"})
            t0 = time.perf_counter()
            surface, level, info = store.extract_isosurface(spec, source, 0, **quiet)
            t1 = time.perf_counter()
            scale, physical = store._scale(volume)
            columns = mesh_mod.measure(surface, scale=scale, physical=physical,
                                       dims=(volume.width, volume.height, volume.depth))
            t2 = time.perf_counter()
            out["isosurface"] = {"iso": iso, "level": level, "triangles": int(surface.n_triangles),
                                 "extract_ms": round((t1 - t0) * 1000, 1),
                                 "measure_ms": round((t2 - t1) * 1000, 1),
                                 "mesh": mesh_sha(surface),
                                 # Per column, exactly and to 12 significant digits:
                                 # a cube root's last bit is the platform's libm's.
                                 "measured": {k: f"{np.asarray(columns[k]).dtype}:" + sha(
                                     np.asarray(columns[k]).astype(np.float64))[:16]
                                     for k in sorted(columns)},
                                 "measured_12": sha(*[np.array([float(f"{v:.12g}") for v in
                                                                np.asarray(columns[k], np.float64)])
                                                      for k in sorted(columns)]),
                                 "kernels_ms": kernels()}
        except Exception:
            failed("isosurface")
        if mask:
            try:
                spec = store.SurfaceSpec.from_dict({"type": "label_surfaces", "smoothing": "medium"})
                t0 = time.perf_counter()
                surface, level, info = store._build_label_surfaces(project, spec, source, **quiet)
                t1 = time.perf_counter()
                out["cell_surfaces"] = {"level": level, "triangles": int(surface.n_triangles),
                                        "objects": int(len(np.unique(surface.object_id))),
                                        "extract_ms": round((t1 - t0) * 1000, 1),
                                        "mesh": mesh_sha(surface), "kernels_ms": kernels()}
            except Exception:
                failed("cell_surfaces")
    finally:
        source.close()
    if objects:
        try:
            from plexora.server.volume import objects as volume_objects
            import polars as pl

            t0 = time.perf_counter()
            volume_objects.measure_project_objects(Project.load(name), units="voxel")
            t1 = time.perf_counter()
            table_path, _ = volume_objects.object_paths(Project.load(name))
            frame = pl.read_parquet(table_path).sort("label")
            out["objects"] = {"ms": round((t1 - t0) * 1000, 1), "rows": frame.height,
                              "table": sha(*[frame[c].to_numpy() for c in sorted(frame.columns)]),
                              "kernels_ms": kernels()}
        except Exception:
            failed("objects")
    return out
'''


def ops(name, iso=None, mask=False, objects=False, repeat=1) -> dict:
    """`volume_ops` here; with `repeat`, the last of that many runs (numba's
    first call loads or compiles every kernel), the first one's times kept
    under `first`."""
    scope = {}
    exec(OPS, scope)  # noqa: S102 -- the same source the website runs
    runs = [scope["volume_ops"](name, iso=iso, mask=mask, objects=objects)
            for _ in range(max(1, repeat))]
    last = runs[-1]
    if len(runs) > 1:
        last["first"] = {op: {k: v for k, v in runs[0][op].items() if k.endswith("ms")}
                         for op in ("isosurface", "cell_surfaces", "objects") if op in runs[0]}
    return last


# -- comparing two arms ---------------------------------------------------------

def _png_pixels(data: bytes):
    import io

    from PIL import Image

    image = Image.open(io.BytesIO(data))
    return np.asarray(image), image.mode


def _normal_json(value, names):
    """A JSON answer with every path to a picked file as the file's name, keys sorted."""
    if isinstance(value, dict):
        return {k: _normal_json(v, names) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_normal_json(v, names) for v in value]
    if isinstance(value, str) and ("/" in value or "\\" in value):
        base = value.replace("\\", "/").rstrip("/").split("/")[-1]
        if base in names:
            return base
    return value


def compare(website, desktop, names=()) -> dict:
    """URL by URL: `same` (bytes), `same_pixels`, `same_json`, or `differs`
    with what differs; `missing` where one arm has no answer."""
    def index(folder):
        record = json.loads((Path(folder) / "answers.json").read_text(encoding="utf-8"))
        return {row["url"]: row for row in record}

    web, desk = index(website), index(desktop)
    out = {}
    for url in sorted(set(web) | set(desk)):
        a, b = web.get(url), desk.get(url)
        if a is None or b is None:
            out[url] = {"verdict": "missing", "website": bool(a), "desktop": bool(b),
                        "explicit": bool((a or b).get("explicit"))}
            continue
        if a["status"] != b["status"]:
            out[url] = {"verdict": "differs", "status": [a["status"], b["status"]]}
            continue
        if a["sha256"] == b["sha256"]:
            out[url] = {"verdict": "same", "bytes": a["bytes"]}
            continue
        body_a = (Path(website) / a["file"]).read_bytes()
        body_b = (Path(desktop) / b["file"]).read_bytes()
        kind = a.get("type") or ""
        if "png" in kind:
            (pa, ma), (pb, mb) = _png_pixels(body_a), _png_pixels(body_b)
            same = ma == mb and pa.shape == pb.shape and np.array_equal(pa, pb)
            out[url] = {"verdict": "same_pixels" if same else "differs", "mode": [ma, mb],
                        "shape": [list(pa.shape), list(pb.shape)]}
            continue
        if "json" in kind:
            ja = _normal_json(json.loads(body_a), set(names))
            jb = _normal_json(json.loads(body_b), set(names))
            if ja == jb:
                out[url] = {"verdict": "same_json"}
            else:
                keys = sorted(k for k in set(ja) | set(jb) if ja.get(k) != jb.get(k)) \
                    if isinstance(ja, dict) and isinstance(jb, dict) else None
                out[url] = {"verdict": "differs", "keys": keys,
                            "website": json.dumps(ja)[:600], "desktop": json.dumps(jb)[:600]}
            continue
        out[url] = {"verdict": "differs", "bytes": [a["bytes"], b["bytes"]]}
    return out


def main(argv=None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("make")
    p.add_argument("out")
    p.add_argument("--scale", default="test", choices=sorted(SIZES))
    p = sub.add_parser("ops")
    p.add_argument("name")
    p.add_argument("--iso", type=float)
    p.add_argument("--mask", action="store_true")
    p.add_argument("--objects", action="store_true")
    p.add_argument("--repeat", type=int, default=1)
    p = sub.add_parser("compare")
    p.add_argument("website")
    p.add_argument("desktop")
    p.add_argument("--names", default="")
    args = parser.parse_args(argv)
    if args.cmd == "make":
        print(json.dumps(make(args.out, args.scale)))
    elif args.cmd == "ops":
        print(json.dumps(ops(args.name, args.iso, args.mask, args.objects, args.repeat)))
    else:
        print(json.dumps(compare(args.website, args.desktop,
                                 [n for n in args.names.split(",") if n])))


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
