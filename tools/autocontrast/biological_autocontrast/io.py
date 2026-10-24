"""Lazy, read-only access to a multiplex image: OME-TIFF / TIFF / OME-Zarr.

Every read is a region of one plane of one channel at one pyramid level.
Z and T are explicit arguments; nothing here ever projects a stack. The source
file is only ever opened for reading.
"""

from __future__ import annotations

import os
import re
import warnings
from pathlib import Path

import numpy as np

_NUCLEAR = re.compile(r"(dapi|hoechst|dna|nucle|draq|sytox|ir19[13])", re.I)


class UnsupportedImage(ValueError):
    pass


def fingerprint(path) -> str:
    """Size and mtime, the identity Plexora's quantization cache uses
    (data_model._image_fingerprint). A directory store uses its metadata file."""
    path = Path(path)
    if path.is_dir():
        for name in ("zarr.json", ".zattrs", ".zgroup"):
            if (path / name).exists():
                stat = (path / name).stat()
                return f"dir:{stat.st_size}:{stat.st_mtime_ns}"
        stat = path.stat()
        return f"dir:0:{stat.st_mtime_ns}"
    stat = path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def is_nuclear(name: str) -> bool:
    return bool(_NUCLEAR.search(name or ""))


def _to_um(value, unit):
    if value is None:
        return None
    unit = (unit or "µm").strip().lower()
    scale = {"µm": 1.0, "um": 1.0, "micron": 1.0, "micrometer": 1.0, "micrometre": 1.0,
             "nm": 1e-3, "nanometer": 1e-3, "mm": 1e3, "millimeter": 1e3}.get(unit)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if scale is None or not np.isfinite(value) or value <= 0:
        return None
    return value * scale


class Image:
    """Common surface of the two readers.

    `levels` (count), `level_shape(l) -> (h, w)`, `channels` (names),
    `dtype`, `pixel_size_um` (None when unknown), `n_z`, `n_t`,
    `read(c, level, box=None, z=0, t=0)`, and the metadata the domain detector
    needs: `significant_bits`, `bits_per_sample`.
    """

    path: str
    kind: str
    channels: list
    dtype: np.dtype
    n_z: int = 1
    n_t: int = 1
    pixel_size_um = None
    significant_bits = None
    bits_per_sample = None

    @property
    def n_channels(self) -> int:
        return len(self.channels)

    def fingerprint(self) -> str:
        return fingerprint(self.path)

    def downsample(self, level: int) -> float:
        return self.level_shape(0)[1] / max(1, self.level_shape(level)[1])

    def level_pixel_size(self, level: int):
        if self.pixel_size_um is None:
            return None
        return self.pixel_size_um * self.downsample(level)

    def check_plane(self, z, t):
        z = 0 if z is None else int(z)
        t = 0 if t is None else int(t)
        if not 0 <= z < self.n_z:
            raise ValueError(f"z={z} is outside this image's planes 0..{self.n_z - 1}")
        if not 0 <= t < self.n_t:
            raise ValueError(f"t={t} is outside this image's timepoints 0..{self.n_t - 1}")
        return z, t

    def channel_index(self, key) -> int:
        if isinstance(key, (int, np.integer)):
            if not 0 <= int(key) < self.n_channels:
                raise ValueError(f"channel {key} is outside 0..{self.n_channels - 1}")
            return int(key)
        if key in self.channels:
            return self.channels.index(key)
        raise ValueError(f"{key!r} is not a channel of {Path(self.path).name}")

    def nuclear_channel(self):
        for index, name in enumerate(self.channels):
            if is_nuclear(name):
                return index
        return None

    def overview_level(self, min_pixels=1_000_000, max_pixels=16_000_000) -> int:
        """plexora.agent.evidence.image_qc.overview_level's rule: the coarsest
        level with at least `min_pixels`, capped at `max_pixels`."""
        best = 0
        for level in range(self.levels):
            height, width = self.level_shape(level)
            if height * width >= min_pixels:
                best = level
            else:
                break
        while best < self.levels - 1:
            height, width = self.level_shape(best)
            if height * width <= max_pixels:
                break
            best += 1
        return best

    def read(self, c, level, box=None, z=0, t=0, *, max_pixels=120_000_000):
        """One region `(x, y, w, h)` (whole level when None) as a fresh array."""
        z, t = self.check_plane(z, t)
        height, width = self.level_shape(level)
        if box is None:
            box = (0, 0, width, height)
        x, y, w, h = (int(v) for v in box)
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(width, x + w), min(height, y + h)
        if x1 <= x0 or y1 <= y0:
            return np.zeros((0, 0), dtype=self.dtype)
        if (x1 - x0) * (y1 - y0) > max_pixels:
            raise ValueError(
                f"refusing a {(x1 - x0) * (y1 - y0) / 1e6:.0f} Mpx read at level {level}; "
                "the image needs a pyramid")
        return np.array(self._read(int(c), level, y0, y1, x0, x1, z, t))

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def describe(self) -> dict:
        return {"path": str(self.path), "kind": self.kind, "dtype": str(self.dtype),
                "channels": list(self.channels), "n_z": self.n_z, "n_t": self.n_t,
                "levels": [list(self.level_shape(l)) for l in range(self.levels)],
                "pixel_size_um": self.pixel_size_um,
                "significant_bits": self.significant_bits,
                "bits_per_sample": self.bits_per_sample,
                "fingerprint": self.fingerprint()}


