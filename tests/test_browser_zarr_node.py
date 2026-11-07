"""The browser node reads an OME-Zarr store as Plexora's Python does.

The page's OME-Zarr reader (client src/js/node/zarrReader.js) serves a picked
OME-Zarr folder on the website and a project's own store on the desktop app
(Browser rendering), as node/tiffReader.js serves a TIFF. Python says what it
reads of the store (`node_spec.describe`'s `zarr`: the store's own levels, the
axes, the plane); the reader reads each array's metadata itself and every
chunk it needs.

Each case writes a store with one writer's choices -- zarr v2 and v3, blosc
with each shuffle, zstd, zlib, gzip, lz4, crc32c, shards, "/" and "." keys,
chunks never written, 8- and 16-bit, two to five axes -- registers it as a
project, and compares every tile of every channel and level the reader reads
(tests/js/zarr_probe.mjs, in Node over the modules the workers load) with the
16-bit PNG the tile route answers for the same URL (`?q=hd`), sample for
sample. A second reader opens from the first's recorded metadata and must read
none of its own.
"""

import hashlib
import io
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

import plexora
from plexora import datasource
from plexora.browser import node_spec
from plexora.server.models import data_model

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests" / "js" / "zarr_probe.mjs"
NODE = shutil.which("node")
ZSTDDEC = ROOT / "plexora" / "client" / "node_modules" / "zstddec"

pytestmark = pytest.mark.skipif(not NODE or not ZSTDDEC.is_dir(),
                                reason="needs node and plexora/client/node_modules (npm ci)")

zarr = pytest.importorskip("zarr")
numcodecs = pytest.importorskip("numcodecs")

_AXIS_TYPES = {"t": "time", "c": "channel", "z": "space", "y": "space", "x": "space"}


def _codecs(version, name):
    """A writer's compression by name: numcodecs for zarr v2, zarr's codecs
    for v3 (a list of bytes-to-bytes codecs)."""
    from numcodecs import GZip, LZ4, Blosc, Zlib, Zstd

    if version == "0.4":
        return {
            "blosc-lz4-shuffle": Blosc(cname="lz4", clevel=5, shuffle=Blosc.SHUFFLE),
            "blosc-zstd-bitshuffle": Blosc(cname="zstd", clevel=3, shuffle=Blosc.BITSHUFFLE),
            "blosc-zlib-noshuffle": Blosc(cname="zlib", clevel=4, shuffle=Blosc.NOSHUFFLE),
            "blosc-blosclz": Blosc(cname="blosclz", clevel=5, shuffle=Blosc.SHUFFLE),
            "zstd": Zstd(level=3),
            "zlib": Zlib(level=5),
            "gzip": GZip(level=5),
            "lz4": LZ4(),
            "none": None,
        }[name]
    from zarr.codecs import BloscCodec, Crc32cCodec, GzipCodec, ZstdCodec

    return {
        "zstd": [ZstdCodec(level=3)],
        "zstd-checksum": [ZstdCodec(level=3, checksum=True)],
        "blosc-lz4-shuffle": [BloscCodec(cname="lz4", clevel=5, shuffle="shuffle")],
        "gzip-crc32c": [GzipCodec(level=5), Crc32cCodec()],
        "none": [],
    }[name]


