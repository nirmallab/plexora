"""Stacks for the 3-D benchmark, and their registration as projects.

    python tools/bench3d/fixtures.py make small twoplane flat [--out DIR]
    python tools/bench3d/fixtures.py make medium large mediumz flatstack
    python tools/bench3d/fixtures.py register [--data DIR] [--tree DIR] [--ims PATH] [--meso PATH]
                                              [--prescan]

Fixtures (written under $PLEXORA_BENCH_HOME, default ~/plexora-bench -- never
inside Dropbox):

- small: 3 channels x 24 planes x 1024^2 uint16 OME-TIFF, two SubIFD levels,
  cells as blurred ellipsoids (legacy/make_volume.py's recipe);
- twoplane: 1 channel x 2 planes x 1024^2 uint16: plane 0 at 25 % of the
  window and plane 1 at 75 %, the window fixed by one 65535 pixel -- a step
  between them must never look brighter than plane 1 or darker than plane 0;
- flat: a planar 3-channel 2048^2 OME-TIFF, the 2-D parity check;
- medium: 3 x 200 x 2048^2 uint16 OME-Zarr v3, sharded, zstd, levels 0-3;
- large: DECLARED 4 x 4000 x 16384^2 (8.6 TB) OME-Zarr v3, sharded, levels
  0-5, inner chunk z = 8; only a 128-plane core is written at each level --
  zarr returns the fill value for shards that were never written. Anything that
  materialises a whole declared level allocates terabytes: that is the test.
- mediumz: 3 x 128 x 2048^2 OME-Zarr v3 whose coarse levels halve z as well
  as x and y (128, 64, 32, 16 planes), as some writers store them;
- flatstack: 3 x 100 x 2048^2 uint16 OME-TIFF with ONE level: no pyramid of
  its own, under the 2-D rule's 4096 px, over the stack rule's voxels.

The real stacks (an Imaris file, a light-sheet OME-Zarr) are registered from
paths given on the command line or in PLEXORA_BENCH_IMS / PLEXORA_BENCH_MESO /
PLEXORA_BENCH_XENIUM; nothing machine-specific lives in the repository.

`--prescan` starts a scratch server once and asks each volume for a tile per
channel and its info, so the window scan and any background 3-D copy are done
before a "cold" run measures anything.

`--tree` registers (and prescans) with another checkout's code: a before/after
comparison needs a data root per tree, since what an import derives -- a
pyramid copy, the levels it records -- is part of what changed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
from serve import bench_home, server_env, start, stop  # noqa: E402


def out_dir(path=None) -> Path:
    out = Path(path).expanduser() if path else bench_home() / "fixtures"
    out.mkdir(parents=True, exist_ok=True)
    return out


# -- writers -----------------------------------------------------------------


def make_small(out: Path) -> Path:
    path = out / "small.ome.tif"
    if not path.exists():
        subprocess.run([sys.executable, str(HERE / "legacy" / "make_volume.py"), str(path)], check=True)
    return path


def make_twoplane(out: Path) -> Path:
    import numpy as np
    import tifffile

    path = out / "twoplane.ome.tif"
    if path.exists():
        return path
    size = 1024
    rng = np.random.default_rng(3)
    stack = np.empty((2, size, size), np.uint16)
    for z, share in enumerate((0.25, 0.75)):
        base = share * 65535
        stack[z] = np.clip(base + rng.normal(0, 600, (size, size)), 0, 65534).astype(np.uint16)
    stack[:, 0, 0] = 65535          # the window is (0, 65535) on both planes
    md = {"axes": "ZYX", "PhysicalSizeX": 0.5, "PhysicalSizeY": 0.5, "PhysicalSizeZ": 2.0,
          "Channel": {"Name": ["Flat"]}}
    with tifffile.TiffWriter(path, bigtiff=True, ome=True) as writer:
        writer.write(stack, subifds=1, tile=(256, 256), metadata=md, compression="zlib")
        writer.write(stack[..., ::2, ::2], subfiletype=1, tile=(256, 256), compression="zlib")
    return path


def make_flat(out: Path) -> Path:
    import numpy as np
    import tifffile
    from scipy import ndimage

    path = out / "flat.ome.tif"
    if path.exists():
        return path
    rng = np.random.default_rng(11)
    size = 2048
    planes = []
    for c in range(3):
        field = ndimage.gaussian_filter(rng.random((size, size), np.float32), 6 + 4 * c)
        field = (field - field.min()) / (field.max() - field.min())
        planes.append((field * 40000 + rng.normal(300, 40, (size, size))).clip(0, 65535))
    data = np.stack(planes).astype(np.uint16)
    md = {"axes": "CYX", "PhysicalSizeX": 0.5, "PhysicalSizeY": 0.5,
          "Channel": {"Name": ["DNA", "Membrane", "Marker"]}}
    with tifffile.TiffWriter(path, bigtiff=True, ome=True) as writer:
        writer.write(data, subifds=2, tile=(256, 256), metadata=md, compression="zlib")
        for k in (2, 4):
            writer.write(data[..., ::k, ::k], subfiletype=1, tile=(256, 256), compression="zlib")
    return path


def _ngff_group(path: Path, shape, levels, scale_z, names, chunks, shards, dtype="uint16",
                z_halving=False):
    """An empty OME-Zarr 0.5 (zarr v3) image: (c, z, y, x), XY halving per level
    (and z too, with `z_halving`)."""
    import numpy as np  # noqa: F401
    import zarr
    from zarr.codecs import ZstdCodec

    root = zarr.open_group(str(path), mode="w", zarr_format=3)
    datasets = []
    arrays = []
    c, z, y, x = shape
    for level in range(levels):
        f = 2 ** level
        fz = f if z_halving else 1
        level_shape = (c, -(-z // fz), -(-y // f), -(-x // f))
        level_chunks = tuple(min(a, b) for a, b in zip(chunks, level_shape))
        level_shards = tuple(max(cs, min(s, ls)) for cs, s, ls in zip(level_chunks, shards, level_shape))
        level_shards = tuple(-(-ls // cs) * cs if ls < s else s
                             for cs, s, ls in zip(level_chunks, level_shards, level_shape))
        arr = root.create_array(str(level), shape=level_shape, dtype=dtype, chunks=level_chunks,
                                shards=level_shards, compressors=ZstdCodec(level=3), fill_value=0,
                                dimension_names=("c", "z", "y", "x"))
        arrays.append(arr)
        datasets.append({"path": str(level), "coordinateTransformations": [
            {"type": "scale", "scale": [1.0, scale_z * fz, 0.5 * f, 0.5 * f]}]})
    root.attrs["ome"] = {
        "version": "0.5",
        "multiscales": [{
            "axes": [{"name": "c", "type": "channel"},
                     {"name": "z", "type": "space", "unit": "micrometer"},
                     {"name": "y", "type": "space", "unit": "micrometer"},
                     {"name": "x", "type": "space", "unit": "micrometer"}],
            "datasets": datasets, "name": path.stem}],
        "omero": {"channels": [{"label": n, "color": "FFFFFF", "active": True,
                                "window": {"start": 0, "end": 4000, "min": 0, "max": 65535}}
                               for n in names],
                  "rdefs": {"defaultZ": z // 2}},
    }
    return arrays


def _cells(rng, depth, height, width, count, zr=(2.5, 5.0), r=(8, 16)):
    import numpy as np
    from scipy import ndimage

    vol = np.zeros((3, depth, height, width), np.float32)
    for _ in range(count):
        cz, cy, cx = rng.uniform(0, depth), rng.uniform(0, height), rng.uniform(0, width)
        rz, ry, rx = rng.uniform(*zr), rng.uniform(*r), rng.uniform(*r)
        z0, z1 = int(max(0, cz - rz - 1)), int(min(depth, cz + rz + 2))
        y0, y1 = int(max(0, cy - ry - 4)), int(min(height, cy + ry + 5))
        x0, x1 = int(max(0, cx - rx - 4)), int(min(width, cx + rx + 5))
        if z1 <= z0 or y1 <= y0 or x1 <= x0:
            continue
        gz, gy, gx = np.mgrid[z0:z1, y0:y1, x0:x1]
        d = ((gz - cz) / rz) ** 2 + ((gy - cy) / ry) ** 2 + ((gx - cx) / rx) ** 2
        vol[0, z0:z1, y0:y1, x0:x1] += (d <= 1) * rng.uniform(2500, 6000)
        vol[1, z0:z1, y0:y1, x0:x1] += ((d > 1) & (d <= 1.6)) * rng.uniform(800, 2500)
        if rng.random() < 0.3:
            vol[2, z0:z1, y0:y1, x0:x1] += (d <= 1.4) * rng.uniform(1500, 5000)
    for c in range(3):
        vol[c] = ndimage.gaussian_filter(vol[c], (1.0, 1.5, 1.5))
    vol += rng.normal(150, 30, vol.shape).astype(np.float32)
    return np.clip(vol, 0, 65535).astype(np.uint16)


def make_medium(out: Path) -> Path:
    """3 x 200 x 2048^2, written slab by slab (32 planes) so memory stays small."""
    import numpy as np

    path = out / "medium.ome.zarr"
    if (path / "zarr.json").exists() and (path / "done").exists():
        return path
    shape = (3, 200, 2048, 2048)
    arrays = _ngff_group(path, shape, 4, 2.0, ["DNA", "Membrane", "CD8"],
                         chunks=(1, 8, 128, 128), shards=(1, 32, 512, 512))
    rng = np.random.default_rng(5)
    for z0 in range(0, 200, 32):
        z1 = min(200, z0 + 32)
        block = _cells(rng, z1 - z0, 2048, 2048, 2600)
        for level, arr in enumerate(arrays):
            f = 2 ** level
            arr[:, z0:z1] = block[:, :, ::f, ::f]
        print(f"medium: planes {z0}-{z1}", flush=True)
    (path / "done").write_text("ok")
    return path


def make_large(out: Path) -> Path:
    """Declared 8.6 TB; a 128-plane x 1024^2 core per level, nothing else."""
    import numpy as np

    path = out / "large.ome.zarr"
    if (path / "zarr.json").exists() and (path / "done").exists():
        return path
    shape = (4, 4000, 16384, 16384)
    arrays = _ngff_group(path, shape, 6, 2.0, ["DNA", "Membrane", "CD8", "Vessel"],
                         chunks=(1, 8, 256, 256), shards=(1, 32, 1024, 1024))
    rng = np.random.default_rng(9)
    zc, yc, xc = 2000, 8192, 8192        # the core sits at the stack's centre
    depth = 128
    core = _cells(rng, depth, 1024, 1024, 500)
    core = np.concatenate([core, core[1:2]], axis=0)   # a fourth channel
    for level, arr in enumerate(arrays):
        f = 2 ** level
        side = max(16, 1024 // f)
        y0, x0 = (yc - 512) // f, (xc - 512) // f
        arr[:, zc - depth // 2: zc + depth // 2, y0:y0 + side, x0:x0 + side] = core[:, :, ::f, ::f][:, :, :side, :side]
        print(f"large: level {level} core written", flush=True)
    (path / "done").write_text("ok")
    return path


def make_mediumz(out: Path) -> Path:
    """3 x 128 x 2048^2 whose levels halve z with x and y, written 32 planes at a time."""
    import numpy as np

    path = out / "mediumz.ome.zarr"
    if (path / "zarr.json").exists() and (path / "done").exists():
        return path
    shape = (3, 128, 2048, 2048)
    arrays = _ngff_group(path, shape, 4, 2.0, ["DNA", "Membrane", "CD8"],
                         chunks=(1, 8, 128, 128), shards=(1, 32, 512, 512), z_halving=True)
    rng = np.random.default_rng(13)
    for z0 in range(0, 128, 32):
        block = _cells(rng, 32, 2048, 2048, 2600)
        for level, arr in enumerate(arrays):
            f = 2 ** level
            arr[:, z0 // f:(z0 + 32) // f] = block[:, ::f, ::f, ::f]
        print(f"mediumz: planes {z0}-{z0 + 32}", flush=True)
    (path / "done").write_text("ok")
    return path


def make_flatstack(out: Path) -> Path:
    """3 x 100 x 2048^2, one level, tiled; built in a scratch memmap 20 planes
    at a time so memory stays small."""
    import numpy as np
    import tifffile

    path = out / "flatstack.ome.tif"
    if path.exists():
        return path
    shape = (3, 100, 2048, 2048)
    scratch = out / "flatstack.scratch"
    stack = np.lib.format.open_memmap(scratch, mode="w+", dtype=np.uint16, shape=shape)
    rng = np.random.default_rng(17)
    try:
        for z0 in range(0, 100, 20):
            stack[:, z0:z0 + 20] = _cells(rng, 20, 2048, 2048, 1600)
            print(f"flatstack: planes {z0}-{z0 + 20}", flush=True)
        stack.flush()
        md = {"axes": "CZYX", "PhysicalSizeX": 0.5, "PhysicalSizeY": 0.5, "PhysicalSizeZ": 2.0,
              "Channel": {"Name": ["DNA", "Membrane", "CD8"]}}
        partial = path.with_name(path.name + ".partial")
        with tifffile.TiffWriter(partial, bigtiff=True, ome=True) as writer:
            writer.write(stack, tile=(256, 256), metadata=md, compression="zlib",
                         buffersize=64 * 2**20)
        partial.replace(path)
    finally:
        del stack
        scratch.unlink(missing_ok=True)
    return path


MAKERS = {"small": make_small, "twoplane": make_twoplane, "flat": make_flat,
          "medium": make_medium, "large": make_large, "mediumz": make_mediumz,
          "flatstack": make_flatstack}


# -- registration ------------------------------------------------------------------


def register(data: Path, entries: dict, tree: Path | None = None) -> dict:
    """Register `{name: path}` under `data` in a child process with the bench's env,
    importing the checkout at `tree` (default: this one)."""
    script = (
        "import json, sys\n"
        "from plexora.datasource import register_image_datasource\n"
        "entries = json.loads(sys.argv[1])\n"
        "out = {}\n"
        "for name, path in entries.items():\n"
        "    try:\n"
        "        e = register_image_datasource(name, path, data_dir=sys.argv[2])\n"
        "        out[name] = {k: e.get(k) for k in ('imageShape', 'num_channels', 'imageZSemantics', 'maxLevel')}\n"
        "    except Exception as error:\n"
        "        out[name] = {'error': repr(error)}\n"
        "print(json.dumps(out))\n")
    root = Path(tree).expanduser().resolve() if tree else ROOT
    env = server_env(data, root=root)
    result = subprocess.run([sys.executable, "-c", script, json.dumps({k: str(v) for k, v in entries.items()}),
                             str(data)], cwd=str(root), env={**env, "PYTHONIOENCODING": "utf-8"},
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(result.stderr[-4000:])
    return json.loads(result.stdout.strip().splitlines()[-1])


def _get(port, path, timeout=600):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as reply:
        return reply.status, reply.read()


def prescan(data: Path, names, tree: Path | None = None) -> None:
    """Window scans and 3-D copies, done once before anything is measured."""
    server = start(data, root=tree)
    try:
        config = json.loads((data / "config.json").read_text(encoding="utf-8"))
        for name in names:
            entry = config.get(name) or {}
            t0 = time.time()
            view = entry.get("imageDefaultView") or ""
            try:
                for channel in entry.get("imageData") or []:
                    # Any tile starts the channel's window scan; the finest
                    # level's first tile exists at every size.
                    query = f"?view={view}" if view else ""
                    _get(server["port"], f"{channel['src']}0/0_0.png{query}")
            except urllib.error.HTTPError as error:
                # A build that cannot serve this stack: what the run measures,
                # not a reason to leave the others unscanned.
                print(f"prescan {name}: HTTP {error.code} on its first tile", flush=True)
                continue
            if view:
                status, body = _get(server["port"], f"/volume/{name}/info")
                state = json.loads(body).get("bricks", {}).get("state")
                bricks = json.loads(body).get("bricks", {})
                # A copy still filling, or an OME-Zarr stack's coarse levels
                # still being derived (`levels`, a tree that has them).
                while (bricks.get("state") not in (None, "not_needed", "ready", "error", "failed")
                       or (bricks.get("levels") or {}).get("status") == "pending"):
                    time.sleep(2)
                    _, body = _get(server["port"], f"/volume/{name}/info")
                    bricks = json.loads(body).get("bricks", {})
                state = bricks.get("state")
                print(f"prescan {name}: {time.time() - t0:.1f} s (bricks: {state})", flush=True)
    finally:
        stop(server["pid"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    mk = sub.add_parser("make")
    mk.add_argument("which", nargs="+", choices=[*MAKERS, "all"])
    mk.add_argument("--out")
    rg = sub.add_parser("register")
    rg.add_argument("--data", default=str(bench_home() / "data"))
    rg.add_argument("--tree", default=None,
                    help="register with this checkout (a detached worktree of an older commit)")
    rg.add_argument("--fixtures", default=None)
    rg.add_argument("--ims", default=os.environ.get("PLEXORA_BENCH_IMS"))
    rg.add_argument("--meso", default=os.environ.get("PLEXORA_BENCH_MESO"))
    rg.add_argument("--xenium", default=os.environ.get("PLEXORA_BENCH_XENIUM"))
    rg.add_argument("--only", nargs="*")
    rg.add_argument("--prescan", action="store_true")
    rg.add_argument("--prescan-only", action="store_true",
                    help="prescan what --data already holds, registering nothing")
    args = parser.parse_args(argv)
    if args.cmd == "make":
        out = out_dir(args.out)
        which = list(MAKERS) if "all" in args.which else args.which
        for name in which:
            t0 = time.time()
            print(name, MAKERS[name](out), f"{time.time() - t0:.0f} s", flush=True)
        return
    data = Path(args.data).expanduser()
    data.mkdir(parents=True, exist_ok=True)
    fixtures = out_dir(args.fixtures)
    entries = {}
    for name, file in (("small", "small.ome.tif"), ("twoplane", "twoplane.ome.tif"),
                       ("flat", "flat.ome.tif"), ("medium", "medium.ome.zarr"),
                       ("large", "large.ome.zarr"), ("mediumz", "mediumz.ome.zarr"),
                       ("flatstack", "flatstack.ome.tif")):
        if (fixtures / file).exists():
            entries[name] = fixtures / file
    for name, path in (("ims", args.ims), ("meso", args.meso), ("xenium", args.xenium)):
        if path and Path(path).exists():
            entries[name] = Path(path)
    if args.only:
        entries = {k: v for k, v in entries.items() if k in args.only}
    if args.prescan_only:
        prescan(data, [n for n in entries if n != "flat"], args.tree)
        return
    t0 = time.time()
    print(json.dumps(register(data, entries, args.tree), indent=1))
    print(f"registered in {time.time() - t0:.1f} s")
    if args.prescan:
        prescan(data, [n for n in entries if n != "flat"], args.tree)


if __name__ == "__main__":
    main()
