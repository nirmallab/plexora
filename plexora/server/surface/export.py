"""Surfaces out of Plexora: PLY, OBJ, STL and glTF (GLB), with their provenance.

Coordinates are written in microns when the spacing is known (voxels
otherwise), along the IMAGE's axes -- x to the right, y down, z through the
planes -- so a mesh exported here lands on the stack it came from in any tool
that opens the image with the same spacing. The units and the frame are said
in every file that has room for words (PLY and OBJ comments, the GLB's
`extras`) and in the `manifest.json` written beside it, which carries the
whole record of how the surface was made.

Object ids travel where the format allows: a PLY vertex property
`object_id`, an OBJ group per object (`o object_<id>`), a glTF vertex
attribute `_OBJECT_ID`. STL has nowhere to put one and says so.
"""

from __future__ import annotations

import io
import json
import struct
import zipfile

import numpy as np

from plexora.server.surface import mesh as mesh_mod

FORMATS = {"ply": "application/octet-stream", "obj": "text/plain",
           "stl": "application/octet-stream", "glb": "model/gltf-binary"}


class ExportRefused(ValueError):
    pass


def _prepare(surface, *, scale, ids=None):
    if ids is not None:
        surface = mesh_mod.subset(surface, ids)
    if not surface.n_triangles:
        raise ExportRefused("nothing to export: no object of this surface is selected")
    scale = np.asarray(scale, dtype=np.float64)
    positions = (surface.positions.astype(np.float64) * scale).astype(np.float32)
    normals = mesh_mod.vertex_normals(positions, surface.triangles)
    return surface, positions, normals


def _comments(manifest, units):
    label = str((manifest or {}).get("label") or "surface").replace("\n", " ")
    return [f"plexora surface: {label}",
            f"units: {units}; axes: image x right, y down, z through the planes",
            f"type: {(manifest or {}).get('type')}; algorithm: {(manifest or {}).get('algorithm')}"]


def ply(surface, *, scale, units, manifest=None, ids=None) -> bytes:
    surface, positions, normals = _prepare(surface, scale=scale, ids=ids)
    n, m = surface.n_vertices, surface.n_triangles
    header = ["ply", "format binary_little_endian 1.0"]
    header += [f"comment {line}" for line in _comments(manifest, units)]
    header += [f"element vertex {n}", "property float x", "property float y", "property float z",
               "property float nx", "property float ny", "property float nz",
               "property uint object_id", f"element face {m}",
               "property list uchar uint vertex_indices", "end_header"]
    vertex = np.zeros(n, dtype=[("p", "<f4", 3), ("n", "<f4", 3), ("id", "<u4")])
    vertex["p"] = positions
    vertex["n"] = normals
    vertex["id"] = surface.object_id
    face = np.zeros(m, dtype=[("k", "u1"), ("v", "<u4", 3)])
    face["k"] = 3
    face["v"] = surface.triangles
    return ("\n".join(header) + "\n").encode("ascii") + vertex.tobytes() + face.tobytes()


def obj(surface, *, scale, units, manifest=None, ids=None) -> bytes:
    surface, positions, normals = _prepare(surface, scale=scale, ids=ids)
    out = io.StringIO()
    for line in _comments(manifest, units):
        out.write(f"# {line}\n")
    np.savetxt(out, positions, fmt="v %.5f %.5f %.5f")
    np.savetxt(out, normals, fmt="vn %.4f %.4f %.4f")
    tris = surface.triangles.astype(np.int64) + 1
    for object_id, first, count in surface.objects:
        out.write(f"o object_{int(object_id)}\n")
        part = tris[int(first):int(first) + int(count)]
        faces = np.stack([part[:, 0], part[:, 0], part[:, 1], part[:, 1], part[:, 2], part[:, 2]],
                         axis=1)
        np.savetxt(out, faces, fmt="f %d//%d %d//%d %d//%d")
    return out.getvalue().encode("utf-8")


