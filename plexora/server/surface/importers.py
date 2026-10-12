"""Meshes into Plexora: PLY, OBJ, STL and GLB, placed on the stack.

An imported mesh carries no voxels and no record of how it was made, so it
is placed by what its file (or the person importing it) says: its units
(microns or voxels) and an offset. A file Plexora exported comes back where
it was -- its manifest names the units and the frame. Measurements of an
imported mesh are the mesh's only, and say so.

Triangles only: polygons are fanned, points and lines are ignored.
"""

from __future__ import annotations

import io
import json
import struct

import numpy as np

from plexora.server.surface import mesh as mesh_mod

MAX_BYTES = 512 * 2**20


class ImportRefused(ValueError):
    pass


def _fan(faces):
    out = []
    for face in faces:
        for k in range(1, len(face) - 1):
            out.append((face[0], face[k], face[k + 1]))
    return out


def read_ply(data: bytes):
    """`(positions, triangles, object_ids or None, comments)`."""
    end = data.find(b"end_header")
    if not data.startswith(b"ply") or end < 0:
        raise ImportRefused("not a PLY file")
    header = data[:end].decode("ascii", "replace").splitlines()
    body = data[data.index(b"\n", end) + 1:]
    fmt = next((line.split()[1] for line in header if line.startswith("format")), "ascii")
    comments = [line[8:] for line in header if line.startswith("comment ")]
    elements, current = [], None
    for line in header:
        parts = line.split()
        if parts and parts[0] == "element":
            current = {"name": parts[1], "count": int(parts[2]), "props": []}
            elements.append(current)
        elif parts and parts[0] == "property" and current is not None:
            current["props"].append(parts[1:])
    types = {"char": "i1", "uchar": "u1", "short": "i2", "ushort": "u2", "int": "i4", "uint": "u4",
             "float": "f4", "double": "f8", "int8": "i1", "uint8": "u1", "int16": "i2",
             "uint16": "u2", "int32": "i4", "uint32": "u4", "float32": "f4", "float64": "f8"}
    positions = triangles = ids = None
    if fmt == "ascii":
        lines = iter(body.decode("ascii", "replace").split("\n"))
        for element in elements:
            rows = [next(lines).split() for _ in range(element["count"])]
            if element["name"] == "vertex":
                names = [p[-1] for p in element["props"]]
                table = np.asarray(rows, dtype=np.float64)
                positions = table[:, [names.index("x"), names.index("y"), names.index("z")]]
                if "object_id" in names:
                    ids = table[:, names.index("object_id")].astype(np.uint32)
            elif element["name"] == "face":
                triangles = np.asarray(_fan([[int(v) for v in row[1:1 + int(row[0])]]
                                             for row in rows]), dtype=np.int64)
    else:
        endian = "<" if "little" in fmt else ">"
        pos = 0
        for element in elements:
            props = element["props"]
            if element["name"] == "face" and props and props[0][0] == "list":
                count_type, index_type = types[props[0][1]], types[props[0][2]]
                faces = []
                count_size = np.dtype(count_type).itemsize
                index_size = np.dtype(index_type).itemsize
                fixed = element["count"] > 0
                # Fast path: every face a triangle.
                if fixed:
                    record = np.dtype([("k", endian + count_type), ("v", endian + index_type, 3)])
                    span = record.itemsize * element["count"]
                    trial = np.frombuffer(body, dtype=record, count=element["count"], offset=pos) \
                        if pos + span <= len(body) else None
                    if trial is not None and np.all(trial["k"] == 3):
                        triangles = trial["v"].astype(np.int64)
                        pos += span
                        continue
                for _ in range(element["count"]):
                    k = int(np.frombuffer(body, endian + count_type, 1, pos)[0])
                    pos += count_size
                    faces.append(np.frombuffer(body, endian + index_type, k, pos).tolist())
                    pos += k * index_size
                triangles = np.asarray(_fan(faces), dtype=np.int64)
                continue
            record = np.dtype([(p[-1], endian + types[p[0]]) for p in props])
            table = np.frombuffer(body, dtype=record, count=element["count"], offset=pos)
            pos += record.itemsize * element["count"]
            if element["name"] == "vertex":
                positions = np.stack([table["x"], table["y"], table["z"]], axis=1).astype(np.float64)
                if "object_id" in table.dtype.names:
                    ids = table["object_id"].astype(np.uint32)
    if positions is None or triangles is None:
        raise ImportRefused("the PLY file has no vertices or no faces")
    return positions, triangles, ids, comments