class TiffImage(Image):
    kind = "tiff"

    def __init__(self, path):
        import tifffile
        import zarr

        self.path = str(path)
        self._tiff = tifffile.TiffFile(self.path)
        series = self._tiff.series[0]
        axes = str(series.axes)
        shape = tuple(int(s) for s in series.shape)
        if "S" in axes:
            self.close()
            raise UnsupportedImage(
                f"{Path(path).name} is an RGB/interleaved image ({axes}); brightfield is "
                "out of scope for this model")
        if not axes.endswith("YX"):
            self.close()
            raise UnsupportedImage(f"cannot read axes {axes!r}")
        leading = axes[:-2]
        sizes = dict(zip(leading, shape[:-2]))
        # Any leading axis tifffile could not name (I, Q, ...) is the channel
        # axis when there is no C; more than one unnamed axis is refused.
        unnamed = [a for a in leading if a not in "CZT" and sizes[a] > 1]
        if unnamed and "C" not in leading and len(unnamed) == 1:
            self._caxis = unnamed[0]
        elif unnamed:
            self.close()
            raise UnsupportedImage(f"cannot tell channels from axes {axes!r}")
        else:
            self._caxis = "C"
        self._leading = leading
        self.n_z = int(sizes.get("Z", 1))
        self.n_t = int(sizes.get("T", 1))
        n_c = int(sizes.get(self._caxis, 1))
        self.dtype = np.dtype(series.dtype)
        self._levels = [zarr.open(series.aszarr(level=i), mode="r")
                        for i in range(len(series.levels))]
        self.levels = len(self._levels)

        page = self._tiff.pages.first
        self.bits_per_sample = int(page.bitspersample) if page.bitspersample else None
        self.ome_xml = self._tiff.ome_metadata or ""
        self.channels = self._channel_names(n_c)
        self.significant_bits = None
        self.pixel_size_um = None
        if self.ome_xml:
            match = re.search(r'<Pixels\b[^>]*?\bSignificantBits="(\d+)"', self.ome_xml)
            if match:
                self.significant_bits = int(match.group(1))
            size = re.search(r'<Pixels\b[^>]*?\bPhysicalSizeX="([^"]+)"', self.ome_xml)
            unit = re.search(r'<Pixels\b[^>]*?\bPhysicalSizeXUnit="([^"]+)"', self.ome_xml)
            self.pixel_size_um = _to_um(size.group(1) if size else None,
                                        unit.group(1) if unit else None)
        if self.pixel_size_um is None:
            self.pixel_size_um = self._resolution_um(page)

    def _channel_names(self, n_c):
        names = re.findall(r'<Channel\b[^>]*?\bName="([^"]*)"', self.ome_xml or "")
        if len(names) == n_c and all(n.strip() for n in names):
            return [n.strip() for n in names]
        labels = (self._tiff.imagej_metadata or {}).get("Labels") if self._tiff.is_imagej else None
        if isinstance(labels, (list, tuple)) and len(labels) == n_c:
            return [str(l) for l in labels]
        return [f"ch{i}" for i in range(n_c)]

    @staticmethod
    def _resolution_um(page):
        try:
            tags = page.tags
            unit = tags.get("ResolutionUnit")
            res = tags.get("XResolution")
            if res is None or unit is None:
                return None
            num, den = res.value
            per_unit = num / den
            if per_unit <= 0:
                return None
            unit = int(unit.value)
            if unit == 3:  # centimetre
                return 1e4 / per_unit
            return None  # inch or none: not a microscope calibration
        except Exception:
            return None

    def level_shape(self, level):
        shape = self._levels[level].shape
        return int(shape[-2]), int(shape[-1])

    def _read(self, c, level, y0, y1, x0, x1, z, t):
        index = []
        for axis in self._leading:
            if axis == self._caxis:
                index.append(c)
            elif axis == "Z":
                index.append(z)
            elif axis == "T":
                index.append(t)
            else:
                index.append(0)
        index += [slice(y0, y1), slice(x0, x1)]
        return self._levels[level][tuple(index)]

    def close(self):
        try:
            self._tiff.close()
        except Exception:
            pass


