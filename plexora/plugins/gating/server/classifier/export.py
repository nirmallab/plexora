"""The classifier's files: three wide CSVs, one annotations JSON, and its import.

Every CSV is one row per cell (`image_id, cell_id`) and one column per marker
-- matrix form, never one row per (cell, marker):

- **binary CSV** -- 1 positive, 0 negative, blank where the cell was not
  classified (QC ruled it out, it has no value).
- **scores CSV** -- the same rows and columns, the model's score (6
  significant figures), blank where not classified. The scores are called
  probabilities only where the run was calibrated (`learner.CALIBRATE_AT`).
- **overrides CSV** -- where a run made at a gate parts ways with that gate:
  1 positive below it, -1 negative above it, 0 the two agree, blank where
  either does not call the cell. The audit file: a gating session's layer
  (or an adopted one) may call a weak positive below the threshold, or take
  a positive above it away, and this is where each such cell is listed.
- **annotations JSON** -- the training examples by cell id, per marker, each
  with where it came from (`origin`: a click, an import, the wand's pick, an
  agent's label), with which image and mask they were made on, and per
  marker a summary of its runs. Loading one back is checked first
  (`preview`) and applied only when the person picks Merge or Replace.

Which markers each CSV carries (`plan`), in table order:

- binary / scores: every marker the classifier decides, its run's calls; with
  `include_session_runs` also every marker that only has a gating session's
  layer, the layer's calls (the threshold still decides such a marker).
- overrides: every marker with a run made at a gate (`gate_used`): the run
  that decides the marker when it was made at one, else the session's layer.

The CSVs are written in row chunks so a million-cell table streams rather
than being built in memory as text.
"""

from __future__ import annotations

import csv
import io
import json
import secrets
import time

import numpy as np

from plexora.plugins.gating.server.classifier import ClassifierRefused, calls, features, store

FORMAT = "plexora.classifier.annotations"
#: 2: every example's `origin`, and each marker's `runs`. Version 1 files
#: (positive / negative lists only) are still read.
SCHEMA_VERSION = 2
CSV_KINDS = ("binary", "scores", "overrides")
CHUNK_ROWS = 50_000
#: How long a previewed import waits to be applied.
IMPORT_TTL_S = 600
#: Origins an import keeps as they were written. Everything else -- a click
#: on another machine, an earlier import -- comes back as "import": a
#: person's label, weight 1. The wand's and an agent's labels weigh half
#: (`runs.PERSON_ORIGINS`), and a file passing through must not promote them.
KEPT_ORIGINS = ("auto", "agent")

#: What a run summary (annotations `runs`, `uns["plexora_classifier"]`, the
#: gates export) says about a run.
RUN_KEYS = ("run_id", "created", "origin", "session_id", "gate_used", "gate_source",
            "call_rule", "c_add", "c_clean", "c_add_used", "status", "versions")
COMPARE_KEYS = ("n_called", "n_agree", "n_added", "n_removed", "n_gate_positive",
                "n_positive", "n_raw_added", "n_raw_removed")


def _quote(text) -> str:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="").writerow([text])
    return buffer.getvalue()


def _image_ids(ds, n):
    """The image id of each row: the table's image-id column, else the
    project's name."""
    frame = ds.table.geometry()
    column = ds.schema.image_id if ds.schema else None
    if frame is not None and column and column in frame.columns:
        values = frame[column].cast(str).to_numpy().astype(str)
        return np.array([_quote(v) for v in values], dtype=object) \
            if any(("," in v or '"' in v) for v in set(values.tolist())) else values
    return np.full(n, _quote(ds.name))


# -- which run each marker's column comes from ----------------------------------


def made_at_gate(record) -> bool:
    used = (record or {}).get("gate_used")
    return bool(used) and used[0] is not None


def _read(ds, marker, which):
    return (calls.run_arrays if which == "run" else calls.session_arrays)(ds, marker)


