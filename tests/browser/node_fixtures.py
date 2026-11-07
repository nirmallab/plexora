"""Images for tests/browser/node_parity.mjs: the TIFF layouts the website's
browser node may meet, each small enough to compare every tile of.

    python tests/browser/node_fixtures.py OUT_DIR

Writes, if absent:
- pyr16_zlib.ome.tif   3 channels, uint16, 3000 x 2200, tiled 512, deflate with
                       the horizontal predictor, two sub-resolutions
- pyr8_lzw.ome.tif     2 channels, uint8, 2100 x 1500, tiled 256, LZW, one
- pyr16_raw.ome.tif    1 channel, uint16, 1300 x 1100, tiled 256, uncompressed
- pyr8_jpeg.ome.tif    1 channel, uint8, 1800 x 1200, tiled 256, JPEG, one
                       (lossy: the page must leave it to Python)
- pyr16_zstd.ome.tif   2 channels, uint16, 1700 x 1300, tiled 512, Zstandard, one
- pyr8_packbits.ome.tif 1 channel, uint8, 1500 x 1100, tiled 256, PackBits, one
- flat16.tif           2 channels, uint16, 1800 x 1800, stripped, no pyramid
                       (the page must leave it to Python)
and three projects with a mask, each an 8-bit image and its labels:
- labels32             uint32 ids up to 2^25 (the high byte Python drops),
                       tiled 256, deflate, one sub-resolution
- labels16             uint16 ids, tiled 256, LZW, one sub-resolution
- labels_flat          uint32, one level: the page builds its label pyramid
                       (tests/test_tiff_writer.py), and the node serves it
and two OME-Zarr stores, picked as folders (node/zarrReader.js):
- zarr_v2_blosc        NGFF 0.4, zarr v2, 3 channels, uint16, 3000 x 2200,
                       chunks 512, blosc lz4 + shuffle, "/" keys, two levels more
- zarr_v3_sharded      NGFF 0.5, zarr v3, 2 channels, uint8, 2100 x 1500,
                       chunks 256 in shards of 1024, zstd, one level more
Prints {name: [path, ...]} as JSON: the files (or the store) to import together.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import tifffile


def _planes(channels: int, height: int, width: int, dtype) -> np.ndarray:
    """Gradients, rings and noise per channel: a mismatch shows wherever it is."""
    rng = np.random.default_rng(7)
    y, x = np.mgrid[0:height, 0:width].astype(np.float32)
    top = np.iinfo(dtype).max
    out = np.empty((channels, height, width), dtype=dtype)
    for c in range(channels):
        field = (np.sin(x / (37.0 + 11 * c)) + np.cos(y / (23.0 + 7 * c)) + 2.0) / 4.0
        field += (x + y * (c + 1)) / (width + height * (c + 1)) * 0.5
        field = field / field.max() * 0.9 + rng.random((height, width), dtype=np.float32) * 0.1
        out[c] = (field * top).astype(dtype)
    return out


def _pyramid(path: Path, data: np.ndarray, *, tile: int, levels: int, **options) -> None:
    with tifffile.TiffWriter(path, bigtiff=False, ome=True) as tif:
        tif.write(data, subifds=levels, tile=(tile, tile), metadata={"axes": "CYX"}, **options)
        level = data
        for _ in range(levels):
            level = level[:, ::2, ::2]
            tif.write(level, subfiletype=1, tile=(tile, tile), **options)


def _labels(height: int, width: int, top: int) -> np.ndarray:
    """Cells of a few pixels across with ids spread up to `top`, and
    background (0) between some of them."""
    y, x = np.mgrid[0:height, 0:width]
    cell = (y // 9) * (width // 7 + 1) + (x // 7)
    ids = (cell.astype(np.uint64) * 2654435761 % top).astype(np.uint32) + 1
    ids[(y % 9 == 0) | ((x // 7) % 5 == 0)] = 0
    return ids


def _mask(path: Path, labels: np.ndarray, *, levels: int, **options) -> None:
    """As tests/agent_fixtures.write_mask_pyramid writes one: not OME."""
    with tifffile.TiffWriter(path) as tif:
        tif.write(labels, subifds=levels, tile=(256, 256), metadata={"axes": "YX"}, **options)
        for level in range(1, levels + 1):
            tif.write(labels[::2 ** level, ::2 ** level], subfiletype=1, tile=(256, 256), **options)


def _zarr(path: Path, data: np.ndarray, *, version: str, chunks: int, levels: int, shards: int = 0) -> None:
    """An OME-Zarr (CYX) whose levels halve: NGFF 0.4 as zarr v2 with blosc
    (lz4, shuffle, "/" keys, as bioformats2raw writes), or 0.5 as zarr v3 with
    zstd, sharded when `shards`. Beside it, `<store>.files.json`: its files,
    for the driver to fetch and mount as a picked folder."""
    import zarr

    v2 = version == "0.4"
    group = zarr.open_group(str(path), mode="w", zarr_format=2 if v2 else 3)
    datasets = []
    level = data
    for index in range(levels + 1):
        options = {"shape": level.shape, "dtype": level.dtype, "fill_value": 0}
        if v2:
            from numcodecs import Blosc

            options.update(chunks=(1, chunks, chunks), compressors=[Blosc(cname="lz4", clevel=5, shuffle=1)],
                           chunk_key_encoding={"name": "v2", "separator": "/"})
        else:
            from zarr.codecs import ZstdCodec

            options.update(chunks=(1, chunks, chunks), compressors=[ZstdCodec(level=3)])
            if shards:
                side = lambda n: min(shards, -(-n // chunks) * chunks)  # noqa: E731
                options["shards"] = (1, side(level.shape[1]), side(level.shape[2]))
        group.create_array(str(index), **options)[...] = level
        datasets.append({"path": str(index), "coordinateTransformations": [
            {"type": "scale", "scale": [1.0, 0.5 * 2 ** index, 0.5 * 2 ** index]}]})
        level = level[:, ::2, ::2]
    axes = [{"name": "c", "type": "channel"}, {"name": "y", "type": "space"}, {"name": "x", "type": "space"}]
    payload = {"multiscales": [{"version": version, "axes": axes, "datasets": datasets}]}
    group.attrs.update(payload if v2 else {"ome": dict(payload, version=version)})
    files = sorted(p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file())
    Path(f"{path}.files.json").write_text(json.dumps(files), encoding="utf-8")


def _with_mask(out: Path, name: str, labels: np.ndarray, levels: int, **options) -> list:
    image = out / f"{name}_image.ome.tif"
    mask = out / f"{name}_mask.tif"
    if not image.exists():
        _pyramid(image, _planes(1, *labels.shape, np.uint8), tile=256, levels=1)
    if not mask.exists():
        if levels:
            _mask(mask, labels, levels=levels, **options)
        else:
            tifffile.imwrite(mask, labels, metadata={"axes": "YX"}, **options)
    return [image, mask]


def main(out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    made = {}
    path = out / "pyr16_zlib.ome.tif"
    if not path.exists():
        _pyramid(path, _planes(3, 2200, 3000, np.uint16), tile=512, levels=2,
                 compression="zlib", predictor=True)
    made["pyr16_zlib"] = path
    path = out / "pyr8_lzw.ome.tif"
    if not path.exists():
        _pyramid(path, _planes(2, 1500, 2100, np.uint8), tile=256, levels=1, compression="lzw")
    made["pyr8_lzw"] = path
    path = out / "pyr16_raw.ome.tif"
    if not path.exists():
        _pyramid(path, _planes(1, 1100, 1300, np.uint16), tile=256, levels=1)
    made["pyr16_raw"] = path
    path = out / "pyr8_jpeg.ome.tif"
    if not path.exists():
        _pyramid(path, _planes(1, 1200, 1800, np.uint8), tile=256, levels=1,
                 compression="jpeg", compressionargs={"level": 90})
    made["pyr8_jpeg"] = path
    path = out / "pyr16_zstd.ome.tif"
    if not path.exists():
        _pyramid(path, _planes(2, 1300, 1700, np.uint16), tile=512, levels=1, compression="zstd")
    made["pyr16_zstd"] = path
    path = out / "pyr8_packbits.ome.tif"
    if not path.exists():
        _pyramid(path, _planes(1, 1100, 1500, np.uint8), tile=256, levels=1, compression="packbits")
    made["pyr8_packbits"] = path
    path = out / "flat16.tif"
    if not path.exists():
        tifffile.imwrite(path, _planes(2, 1800, 1800, np.uint16), metadata={"axes": "CYX"})
    made["flat16"] = path
    made["labels32"] = _with_mask(out, "labels32", _labels(1100, 1300, 1 << 25), 1, compression="zlib")
    made["labels16"] = _with_mask(out, "labels16", _labels(1100, 1300, 60000).astype(np.uint16), 1,
                                  compression="lzw")
    made["labels_flat"] = _with_mask(out, "labels_flat", _labels(1100, 1300, 1 << 20), 0)
    path = out / "zarr_v2_blosc.ome.zarr"
    if not Path(f"{path}.files.json").exists():
        _zarr(path, _planes(3, 2200, 3000, np.uint16), version="0.4", chunks=512, levels=2)
    made["zarr_v2_blosc"] = path
    path = out / "zarr_v3_sharded.ome.zarr"
    if not Path(f"{path}.files.json").exists():
        _zarr(path, _planes(2, 1500, 2100, np.uint8), version="0.5", chunks=256, levels=1, shards=1024)
    made["zarr_v3_sharded"] = path
    return {name: [str(p) for p in (paths if isinstance(paths, list) else [paths])]
            for name, paths in made.items()}


if __name__ == "__main__":
    print(json.dumps(main(Path(sys.argv[1]))))
