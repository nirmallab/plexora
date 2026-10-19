"""The classifier's results in the user's AnnData / SpatialData file.

Reached only through the `gating.save_gates` table operation, so it runs
where the file is (this machine, or the data node that holds the table), and
only when the person saves to the source -- the store is where the classifier
keeps its state; the file is where it is handed on.

- When every classified marker is a var name (the usual case), two layers the
  shape of X: `layers["plexora_binary"]` (int8: 1, 0, -1 not classified) and
  `layers["plexora_scores"]` (float32, NaN not classified). Created once, full
  size, filled with "not classified"; after that only this image's rows of the
  saved markers' columns are written, so saving one image of a cohort never
  rewrites the others.
- Otherwise (a marker that is not a var name) the same as two `obsm` frames
  with one column per marker, other images' rows kept.
- `uns["plexora_classifier"]`: what the codes mean, where the calls are, and
  per image a JSON record of each marker's examples, run, model and QC.

A SpatialData store's table is written the same way through
`anndata_gates._resolve_path`; the region / instance keys in obs are never
touched (obs is not rewritten at all).
"""

from __future__ import annotations

import base64
import gzip
import json
import re

import numpy as np
import pandas as pd

from plexora import api
from plexora.plugins.gating.server import anndata_gates as ag

BINARY_KEY = "plexora_binary"
SCORES_KEY = "plexora_scores"
UNS_KEY = "plexora_classifier"
CODES = {"positive": 1, "negative": 0, "not_classified": -1}


def pack_array(values) -> str:
    return base64.b64encode(gzip.compress(np.ascontiguousarray(values).tobytes())).decode("ascii")


def unpack_array(text, dtype) -> np.ndarray:
    return np.frombuffer(gzip.decompress(base64.b64decode(text)), dtype=dtype)


def _image_rows(group, imageid_column, image_id, n_rows):
    obs = ag._read_frame(group, "obs")
    if imageid_column in obs.columns:
        rows = np.flatnonzero(obs[imageid_column].astype(str).to_numpy() == str(image_id))
    else:
        rows = np.arange(len(obs))
    if rows.size != int(n_rows):
        raise ValueError(f"this project's table has {n_rows} cells but the file has "
                         f"{rows.size} rows for {image_id!r}; reopen the project and try again")
    return obs, rows


def _create_filled(group, name, shape, dtype, fill):
    """A full-size array filled with `fill`, written chunk by chunk by the
    backend rather than built in memory, with anndata's array encoding."""
    import h5py

    if isinstance(group, h5py.Group):
        array = group.create_dataset(name, shape=shape, dtype=dtype, fillvalue=fill,
                                     chunks=(min(shape[0], 65536) or 1, 1))
    else:
        array = group.create_array(name, shape=shape, dtype=dtype, fill_value=fill,
                                   chunks=(min(shape[0], 65536) or 1, 1))
    array.attrs["encoding-type"] = "array"
    array.attrs["encoding-version"] = "0.2.0"
    return array


def _assign(array, rows, column, values):
    """`array[rows, column] = values` for sorted `rows`: one contiguous slab
    read and written back -- what h5py and zarr both do well."""
    if rows.size == 0:
        return
    r0, r1 = int(rows[0]), int(rows[-1]) + 1
    if r1 - r0 == rows.size:
        array[r0:r1, column] = values
        return
    block = np.asarray(array[r0:r1, column])
    block[rows - r0] = values
    array[r0:r1, column] = block


def _layers(group, obs_n, var_n, rows, columns, markers):
    layers = group.require_group("layers")
    if "encoding-type" not in layers.attrs:
        layers.attrs["encoding-type"] = "dict"
        layers.attrs["encoding-version"] = "0.1.0"
    made = {}
    for key, dtype, fill, field in ((BINARY_KEY, np.int8, -1, "calls"),
                                    (SCORES_KEY, np.float32, np.nan, "scores")):
        array = layers[key] if key in layers else None
        if array is not None and tuple(array.shape) != (obs_n, var_n):
            del layers[key]
            array = None
        if array is None:
            array = _create_filled(layers, key, (obs_n, var_n), dtype, fill)
        for marker, entry in markers.items():
            _assign(array, rows, columns[marker], entry[field])
        made[key] = f"layers/{key}"
    return made


def _obsm(group, obs, rows, markers):
    obsm = group.require_group("obsm")
    index = obs.index.astype(str)
    made = {}
    for key, dtype, fill, field in ((BINARY_KEY, np.int8, -1, "calls"),
                                    (SCORES_KEY, np.float32, np.nan, "scores")):
        frame = None
        if key in obsm:
            existing = ag.read_elem(obsm[key])
            if isinstance(existing, pd.DataFrame) and len(existing) == len(obs):
                frame = existing.copy()
                frame.index = index
        if frame is None:
            frame = pd.DataFrame(index=index)
        for marker, entry in sorted(markers.items()):
            column = (frame[marker].to_numpy(dtype=dtype, copy=True) if marker in frame.columns
                      else np.full(len(obs), fill, dtype=dtype))
            column[rows] = entry[field]
            frame[marker] = column
        if key in obsm:
            del obsm[key]
        ag.write_elem(obsm, key, frame)
        made[key] = f"obsm/{key}"
    return made