def plan(ds, kind="binary", include_session_runs=False) -> dict:
    """{marker: {"record", "arrays", "which", "active"}} for the CSV of `kind`,
    in table order. `which` is "run" (the run that decides the marker) or
    "session_run" (a gating session's layer); `active` is the source that
    decides the marker. A run whose arrays no longer line up with the table
    is left out. For "overrides" the arrays carry `gate`: the gate calls the
    run is compared with (`calls._gate_of`, today's QC)."""
    if kind not in CSV_KINDS:
        raise ClassifierRefused(f"kind is one of {', '.join(CSV_KINDS)}")
    out = {}
    for marker, entry in calls.markers_with_runs(ds).items():
        decided = entry["active"] == "classifier"
        if kind == "overrides":
            order = (["run"] if decided else []) + (["session_run"] if entry["session_run"]
                                                    else [])
            which = next((w for w in order if made_at_gate(
                store.run_record(ds.name, marker) if w == "run"
                else store.session_run_record(ds.name, marker))), None)
        elif decided:
            which = "run"
        else:
            which = "session_run" if include_session_runs and entry["session_run"] else None
        if which is None:
            continue
        record, arrays = _read(ds, marker, which)
        if arrays is None:
            continue
        if kind == "overrides":
            gate = calls._gate_of(ds, marker, record, arrays)
            if gate is None or gate.shape != arrays["calls"].shape:
                continue
            arrays = {**arrays, "gate": gate}
        out[marker] = {"record": record, "arrays": arrays, "which": which,
                       "active": "classifier" if decided else "threshold"}
    return out


def exported_markers(ds, include_session_runs=False):
    """(markers, {marker: arrays}) the binary and scores CSVs carry."""
    picked = plan(ds, "binary", include_session_runs)
    return list(picked), {m: p["arrays"] for m, p in picked.items()}


def columns_of(picked) -> dict:
    """{marker: {which, active, run_id}}: where each CSV column came from."""
    return {m: {"which": p["which"], "active": p["active"],
                "run_id": (p["record"] or {}).get("run_id")} for m, p in picked.items()}


def override_codes(calls_, gate):
    """int8 per row: 1 positive below the gate, -1 negative above it, 0 the
    two agree; -2 where either does not call the cell."""
    calls_ = np.asarray(calls_, dtype=np.int8)
    gate = np.asarray(gate, dtype=np.int8)
    out = np.zeros(calls_.shape, dtype=np.int8)
    out[(calls_ == 1) & (gate == 0)] = 1
    out[(calls_ == 0) & (gate == 1)] = -1
    out[(calls_ < 0) | (gate < 0)] = -2
    return out


def stream_csv(ds, kind, include_session_runs=False, picked=None):
    """(markers, a generator of CSV text): `kind` "binary", "scores" or
    "overrides" (see the module docstring). `picked`: a `plan` already made."""
    from plexora.plugins.gating.server.classifier.geometry import cells

    if picked is None:
        picked = plan(ds, kind, include_session_runs)
    markers = list(picked)
    ids = cells(ds).ids
    n = ids.shape[0]
    image_ids = _image_ids(ds, n)

    def generate():
        yield ",".join(["image_id", "cell_id"] + [_quote(m) for m in markers]) + "\n"
        for start in range(0, n, CHUNK_ROWS):
            stop = min(n, start + CHUNK_ROWS)
            columns = [np.asarray(image_ids[start:stop], dtype=str),
                       np.where(ids[start:stop] >= 0, ids[start:stop].astype(str), "")]
            for marker in markers:
                held = picked[marker]["arrays"]
                block_calls = held["calls"][start:stop]
                if kind == "binary":
                    text = np.where(block_calls >= 0, block_calls.astype(str), "")
                elif kind == "scores":
                    scores = held["scores"][start:stop]
                    ok = (block_calls >= 0) & np.isfinite(scores)
                    text = np.where(ok, np.char.mod("%.6g", np.nan_to_num(scores)), "")
                else:
                    codes = override_codes(block_calls, held["gate"][start:stop])
                    text = np.where(codes > -2, codes.astype(str), "")
                columns.append(text.astype(str))
            lines = columns[0]
            for column in columns[1:]:
                lines = np.char.add(np.char.add(lines, ","), column)
            yield "\n".join(lines.tolist()) + "\n"

    return markers, generate()


