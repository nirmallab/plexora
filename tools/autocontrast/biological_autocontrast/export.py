"""Writers for proposals and labels. Nested fields go into CSV as JSON text."""

from __future__ import annotations

import csv
import json
from pathlib import Path

#: Columns first in the CSV, in this order; the rest follow alphabetically.
LEAD = ("image_id", "channel_index", "channel_name", "z", "t", "method", "model_available",
        "display_min_raw", "display_max_raw", "display_min_norm", "display_max_norm",
        "gamma", "confidence", "abstain", "abstain_reason", "storage_dtype",
        "effective_bits", "bits_source", "offset", "full_scale")


def _cell(value):
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True)
    if value is None:
        return ""
    return value


def write_rows_csv(rows, path):
    path = Path(path)
    keys = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    columns = [k for k in LEAD if k in keys] + sorted(k for k in keys if k not in LEAD)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _cell(row.get(k)) for k in columns})
    return path


def write_rows_json(rows, path):
    path = Path(path)
    path.write_text(json.dumps(rows, indent=1, sort_keys=False), encoding="utf-8")
    return path


def write_predictions(rows, out_dir) -> dict:
    """contrast_predictions[.heuristic].csv/.json: a heuristic run says so in its name."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    heuristic = not any(r.get("model_available") for r in rows)
    stem = "contrast_predictions.heuristic" if heuristic else "contrast_predictions"
    return {"csv": str(write_rows_csv(rows, out_dir / f"{stem}.csv")),
            "json": str(write_rows_json(rows, out_dir / f"{stem}.json"))}
