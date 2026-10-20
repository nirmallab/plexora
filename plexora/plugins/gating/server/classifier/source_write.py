"""The classifier's results in the user's AnnData / SpatialData file.

Written only when the person saves to the source, and always where the file
is (this machine, or the data node that holds the table): the store is where
the classifier keeps its state; the file is where it is handed on. `save_all`
is the one entry point both save paths call (the panel's button and
`write_gates_to_source`): the gates first (`gating.save_gates`: `uns` gates,
provenance, not-called), then the classifier marker by marker
(`gating.save_classifier_marker`, batched under `BATCH_BYTES` of base64 a
request, so a thousand-marker panel on a million cells never travels as one
body), then `gating.save_classifier_done`, which writes the image's record
and clears what an earlier save left for markers no longer classified.

What lands in the file:

- Matrix form, never one row per (cell, marker). When every saved marker is a
  var name (the usual case), two layers the shape of X:
  `layers["plexora_binary"]` (int8: 1, 0, -1 not classified) and
  `layers["plexora_scores"]` (float32, NaN not classified). Created once, full
  size, filled with "not classified"; after that only this image's rows of
  the saved markers' columns are written, so saving one image of a cohort
  never rewrites the others. Otherwise (a marker that is not a var name) the
  same as two `obsm` frames with one column per marker, other images' rows
  kept.
- A column for every marker the classifier decides (its run's calls) AND
  every marker with a gating session's layer (the layer's calls). No
  override layer: where a layer parts ways with its gate is the binary
  column against `uns["gates"]`, and the overrides CSV is the audit file.
- `uns["plexora_classifier"]` (schema 2): what the codes mean, where the
  calls are, a note that the threshold decides unless a marker's record says
  `active: classifier`, and per image a JSON record with, per marker, which
  source decides it (`active`), where its calls came from (`origin`,
  `session_id`, `gate_used`, `call_rule`, `c_add`, `c_clean`, `versions`),
  how far they part from that gate (`n_added`, `n_removed`), `stale`, and
  its examples. `complete` is False until the done op ran, so a save that
  failed half way is readable and says so.

A SpatialData store's table is written the same way through
`anndata_gates._resolve_path`; the region / instance keys in obs are never
touched (obs is not rewritten at all).
"""

from __future__ import annotations

import base64
import gzip
import json
import re
import secrets

import numpy as np
import pandas as pd

from plexora import api
from plexora.plugins.gating.server import anndata_gates as ag

BINARY_KEY = "plexora_binary"
SCORES_KEY = "plexora_scores"
UNS_KEY = "plexora_classifier"
CODES = {"positive": 1, "negative": 0, "not_classified": -1}
SCHEMA_VERSION = 2
#: The most base64 one `gating.save_classifier_marker` request carries; a
#: marker larger than this travels alone.
BATCH_BYTES = 32 * 1024 * 1024

NOTE = ("uns['gates'] (or the key an image's record names as gates_key) holds the "
        "threshold of every thresholded marker, and the threshold "
        "decides a marker's calls unless its record here says active: classifier -- then "
        "its column of the binary layer is the calls that decide it. A marker whose "
        "record says active: threshold has a gating session's layer: the classifier's "
        "calls at that gate (gate_used), kept to be inspected, never deciding; n_added "
        "counts its positives below that gate, n_removed its negatives above it.")
OBSM_NOTE = ("a saved marker is not a var name, so the calls are obsm frames with one "
             "column per marker")


def pack_array(values) -> str:
    return base64.b64encode(gzip.compress(np.ascontiguousarray(values).tobytes())).decode("ascii")


def unpack_array(text, dtype) -> np.ndarray:
    return np.frombuffer(gzip.decompress(base64.b64decode(text)), dtype=dtype)


# -- the file side (runs where the file is) ---------------------------------------


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


_FIELDS = ((BINARY_KEY, np.int8, -1, "calls"), (SCORES_KEY, np.float32, np.nan, "scores"))


def _layers(group, obs_n, var_n, rows, columns, markers):
    layers = group.require_group("layers")
    if "encoding-type" not in layers.attrs:
        layers.attrs["encoding-type"] = "dict"
        layers.attrs["encoding-version"] = "0.1.0"
    made = {}
    for key, dtype, fill, field in _FIELDS:
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
    for key, dtype, fill, field in _FIELDS:
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