def write_store(path, *, version="0.4", shape=(2, 300, 420), axes="cyx", levels=3, dtype="uint16",
                chunks=64, codec="blosc-lz4-shuffle", separator=None, shards=None, fill=0,
                unwritten=False, seed=0):
    """An OME-Zarr image whose every level is written with `codec`;
    `unwritten` leaves the lower-right quarter of every level to the fill."""
    path = Path(path)
    zarr_format = 2 if version == "0.4" else 3
    group = zarr.open_group(str(path), mode="w", zarr_format=zarr_format)
    rng = np.random.default_rng(seed)
    info = np.iinfo(np.dtype(dtype))
    current = rng.integers(0, int(info.max), size=shape, endpoint=True).astype(dtype)
    y, x = axes.index("y"), axes.index("x")
    datasets = []
    for level in range(levels):
        # A shard holds whole inner chunks, so a sharded level's chunks are not clipped to it.
        level_chunks = tuple((chunks if shards else min(chunks, side)) if i in (y, x) else 1
                             for i, side in enumerate(current.shape))
        kwargs = {"shape": current.shape, "dtype": dtype, "chunks": level_chunks, "fill_value": fill}
        compressors = _codecs(version, codec)
        if version == "0.4":
            kwargs["compressors"] = None if compressors is None else [compressors]
        else:
            kwargs["compressors"] = compressors
        if separator:
            kwargs["chunk_key_encoding"] = {"name": "v2", "separator": separator}
        if shards:
            kwargs["shards"] = tuple(min(shards, -(-side // chunks) * chunks) if i in (y, x) else 1
                                     for i, side in enumerate(current.shape))
        name = f"s{level}"
        array = group.create_array(name, **kwargs)
        if unwritten:
            window = [slice(None)] * len(shape)
            window[y] = slice(0, current.shape[y] // 2)
            array[tuple(window)] = current[tuple(window)]
            window[y] = slice(current.shape[y] // 2, None)
            window[x] = slice(0, current.shape[x] // 2)
            array[tuple(window)] = current[tuple(window)]
        else:
            array[...] = current
        datasets.append({"path": name, "coordinateTransformations": [
            {"type": "scale", "scale": [0.5 * 2 ** level if a in "xy" else 1.0 for a in axes]}]})
        window = [slice(None)] * len(shape)
        window[y] = slice(None, None, 2)
        window[x] = slice(None, None, 2)
        current = current[tuple(window)]
    payload = {"multiscales": [{"version": version, "datasets": datasets,
                                "axes": [{"name": a, "type": _AXIS_TYPES[a]} for a in axes]}]}
    if zarr_format == 2:
        group.attrs.update(payload)
    else:
        group.attrs.update({"ome": dict(payload, version=version)})
    return path


CASES = {
    # name: write_store's options
    "v2_blosc_lz4": {},
    "v2_blosc_zstd_bitshuffle_u8": {"codec": "blosc-zstd-bitshuffle", "dtype": "uint8"},
    "v2_blosc_zlib_noshuffle": {"codec": "blosc-zlib-noshuffle"},
    "v2_blosc_blosclz": {"codec": "blosc-blosclz", "chunks": 100},
    "v2_zstd_slash_keys": {"codec": "zstd", "separator": "/"},
    "v2_zlib": {"codec": "zlib", "chunks": 128},
    "v2_gzip": {"codec": "gzip"},
    "v2_lz4": {"codec": "lz4"},
    "v2_raw_unwritten_fill": {"codec": "none", "unwritten": True, "fill": 7},
    "v2_yx": {"shape": (300, 420), "axes": "yx"},
    "v2_tczyx": {"shape": (1, 3, 1, 260, 330), "axes": "tczyx", "codec": "zstd"},
    "v3_zstd": {"version": "0.5", "codec": "zstd"},
    "v3_zstd_checksum_u8": {"version": "0.5", "codec": "zstd-checksum", "dtype": "uint8"},
    "v3_blosc": {"version": "0.5", "codec": "blosc-lz4-shuffle", "chunks": 128},
    "v3_gzip_crc32c": {"version": "0.5", "codec": "gzip-crc32c"},
    "v3_sharded": {"version": "0.5", "codec": "zstd", "chunks": 32, "shards": 128},
    "v3_sharded_unwritten": {"version": "0.5", "codec": "blosc-lz4-shuffle", "chunks": 32, "shards": 64,
                             "unwritten": True, "fill": 11},
    "v3_raw_dot_keys": {"version": "0.5", "codec": "none", "separator": "."},
}


def _probe(root, spec, tmp_path):
    request = {"root": str(root), "zarr": spec["zarr"], "tile": spec["tile"],
               "channels": spec["levels"][0][0]}
    spec_file = tmp_path / "zarr_probe.json"
    spec_file.write_text(json.dumps(request), encoding="utf-8")
    done = subprocess.run([NODE, str(PROBE), str(spec_file)], capture_output=True, text=True,
                          encoding="utf-8", timeout=300)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def _python_tile(client, name, key, level, tx, ty):
    """The tile route's HD answer for one tile URL: its 16-bit samples as the
    big-endian bytes the viewer decodes the PNG to."""
    from PIL import Image

    response = client.get(f"/generated/data/{name}/{key}/{level}/{tx}_{ty}.png?q=hd")
    assert response.status_code == 200, response.data[:200]
    image = Image.open(io.BytesIO(response.data))
    values = np.asarray(image).astype(">u2")
    return values.tobytes(), image.size


def _register(tmp_path, name, store, tile=None):
    """The store as project `name`; `tile` a smaller tile grid than the
    import's 1024, so a small store has tiles across and down and at its edges."""
    entry = datasource.register_image_datasource(name=name, image=str(store), data_dir=tmp_path)
    if tile:
        config_path = Path(tmp_path) / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config[name]["tileWidth"] = config[name]["tileHeight"] = tile
        config_path.write_text(json.dumps(config), encoding="utf-8")
    data_model.load_datasource(name, reload=True)
    return entry


@pytest.mark.parametrize("case", sorted(CASES))
def test_every_tile_is_the_tile_routes(tmp_path, case):
    options = dict(CASES[case])
    store = write_store(tmp_path / f"{case}.ome.zarr", seed=len(case), **options)
    entry = _register(tmp_path, case, store, tile=128)
    spec = node_spec.describe(case, local=True)
    assert spec["served"] is True, spec
    assert spec["tile"] == [128, 128]
    assert spec["format"] == "ome-zarr"
    assert spec["path"] == str(store)
    assert [a["path"] for a in spec["zarr"]["arrays"]] == ["s0", "s1", "s2"]
    assert spec["zarr"]["axes"] == list(options.get("axes", "cyx"))
    found = _probe(store, spec, tmp_path)
    assert "refused" not in found, found
    assert found["metadataAsks"] == 3 and found["seededAsks"] == 0
    # The page reads every level Python serves from the store itself.
    assert len(found["geometry"]["levels"]) == len(spec["levels"]) == 3
    client = plexora.app.test_client()
    keys = [channel["src"].rstrip("/").split("/")[-1] for channel in entry["imageData"]]
    compared = 0
    for tile, got in found["tiles"].items():
        channel, level, at = tile.split("/")
        tx, ty = at.split("_")
        want, size = _python_tile(client, case, keys[int(channel)], level, tx, ty)
        assert (got["width"], got["height"]) == size, tile
        assert got["hash"] == hashlib.sha256(want).hexdigest(), f"{case} {tile} differs"
        compared += 1
    assert compared == len(found["tiles"]) > 0


def test_the_derived_levels_stay_pythons(tmp_path):
    """A store whose levels do not halve: the page reads level 0, Python the
    levels it derived into the project."""
    store = write_store(tmp_path / "steps.ome.zarr", shape=(1, 2600, 2400), levels=2, chunks=256,
                        codec="zstd")
    # Level 1 a third, not a half: rewritten as a non-halving pyramid.
    group = zarr.open_group(str(store), mode="r+")
    third = np.asarray(group["s0"])[:, ::3, ::3]
    del group["s1"]
    group.create_array("s1", shape=third.shape, dtype=third.dtype, chunks=(1, 256, 256))[...] = third
    _register(tmp_path, "steps", store)
    spec = node_spec.describe("steps", local=True)
    assert spec["served"] is True, spec
    assert [a["path"] for a in spec["zarr"]["arrays"]] == ["s0"]
    assert len(spec["levels"]) > 1, "Python derived coarser levels into the project"
    found = _probe(store, spec, tmp_path)
    assert len(found["geometry"]["levels"]) == 1


def test_a_store_on_a_share_or_not_a_store_stays_pythons(tmp_path, monkeypatch):
    store = write_store(tmp_path / "s.ome.zarr", levels=1)
    _register(tmp_path, "s", store)
    assert node_spec.is_local_store(str(store))
    assert not node_spec.is_local_store(str(tmp_path))  # a folder, but no zarr metadata
    assert not node_spec.is_local_store(r"\\server\share\s.ome.zarr")
    assert not node_spec.is_local_store("s3://bucket/s.ome.zarr")
    monkeypatch.setattr(data_model._providers.image, "_path", r"\\server\share\s.ome.zarr")
    spec = node_spec.describe("s", local=True)
    assert spec["served"] is False and spec["reason"] == "not a file on this computer's own disks"


def test_the_website_describes_a_picked_store(tmp_path, monkeypatch):
    store = write_store(tmp_path / "picked.ome.zarr", levels=2)
    _register(tmp_path, "picked", store)
    # Not a pick under CPython: the website's question leaves it to Python...
    assert node_spec.describe("picked")["reason"] == "the file is not one the page holds"
    # ...and serves it where it is one (the provider's path a /picked/ mount).
    pyramid = data_model.channels
    monkeypatch.setattr(data_model._providers.image, "_path", "/picked/ab12/picked.ome.zarr")
    monkeypatch.setattr(pyramid, "path", "/picked/ab12/picked.ome.zarr")
    spec = node_spec.describe("picked")
    assert spec["served"] is True, spec
    assert spec["format"] == "ome-zarr" and "ifds" not in spec
    assert [a["format"] for a in spec["zarr"]["arrays"]] == [2, 2]