# -- a run, summarised ------------------------------------------------------------


def run_summary(ds, marker, record, which) -> dict | None:
    """What every output says about one run: where it came from, the gate and
    rule it was made with, its versions, and where its calls part ways with
    that gate (`compare`, under today's QC; the counts kept at the run when
    its arrays no longer line up), and whether it is stale."""
    from plexora.plugins.gating.server.classifier import runs

    if record is None:
        return None
    out = {key: record.get(key) for key in RUN_KEYS}
    out["origin"] = out["origin"] or "panel"
    out["call_rule"] = out["call_rule"] or "score_0.5"
    out["status"] = out["status"] or "ready"
    compared = None
    if made_at_gate(record):
        compared = calls.compare(ds, marker, which) or record.get("compare")
    out["compare"] = None if not compared else {
        key: compared.get(key) for key in COMPARE_KEYS if compared.get(key) is not None}
    try:
        out["stale"] = runs.stale_reason(ds, record)
    except Exception:  # noqa: BLE001 - a summary; the run is reported without it
        out["stale"] = None
    return out


def marker_runs(ds, marker) -> dict:
    """{"active", "run", "session_run"}: which source decides the marker, and
    a summary of each of its runs (None when it has none)."""
    return {"active": calls.active_source(ds, marker),
            "run": run_summary(ds, marker, store.run_record(ds.name, marker), "run"),
            "session_run": run_summary(ds, marker, store.session_run_record(ds.name, marker),
                                       "session_run")}


def gate_columns(ds, marker, entry=None) -> dict:
    """The classifier's columns of the gates export for one marker:
    `source` (which decides it), the run in its layer (the deciding run, else
    the session's layer) -- `classifier_run`, `classifier_origin`,
    `classifier_session`, `call_rule` -- where that run parts ways with the
    gate it was made at (`n_added`, `n_removed`), and `classifier_stale`.
    `entry`: the marker's `calls.markers_with_runs` entry, when known."""
    entry = entry if entry is not None else calls.markers_with_runs(ds).get(marker)
    out = {"source": "threshold", "classifier_run": "", "classifier_origin": "",
           "classifier_session": "", "call_rule": "", "n_added": "", "n_removed": "",
           "classifier_stale": ""}
    if not entry:
        return out
    out["source"] = entry["active"]
    which = "run" if entry["active"] == "classifier" else (
        "session_run" if entry.get("session_run") else "run")
    record = (store.run_record(ds.name, marker) if which == "run"
              else store.session_run_record(ds.name, marker))
    summary = run_summary(ds, marker, record, which)
    if summary is None:
        return out
    compared = summary.get("compare") or {}
    out.update({"classifier_run": summary.get("run_id") or "",
                "classifier_origin": summary.get("origin") or "",
                "classifier_session": summary.get("session_id") or "",
                "call_rule": summary.get("call_rule") or "",
                "n_added": compared.get("n_added", ""),
                "n_removed": compared.get("n_removed", ""),
                "classifier_stale": summary.get("stale") or ""})
    return out


# -- annotations --------------------------------------------------------------------


