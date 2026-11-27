"""A notebook, written out: a Python script, a Jupyter notebook, a report, or
Plexora's own `.scimap` file.

The script and the Jupyter notebook are the same program -- the exact Scimap
Pro calls each step made, in order, with every argument and seed it ran with
(`run.effective`), so they rerun without Plexora. Paths are relative to the
export (`ROOT`), never this computer's; licence keys, server addresses and
images are never written.

The report is for reading: each step's settings, its headline numbers and its
tables, with the experimental unit beside every one. It says it is not a
workflow.

`.scimap` is a zip Plexora can open again: the notebook, its runs and its
result summaries, optionally the result columns, optionally the working file.
Importing one checks every member and runs nothing.
"""

from __future__ import annotations

import html
import io
import json
import time
import zipfile
from pathlib import Path

from plexora.plugins.scimap.server import notebooks, store, tools
from plexora.plugins.scimap.server.adapter import RunError

FORMAT = "plexora.scimap"
FORMAT_VERSION = 1
ALLOWED_MEMBERS = (".json", ".csv", ".parquet", ".png", ".h5ad")


def _done_cells(document):
    out = []
    for index, cell in enumerate(document["cells"], start=1):
        run = notebooks._shown_run(cell)
        if run is not None:
            out.append((index, cell, run))
    return out


def _slug(title):
    import re

    return re.sub(r"[^A-Za-z0-9._-]+", "_", title).strip("_") or "notebook"


def _call_lines(cell, run, work):
    """The Python for one step."""
    tool_id = cell["tool"]
    effective = run.get("effective") or {}
    key = effective.get("key")
    kwargs = effective.get("kwargs") or {}
    if tool_id == "prepare":
        made = run.get("working") or {}
        lines = []
        calls = made.get("calls") or []
        if len(calls) == 1 and calls[0].get("function") == "io.readIntensityTable":
            call = calls[0]
            args = ", ".join(f"{k}={v!r}" for k, v in call["kwargs"].items())
            lines.append(f'adata = sm.io.readIntensityTable(str(DATA / {call["file"]!r}), {args})')
        elif calls and all(c.get("function") == "io.readIntensityTable" for c in calls):
            lines.append("parts = []")
            for call in calls:
                args = ", ".join(f"{k}={v!r}" for k, v in call["kwargs"].items())
                lines.append(f'parts.append(sm.io.readIntensityTable(str(DATA / {call["file"]!r}), {args}))')
            keys = [c.get("kwargs", {}).get("customImageid") for c in calls]
            lines.append(f"adata = sm.tl.concatenate(parts, join='inner', keys={keys!r})")
        else:
            lines.append(f'adata = sm.io.init(str(DATA / {made.get("file", "working.h5ad")!r}), streamData=False)')
        lines.append(f'adata.obs["imageid"] = adata.obs["imageid"].astype(str).astype("category")')
        return lines
    if key:
        lines = []
        synced = False
        for step in effective.get("before") or []:
            kwargs = dict(step.get("kwargs") or {})
            if step["key"] == "hl.syncFromViewer":
                # Run outside Plexora, the script has no viewer to read the
                # gates from: it carries the ones the working file holds.
                if not synced:
                    synced = True
                    lines.append("# The gates were read out of Plexora by sm.hl.syncFromViewer; these are them.")
                    lines.append(f"adata.uns['gates'] = {_gates_frame(work)}")
                continue
            lines.append(f"sm.{step['key']}(adata, {_args(kwargs, lines)})")
        lines.append(f"sm.{key}(adata, {_args(kwargs, lines)})")
        return lines
    if tool_id == "composition":
        return [f"counts = sm.pl.barplot(adata, xAxis={kwargs.get('imageId', 'imageid')!r}, "
                f"yAxis={kwargs.get('groupBy')!r}, method='absolute', returnData=True, show=False)",
                f"shares = sm.pl.barplot(adata, xAxis={kwargs.get('imageId', 'imageid')!r}, "
                f"yAxis={kwargs.get('groupBy')!r}, method='percent', returnData=True, show=False)",
                "print(counts)", "print((shares * 100).round(1))"]
    if tool_id == "expression":
        markers = kwargs.get("markers")
        return [f"means = sm.pl.heatmap(adata, groupBy={kwargs.get('groupBy')!r}, subsetMarkers={markers!r}, "
                "standardScale=None, returnData=True, show=False)",
                "print(means.round(3))"]
    return []


