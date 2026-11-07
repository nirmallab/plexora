"""Spike S-4's Python side: the same reads in the website's worker (Pyodide,
h5py over WORKERFS) and in CPython, so the two are compared line for line.

- `environment()`: h5py's and HDF5's versions and which filters it decodes.
- `reads(truth_source, path, repeat)`: P4's reads of an .h5ad
  (tools/poc_browser_node/bench/truth_p4.py `h5ad_truth`, run from its source
  so both sides run the same code), with a read that fails recorded as
  `{"error": ...}` instead of ending the file.
- `adapter(path, repeat)`: what the viewer's table load does with the file,
  `AnnDataAdapter.load_table()`, timed, with a digest of the frame.

    python tests/browser/spike_h5ad.py --truth truth_p4.py --repeat 3 A.h5ad ...

prints the CPython side as JSON; tests/browser/spike_h5ad.mjs runs the same
functions in the worker.

tests/browser/h5ad.mjs (the website end to end) uses two more, in CPython:
`facts(...)` (`--facts`), anndata's reading of the table it checks the
website's answers against, and `desktop(...)` (`--desktop`), the same import
through the desktop's app, whose project record the website's must equal.
"""

from __future__ import annotations

import hashlib
import json
import statistics
import sys
import time
import types


def environment() -> dict:
    import h5py

    z = h5py.h5z
    filters = {name: bool(z.filter_avail(code)) for name, code in
               (("gzip", z.FILTER_DEFLATE), ("lzf", z.FILTER_LZF), ("szip", z.FILTER_SZIP),
                ("shuffle", z.FILTER_SHUFFLE))}
    return {"python": sys.version.split()[0], "platform": sys.platform, "h5py": h5py.__version__,
            "hdf5": h5py.version.hdf5_version, "filters": filters}


class _Failed:
    """A read that raised, standing in for its value so the rest go on."""

    def __init__(self, error):
        self.error = error

    def __getitem__(self, key):
        return self

    def astype(self, *args, **kwargs):
        return self


def reads(truth_source: str, path: str, repeat: int) -> dict:
    ns = {"__name__": "truth_p4"}
    # truth_p4 imports hdf5plugin for blosc and zstd; these fixtures use none.
    sys.modules.setdefault("hdf5plugin", types.ModuleType("hdf5plugin"))
    exec(compile(truth_source, "truth_p4.py", "exec"), ns)
    timed, sha = ns["timed"], ns["sha"]

    def guarded_timed(fn, n):
        try:
            return timed(fn, n)
        except Exception as exc:  # noqa: BLE001 -- recorded per read
            return _Failed(f"{type(exc).__name__}: {exc}"[:300]), None

    def guarded_sha(data):
        return {"error": data.error} if isinstance(data, _Failed) else sha(data)

    ns["timed"], ns["sha"] = guarded_timed, guarded_sha
    try:
        return ns["h5ad_truth"](path, repeat)
    except Exception as exc:  # noqa: BLE001 -- the file as a whole
        return {"file": path, "error": f"{type(exc).__name__}: {exc}"[:300], "ops": {}}


def adapter(path: str, repeat: int) -> dict:
    """`AnnDataAdapter.load_table()` on `path` as an import reads it (X,
    coordinates from obsm["spatial"], the index as the identifier)."""
    import numpy as np

    from plexora.server.models.adapters.anndata_adapter import AnnDataAdapter

    spec = types.SimpleNamespace(
        src=path, type="h5ad", coordinates={}, features={}, subset={}, obs_id_field=None,
        is_transformed=False, roles=types.SimpleNamespace(celltype=None, image_id=None))
    runs, table = [], None
    for _ in range(repeat):
        table = None
        started = time.perf_counter()
        table = AnnDataAdapter(spec).load_table()
        runs.append((time.perf_counter() - started) * 1000)
    frame = table.table
    digest = hashlib.sha256()
    for name in ["X", "Y", *table.feature_columns]:
        digest.update(np.ascontiguousarray(frame[name].to_numpy()).tobytes())
    digest.update("\n".join(str(v) for v in frame["obs_id"].to_list()).encode())
    return {"ms": statistics.median(runs), "runs": [round(r, 1) for r in runs],
            "rows": frame.height, "columns": frame.width, "features": len(table.feature_columns),
            "sha256": digest.hexdigest()}