def _clear(group, rows, columns, markers):
    """This image's rows of `markers` back to "not classified", wherever an
    earlier save put them (layers or obsm)."""
    if not markers or rows.size == 0:
        return
    if "layers" in group:
        layers = group["layers"]
        for key, dtype, fill, _field in _FIELDS:
            if key not in layers:
                continue
            array = layers[key]
            for marker in markers:
                if marker in columns and columns[marker] < array.shape[1]:
                    _assign(array, rows, columns[marker], np.full(rows.size, fill, dtype=dtype))
    if "obsm" in group:
        obsm = group["obsm"]
        for key, dtype, fill, _field in _FIELDS:
            if key not in obsm:
                continue
            frame = ag.read_elem(obsm[key])
            if not isinstance(frame, pd.DataFrame):
                continue
            hit = [m for m in markers if m in frame.columns]
            if not hit:
                continue
            frame = frame.copy()
            for marker in hit:
                column = frame[marker].to_numpy(dtype=dtype, copy=True)
                column[rows] = fill
                frame[marker] = column
            del obsm[key]
            ag.write_elem(obsm, key, frame)


def _image_key(image_id):
    return re.sub(r"[/\x00]", "_", str(image_id)) or "_"


def _read_document(group) -> dict:
    uns = group["uns"] if "uns" in group else None
    if uns is None or UNS_KEY not in uns:
        return {}
    try:
        return dict(ag.read_elem(uns[UNS_KEY]) or {})
    except Exception:  # noqa: BLE001 - unreadable: written afresh
        return {}


def _image_record(document, image_id) -> dict:
    text = dict(document.get("images") or {}).get(_image_key(image_id))
    try:
        return dict(json.loads(text)) if text else {}
    except (TypeError, ValueError):
        return {}


def _write_document(group, document, image_id, record):
    images = dict(document.get("images") or {})
    images[_image_key(image_id)] = json.dumps(record, sort_keys=True, default=str)
    layout = record.get("layout") or document.get("layout") or "layers"
    out = {
        "schema_version": SCHEMA_VERSION,
        "codes": dict(CODES),
        "layout": layout,
        "binary": record.get("binary") or document.get("binary") or "",
        "scores": record.get("scores") or document.get("scores") or "",
        "note": NOTE,
        "layout_note": OBSM_NOTE if layout == "obsm" else "",
        "images": images,
    }
    uns = group.require_group("uns")
    if UNS_KEY in uns:
        del uns[UNS_KEY]
    ag.write_elem(uns, UNS_KEY, out)
    return out


def _target(source, datasource_name, imageid_column):
    path = ag._resolve_path(source)
    image_id = ag.resolve_current_image_id(path, source, datasource_name, imageid_column)
    var_names = ag._read_var_names(path)
    columns = {name: i for i, name in enumerate(api.deduplicate_names(var_names))}
    return path, image_id, var_names, columns


def save_markers(source, datasource_name, payload, imageid_column) -> dict:
    """One batch of markers (`gating.save_classifier_marker`): their columns
    of the layers (created full size the first time) and their records in
    this image's entry of `uns`, which stays `complete: False` until
    `save_done`.

    `payload`: {"n_rows", "save_id", "layout_markers" (every marker this save
    writes: all var names -> layers, else obsm), "markers": {marker:
    {"calls", "scores" (pack_array), "record"}}}.
    """
    path, image_id, var_names, columns = _target(source, datasource_name, imageid_column)
    n_rows = int(payload.get("n_rows") or 0)
    save_id = str(payload.get("save_id") or "")
    markers = {}
    for marker, entry in (payload.get("markers") or {}).items():
        calls = unpack_array(entry["calls"], np.int8)
        scores = unpack_array(entry["scores"], np.float32)
        if calls.size != n_rows or scores.size != n_rows:
            raise ValueError(f"the classifier's calls for {marker!r} do not match the table")
        markers[marker] = {"calls": calls, "scores": scores}
    layout_markers = list(payload.get("layout_markers") or markers)
    aligned = all(marker in columns for marker in layout_markers)
    with ag._open_group(path, writable=True) as group:
        obs, rows = _image_rows(group, imageid_column, image_id, n_rows)
        if not markers:
            written = {}
        elif aligned:
            written = _layers(group, len(obs), len(var_names), rows, columns, markers)
        else:
            written = _obsm(group, obs, rows, markers)
        document = _read_document(group)
        record = _image_record(document, image_id)
        if record.get("save_id") != save_id:
            # A new save: the markers an earlier one wrote stay on record
            # until the done op prunes the ones this save no longer has.
            record.update({"save_id": save_id, "complete": False})
        record.update({"schema_version": SCHEMA_VERSION, "image_id": str(image_id),
                       "layout": "layers" if aligned else "obsm",
                       "binary": written.get(BINARY_KEY, record.get("binary") or ""),
                       "scores": written.get(SCORES_KEY, record.get("scores") or "")})
        held = dict(record.get("markers") or {})
        for marker, entry in (payload.get("markers") or {}).items():
            held[marker] = {**(entry.get("record") or {}), "save_id": save_id}
        record["markers"] = held
        _write_document(group, document, image_id, record)
    return {"classifier_layout": record["layout"], "classifier_markers": sorted(markers),
            "classifier_binary": record["binary"], "classifier_scores": record["scores"]}