def read_obj(data: bytes):
    positions, faces, owners = [], [], []
    group = 0
    groups = {}
    comments = []
    for raw in data.decode("utf-8", "replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            comments.append(line[1:].strip())
        elif line.startswith("v "):
            positions.append([float(v) for v in line.split()[1:4]])
        elif line.startswith(("o ", "g ")):
            name = line.split(maxsplit=1)[1] if len(line.split()) > 1 else ""
            if name.startswith("object_") and name[7:].isdigit():
                group = int(name[7:])
            else:
                group = groups.setdefault(name, len(groups) + 1)
        elif line.startswith("f "):
            idx = [int(token.split("/")[0]) for token in line.split()[1:]]
            idx = [i - 1 if i > 0 else len(positions) + i for i in idx]
            for tri in _fan([idx]):
                faces.append(tri)
                owners.append(group)
    if not positions or not faces:
        raise ImportRefused("the OBJ file has no vertices or no faces")
    triangles = np.asarray(faces, dtype=np.int64)
    ids = None
    if any(owners):
        ids = np.zeros(len(positions), np.uint32)
        ids[triangles.ravel()] = np.repeat(np.asarray(owners, dtype=np.uint32), 3)
    return np.asarray(positions, dtype=np.float64), triangles, ids, comments


def read_stl(data: bytes):
    if len(data) >= 84:
        (count,) = struct.unpack("<I", data[80:84])
        if 84 + count * 50 == len(data):
            record = np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
            table = np.frombuffer(data, dtype=record, count=count, offset=84)
            positions = table["v"].reshape(-1, 3).astype(np.float64)
            triangles = np.arange(len(positions), dtype=np.int64).reshape(-1, 3)
            return positions, triangles, None, [data[:80].decode("ascii", "replace").strip()]
    text = data.decode("ascii", "replace")
    vertices = [[float(v) for v in line.split()[1:4]] for line in text.splitlines()
                if line.strip().startswith("vertex")]
    if not vertices:
        raise ImportRefused("not an STL file")
    positions = np.asarray(vertices, dtype=np.float64)
    return positions, np.arange(len(positions), dtype=np.int64).reshape(-1, 3), None, []


def read_glb(data: bytes):
    if data[:4] != b"glTF":
        raise ImportRefused("not a GLB file")
    (json_len,) = struct.unpack("<I", data[12:16])
    gltf = json.loads(data[20:20 + json_len].decode("utf-8"))
    binary_at = 20 + json_len
    binary = data[binary_at + 8:]
    sizes = {5126: ("<f4", 4), 5125: ("<u4", 4), 5123: ("<u2", 2), 5121: ("u1", 1)}
    counts = {"SCALAR": 1, "VEC3": 3, "VEC2": 2, "VEC4": 4}

    def accessor(index):
        acc = gltf["accessors"][index]
        view = gltf["bufferViews"][acc["bufferView"]]
        dtype, _ = sizes[acc["componentType"]]
        width = counts[acc["type"]]
        start = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
        out = np.frombuffer(binary, dtype=dtype, count=acc["count"] * width, offset=start)
        return out.reshape(acc["count"], width) if width > 1 else out

    positions, triangles, ids = [], [], []
    base = 0
    for mesh in gltf.get("meshes", []):
        for primitive in mesh.get("primitives", []):
            if primitive.get("mode", 4) != 4:
                continue
            p = accessor(primitive["attributes"]["POSITION"]).astype(np.float64)
            t = (accessor(primitive["indices"]).astype(np.int64).reshape(-1, 3)
                 if "indices" in primitive else np.arange(len(p)).reshape(-1, 3))
            owner = primitive["attributes"].get("_OBJECT_ID")
            ids.append(accessor(owner).astype(np.uint32) if owner is not None
                       else np.zeros(len(p), np.uint32))
            positions.append(p)
            triangles.append(t + base)
            base += len(p)
    if not positions:
        raise ImportRefused("the GLB file has no triangle mesh")
    extras = gltf.get("extras", {}).get("plexora", {})
    comments = [f"units: {extras.get('units')}"] if extras.get("units") else []
    node_scale = gltf.get("nodes", [{}])[0].get("scale")
    pos = np.concatenate(positions)
    if node_scale and extras.get("units") != "um":
        pos = pos * np.asarray(node_scale, dtype=np.float64)
    owners = np.concatenate(ids)
    return pos, np.concatenate(triangles), owners if owners.any() else None, comments


READERS = {"ply": read_ply, "obj": read_obj, "stl": read_stl, "glb": read_glb}


def read(data: bytes, fmt: str):
    fmt = str(fmt or "").lower().lstrip(".")
    if fmt not in READERS:
        raise ImportRefused(f"format is one of {', '.join(READERS)}")
    if len(data) > MAX_BYTES:
        raise ImportRefused("the file is larger than a surface can be (512 MB)")
    return READERS[fmt](data)


def place(positions, triangles, ids, *, units, spacing_um, offset=(0.0, 0.0, 0.0)):
    """A Mesh in level-0 voxels from a file's coordinates: microns (divided by
    the spacing) or voxels, minus `offset` (in the same units)."""
    p = np.asarray(positions, dtype=np.float64) - np.asarray(offset, dtype=np.float64)
    if units == "um":
        p = p / np.asarray(spacing_um, dtype=np.float64)
    elif units != "voxel":
        raise ImportRefused("units are um or voxel")
    triangles = np.asarray(triangles, dtype=np.int64)
    if len(triangles) and (triangles.min() < 0 or triangles.max() >= len(p)):
        raise ImportRefused("the file's faces name vertices it does not have")
    owner = np.ones(len(p), np.uint32) if ids is None else np.asarray(ids, dtype=np.uint32).copy()
    if not owner.any():
        owner[:] = 1                 # one object, numbered as an isosurface's largest
    positions32, tris, owner = mesh_mod.weld(p.astype(np.float32), triangles, owner)
    surface = mesh_mod.Mesh(positions=positions32, triangles=tris, object_id=owner)
    surface = mesh_mod.sort_by_object(surface)
    surface.normals = mesh_mod.vertex_normals(surface.positions, surface.triangles)
    return surface


def units_from_comments(comments) -> str | None:
    for line in comments or ():
        text = str(line).lower()
        if "units: um" in text or "units um" in text or "(um" in text:
            return "um"
        if "units: voxel" in text or "(voxel" in text:
            return "voxel"
    return None


def sniff(name: str) -> str:
    return str(name or "").rsplit(".", 1)[-1].lower()


def from_bundle(data: bytes, name: str):
    """A zip Plexora exported: the mesh inside and its manifest."""
    import zipfile

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.namelist()
        mesh_name = next((m for m in members if sniff(m) in READERS), None)
        if mesh_name is None:
            raise ImportRefused("the zip holds no PLY, OBJ, STL or GLB file")
        manifest = None
        side = next((m for m in members if m.endswith("manifest.json")), None)
        if side:
            try:
                manifest = json.loads(archive.read(side).decode("utf-8"))
            except ValueError:
                manifest = None
        return archive.read(mesh_name), sniff(mesh_name), manifest
