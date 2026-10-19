"""The classifier's files: two wide CSVs, one annotations JSON, and its import.

- **binary CSV** -- `image_id, cell_id, <marker>...`: 1 positive, 0 negative,
  blank where the cell was not classified (QC ruled it out, it has no value).
- **scores CSV** -- the same rows and columns, the model's score (6
  significant figures), blank where not classified. The scores are called
  probabilities only where the run was calibrated (`learner.CALIBRATE_AT`).
- **annotations JSON** -- the training examples by cell id, per marker, with
  which image and mask they were clicked on. Loading one back is checked
  first (`preview`) and applied only when the person picks Merge or Replace.

Both CSVs carry every marker the classifier decides, in table order, and are
written in row chunks so a million-cell table streams rather than being built
in memory as text.
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
SCHEMA_VERSION = 1
CHUNK_ROWS = 50_000
#: How long a previewed import waits to be applied.
IMPORT_TTL_S = 600


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


def exported_markers(ds):
    """(markers, {marker: arrays}) the CSVs carry."""
    out = {}
    for marker in calls.classified_markers(ds):
        _record, arrays = calls.run_arrays(ds, marker)
        if arrays is not None:
            out[marker] = arrays
    return [m for m in ds.table.markers if m in out], out


def stream_csv(ds, kind):
    """A generator of CSV text: `kind` "binary" or "scores"."""
    from plexora.plugins.gating.server.classifier.geometry import cells

    markers, arrays = exported_markers(ds)
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
                held = arrays[marker]
                block_calls = held["calls"][start:stop]
                if kind == "binary":
                    text = np.where(block_calls >= 0, block_calls.astype(str), "")
                else:
                    scores = held["scores"][start:stop]
                    ok = (block_calls >= 0) & np.isfinite(scores)
                    text = np.where(ok, np.char.mod("%.6g", np.nan_to_num(scores)), "")
                columns.append(text.astype(str))
            lines = columns[0]
            for column in columns[1:]:
                lines = np.char.add(np.char.add(lines, ","), column)
            yield "\n".join(lines.tolist()) + "\n"

    return markers, generate()


def annotations_document(ds) -> dict:
    """Every marker's training examples as the annotations JSON."""
    counts = store.counts(ds.name)
    markers = {}
    for marker in ds.table.markers:
        if marker not in counts:
            continue
        pos, neg = store.examples(ds.name, marker)
        markers[marker] = {"positive": sorted(pos), "negative": sorted(neg)}
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


def preview(ds, document) -> dict:
    """Check an annotations document against this project; the report, with
    a token `apply` takes. Nothing is changed."""
    from plexora.plugins.gating.server.classifier.geometry import cells

    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise ClassifierRefused("This is not a Plexora classifier annotations file")
    if int(document.get("schema_version") or 0) > SCHEMA_VERSION:
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
    report, clean = {}, {}
    for marker, entry in markers.items():
        if marker not in ds.table.markers:
            problems.append(f"{marker!r} is not a marker of this project")
            continue
        entry = entry if isinstance(entry, dict) else {}
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
    if not clean:
        raise ClassifierRefused("Nothing in this file matches this project: "
                                + "; ".join(problems[:3]))
    token = secrets.token_hex(8)
    store.write_atomic(_imports(ds.name) / f"{token}.json",
                       json.dumps({"markers": clean}).encode("utf-8"))
    return {"token": token, "markers": report, "problems": problems[:50],
            "warnings": warnings, "image": image}


def apply(ds, token, mode) -> dict:
    """Apply a previewed import: `mode` "merge" or "replace" (the named
    markers' examples become exactly the file's)."""
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
    store.replace_markers(ds.name, by_marker, origin="import", merge=(mode == "merge"))
    path.unlink(missing_ok=True)
    return {"markers": sorted(by_marker), "mode": mode, "counts": {
        m: dict(zip(("positive", "negative"), store.counts(ds.name).get(m, (0, 0))))
        for m in by_marker}}