def save_done(source, datasource_name, payload, imageid_column) -> dict:
    """The end of a save (`gating.save_classifier_done`): this image's record
    made whole -- fingerprints, which source decides each marker, `complete:
    True` -- and the columns an earlier save wrote for markers this one does
    not carry cleared back to "not classified" (a run erased since, a
    session's layer discarded). Refused when a marker the save names was not
    written by it.

    `payload`: {"n_rows", "save_id", "markers" (the markers this save wrote),
    "image": {"segmentation_fingerprint", "qc_fingerprint", "qc_mode",
    "active", "not_written"}}.
    """
    path, image_id, _var_names, columns = _target(source, datasource_name, imageid_column)
    n_rows = int(payload.get("n_rows") or 0)
    save_id = str(payload.get("save_id") or "")
    names = list(payload.get("markers") or [])
    meta = payload.get("image") or {}
    if not names:
        # Nothing classified now: the file is written only when an earlier
        # save left something of this image's to clear.
        with ag._open_group(path) as group:
            if not _image_record(_read_document(group), image_id) \
                    and not meta.get("not_written"):
                return {"classifier_markers": [], "classifier_cleared": []}
    with ag._open_group(path, writable=True) as group:
        _obs, rows = _image_rows(group, imageid_column, image_id, n_rows)
        document = _read_document(group)
        record = _image_record(document, image_id)
        held = dict(record.get("markers") or {})
        missing = [m for m in names if (held.get(m) or {}).get("save_id") != save_id]
        if missing:
            raise ValueError("the classifier's calls for " + ", ".join(missing[:5])
                             + " were not written by this save; save again")
        stale = sorted(m for m in held if m not in names)
        _clear(group, rows, columns, stale)
        for marker in stale:
            held.pop(marker, None)
        record.update({
            "schema_version": SCHEMA_VERSION,
            "image_id": str(image_id),
            "save_id": save_id,
            "complete": True,
            "saved": meta.get("saved") or "",
            "segmentation_fingerprint": meta.get("segmentation_fingerprint") or "",
            "qc_fingerprint": meta.get("qc_fingerprint") or "",
            "qc_mode": meta.get("qc_mode") or "",
            "active": dict(meta.get("active") or {}),
            "not_written": dict(meta.get("not_written") or {}),
            "gates_key": meta.get("gates_key") or record.get("gates_key") or "gates",
            "markers": held,
        })
        written = _write_document(group, document, image_id, record)
    return {"classifier_markers": sorted(names), "classifier_cleared": stale,
            "classifier_layout": record.get("layout") or written["layout"],
            "classifier_binary": record.get("binary") or "",
            "classifier_scores": record.get("scores") or ""}


def save_classifier(source, datasource_name, payload, imageid_column) -> dict:
    """Write one image's classifier results in one go: what `gating.save_gates`
    does with a `classifier` member (a primary from before the chunked ops).

    `payload`: {"n_rows", "markers": {marker: {"calls", "scores" (pack_array),
    "positive", "negative", "run"} or {"calls", "scores", "record"}},
    "active": {marker: source}, "segmentation_fingerprint", "qc_fingerprint",
    "qc_mode"}.
    """
    save_id = str(payload.get("save_id") or secrets.token_hex(6))
    active = dict(payload.get("active") or {})
    markers = {}
    for marker, entry in (payload.get("markers") or {}).items():
        record = entry.get("record") or {
            "positive": list(entry.get("positive") or []),
            "negative": list(entry.get("negative") or []),
            "run": entry.get("run"), "active": active.get(marker, "classifier")}
        markers[marker] = {"calls": entry["calls"], "scores": entry["scores"], "record": record}
    base = {"n_rows": payload.get("n_rows"), "save_id": save_id}
    result = {}
    if markers:
        result = save_markers(source, datasource_name,
                              {**base, "layout_markers": list(markers), "markers": markers},
                              imageid_column)
    done = save_done(source, datasource_name, {
        **base, "markers": list(markers), "image": {
            "segmentation_fingerprint": payload.get("segmentation_fingerprint"),
            "qc_fingerprint": payload.get("qc_fingerprint"), "qc_mode": payload.get("qc_mode"),
            "active": active}}, imageid_column)
    return {"classifier_layout": result.get("classifier_layout")
            or done.get("classifier_layout") or "layers",
            "classifier_markers": sorted(markers),
            "classifier_binary": result.get("classifier_binary", ""),
            "classifier_scores": result.get("classifier_scores", "")}


