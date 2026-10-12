"""Refining a surface: each edit a new version, the mask never touched.

An edit reads the surface's current mesh and writes a NEW file beside it
(`mesh-L<k>-v<n>.bin`), appended to the manifest's `lods` with the operation
and its parameters in `versions` -- so the record of a surface says every
step between the extraction and what is drawn, and `undo` is dropping the
last version. Nothing here writes into a mask or an image: a cell surface
edited here is a different picture of the same segmentation, and the voxel
measurements in its table still describe the mask.

    remove_small   drop objects enclosing less than `min_size`
    keep_largest   keep the `n` largest objects
    delete / keep  drop / keep the objects `ids`
    smooth         Taubin again (`smoothing` preset), half a voxel at most
    decimate       fewer triangles (`ratio` kept), with fast-simplification
                   (`pip install fast-simplification`, optional and not a
                   dependency); refused without it, with that line
    undo           back to the version before
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import replace
from pathlib import Path

import numpy as np

from plexora.server.surface import mesh as mesh_mod

OPS = ("remove_small", "keep_largest", "delete", "keep", "smooth", "decimate", "undo")
INSTALL = "pip install fast-simplification"


class RefineRefused(ValueError):
    INSTALL = None

    def __init__(self, message, install=None):
        super().__init__(message)
        self.install = install


def _volumes(surface, scale):
    t = surface.triangles.astype(np.int64)
    p = surface.positions.astype(np.float64) * np.asarray(scale, dtype=np.float64)
    per = np.einsum("ij,ij->i", p[t[:, 0]], np.cross(p[t[:, 1]], p[t[:, 2]])) / 6.0
    return np.abs(np.add.reduceat(per, surface.objects[:, 1].astype(np.int64)))


def apply(surface, op, params, *, scale=(1.0, 1.0, 1.0)):
    """The edited mesh, and a sentence on what changed."""
    params = params or {}
    if op not in OPS or op == "undo":
        raise RefineRefused(f"op is one of {', '.join(o for o in OPS if o != 'undo')} or undo")
    surface = surface if surface.objects is not None else mesh_mod.sort_by_object(surface)
    ids = surface.objects[:, 0].astype(np.int64)
    if op == "remove_small":
        try:
            minimum = float(params.get("min_size"))
        except (TypeError, ValueError):
            raise RefineRefused("remove_small needs min_size") from None
        keep = ids[_volumes(surface, scale) >= minimum]
        out = mesh_mod.subset(surface, keep)
        return out, f"removed {len(ids) - len(keep)} object(s) smaller than {minimum:g}"
    if op == "keep_largest":
        try:
            n = max(1, int(params.get("n")))
        except (TypeError, ValueError):
            raise RefineRefused("keep_largest needs n") from None
        order = np.argsort(-_volumes(surface, scale), kind="stable")[:n]
        out = mesh_mod.subset(surface, ids[order])
        return out, f"kept the {min(n, len(ids))} largest of {len(ids)} object(s)"
    if op in ("delete", "keep"):
        try:
            chosen = {int(v) for v in params.get("ids") or ()}
        except (TypeError, ValueError):
            raise RefineRefused(f"{op} needs ids") from None
        if not chosen:
            raise RefineRefused(f"{op} needs ids")
        keep = [i for i in ids.tolist() if (i in chosen) == (op == "keep")]
        if not keep:
            raise RefineRefused("that would leave no object")
        out = mesh_mod.subset(surface, keep)
        return out, f"{'kept' if op == 'keep' else 'deleted'} {len(chosen & set(ids.tolist()))} object(s)"
    if op == "smooth":
        smoothing = mesh_mod.Smoothing.preset(params.get("smoothing") or "light")
        if smoothing.iterations <= 0:
            raise RefineRefused("smooth needs a smoothing of light, medium or strong")
        positions, moved = mesh_mod.taubin(surface.positions, surface.triangles, smoothing,
                                           scale=scale)
        out = replace(surface, positions=positions,
                      positions_raw=surface.positions_raw if surface.positions_raw is not None
                      else surface.positions)
        out.normals = mesh_mod.vertex_normals(out.positions, out.triangles)
        return out, (f"smoothed ({params.get('smoothing') or 'light'}): moved at most "
                     f"{moved['max_displacement']:.3g}")
    # decimate
    try:
        ratio = float(params.get("ratio", 0.5))
    except (TypeError, ValueError):
        raise RefineRefused("decimate needs ratio, the share of triangles kept") from None
    if not 0.02 <= ratio < 1:
        raise RefineRefused("ratio is between 0.02 and 1")
    try:
        import fast_simplification
    except ImportError:
        raise RefineRefused("decimating needs fast-simplification, which this Plexora was "
                            "installed without", install=INSTALL) from None
    pieces = []
    for object_id, first, count in surface.objects:
        part = mesh_mod.compact(replace(surface, triangles=surface.triangles[int(first):int(first) + int(count)],
                                        objects=None, positions_raw=None, normals=None))
        if part.n_triangles < 40:
            pieces.append(part)
            continue
        points, faces = fast_simplification.simplify(part.positions.astype(np.float32),
                                                     part.triangles.astype(np.int64),
                                                     target_reduction=1.0 - ratio)
        pieces.append(mesh_mod.Mesh(positions=np.asarray(points, np.float32),
                                    triangles=np.asarray(faces, np.uint32),
                                    object_id=np.full(len(points), int(object_id), np.uint32)))
    out = mesh_mod.sort_by_object(mesh_mod.concat(pieces))
    out.normals = mesh_mod.vertex_normals(out.positions, out.triangles)
    return out, f"decimated to {out.n_triangles} triangles ({ratio:.0%} kept)"


def refine(folder, op, params, *, scale, frame_for, physical):
    """Apply `op` to the surface in `folder` as a new version; returns the
    updated manifest. `frame_for(mesh) -> polars frame` measures a version."""
    folder = Path(folder)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    lods = list(manifest.get("lods") or [])
    if not lods:
        raise RefineRefused("this surface has no mesh yet")
    versions = list(manifest.get("versions") or [])
    if op == "undo":
        if not versions:
            raise RefineRefused("nothing to undo: this is the surface as it was extracted")
        last = versions.pop()
        dropped = lods.pop()
        (folder / dropped["path"]).unlink(missing_ok=True)
        current = mesh_mod.read(folder / lods[-1]["path"])
        note = f"undid: {last.get('note') or last.get('op')}"
    else:
        current = mesh_mod.read(folder / lods[-1]["path"])
        edited, note = apply(current, op, params, scale=scale)
        level = lods[-1]["level"]
        number = len(versions) + 1
        name = f"mesh-L{level}-v{number}.bin"
        info = mesh_mod.write(edited, folder / name, header={**current.header, "version": number})
        lods.append({"level": level, "path": name, "version": number, **info})
        versions.append({"version": number, "op": op, "params": params or {}, "note": note,
                         "parent": lods[-2].get("sha256"),
                         "at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()})
        current = edited
    frame = frame_for(current)
    tmp = folder / "objects.parquet.tmp"
    frame.write_parquet(tmp)
    tmp.replace(folder / "objects.parquet")
    manifest["lods"] = lods
    manifest["versions"] = versions
    manifest.setdefault("objects", {})["count"] = current.n_objects
    if "touches_bounds" in frame.columns:
        manifest["objects"]["touching_bounds"] = int(np.asarray(frame["touches_bounds"]).sum())
    manifest["last_edit"] = note
    tmp = folder / "manifest.json.tmp"
    tmp.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    tmp.replace(folder / "manifest.json")
    return manifest
