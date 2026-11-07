"""A synthetic Sakura SVSlide, for L2: OpenSlide publishes no Sakura slide, and
openslide-js lists Sakura as "not yet tested" (an SQLite database read
through its I/O layer). This writes one the way OpenSlide 4.0.0's
vendor-sakura.c reads it, so native OpenSlide and openslide-js can be held to
each other on it.

    python -I bench/sakura_fixture.py OUT.svslide [--width 3000 --height 2200 --tile 256]

The layout (vendor-sakura.c): `DataManagerSQLiteConfigXPO.TableName` names the
one image table (`id` TEXT, `data` BLOB); in it `++MagicBytes` is
"SVGigaPixelImage", `++VersionBytes` any blob, `Header` little-endian uint32s
(tile size at 0, width at 4, height at 8, focal planes at 16), and every tile
is three grayscale JPEGs, one per colour, under
`T;<x>|<y>;<downsample>;<colour>;<focal plane>` with x, y the tile's level-0
origin. Downsamples are powers of two. `SVSlideDataXPO` and `SVHRScanDataXPO`
carry the properties (ResolutionMmPerPix gives the mpp), and
`SVScannedImageDataXPO` the label and overview JPEGs.
"""

from __future__ import annotations

import argparse
import io
import sqlite3
import struct
from pathlib import Path

import numpy as np
from PIL import Image

TABLE = "SVGigaPixelImage"


def _jpeg(plane: np.ndarray, quality: int = 90) -> bytes:
    out = io.BytesIO()
    Image.fromarray(plane).save(out, "JPEG", quality=quality)
    return out.getvalue()


def picture(width: int, height: int) -> np.ndarray:
    """An H&E-coloured picture with structure at every scale: a pale field,
    stained blobs, and a grid of fine lines that a wrong tile placement
    breaks."""
    y, x = np.mgrid[0:height, 0:width].astype(np.float32)
    out = np.empty((height, width, 3), dtype=np.float32)
    out[...] = (238, 232, 240)
    for cx, cy, r, colour in ((0.3, 0.4, 0.18, (150, 92, 172)), (0.65, 0.55, 0.22, (214, 138, 156)),
                              (0.5, 0.8, 0.1, (110, 60, 150))):
        inside = (x - cx * width) ** 2 + (y - cy * height) ** 2 < (r * min(width, height)) ** 2
        out[inside] = colour
    lines = ((x.astype(int) % 97) < 2) | ((y.astype(int) % 89) < 2)
    out[lines] = out[lines] * 0.6
    return out.clip(0, 255).astype(np.uint8)


def write(path: Path, width: int = 3000, height: int = 2200, tile: int = 256) -> Path:
    path = Path(path)
    if path.exists():
        path.unlink()
    rgb = picture(width, height)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE DataManagerSQLiteConfigXPO (OID INTEGER PRIMARY KEY, TableName TEXT)")
    db.execute("INSERT INTO DataManagerSQLiteConfigXPO (TableName) VALUES (?)", (TABLE,))
    db.execute(f'CREATE TABLE "{TABLE}" (id TEXT PRIMARY KEY, data BLOB)')
    put = lambda key, data: db.execute(f'INSERT INTO "{TABLE}" (id, data) VALUES (?, ?)', (key, data))  # noqa: E731
    put("++MagicBytes", b"SVGigaPixelImage")
    put("++VersionBytes", b"1.0.0")
    put("Header", struct.pack("<IIIII", tile, width, height, 0, 1))
    downsample = 1
    level = rgb
    while True:
        h, w = level.shape[:2]
        for row in range(-(-h // tile)):
            for col in range(-(-w // tile)):
                block = np.full((tile, tile, 3), 255, dtype=np.uint8)
                part = level[row * tile:(row + 1) * tile, col * tile:(col + 1) * tile]
                block[:part.shape[0], :part.shape[1]] = part
                for colour in range(3):
                    key = f"T;{col * tile * downsample}|{row * tile * downsample};{downsample};{colour};0"
                    put(key, _jpeg(np.ascontiguousarray(block[..., colour])))
        if max(h, w) <= tile:
            break
        downsample *= 2
        level = np.asarray(Image.fromarray(rgb).resize((max(1, width // downsample), max(1, height // downsample)),
                                                       Image.BOX))
    db.execute("CREATE TABLE SVSlideDataXPO (OID INTEGER PRIMARY KEY, SlideId TEXT, Date TEXT, "
               "Description TEXT, Creator TEXT, DiagnosisCode TEXT, Keywords TEXT, "
               "m_labelScan INTEGER, m_overviewScan INTEGER)")
    db.execute("CREATE TABLE SVHRScanDataXPO (OID INTEGER PRIMARY KEY, ParentSlide INTEGER, ScanId TEXT, "
               "Date TEXT, Name TEXT, Description TEXT, ResolutionMmPerPix REAL, "
               "NominalLensMagnification REAL, FocussingMethod TEXT, ThumbnailImage BLOB)")
    db.execute("CREATE TABLE SVScannedImageDataXPO (OID INTEGER PRIMARY KEY, Image BLOB)")
    label = _jpeg(np.full((120, 200), 200, dtype=np.uint8))
    macro = _jpeg(np.asarray(Image.fromarray(rgb).resize((300, 220)).convert("L")))
    db.execute("INSERT INTO SVScannedImageDataXPO (OID, Image) VALUES (1, ?), (2, ?)", (label, macro))
    db.execute("INSERT INTO SVSlideDataXPO VALUES (1, 'SYNTH-1', '2026-11-07', 'synthetic Sakura slide', "
               "'plexora tests', '', 'l2', 1, 2)")
    thumb = io.BytesIO()
    Image.fromarray(rgb).resize((300, 220)).save(thumb, "JPEG")
    db.execute("INSERT INTO SVHRScanDataXPO VALUES (1, 1, 'SCAN-1', '2026-11-07', 'scan', 'synthetic', "
               "0.00025, 40.0, 'auto', ?)", (thumb.getvalue(),))
    db.commit()
    db.close()
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("out")
    parser.add_argument("--width", type=int, default=3000)
    parser.add_argument("--height", type=int, default=2200)
    parser.add_argument("--tile", type=int, default=256)
    args = parser.parse_args()
    print(write(Path(args.out), args.width, args.height, args.tile))


if __name__ == "__main__":
    main()