#: What a table argument is called in the script, by the argument it fills.
_FRAME_NAMES = {"gate": "gates", "phenotype": "rules"}


def _args(kwargs, lines) -> str:
    """`k=v, ...` for a call; a table argument (`tools.frame`) is written as a
    DataFrame on the lines before it."""
    out = []
    for k, v in kwargs.items():
        if isinstance(v, dict) and "__frame__" in v:
            name = _FRAME_NAMES.get(k, k)
            spec = v["__frame__"]
            if name == "gates":
                lines.append("# Gates set in Plexora's gating, as log1p of the raw intensity, one column per image.")
            lines.append(f"{name} = pd.DataFrame({spec.get('rows')!r}, columns={spec.get('columns')!r})")
            out.append(f"{k}={name}")
        else:
            out.append(f"{k}={v!r}")
    return ", ".join(out)


def _gates_frame(work) -> str:
    """`uns["gates"]` of the working file, as the Python that builds it."""
    try:
        from plexora.plugins.scimap.server import results

        frame = results._uns_frame(work["path"], "gates")
    except Exception:  # noqa: BLE001 -- no gates stored: say so in the script
        return "pd.DataFrame()  # no gates were stored"
    frame = frame.reset_index()
    first = str(frame.columns[0])
    return (f"pd.DataFrame({frame.values.tolist()!r}, columns={[str(c) for c in frame.columns]!r})"
            f".set_index({first!r})")


def _header(document):
    versions = {}
    for _, _, run in _done_cells(document):
        versions.update({k: v for k, v in (run.get("software") or {}).items() if v})
    context = document.get("context") or {}
    return {
        "title": document.get("title") or "SCIMAP notebook",
        "context": f"{context.get('kind', '')} {context.get('name', '')}".strip(),
        "projects": context.get("projects") or [],
        "versions": versions,
        "exported": time.strftime("%Y-%m-%d %H:%M"),
    }


def _program(document, work):
    """`[(markdown, code)]` per step, for the script and the notebook."""
    head = _header(document)
    preamble = (
        "from pathlib import Path\n"
        "import os\n\n"
        "import pandas as pd\n"
        "import scimappro as sm\n\n"
        "# The folder this export is in; set SCIMAP_EXPORT_ROOT to run it from elsewhere.\n"
        "_HERE = Path(__file__).resolve().parent if '__file__' in globals() else Path.cwd()\n"
        "ROOT = Path(os.environ.get('SCIMAP_EXPORT_ROOT') or _HERE)\n"
        "# Put the cell table(s) named below in this folder.\n"
        "DATA = ROOT / 'data'"
    )
    versions = ", ".join(f"{k} {v}" for k, v in head["versions"].items())
    intro = (f"# {head['title']}\n\nExported from Plexora's SCIMAP notebook on {head['exported']}. "
             f"Context: {head['context']}" + (f" ({', '.join(head['projects'])})" if len(head['projects']) > 1 else "")
             + f".\n\nRan with {versions}." if versions else "")
    blocks = [(intro, preamble)]
    for index, cell, run in _done_cells(document):
        name = tools.TOOLS.get(cell["tool"], {}).get("name", cell["tool"])
        unit = run.get("experimental_unit") or {}
        note = f"## {index:02d} {name}"
        if unit.get("unit"):
            note += f"\n\nExperimental unit: {unit['unit']} · {unit.get('nReplicates')} replicate(s)."
        lines = _call_lines(cell, run, work)
        if lines:
            blocks.append((note, "\n".join(lines)))
    blocks.append(("## Save", "adata.write_h5ad(ROOT / 'results.h5ad')"))
    return blocks


def script(document, work) -> str:
    out = []
    for markdown, code in _program(document, work):
        comment = "\n".join(f"# {line}" if line else "#" for line in markdown.splitlines())
        out.append(f"{comment}\n{code}\n")
    return '"""' + (document.get("title") or "SCIMAP notebook") + ', exported from Plexora."""\n\n' \
        + "\n".join(out)