class NgffImage(Image):
    kind = "ngff"

    def __init__(self, path):
        from plexora.server.utils import ome_zarr

        self.path = str(path)
        self._ome_zarr = ome_zarr
        self._pyramids = {}
        pyramid = self._pyramid(0, 0)
        root = ome_zarr._open_group(self.path)
        multiscale = ome_zarr._multiscale_of(root)
        names = ome_zarr._dataset_paths(multiscale)
        array = root[names[0]]
        axes = ome_zarr._axes_of(multiscale, array.ndim)
        sizes = dict(zip(axes, (int(s) for s in array.shape)))
        self.n_z = sizes.get("z", 1)
        self.n_t = sizes.get("t", 1)
        n_c = sizes.get("c", 1)
        self.dtype = np.dtype(array.dtype)
        self.levels = len(pyramid)
        self._shapes = pyramid.level_shapes
        self.channels = ome_zarr.channel_labels(self.path, n_c) or [f"ch{i}" for i in range(n_c)]
        physical = ome_zarr.physical_metadata(pyramid)
        self.pixel_size_um = _to_um(physical.get("physical_size_x"),
                                    physical.get("physical_size_x_unit"))
        self.ome_xml = ""

    def _pyramid(self, z, t):
        key = (z, t)
        if key not in self._pyramids:
            self._pyramids[key] = self._ome_zarr.open_image(self.path, t=t, z=z)
        return self._pyramids[key]

    def level_shape(self, level):
        height, width = self._shapes[level]
        return int(height), int(width)

    def _read(self, c, level, y0, y1, x0, x1, z, t):
        return self._pyramid(z, t)[level][c, y0:y1, x0:x1]


#: Plexora data roots whose nodes.json may hold a node's endpoint and token.
_PLEXORA_ROOTS: list = []


def register_plexora_root(root):
    root = str(root)
    if root not in _PLEXORA_ROOTS:
        _PLEXORA_ROOTS.insert(0, root)


def _node_entry(name):
    import json

    roots = list(_PLEXORA_ROOTS)
    if os.environ.get("PLEXORA_DATA_PATH"):
        roots.append(os.environ["PLEXORA_DATA_PATH"])
    if not roots:
        from plexora.paths import data_root

        roots.append(str(data_root()))
    for root in roots:
        nodes = Path(root) / "nodes.json"
        if nodes.exists():
            entry = json.loads(nodes.read_text(encoding="utf-8")).get(name)
            if entry:
                return entry
    raise UnsupportedImage(f"node {name!r} is not connected (no entry in nodes.json under "
                           f"{', '.join(roots)}); connect it in Plexora first")


class NodeImage(Image):
    """An image held by a Plexora data node, read through the node's raw
    region route (`POST /node/v1/image/<id>/region`): the file's own numbers,
    not the viewer's quantised tiles. Addressed as `node://<node>/<resource_id>`.
    The node's tunnel only lives while Plexora keeps it connected."""

    kind = "node"

    def __init__(self, path):
        import json
        import urllib.request

        self.path = str(path)
        node, _, self.resource_id = self.path[len("node://"):].partition("/")
        entry = _node_entry(node)
        self._base = entry["endpoint"].rstrip("/") + f"/node/v1/image/{self.resource_id}"
        self._headers = {"X-Plexora-Node-Token": entry["token"]}
        self._urlopen = urllib.request.urlopen
        self._request = urllib.request.Request
        geometry = self._get_json("/geometry")
        meta = self._get_json("/ome_metadata")
        self._shapes = [tuple(int(v) for v in s) for s in geometry["level_shapes"]]
        self.levels = len(self._shapes)
        n_c = int(geometry["num_channels"])
        names = geometry.get("channel_names") or []
        self.channels = list(names) if len(names) == n_c else [f"ch{i}" for i in range(n_c)]
        self.dtype = np.dtype(meta.get("type") or "uint16")
        self.significant_bits = meta.get("significant_bits")
        self.bits_per_sample = None
        self.pixel_size_um = _to_um(meta.get("physical_size_x"), meta.get("physical_size_x_unit"))
        self.n_z = int(meta.get("size_z") or 1)
        self.n_t = int(meta.get("size_t") or 1)
        if self.n_z > 1 or self.n_t > 1:
            raise UnsupportedImage("node z/t stacks are not read here yet")
        self._identity = json.dumps(geometry, sort_keys=True)

    def _get_json(self, route):
        import json

        req = self._request(self._base + route, headers=self._headers)
        with self._urlopen(req, timeout=120) as response:
            return json.loads(response.read())

    def fingerprint(self) -> str:
        import hashlib

        return "node:" + self.resource_id + ":" + hashlib.sha1(self._identity.encode()).hexdigest()[:12]

    def level_shape(self, level):
        return self._shapes[level]

    def _read(self, c, level, y0, y1, x0, x1, z, t):
        import json

        from plexora.server.providers.wire import unpack_array

        body = json.dumps({"level": int(level), "box": [x0, y0, x1, y1],
                           "channels": [int(c)]}).encode()
        req = self._request(self._base + "/region", data=body, method="POST",
                            headers={**self._headers, "Content-Type": "application/json"})
        with self._urlopen(req, timeout=600) as response:
            array, _meta = unpack_array(response.read())
        return array[0]