# -- the primary side (where the store is) ------------------------------------------


def marker_record(ds, marker, record, arrays, which, decided) -> dict:
    """The `uns` record of one marker's column: which source decides it, the
    run the column's calls came from, how far they part from its gate, and
    the marker's examples."""
    from plexora.plugins.gating.server.classifier import calls, export, store

    summary = export.run_summary(ds, marker, record, which) or {}
    compared = summary.get("compare") or {}
    pos, neg = store.examples(ds.name, marker)
    called = arrays["calls"]
    return {
        "active": "classifier" if decided else "threshold",
        "which": which,
        "run_id": summary.get("run_id") or "",
        "origin": summary.get("origin") or "panel",
        "session_id": summary.get("session_id"),
        "gate_used": summary.get("gate_used"),
        "gate_source": summary.get("gate_source"),
        "call_rule": summary.get("call_rule"),
        "c_add": summary.get("c_add"),
        "c_clean": summary.get("c_clean"),
        "c_add_used": summary.get("c_add_used"),
        "versions": summary.get("versions"),
        "n_added": compared.get("n_added"),
        "n_removed": compared.get("n_removed"),
        "n_positive": int((called == 1).sum()),
        "n_classified": int((called >= 0).sum()),
        "stale": summary.get("stale"),
        "positive": pos,
        "negative": neg,
        "run": calls.public_run(record),
    }


def candidates(ds) -> dict:
    """{marker: (which, decided)} for every marker a save gives a column: the
    run that decides it, else a gating session's layer; table order."""
    from plexora.plugins.gating.server.classifier import calls

    out = {}
    for marker, entry in calls.markers_with_runs(ds).items():
        decided = entry["active"] == "classifier"
        if decided:
            out[marker] = ("run", True)
        elif entry.get("session_run"):
            out[marker] = ("session_run", False)
    return out


def entries(ds, not_written=None):
    """Yield {"marker", "calls", "scores", "record"} per `candidates` marker,
    one at a time (a panel of markers on a million cells is never held at
    once). A run whose arrays no longer line up with the table is left out
    and named in `not_written` ({marker: reason}) when one is given."""
    from plexora.plugins.gating.server.classifier import calls

    for marker, (which, decided) in candidates(ds).items():
        record, arrays = (calls.run_arrays if which == "run" else calls.session_arrays)(
            ds, marker)
        if arrays is None:
            if not_written is not None:
                not_written[marker] = ("its run no longer matches the table"
                                       if record is not None else "no run")
            continue
        scores = np.where(arrays["calls"] >= 0, arrays["scores"], np.nan).astype(np.float32)
        yield {"marker": marker, "calls": arrays["calls"].astype(np.int8), "scores": scores,
               "record": marker_record(ds, marker, record, arrays, which, decided)}


def _image_meta(ds, written, not_written):
    from plexora.agent import cell_exclusions
    from plexora.plugins.gating.server.classifier import features, store

    record = cell_exclusions.call_record(ds)
    active = {m: r["active"] for m, r in written.items()}
    for marker in not_written:
        active.setdefault(marker, store.active(ds.name, marker))
    return {"segmentation_fingerprint": features.mask_fingerprint(ds),
            "qc_fingerprint": cell_exclusions.fingerprint(record),
            "qc_mode": cell_exclusions.current_call_mode(), "active": active,
            "not_written": dict(not_written), "saved": store.now()}


