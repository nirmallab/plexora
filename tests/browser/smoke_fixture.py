"""The image tests/browser/smoke.mjs opens on the website: small, calibrated,
and in the layout the page's browser node reads itself.

    python tests/browser/smoke_fixture.py OUT_DIR

Writes `OUT_DIR/smoke.ome.tif` (if absent): two uint16 channels, 1024 x 768,
tiled 256, deflate, two sub-resolutions in SubIFDs, 0.5 um per pixel. Needs
numpy and tifffile only (deflate is the standard library's), so any Python
with those two can make it. Prints its path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import tifffile

PIXEL_SIZE_UM = 0.5


def make(out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    path = out / "smoke.ome.tif"
    if path.exists():
        return path
    y, x = np.mgrid[0:768, 0:1024].astype(np.float32)
    rings = (np.sin(np.hypot(x - 512, y - 384) / 23.0) + 1.0) / 2.0
    ramp = (x + y) / (1024 + 768)
    data = (np.stack([rings, ramp]) * 60000).astype(np.uint16)
    metadata = {"axes": "CYX", "Channel": {"Name": ["DNA", "CD3"]},
                "PhysicalSizeX": PIXEL_SIZE_UM, "PhysicalSizeXUnit": "µm",
                "PhysicalSizeY": PIXEL_SIZE_UM, "PhysicalSizeYUnit": "µm"}
    with tifffile.TiffWriter(path, ome=True) as tif:
        tif.write(data, subifds=2, tile=(256, 256), compression="zlib", metadata=metadata)
        for level in (1, 2):
            tif.write(data[:, ::2 ** level, ::2 ** level], subfiletype=1, tile=(256, 256),
                      compression="zlib")
    return path


if __name__ == "__main__":
    print(make(Path(sys.argv[1])))