REMOTE_SCHEMES = ("gs://", "s3://", "http://", "https://")


class DicomImage(Image):
    """A DICOM-WSI multiplex slide (local folder or a bucket such as IDC's),
    through Plexora's own reader (`dicom_wsi.open_image`): raw values via
    wsidicom, remote bytes through Plexora's chunk cache."""

    kind = "dicom"

    def __init__(self, path):
        from plexora.server.utils import dicom_wsi

        self.path = str(path)
        self._pyramid = dicom_wsi.open_image(self.path)
        if self._pyramid.is_color:
            raise UnsupportedImage("colour (brightfield) DICOM is out of scope for this model")
        self._shapes = [tuple(int(v) for v in s) for s in self._pyramid.level_shapes]
        self.levels = len(self._shapes)
        first = self._pyramid["0"]
        n_c = int(first.shape[0])
        names = self._pyramid.channel_names or []
        self.channels = list(names) if len(names) == n_c else [f"ch{i}" for i in range(n_c)]
        self.dtype = np.dtype(first.dtype)
        mpp = self._pyramid.mpp
        self.pixel_size_um = float(mpp[1] or mpp[0]) if mpp and mpp[0] else None

    def fingerprint(self) -> str:
        import hashlib

        blob = f"{self.path}|{self._shapes}|{self.channels}|{self.dtype}"
        return "dicom:" + hashlib.sha1(blob.encode()).hexdigest()[:16]

    def level_shape(self, level):
        return self._shapes[level]

    def _read(self, c, level, y0, y1, x0, x1, z, t):
        return self._pyramid[str(level)][c, y0:y1, x0:x1]


def _is_dicom(path) -> bool:
    text = str(path)
    if text.startswith(REMOTE_SCHEMES):
        return True
    p = Path(text)
    return p.suffix.lower() == ".dcm" or (p.is_dir() and any(p.glob("*.dcm")))


def fingerprint_of(path) -> str:
    """Like `fingerprint`, for any address `open_image` accepts."""
    if str(path).startswith("node://") or _is_dicom(path):
        with open_image(path) as image:
            return image.fingerprint()
    return fingerprint(path)


def open_image(path) -> Image:
    if str(path).startswith("node://"):
        return NodeImage(str(path))
    if _is_dicom(path):
        return DicomImage(str(path))
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    if path.is_dir() or path.suffix.lower() == ".zarr":
        return NgffImage(path)
    return TiffImage(path)


def plane_or_warn(image: Image, z, t):
    """The plane to use, warning when a stack was not given one explicitly."""
    warn = []
    if z is None and image.n_z > 1:
        warn.append(f"no z given for a {image.n_z}-plane stack; using z=0 (never a projection)")
    if t is None and image.n_t > 1:
        warn.append(f"no t given for {image.n_t} timepoints; using t=0")
    for message in warn:
        warnings.warn(message, stacklevel=3)
    z, t = image.check_plane(z, t)
    return z, t, warn


def file_identity(path) -> tuple:
    """(size, mtime_ns) of a file, or of every file under a directory store:
    what the tests compare before and after a run to prove nothing was written."""
    path = Path(path)
    if path.is_file():
        stat = path.stat()
        return (stat.st_size, stat.st_mtime_ns)
    items = []
    for root, _dirs, files in os.walk(path):
        for name in sorted(files):
            stat = (Path(root) / name).stat()
            items.append((str(Path(root) / name), stat.st_size, stat.st_mtime_ns))
    return tuple(sorted(items))