def payload_for(ds) -> dict | None:
    """`save_classifier`'s single payload for this project (the one-request
    form), or None when no marker has a column to write."""
    not_written = {}
    markers = {}
    for entry in entries(ds, not_written):
        markers[entry["marker"]] = {"calls": pack_array(entry["calls"]),
                                    "scores": pack_array(entry["scores"]),
                                    "record": entry["record"]}
    if not markers:
        return None
    meta = _image_meta(ds, {m: e["record"] for m, e in markers.items()}, not_written)
    frame = ds.table.geometry()
    return {"n_rows": int(frame.height) if frame is not None else 0, "markers": markers,
            "active": meta["active"], "segmentation_fingerprint": meta["segmentation_fingerprint"],
            "qc_fingerprint": meta["qc_fingerprint"], "qc_mode": meta["qc_mode"]}


def _refusal(answer, written, what):
    message = (answer or {}).get("message") or "the write was refused"
    return {**(answer or {}), "ok": False,
            "message": f"{what}: {message}" if written else message,
            "classifier_markers": sorted(written)}


def write_classifier(ds, *, image_id, imageid_column, gates_key="gates") -> dict:
    """The classifier's part of a save, as table operations: batches of
    markers, then the done op. {"ok": True, classifier_*} or the first
    refusal (with the markers written before it)."""
    frame = ds.table.geometry()
    n_rows = int(frame.height) if frame is not None else 0
    base = {"image_id": image_id, "imageid_column": imageid_column, "n_rows": n_rows,
            "save_id": secrets.token_hex(6), "layout_markers": list(candidates(ds))}
    written, not_written = {}, {}
    batch, size, requests, layout = {}, 0, 0, {}

    def send():
        nonlocal batch, size, requests
        answer = ds.table.run("gating.save_classifier_marker", {**base, "markers": batch})
        requests += 1
        if not (answer or {}).get("ok"):
            return answer
        layout.update({k: answer.get(k) for k in ("classifier_layout", "classifier_binary",
                                                  "classifier_scores")})
        written.update({m: e["record"] for m, e in batch.items()})
        batch, size = {}, 0
        return None

    for entry in entries(ds, not_written):
        packed = {"calls": pack_array(entry["calls"]), "scores": pack_array(entry["scores"]),
                  "record": entry["record"]}
        weight = len(packed["calls"]) + len(packed["scores"]) + len(
            json.dumps(entry["record"], default=str))
        if batch and size + weight > BATCH_BYTES:
            refused = send()
            if refused is not None:
                return _refusal(refused, written, "The gates were saved, but not every "
                                                  "classifier marker")
        batch[entry["marker"]] = packed
        size += weight
    if batch:
        refused = send()
        if refused is not None:
            return _refusal(refused, written, "The gates were saved, but not every "
                                              "classifier marker")
    done = ds.table.run("gating.save_classifier_done", {
        **base, "markers": list(written),
        "image": {**_image_meta(ds, written, not_written), "gates_key": gates_key}})
    requests += 1
    if not (done or {}).get("ok"):
        return _refusal(done, written, "The gates and the classifier's calls were saved, but "
                                       "not the classifier's record")
    decides = sorted(m for m, r in written.items() if r["active"] == "classifier")
    return {"ok": True, "classifier_markers": sorted(written),
            "classifier_decides": decides,
            "classifier_session_layers": sorted(m for m in written if m not in decides),
            "classifier_not_written": not_written,
            "classifier_cleared": done.get("classifier_cleared") or [],
            "classifier_layout": layout.get("classifier_layout")
            or done.get("classifier_layout") or "",
            "classifier_binary": layout.get("classifier_binary")
            or done.get("classifier_binary") or "",
            "classifier_scores": layout.get("classifier_scores")
            or done.get("classifier_scores") or "",
            "classifier_requests": requests}


def save_all(ds, payload) -> dict:
    """Save to the source: the gates op (`payload` as `gating.save_gates`
    takes it -- gates, and provenance / not-called when the caller has them),
    then the classifier's calls. The gates op's result with the classifier's
    keys added, or {"ok": False, "message", ...} at the first refusal."""
    payload = {k: v for k, v in dict(payload).items() if k != "classifier"}
    result = ds.table.run("gating.save_gates", payload)
    if not (result or {}).get("ok"):
        return result
    classifier = write_classifier(ds, image_id=payload.get("image_id") or ds.name,
                                  imageid_column=payload.get("imageid_column"),
                                  gates_key=payload.get("table_name") or "gates")
    out = {**result, **{k: v for k, v in classifier.items() if k != "ok"}}
    out["ok"] = bool(classifier.get("ok"))
    return out
