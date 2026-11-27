"""A step's versions on disk: what each run asked for, and the result of a run
a newer one replaced.

Every run of a step is a version (its record is in the notebook's database).
Its folder, `<notebook>/<NN-step>/v<N>/`, always holds `params.json` -- the
settings and the exact call -- and, once a newer run of the step overwrites
its result in the working file, the result itself:

    results.parquet   the obs columns it wrote (and obsm arrays, as columns)
    uns.h5            the uns elements it wrote (tables, `_params` records)
    manifest.json     which elements those are, the cells they belong to, and
                      Scimap Pro's ledger entry for the label

Nothing is copied twice: the working file holds the current version's result,
and only a replaced one is written out. "Make current" writes a saved version
back into the working file -- the ledger entry too, so Scimap Pro's own cache
sees exactly the result it recorded and reports a rerun of it as identical.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

MANIFEST = "manifest.json"
PARAMS = "params.json"
PARQUET = "results.parquet"
UNS = "uns.h5"


def step_folder(notebook_dir: Path, cell: dict, position: int) -> str:
    """`NN-step`, named once (on the step's first run) and kept: moving a step
    does not rename its folder."""
    if cell.get("folder"):
        return cell["folder"]
    from plexora.plugins.scimap.server import store, tools

    name = store.slug(tools.TOOLS.get(cell["tool"], {}).get("name") or cell["tool"], "step")
    base, i = f"{position + 1:02d}-{name}", 2
    candidate = base
    while (Path(notebook_dir) / candidate).exists():
        candidate, i = f"{base}-{i}", i + 1
    return candidate


def version_dir(notebook_dir: Path, folder: str, version: int) -> Path:
    return Path(notebook_dir) / folder / f"v{int(version)}"


def write_params(dest: Path, run: dict) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    body = {k: run.get(k) for k in ("version", "parent", "tool", "params", "effective",
                                    "started_at", "software", "licence")}
    (dest / PARAMS).write_text(json.dumps(body, indent=1, default=str))


def is_saved(dest: Path) -> bool:
    return (Path(dest) / MANIFEST).is_file()


def size(dest: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(dest):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def specs_of(run: dict) -> list:
    """The elements a run's result is: what its ledger named, plus the
    `_params`/`_samples` records Scimap Pro keeps beside a label."""
    label = (run.get("effective") or {}).get("label")
    specs = [s for s in run.get("outputs") or [] if isinstance(s, str) and not s.startswith("~")]
    if label and (run.get("effective") or {}).get("key"):
        specs += [f"uns:{label}_params", f"uns:{label}_samples"]
    return list(dict.fromkeys(specs))


def _index(root):
    import anndata.io as aio

    obs = root["obs"]
    name = obs.attrs.get("_index", "_index")
    if isinstance(name, bytes):
        name = name.decode()
    return [str(v) for v in aio.read_elem(obs[name])]


def _fingerprint(names) -> str:
    digest = hashlib.sha1()
    for name in names:
        digest.update(name.encode())
        digest.update(b"\0")
    return digest.hexdigest()


def _ledger(root) -> dict:
    import anndata.io as aio

    if "uns" not in root or "scimappro" not in root["uns"] or "results" not in root["uns/scimappro"]:
        return {}
    try:
        value = aio.read_elem(root["uns/scimappro/results"])
        return json.loads(value) if isinstance(value, str) else {}
    except Exception:  # noqa: BLE001 -- an unreadable ledger is an empty one
        return {}


def save(path, specs, label, dest: Path) -> dict:
    """Copy one result out of the working file into `dest`. Returns
    `{saved: [specs], bytes}`; elements no longer in the file are skipped."""
    import anndata.io as aio
    import h5py
    import pandas as pd

    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    frame, obsm, uns_saved, obs_saved = {}, {}, [], []
    with h5py.File(str(path), "r") as root:
        names = _index(root)
        for spec in specs:
            kind, _, name = spec.partition(":")
            if kind == "obs" and name in root["obs"]:
                frame[name] = aio.read_elem(root["obs"][name])
                obs_saved.append(name)
            elif kind == "obsm" and "obsm" in root and name in root["obsm"]:
                values = aio.read_elem(root["obsm"][name])
                values = values.to_numpy() if hasattr(values, "to_numpy") else values
                obsm[name] = int(values.shape[1])
                for i in range(values.shape[1]):
                    frame[f"{name}__{i}"] = values[:, i]
        ledger = _ledger(root).get(label) if label else None
        uns_specs = [s for s in specs if s.startswith("uns:") and s[4:] in root.get("uns", {})]
        if uns_specs:
            with h5py.File(str(dest / UNS), "w") as out:
                for spec in uns_specs:
                    name = spec[4:]
                    aio.write_elem(out, name, aio.read_elem(root["uns"][name]))
                    uns_saved.append(name)
    if frame:
        # No cell names: the manifest's fingerprint pins the order, and the
        # names would be most of the file.
        pd.DataFrame(frame).to_parquet(dest / PARQUET, index=False)
    manifest = {"label": label, "obs": obs_saved, "obsm": obsm, "uns": uns_saved,
                "ledger": ledger, "n_obs": len(names), "cells": _fingerprint(names)}
    (dest / MANIFEST).write_text(json.dumps(manifest, indent=1, default=str))
    return {"saved": [f"obs:{n}" for n in obs_saved] + [f"obsm:{n}" for n in obsm]
            + [f"uns:{n}" for n in uns_saved], "bytes": size(dest)}


class Mismatch(ValueError):
    """The saved result belongs to other cells (the data was prepared again)."""


def restore(path, dest: Path) -> list:
    """Write a saved result back into the working file, replacing what is
    there under the same names. Returns the elements written."""
    import anndata.io as aio
    import h5py
    import pandas as pd

    dest = Path(dest)
    manifest = json.loads((dest / MANIFEST).read_text())
    table = pd.read_parquet(dest / PARQUET) if (dest / PARQUET).is_file() else None
    written = []
    with h5py.File(str(path), "r+") as root:
        names = _index(root)
        if len(names) != manifest["n_obs"] or _fingerprint(names) != manifest["cells"]:
            raise Mismatch("This version was computed on cells the working file no longer holds "
                           "(the data was prepared again). Run it again instead.")
        obs = root["obs"]
        order = [c.decode() if isinstance(c, bytes) else str(c) for c in obs.attrs.get("column-order", [])]
        for name in manifest.get("obs") or []:
            values = table[name]
            values = values.array if isinstance(values.dtype, pd.CategoricalDtype) else values.to_numpy()
            if name in obs:
                del obs[name]
            aio.write_elem(obs, name, values)
            if name not in order:
                order.append(name)
            written.append(f"obs:{name}")
        obs.attrs["column-order"] = order
        for name, width in (manifest.get("obsm") or {}).items():
            values = table[[f"{name}__{i}" for i in range(int(width))]].to_numpy()
            group = root.require_group("obsm")
            if name in group:
                del group[name]
            aio.write_elem(group, name, values)
            written.append(f"obsm:{name}")
        if manifest.get("uns"):
            uns = root.require_group("uns")
            with h5py.File(str(dest / UNS), "r") as saved:
                for name in manifest["uns"]:
                    if name in uns:
                        del uns[name]
                    aio.write_elem(uns, name, aio.read_elem(saved[name]))
                    written.append(f"uns:{name}")
        label, entry = manifest.get("label"), manifest.get("ledger")
        if label and entry and "uns" in root and "scimappro" in root["uns"]:
            ledger = _ledger(root)
            ledger[label] = entry
            group = root["uns/scimappro"]
            if "results" in group:
                del group["results"]
            aio.write_elem(group, "results", json.dumps(ledger))
    return written


def labels(path, dest: Path | None, label):
    """One categorical result as an array in the working file's cell order:
    from a saved version when `dest` holds one (None if it belongs to other
    cells), else from the working file."""
    import anndata.io as aio
    import h5py
    import numpy as np
    import pandas as pd

    with h5py.File(str(path), "r") as root:
        if dest is not None and (Path(dest) / PARQUET).is_file():
            manifest = json.loads((Path(dest) / MANIFEST).read_text())
            if _fingerprint(_index(root)) != manifest.get("cells"):
                return None
            table = pd.read_parquet(Path(dest) / PARQUET, columns=[label])
            return table[label].astype(str).to_numpy()
        if label not in root["obs"]:
            return None
        return np.asarray(pd.Series(aio.read_elem(root["obs"][label])).astype(str))


def remove(dest: Path) -> None:
    shutil.rmtree(dest, ignore_errors=True)
