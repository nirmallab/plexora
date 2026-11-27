"""Sample metadata: one row per image (`imageid`), any columns the user adds --
patient, condition, timepoint.

Kept once, in Plexora's own records, never in a notebook: with the dataset
(`Dataset.meta["samples"]`) for a dataset, in the project's plugin state for a
single project. Every notebook over that data reads the same table, and so does
anything else that reads the dataset.

Each column is written to every cell of its image: when the data is prepared,
and -- if it already is -- straight into the working file when the table is
saved, so nothing has to be prepared again. An empty value stays empty
(missing) in the data. A column a step reads that changes makes that step say
Outdated.
"""

from __future__ import annotations

import json
import re

KEY = "imageid"
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_ .-]{0,63}$")


def _clean(table) -> dict:
    table = table or {}
    columns = []
    for name in table.get("columns") or []:
        name = str(name).strip()
        if name and name != KEY and _NAME.match(name) and name not in columns:
            columns.append(name)
    rows = {}
    for image, values in (table.get("rows") or {}).items():
        values = values or {}
        rows[str(image)] = {c: str(values.get(c) or "").strip() for c in columns}
    return {"key": KEY, "columns": columns, "rows": rows}


def get(context) -> dict:
    """The table for a notebook's context: `{key, columns, rows: {image: {col: value}}}`."""
    from plexora import api

    if context.get("kind") == "dataset":
        for dataset in api.list_datasets():
            if dataset["id"] == context["id"]:
                return _clean((dataset.get("meta") or {}).get("samples"))
        return _clean(None)
    blob = api.store(context["projects"][0], "scimap").get_state()
    try:
        state = json.loads(blob) if blob else {}
    except ValueError:
        state = {}
    return _clean(state.get("samples"))


def put(context, table) -> dict:
    from plexora import api

    table = _clean(table)
    if context.get("kind") == "dataset":
        api.update_dataset(context["id"], meta={"samples": table})
        return table
    handle = api.store(context["projects"][0], "scimap")
    blob = handle.get_state()
    try:
        state = json.loads(blob) if blob else {}
    except ValueError:
        state = {}
    state["samples"] = table
    handle.put_state(json.dumps(state).encode())
    return table


def from_file(name, payload: bytes) -> dict:
    """A CSV or Excel sheet as a table: the column named `imageid` (or the
    first column) is the key; every other column is kept."""
    import io

    import pandas as pd

    if str(name).lower().endswith((".xlsx", ".xls")):
        frame = pd.read_excel(io.BytesIO(payload), dtype=str)
    else:
        frame = pd.read_csv(io.BytesIO(payload), dtype=str, sep=None, engine="python")
    frame = frame.fillna("")
    key = next((c for c in frame.columns if str(c).strip().lower() == KEY), frame.columns[0])
    columns = [str(c) for c in frame.columns if c != key]
    rows = {str(r[key]).strip(): {c: str(r[c]).strip() for c in columns}
            for _, r in frame.iterrows() if str(r[key]).strip()}
    return _clean({"columns": columns, "rows": rows})


def matched(table, images) -> dict:
    images = [str(i) for i in images or []]
    have = set(table.get("rows") or {})
    return {"matched": sum(1 for i in images if i in have), "images": len(images),
            "unknown": sorted(have - set(images))}


def write_into(path, table, image_column=KEY, previous=()) -> list:
    """Write the columns into a working file with Scimap Pro's
    `tl.addMetadata` (one row per image, joined on `image_column`), replacing
    the columns it already holds. On a Scimap Pro without it nothing is
    written (the table stays with the dataset; the page says why). A column
    taken out of the table stays in the file until the data is prepared again."""
    from plexora.plugins.scimap.server import install

    table = _clean(table)
    if not table["columns"]:
        return []
    if not install.supports("metadata"):
        # Kept with the dataset; written into the data once Scimap Pro can.
        return []
    from plexora.plugins.scimap.server import adapter, tools

    rows = [[image, *[(values.get(c) or None) for c in table["columns"]]]
            for image, values in table["rows"].items()]
    # Through the notebook's run path, which writes the columns back into the
    # file however Scimap Pro opened it (in memory or streamed).
    adapter.execute(str(path), "tl.addMetadata",
                    {"metadata": tools.frame([image_column, *table["columns"]], rows), "on": image_column,
                     "columns": list(table["columns"]), "overwrite": True, "minMatched": 0.0,
                     "label": "plexora_samples", "recompute": True},
                    label="plexora_samples",
                    writes=[f"obs:{c}" for c in table["columns"]] + ["uns:plexora_samples_params"])
    return table["columns"]
