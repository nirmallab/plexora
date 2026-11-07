"""The desktop's side of tests/browser/source_writes.mjs: the same saves into
the same files, made by this checkout's app in CPython (the desktop's own
routes, through Flask's test client, on a data root of its own -- set
PLEXORA_DATA_PATH), so the website's saved files can be held to the bytes the
desktop writes.

    python tests/browser/source_writes_desktop.py --steps STEPS.json --out DIR \\
        --image IMAGE --image2 IMAGE2 [--csv CELLS.csv] [--h5ad CELLS.h5ad]

The inputs are copied into DIR first (the originals are left alone), each is
imported with an image (the CSV with IMAGE, the .h5ad with IMAGE2: a
project each; /import/inspect, /import/sample), its table loaded,
and then: the CSV gets STEPS' regions (`regions`, /plugins/roi/api/import) and
their columns (/plugins/roi/api/map_to_cells, name `rois`); the .h5ad gets
STEPS' gates (`gates`, /plugins/gating/save_gating_list) written into it
(/plugins/gating/save_gates_to_anndata). Prints JSON: per table its project,
each route's answer, the written file and its sha256.

    python tests/browser/source_writes_desktop.py --compare WEBSITE.h5ad DESKTOP.h5ad

prints whether two saved .h5ad files are the same bytes, or the same contents.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _import(client, paths: list) -> str:
    inspected = client.post("/import/inspect", json={"paths": paths})
    assert inspected.status_code == 200, inspected.get_data(as_text=True)[:2000]
    imported = client.post("/import/sample", json={"paths": paths, "answers": {}})
    name = (imported.get_json() or {}).get("name")
    assert name, imported.get_data(as_text=True)[:2000]
    return name


def _ready(client, name: str) -> dict:
    query = f"datasource={name}"
    client.get(f"/init_database?{query}")
    started = time.perf_counter()
    while True:
        status = client.get(f"/get_table_status?{query}").get_json() or {}
        if status.get("status") == "ready" or status.get("error") or time.perf_counter() - started > 300:
            return status
        time.sleep(0.05)


def run(steps: dict, out: Path, images: tuple, csv: Path | None, h5ad: Path | None) -> dict:
    import plexora

    client = plexora.app.test_client()
    out.mkdir(parents=True, exist_ok=True)
    here = []
    for image in images:
        here.append(out / image.name)
        shutil.copyfile(image, here[-1])
    result = {}
    if csv is not None:
        table = out / csv.name
        shutil.copyfile(csv, table)
        name = _import(client, [str(here[0]), str(table)])
        status = _ready(client, name)
        revision = client.get(f"/plugins/roi/api/state?datasource={name}").get_json()["revision"]
        regions = client.post("/plugins/roi/api/import",
                              json={"datasource": name, "document": steps["regions"],
                                    "base_revision": revision}).get_json()
        mapped = client.post("/plugins/roi/api/map_to_cells",
                             json={"datasource": name, "name": "rois"}).get_json()
        # The desktop copies a flat table into the project at import
        # (import_routes._copy_into_project): the columns land in that copy.
        written = Path(mapped.get("path") or table)
        result["csv"] = {"project": name, "table": status.get("status"), "regions": regions,
                         "mapped": mapped, "path": str(written), "sha256": _sha256(written)}
    if h5ad is not None:
        table = out / h5ad.name
        shutil.copyfile(h5ad, table)
        name = _import(client, [str(here[1]), str(table)])
        status = _ready(client, name)
        gates = steps["gates"]
        listed = client.post("/plugins/gating/save_gating_list", json={
            "datasource": name, "filter": gates, "channels": gates}).get_json()
        saved = client.post("/plugins/gating/save_gates_to_anndata",
                            json={"datasource": name}).get_json()
        result["h5ad"] = {"project": name, "table": status.get("status"), "listed": listed,
                          "saved": saved, "path": str(table), "sha256": _sha256(table)}
    return result


def compare(website: Path, desktop: Path) -> dict:
    """Two saved .h5ad files: the same bytes, or the same contents -- X, obs,
    var, obsm and every `uns` entry (HDF5's own layout differs between the
    library builds Pyodide and CPython ship)."""
    import anndata as ad
    import numpy as np

    out = {"same_bytes": website.read_bytes() == desktop.read_bytes()}
    a, b = ad.read_h5ad(website), ad.read_h5ad(desktop)
    uns = sorted(a.uns) == sorted(b.uns) and all(
        _same(a.uns[key], b.uns[key]) for key in a.uns)
    out.update(uns_keys=sorted(a.uns), uns=uns,
               x=bool(np.array_equal(np.asarray(a.X), np.asarray(b.X))),
               obs=a.obs.equals(b.obs), var=a.var.equals(b.var),
               obsm=sorted(a.obsm) == sorted(b.obsm) and all(
                   np.array_equal(np.asarray(a.obsm[k]), np.asarray(b.obsm[k])) for k in a.obsm))
    out["same_contents"] = all(out[k] for k in ("uns", "x", "obs", "var", "obsm"))
    return out


def _same(a, b) -> bool:
    import numpy as np
    import pandas as pd

    if isinstance(a, pd.DataFrame):
        return isinstance(b, pd.DataFrame) and a.equals(b)
    if isinstance(a, dict):
        return isinstance(b, dict) and sorted(a) == sorted(b) and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, np.ndarray) or isinstance(b, np.ndarray):
        return bool(np.array_equal(np.asarray(a), np.asarray(b)))
    return a == b


def main(argv=None) -> None:
    if argv is None and "--compare" in sys.argv:
        at = sys.argv.index("--compare")
        sys.stdout.write(json.dumps(compare(Path(sys.argv[at + 1]), Path(sys.argv[at + 2]))) + "\n")
        return
    parser = argparse.ArgumentParser()
    parser.add_argument("--steps", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--image2", required=True)
    parser.add_argument("--csv")
    parser.add_argument("--h5ad")
    args = parser.parse_args(argv)
    steps = json.loads(Path(args.steps).read_text(encoding="utf-8"))
    result = run(steps, Path(args.out), (Path(args.image), Path(args.image2)), Path(args.csv) if args.csv else None,
                 Path(args.h5ad) if args.h5ad else None)
    sys.stdout.write(json.dumps(result, default=str) + "\n")


if __name__ == "__main__":
    main()