def annotations_document(ds) -> dict:
    """Every marker's training examples, with their origins, and its runs, as
    the annotations JSON (schema 2). A marker with runs but no examples (a
    session's layer trains on picks it does not keep) is listed for its
    runs, with empty example lists."""
    import polars as pl

    frame = store.all_examples(ds.name)
    with_runs = calls.markers_with_runs(ds)
    markers = {}
    for marker in ds.table.markers:
        rows = frame.filter(pl.col("marker") == marker)
        if rows.height == 0 and marker not in with_runs:
            continue
        examples = sorted(
            ({"cell_id": int(cell), "label": "positive" if int(label) == 1 else "negative",
              "origin": str(origin or "click"), "created": str(created or "")}
             for cell, label, origin, created in zip(
                 rows["cell_id"].to_list(), rows["label"].to_list(),
                 rows["origin"].to_list(), rows["created"].to_list())),
            key=lambda e: e["cell_id"])
        markers[marker] = {
            "positive": [e["cell_id"] for e in examples if e["label"] == "positive"],
            "negative": [e["cell_id"] for e in examples if e["label"] == "negative"],
            "examples": examples,
            "runs": marker_runs(ds, marker),
        }
    seg = ds.project.segmentation
    return {
        "format": FORMAT,
        "schema_version": SCHEMA_VERSION,
        "created": store.now(),
        "image": {"project": ds.name, "image_id": _single_image_id(ds)},
        "segmentation": {"fingerprint": features.mask_fingerprint(ds),
                         "available": bool(seg.available)},
        "cell_id": ds.schema.cell_id if ds.schema else None,
        "markers": markers,
        "note": ("positive / negative are the examples; each example's origin is a click, "
                 "an import, the wand's pick (auto) or an agent's label. runs record what "
                 "was trained and where its calls part ways with its gate; an import "
                 "restores the examples only, never a run."),
    }


def _single_image_id(ds):
    frame = ds.table.geometry()
    column = ds.schema.image_id if ds.schema else None
    if frame is not None and column and column in frame.columns:
        values = frame[column].cast(str).unique().to_list()
        if len(values) == 1:
            return values[0]
    return ds.name


# -- import ---------------------------------------------------------------------


def _imports(project):
    path = store.folder(project) / "imports"
    path.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for old in path.glob("*.json"):
        try:
            if now - old.stat().st_mtime > IMPORT_TTL_S:
                old.unlink(missing_ok=True)
        except OSError:
            pass
    return path


def _ids(values, where, problems):
    out = []
    for value in values if isinstance(values, list) else []:
        try:
            number = int(value)
            if float(value) != number:
                raise ValueError
            out.append(number)
        except (TypeError, ValueError):
            problems.append(f"{where}: {value!r} is not a cell id")
    if not isinstance(values, list):
        problems.append(f"{where} is not a list of cell ids")
    return out


def _kept_origins(entry) -> dict:
    """{cell id: origin} for the examples a schema-2 entry says the wand or an
    agent gave; every other example is an import."""
    out = {}
    examples = entry.get("examples")
    for example in examples if isinstance(examples, list) else []:
        if not isinstance(example, dict) or example.get("origin") not in KEPT_ORIGINS:
            continue
        try:
            out[int(example.get("cell_id"))] = str(example["origin"])
        except (TypeError, ValueError):
            continue
    return out