def ipynb(document, work) -> str:
    cells = []
    for markdown, code in _program(document, work):
        if markdown:
            cells.append({"cell_type": "markdown", "metadata": {}, "source": markdown.splitlines(True)})
        cells.append({"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [],
                      "source": code.splitlines(True)})
    return json.dumps({"nbformat": 4, "nbformat_minor": 5,
                       "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3",
                                                   "language": "python"},
                                    "plexora": {"format": FORMAT, "notebook": document.get("title")}},
                       "cells": cells}, indent=1)


def _table_html(head, rows):
    th = "".join(f"<th>{html.escape(str(h))}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{html.escape('—' if v is None else str(v))}</td>" for v in r) + "</tr>"
                   for r in rows)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>"


def report(document, work) -> str:
    head = _header(document)
    parts = [f"<h1>{html.escape(head['title'])}</h1>",
             f"<p class='meta'>{html.escape(head['context'])} · exported {head['exported']} · "
             f"{html.escape(', '.join(f'{k} {v}' for k, v in head['versions'].items()))}</p>",
             "<p class='note'>A report to read: plots' numbers, tables and settings. "
             "It is not a workflow; export a script to rerun the analysis.</p>"]
    for index, cell, run in _done_cells(document):
        name = tools.TOOLS.get(cell["tool"], {}).get("name", cell["tool"])
        parts.append(f"<section><h2>{index:02d} {html.escape(name)}</h2>")
        chips = tools.chips(cell["tool"], run.get("effective") or {}, work)
        if chips:
            parts.append("<p class='chips'>" + " · ".join(html.escape(c) for c in chips) + "</p>")
        body = store.get_result(document["id"], run["run_id"]) or {}
        unit = body.get("unit") or {}
        if unit.get("text"):
            parts.append(f"<p class='meta'>{html.escape(unit['text'])}</p>")
        if body.get("groups"):
            parts.append(_table_html(["Group", "Cells", "%"],
                                     [[g["id"], f"{g['count']:,}", f"{g['pct']:.1f}"] for g in body["groups"]]))
        if body.get("categories") and body.get("pcts") and body.get("images"):
            parts.append(_table_html(["Image"] + body["categories"],
                                     [[img] + [f"{v:.1f}" for v in row] for img, row in zip(body["images"], body["pcts"])]))
        if body.get("matrices") and body.get("categories"):
            for image, matrix in body["matrices"].items():
                grid = matrix["score"] if isinstance(matrix, dict) else matrix
                parts.append(f"<p class='meta'>{html.escape(image)}: from row to column</p>")
                parts.append(_table_html([""] + body["categories"],
                                         [[c] + [None if v is None else f"{v:.2f}" for v in row]
                                          for c, row in zip(body["categories"], grid)]))
        parts.append("</section>")
    style = ("body{font-family:'Source Sans 3','Source Sans Pro',system-ui,sans-serif;color:#344054;"
             "max-width:980px;margin:40px auto;padding:0 24px}h1{font-size:22px;color:#1d2939}"
             "h2{font-size:15px;color:#1d2939;margin:28px 0 6px}.meta{font-size:12px;color:#6b7689}"
             ".note{font-size:13px;background:#f4f5f8;border-radius:8px;padding:10px 12px}"
             ".chips{font-size:12px;color:#526075}table{border-collapse:collapse;font-size:12px;margin:8px 0}"
             "th,td{border-bottom:1px solid #eef0f4;padding:4px 10px;text-align:right}"
             "th:first-child,td:first-child{text-align:left}th{color:#526075;font-weight:600}")
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{html.escape(head['title'])}"
            f"</title><style>{style}</style></head><body>{''.join(parts)}</body></html>")


def _public_document(document):
    """The notebook without this computer's paths."""
    clean = json.loads(json.dumps(document))
    clean.pop("results_dir", None)
    if clean.get("working"):
        clean["working"] = {k: v for k, v in clean["working"].items() if k not in ("path", "results_dir")}
    for cell in clean.get("cells") or []:
        cell["job_id"] = None
        run = cell.get("run") or {}
        if run.get("working"):
            run["working"] = {k: v for k, v in run["working"].items() if k not in ("path", "results_dir")}
        effective = run.get("effective") or {}
        effective.pop("results_dir", None)
    return clean


