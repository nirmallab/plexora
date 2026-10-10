import os, sys
import numpy as np
import tifffile
from scipy import ndimage

out = sys.argv[1]
rng = np.random.default_rng(7)
Z, Y, X = 24, 1024, 1024
zz, yy, xx = np.mgrid[0:Z, 0:Y, 0:X].astype(np.float32) if False else (None, None, None)
nuc = np.zeros((Z, Y, X), np.float32)
mem = np.zeros((Z, Y, X), np.float32)
mark = np.zeros((Z, Y, X), np.float32)
labels = np.zeros((Z, Y, X), np.uint16)
centres = []
for i in range(260):
    cz, cy, cx = rng.uniform(3, Z - 3), rng.uniform(20, Y - 20), rng.uniform(20, X - 20)
    rz, ry, rx = rng.uniform(2.5, 4.5), rng.uniform(9, 15), rng.uniform(9, 15)
    z0, z1 = int(max(0, cz - rz - 1)), int(min(Z, cz + rz + 2))
    y0, y1 = int(max(0, cy - ry - 4)), int(min(Y, cy + ry + 5))
    x0, x1 = int(max(0, cx - rx - 4)), int(min(X, cx + rx + 5))
    gz, gy, gx = np.mgrid[z0:z1, y0:y1, x0:x1]
    d = ((gz - cz) / rz) ** 2 + ((gy - cy) / ry) ** 2 + ((gx - cx) / rx) ** 2
    inside = d <= 1.0
    shell = (d > 1.0) & (d <= 1.6)
    nuc[z0:z1, y0:y1, x0:x1] += inside * rng.uniform(2500, 5000)
    mem[z0:z1, y0:y1, x0:x1] += shell * rng.uniform(800, 2000)
    if rng.random() < 0.3:
        mark[z0:z1, y0:y1, x0:x1] += (d <= 1.4) * rng.uniform(1500, 4000)
    labels[z0:z1, y0:y1, x0:x1][inside] = i + 1
# optical blur, more along z, and depth attenuation
for arr in (nuc, mem, mark):
    arr[:] = ndimage.gaussian_filter(arr, (1.2, 1.5, 1.5))
att = np.exp(-np.arange(Z) / 30.0)[:, None, None]
stack = np.stack([nuc * att, mem * att, mark * att]) + rng.normal(120, 25, (3, Z, Y, X))
stack = np.clip(stack, 0, 65535).astype(np.uint16)
md = {"axes": "CZYX", "PhysicalSizeX": 0.5, "PhysicalSizeY": 0.5, "PhysicalSizeZ": 1.0,
      "Channel": {"Name": ["DNA", "Membrane", "CD8"]}}
with tifffile.TiffWriter(out, bigtiff=True, ome=True) as w:
    w.write(stack, subifds=2, tile=(256, 256), metadata=md, compression="zlib")
    for k in (2, 4):
        w.write(stack[..., ::k, ::k], subfiletype=1, tile=(256, 256), compression="zlib")
tifffile.imwrite(out.replace(".ome.tif", "_labels.tif"), labels, compression="zlib")
print("wrote", out, stack.shape)