def preview(ds, document) -> dict:
    """Check an annotations document (schema 1 or 2) against this project;
    the report, with a token `apply` takes. Nothing is changed."""
    from plexora.plugins.gating.server.classifier.geometry import cells

    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise ClassifierRefused("This is not a Plexora classifier annotations file")
    try:
        version = int(document.get("schema_version") or 0)
    except (TypeError, ValueError):
        raise ClassifierRefused("This annotations file has no readable schema version") from None
    if version > SCHEMA_VERSION:
        raise ClassifierRefused("This annotations file was written by a newer Plexora")
    markers = document.get("markers")
    if not isinstance(markers, dict) or not markers:
        raise ClassifierRefused("The annotations file names no markers")
    problems, warnings = [], []
    image = document.get("image") or {}
    here = _single_image_id(ds)
    if image.get("image_id") not in (None, here) and image.get("project") != ds.name:
        warnings.append(f"written for image {image.get('image_id')!r}, this is {here!r}")
    theirs = (document.get("segmentation") or {}).get("fingerprint")
    if theirs and theirs != features.mask_fingerprint(ds):
        warnings.append("written against a different segmentation mask; cell ids may "
                        "not be the same cells")
    known_ids = cells(ds).ids
    known_ids = np.unique(known_ids[known_ids >= 0])
    report, clean, tagged, origins_report, runs_only = {}, {}, {}, {}, []
    for marker, entry in markers.items():
        if marker not in ds.table.markers:
            problems.append(f"{marker!r} is not a marker of this project")
            continue
        entry = entry if isinstance(entry, dict) else {}
        if not entry.get("positive") and not entry.get("negative"):
            # Listed for its runs alone: there is nothing to restore, and a
            # Replace must not empty the marker's examples on its account.
            runs_only.append(marker)
            continue
        pos = _ids(entry.get("positive", []), f"{marker} positive", problems)
        neg = _ids(entry.get("negative", []), f"{marker} negative", problems)
        dup = (len(pos) - len(set(pos))) + (len(neg) - len(set(neg)))
        conflicts = sorted(set(pos) & set(neg))
        pos = sorted(set(pos) - set(conflicts))
        neg = sorted(set(neg) - set(conflicts))
        both = np.asarray(pos + neg, dtype=np.int64)
        unknown = both[~np.isin(both, known_ids)].tolist()
        unknown_set = set(unknown)
        pos = [v for v in pos if v not in unknown_set]
        neg = [v for v in neg if v not in unknown_set]
        report[marker] = {"valid_positive": len(pos), "valid_negative": len(neg),
                          "unknown_cells": len(unknown), "duplicates": dup,
                          "conflicts": len(conflicts)}
        clean[marker] = (pos, neg)
        valid = set(pos) | set(neg)
        kept = {c: o for c, o in _kept_origins(entry).items() if c in valid}
        tagged[marker] = {str(c): o for c, o in kept.items()}
        origins_report[marker] = {"import": len(valid) - len(kept),
                                  **{o: sum(1 for v in kept.values() if v == o)
                                     for o in KEPT_ORIGINS}}
    if not clean:
        if runs_only and not problems:
            raise ClassifierRefused("This annotations file holds no examples to load")
        raise ClassifierRefused("Nothing in this file matches this project: "
                                + "; ".join(problems[:3]))
    token = secrets.token_hex(8)
    store.write_atomic(_imports(ds.name) / f"{token}.json",
                       json.dumps({"markers": clean, "origins": tagged}).encode("utf-8"))
    return {"token": token, "markers": report, "origins": origins_report,
            "runs_only": runs_only, "problems": problems[:50], "warnings": warnings,
            "image": image, "schema_version": version}


def apply(ds, token, mode) -> dict:
    """Apply a previewed import: `mode` "merge" or "replace" (the named
    markers' examples become exactly the file's). Each example keeps the
    origin the file gave it when that was the wand's or an agent's
    (`KEPT_ORIGINS`); every other one is an import."""
    if mode not in ("merge", "replace"):
        raise ClassifierRefused("Choose Merge or Replace")
    if not token or not str(token).isalnum():
        raise ClassifierRefused("That import has expired; load the file again")
    path = _imports(ds.name) / f"{token}.json"
    try:
        held = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ClassifierRefused("That import has expired; load the file again") from None
    by_marker = {m: (pair[0], pair[1]) for m, pair in held["markers"].items()}
    tagged = held.get("origins") or {}
    with store.lock(ds.name):
        store.replace_markers(ds.name, by_marker, origin="import", merge=(mode == "merge"))
        for origin in KEPT_ORIGINS:
            subset = {}
            for marker, (pos, neg) in by_marker.items():
                own = tagged.get(marker) or {}
                p = [c for c in pos if own.get(str(c)) == origin]
                n = [c for c in neg if own.get(str(c)) == origin]
                if p or n:
                    subset[marker] = (p, n)
            if subset:
                # Merge: only these cells are re-tagged, the rest stay as
                # the first write left them.
                store.replace_markers(ds.name, subset, origin=origin, merge=True)
    path.unlink(missing_ok=True)
    from collections import Counter

    counts = store.counts(ds.name)
    return {"markers": sorted(by_marker), "mode": mode,
            "counts": {m: dict(zip(("positive", "negative"), counts.get(m, (0, 0))))
                       for m in by_marker},
            "origins": {m: dict(Counter(store.origins(ds.name, m).values()))
                        for m in by_marker}}