def _image_key(image_id):
    return re.sub(r"[/\x00]", "_", str(image_id)) or "_"


def save_classifier(source, datasource_name, payload, imageid_column) -> dict:
    """Write one image's classifier results (see the module docstring).

    `payload`: {"n_rows", "markers": {marker: {"calls", "scores" (pack_array),
    "positive", "negative", "run"}}, "active": {marker: source},
    "segmentation_fingerprint", "qc_fingerprint", "qc_mode"}.
    """
    path = ag._resolve_path(source)
    image_id = ag.resolve_current_image_id(path, source, datasource_name, imageid_column)
    n_rows = int(payload.get("n_rows") or 0)
    markers = {}
    for marker, entry in (payload.get("markers") or {}).items():
        calls = unpack_array(entry["calls"], np.int8)
        scores = unpack_array(entry["scores"], np.float32)
        if calls.size != n_rows or scores.size != n_rows:
            raise ValueError(f"the classifier's calls for {marker!r} do not match the table")
        markers[marker] = {"calls": calls, "scores": scores}
    var_names = ag._read_var_names(path)
    display = api.deduplicate_names(var_names)
    columns = {name: i for i, name in enumerate(display)}
    aligned = all(marker in columns for marker in markers)
    with ag._open_group(path, writable=True) as group:
        obs, rows = _image_rows(group, imageid_column, image_id, n_rows)
        if not markers:
            written = {}
        elif aligned:
            written = _layers(group, len(obs), len(var_names), rows, columns, markers)
        else:
            written = _obsm(group, obs, rows, markers)
        uns = group.require_group("uns")
        images = {}
        if UNS_KEY in uns:
            try:
                existing = ag.read_elem(uns[UNS_KEY])
                images = dict((existing or {}).get("images") or {})
            except Exception:
                images = {}
        record = {
            "image_id": str(image_id),
            "segmentation_fingerprint": payload.get("segmentation_fingerprint") or "",
            "qc_fingerprint": payload.get("qc_fingerprint") or "",
            "qc_mode": payload.get("qc_mode") or "",
            "active": payload.get("active") or {},
            "markers": {m: {"positive": list(e.get("positive") or []),
                            "negative": list(e.get("negative") or []),
                            "run": e.get("run")}
                        for m, e in (payload.get("markers") or {}).items()},
        }
        images[_image_key(image_id)] = json.dumps(record, sort_keys=True, default=str)
        document = {
            "schema_version": 1,
            "codes": dict(CODES),
            "layout": "layers" if aligned else "obsm",
            "binary": written.get(BINARY_KEY, ""),
            "scores": written.get(SCORES_KEY, ""),
            "note": ("" if aligned or not markers else
                     "a classified marker is not a var name, so the calls are obsm frames "
                     "with one column per marker"),
            "images": images,
        }
        if UNS_KEY in uns:
            del uns[UNS_KEY]
        ag.write_elem(uns, UNS_KEY, document)
    return {"classifier_layout": document["layout"], "classifier_markers": sorted(markers),
            "classifier_binary": document["binary"], "classifier_scores": document["scores"]}


def payload_for(ds) -> dict | None:
    """What `save_classifier` needs for this project, or None when no marker
    has a run. Built on the primary, where the store is."""
    from plexora.agent import cell_exclusions
    from plexora.plugins.gating.server.classifier import calls, features, store

    markers = {}
    active = {}
    for marker in calls.classified_markers(ds):
        record, arrays = calls.run_arrays(ds, marker)
        if arrays is None:
            continue
        pos, neg = store.examples(ds.name, marker)
        scores = np.where(arrays["calls"] >= 0, arrays["scores"], np.nan).astype(np.float32)
        markers[marker] = {"calls": pack_array(arrays["calls"].astype(np.int8)),
                           "scores": pack_array(scores),
                           "positive": pos, "negative": neg,
                           "run": calls.public_run(record)}
        active[marker] = "classifier"
    if not markers:
        return None
    record = cell_exclusions.call_record(ds)
    frame = ds.table.geometry()
    return {"n_rows": int(frame.height) if frame is not None else 0, "markers": markers,
            "active": active, "segmentation_fingerprint": features.mask_fingerprint(ds),
            "qc_fingerprint": cell_exclusions.fingerprint(record),
            "qc_mode": cell_exclusions.current_call_mode()}