def native(document, work, include) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest = {"format": FORMAT, "version": FORMAT_VERSION, "contents": include,
                    "title": document.get("title"), "exported_at": time.time(),
                    "software": notebooks.software()}
        zf.writestr("manifest.json", json.dumps(manifest, indent=1))
        zf.writestr("notebook.json", json.dumps(_public_document(document), indent=1))
        summaries = {}
        for _, _, run in _done_cells(document):
            body = store.get_result(document["id"], run["run_id"])
            if body is not None:
                summaries[run["run_id"]] = body
        zf.writestr("results/summaries.json", json.dumps(summaries))
        if include in ("results", "data") and work.get("exists"):
            labels = [((run.get("effective") or {}).get("label")) for _, _, run in _done_cells(document)]
            columns = [c for c in labels if c and c in {x["name"] for x in work.get("columns") or []}]
            if columns:
                import pandas as pd

                from plexora.plugins.scimap.server import results

                frame = pd.DataFrame({c: results.obs_column(work["path"], c) for c in columns})
                frame.insert(0, "imageid", results.obs_column(work["path"], work.get("image_column") or "imageid"))
                if work.get("cell_column"):
                    frame.insert(1, work["cell_column"], results.obs_column(work["path"], work["cell_column"]))
                zf.writestr("results/columns.csv", frame.to_csv(index=False))
        if include == "data" and work.get("exists"):
            zf.write(work["path"], f"data/{Path(work['path']).name}")
    return buffer.getvalue()


def build(notebook_id, fmt, include="workflow"):
    document = store.load(notebook_id)
    work = notebooks.work_of(document)
    name = _slug(document.get("title") or "notebook")
    if fmt == "py":
        return f"{name}.py", "text/x-python", script(document, work)
    if fmt == "ipynb":
        return f"{name}.ipynb", "application/x-ipynb+json", ipynb(document, work)
    if fmt == "html":
        return f"{name}.html", "text/html", report(document, work)
    if fmt == "scimap":
        if include not in ("workflow", "results", "data"):
            raise RunError("invalid_input", f"unknown contents {include!r}")
        return f"{name}.scimap", "application/zip", native(document, work, include)
    raise RunError("invalid_input", f"unknown format {fmt!r}")


def estimate(notebook_id) -> dict:
    document = store.load(notebook_id)
    work = notebooks.work_of(document)
    workflow = len(json.dumps(_public_document(document))) + 4096
    n = int(work.get("n_cells") or 0)
    labels = sum(1 for _, _, run in _done_cells(document) if (run.get("effective") or {}).get("label"))
    results_size = workflow + n * (labels + 2) * 8
    data = results_size + int(work.get("size") or 0)
    return {"workflow": workflow, "results": results_size, "data": data}


def import_native(payload: bytes, kind=None, ref=None) -> dict:
    """Open a `.scimap` file as a new notebook. Runs nothing; refuses anything
    it does not recognise."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise RunError("invalid_input", "That is not a .scimap file.") from exc
    with zf:
        for info in zf.infolist():
            name = info.filename
            if name.startswith("/") or ".." in Path(name).parts or not name.endswith(ALLOWED_MEMBERS):
                raise RunError("invalid_input", f"The file holds something unexpected: {name}")
        try:
            manifest = json.loads(zf.read("manifest.json"))
            document = json.loads(zf.read("notebook.json"))
        except (KeyError, ValueError) as exc:
            raise RunError("invalid_input", "That .scimap file is incomplete.") from exc
    if manifest.get("format") != FORMAT or int(manifest.get("version", 0)) > FORMAT_VERSION:
        raise RunError("invalid_input", "That file was written by a newer Plexora.")
    for cell in document.get("cells") or []:
        if cell.get("tool") not in tools.TOOLS:
            raise RunError("invalid_input", f"The notebook uses an unknown tool: {cell.get('tool')}")
        cell["id"] = store.new_cell_id()
        cell["job_id"] = None
        if cell.get("run"):
            cell["stale"] = {"reason": "Imported: run again to recompute here.", "at": time.time()}
    if kind and ref:
        from plexora.plugins.scimap.server import context

        document["context"] = context.resolve(kind, ref)
    document.pop("id", None)
    document.pop("revision", None)
    document["working"] = None
    document["title"] = f"{document.get('title') or 'Notebook'} (imported)"
    from plexora.plugins.scimap.server import context

    document["results_dir"] = context.default_results_dir(document["context"])
    return store.create(document)