def stl(surface, *, scale, units, manifest=None, ids=None) -> bytes:
    surface, positions, _ = _prepare(surface, scale=scale, ids=ids)
    t = surface.triangles.astype(np.int64)
    a, b, c = positions[t[:, 0]], positions[t[:, 1]], positions[t[:, 2]]
    normal = np.cross(b - a, c - a)
    length = np.linalg.norm(normal, axis=1)
    length[length == 0] = 1
    normal = (normal / length[:, None]).astype(np.float32)
    record = np.zeros(len(t), dtype=[("n", "<f4", 3), ("a", "<f4", 3), ("b", "<f4", 3),
                                     ("c", "<f4", 3), ("attr", "<u2")])
    record["n"], record["a"], record["b"], record["c"] = normal, a, b, c
    title = f"plexora surface ({units}; no object ids in STL)".encode("ascii")[:80]
    return title.ljust(80, b" ") + struct.pack("<I", len(t)) + record.tobytes()


def glb(surface, *, scale, units, manifest=None, ids=None) -> bytes:
    """glTF 2.0 binary: one mesh, one primitive, `_OBJECT_ID` per vertex, the
    node scaled to metres from microns (or left in voxels), the manifest in
    `extras`."""
    surface, positions, normals = _prepare(surface, scale=scale, ids=ids)
    object_ids = surface.object_id.astype(np.float32)
    indices = surface.triangles.astype(np.uint32).ravel()
    chunks, views, accessors = [], [], []
    offset = 0

    def add(array, target, component, kind, minmax=False):
        nonlocal offset
        data = np.ascontiguousarray(array).tobytes()
        pad = (-len(data)) % 4
        views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(data), "target": target})
        chunks.append(data + b"\0" * pad)
        accessor = {"bufferView": len(views) - 1, "componentType": component,
                    "count": int(len(array)), "type": kind}
        if minmax:
            accessor["min"] = np.asarray(array).min(axis=0).astype(float).tolist()
            accessor["max"] = np.asarray(array).max(axis=0).astype(float).tolist()
        accessors.append(accessor)
        offset += len(data) + pad
        return len(accessors) - 1

    position = add(positions, 34962, 5126, "VEC3", minmax=True)
    normal = add(normals, 34962, 5126, "VEC3")
    owner = add(object_ids, 34962, 5126, "SCALAR")
    index = add(indices, 34963, 5125, "SCALAR")
    node = {"mesh": 0, "name": "plexora-surface"}
    if units == "um":
        node["scale"] = [1e-6, 1e-6, 1e-6]
    gltf = {
        "asset": {"version": "2.0", "generator": "Plexora Surface Studio"},
        "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [node],
        "meshes": [{"name": str((manifest or {}).get("label") or "surface"),
                    "primitives": [{"attributes": {"POSITION": position, "NORMAL": normal,
                                                   "_OBJECT_ID": owner},
                                    "indices": index, "mode": 4}]}],
        "buffers": [{"byteLength": offset}], "bufferViews": views, "accessors": accessors,
        "extras": {"plexora": {"units": units, "axes": "image x right, y down, z planes",
                               "manifest": manifest or {}}},
    }
    text = json.dumps(gltf, separators=(",", ":"), default=str).encode("utf-8")
    text += b" " * ((-len(text)) % 4)
    binary = b"".join(chunks)
    total = 12 + 8 + len(text) + 8 + len(binary)
    return (struct.pack("<4sII", b"glTF", 2, total)
            + struct.pack("<I4s", len(text), b"JSON") + text
            + struct.pack("<I4s", len(binary), b"BIN\0") + binary)


WRITERS = {"ply": ply, "obj": obj, "stl": stl, "glb": glb}


def export(surface, fmt, *, scale, units, manifest=None, ids=None, bundle=True,
           name=None) -> tuple[bytes, str, str]:
    """`(bytes, filename, mimetype)`: the mesh, zipped with its manifest unless
    `bundle` is False."""
    fmt = str(fmt or "").lower()
    if fmt not in WRITERS:
        raise ExportRefused(f"format is one of {', '.join(WRITERS)}")
    data = WRITERS[fmt](surface, scale=scale, units=units, manifest=manifest, ids=ids)
    name = name or (manifest or {}).get("label") or "surface"
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(name))[:80] or "surface"
    if not bundle:
        return data, f"{safe}.{fmt}", FORMATS[fmt]
    sidecar = dict(manifest or {})
    sidecar["export"] = {"format": fmt, "units": units,
                         "axes": "image x right, y down, z through the planes",
                         "objects": list(map(int, ids)) if ids is not None else "all"}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{safe}.{fmt}", data)
        archive.writestr(f"{safe}.manifest.json", json.dumps(sidecar, indent=2, default=str))
    return buffer.getvalue(), f"{safe}.{fmt}.zip", "application/zip"