def facts(path: str, marker: str | None, obs_column: str | None, subset: str | None = None) -> dict:
    """What tests/browser/h5ad.mjs checks the website's answers against,
    read by anndata here: the table's size and markers, the first cell's
    position and `marker` value, the marker's mean, `obs_column`'s counts --
    of the rows whose obs `COLUMN` is `VALUE` when `subset` is "COLUMN=VALUE"
    (the image the import was told the table is)."""
    import anndata
    import numpy as np

    adata = anndata.read_h5ad(path)
    if subset:
        column, value = subset.split("=", 1)
        adata = adata[(adata.obs[column].astype(str) == value).to_numpy()]
    names = [str(name) for name in adata.var_names]
    marker = marker or names[0]
    column = adata.X[:, names.index(marker)]
    column = np.asarray(column.toarray() if hasattr(column, "toarray") else column).ravel()
    if "spatial" in adata.obsm:
        xy = np.asarray(adata.obsm["spatial"])[:, :2]
    else:
        xy = np.column_stack([adata.obs["X_centroid"], adata.obs["Y_centroid"]])
    out = {"rows": int(adata.n_obs), "markers": names, "marker": marker,
           "first": {"name": str(adata.obs_names[0]), "x": float(xy[0, 0]), "y": float(xy[0, 1]),
                     "value": float(column[0])},
           "mean": float(column.astype(np.float64).mean())}
    if obs_column:
        counts = adata.obs[obs_column].astype(str).value_counts()
        out["obs"] = {str(key): int(value) for key, value in counts.items()}
    return out


def answers_for(inspected: dict, subset: str | None) -> dict:
    """The import's answers: the table's image question answered with
    `subset`'s VALUE, as a user picks it in the Import dialog."""
    if not subset:
        return {}
    value = subset.split("=", 1)[1]
    return {question["id"]: value for sample in inspected.get("samples") or []
            for question in sample.get("questions") or []
            if str(question.get("id", "")).startswith("subset:")
            and value in [option.get("value") for option in question.get("options") or []]}


def desktop(paths: list, subset: str | None = None) -> dict:
    """The same import through the desktop's app (CPython, this checkout, a
    data root of its own: PLEXORA_DATA_PATH set by the caller): the project
    record and Cell Explorer's requirements, which the website's must equal."""
    import plexora

    client = plexora.app.test_client()
    inspected = client.post("/import/inspect", json={"paths": paths})
    answers = answers_for(inspected.get_json() or {}, subset)
    imported = client.post("/import/sample", json={"paths": paths, "answers": answers})
    name = (imported.get_json() or {}).get("name")
    config = client.get("/config").get_json() or {}
    entry = config.get(name) or {}
    requirements = client.get(f"/{name}/tools/cell_explorer/requirements")
    # The viewer's table load and the reads after it, timed as the website's
    # driver times them (from asking for the project to its table ready).
    timings = {}
    query = f"datasource={name}"
    started = time.perf_counter()
    client.get(f"/init_database?{query}")
    while True:
        status = client.get(f"/get_table_status?{query}").get_json() or {}
        if status.get("status") == "ready" or status.get("error") or time.perf_counter() - started > 600:
            break
        time.sleep(0.05)
    timings["tableReadyMs"] = round((time.perf_counter() - started) * 1000)
    marker = (((entry.get("dataset") or {}).get("columns") or {}).get("markers") or [None])[0]
    for key, route in (("describeMs", f"/get_database_description?{query}"),
                       ("nearestMs", f"/get_nearest_cell?point_x=0&point_y=0&{query}"),
                       ("allCellsMs", f"/get_all_cells/float/?{query}&start_keys=id,X,Y,{marker}")):
        began = time.perf_counter()
        client.get(route)
        timings[key] = round((time.perf_counter() - began) * 1000)
    return {"inspect": inspected.status_code, "sample": imported.status_code, "name": name,
            "dataset": entry.get("dataset"), "roles": entry.get("roles"),
            "requirements": requirements.get_json(), "timings": timings}


def main(argv=None) -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--truth", help="truth_p4.py")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--adapter-only", action="store_true")
    parser.add_argument("--facts", action="store_true", help="facts() of each file instead")
    parser.add_argument("--desktop", action="store_true", help="desktop() of the files together")
    parser.add_argument("--marker")
    parser.add_argument("--obs")
    parser.add_argument("--subset", help="COLUMN=VALUE: the image the table is told it is")
    parser.add_argument("files", nargs="+")
    args = parser.parse_args(argv)
    if args.facts:
        print(json.dumps({path: facts(path, args.marker, args.obs, args.subset) for path in args.files}))
        return
    if args.desktop:
        print(json.dumps(desktop(args.files, args.subset)))
        return
    with open(args.truth, encoding="utf-8") as handle:
        source = handle.read()
    out = {"environment": environment(), "files": {}}
    for path in args.files:
        entry = {} if args.adapter_only else {"reads": reads(source, path, args.repeat)}
        try:
            entry["adapter"] = adapter(path, args.repeat)
        except Exception as exc:  # noqa: BLE001
            entry["adapter"] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
        out["files"][path] = entry
    print(json.dumps(out))


if __name__ == "__main__":
    main()
