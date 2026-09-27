"""What an agent did, as a report a methods section can rest on.

Built from the audit log alone -- every write, refusal and conflict is already
a line there -- plus the artifact store for the pictures the agent looked at.
Per operation: when, what, on which project, the arguments, what changed
(before -> after, scalar by scalar), the store's revisions, whether it was an
undo or was later undone, the job it ran as, and its evidence.

Evidence has two sources, and the report keeps them apart: artifacts *cited*
(passed in, or named in the call's arguments) and artifacts *nearby* -- made
for the same project between the previous operation and this one. The second
is a heuristic, and is labelled as one; the audit log does not record which
render an agent was looking at when it decided.

Plain line builders, no template engine, in the style of Figure Builder's
provenance lines.
"""

from __future__ import annotations

import base64
import html
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from plexora.agent.audit import now_iso

#: Pictures embedded in one HTML report, at most. More are listed, not embedded.
MAX_EMBEDDED = 40

#: Characters of one argument value shown before it is elided.
MAX_ARGUMENT_CHARS = 160

_ARTIFACT_ID = re.compile(r"\bart_[0-9a-f]{20}\b")

_STATUSES = ("ok", "failed", "refused", "conflict", "cancelled")


def _epoch(stamp):
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _short(value):
    text = json.dumps(value, default=str, ensure_ascii=False)
    return text if len(text) <= MAX_ARGUMENT_CHARS else text[:MAX_ARGUMENT_CHARS - 1] + "…"


def _scalar(value):
    return value is None or isinstance(value, (bool, int, float, str))


def changes(before, after) -> list:
    """[{field, before, after}] for every scalar that differs."""
    before = before if isinstance(before, dict) else {}
    after = after if isinstance(after, dict) else {}
    out = []
    for key in list(before) + [k for k in after if k not in before]:
        old, new = before.get(key), after.get(key)
        if old != new and _scalar(old) and _scalar(new):
            out.append({"field": key, "before": old, "after": new})
    return out


def _artifact_index(projects):
    from plexora.agent import artifacts

    found = {}
    for project in projects:
        for entry in artifacts.list_artifacts(project, limit=10_000):
            found[entry["id"]] = entry
    return found


def _artifact_path(art):
    from plexora.agent import artifacts

    sidecar = artifacts._find(art)
    return None if sidecar is None else sidecar.with_suffix(".png")


def build(audit, *, since=None, operation_ids=None, project=None, artifact_ids=()) -> dict:
    """The report as data. `operation_ids` narrows to those operations (and
    their undos and per-image children); `project` to one project."""
    lines = audit.entries(since=since)
    started = {line["operation_id"]: line for line in lines if line.get("status") == "started"}
    undone_by = {line["undo_of"]: line["operation_id"] for line in lines
                 if line.get("undo_of") and line.get("status") == "ok"
                 and line.get("capability") == "operation.undo"}
    wanted = set(operation_ids or ())

    def selected(line):
        if line.get("status") not in _STATUSES:
            return False
        if project is not None and line.get("project") not in (project, None):
            return False
        if not wanted:
            return True
        op = line.get("operation_id") or ""
        return (op in wanted or line.get("undo_of") in wanted
                or line.get("parent_operation_id") in wanted
                or op.split(".")[0] in wanted)

    chosen = [line for line in lines if selected(line)]
    finished = {line.get("operation_id") for line in chosen}
    # A job still running has only its "started" line.
    for op, line in started.items():
        if op not in finished and (not wanted or op in wanted) and (
                project is None or line.get("project") == project):
            chosen.append({**line, "status": "running"})
    chosen.sort(key=lambda line: line.get("timestamp") or "")

    projects = sorted({line["project"] for line in chosen if line.get("project")})
    index = _artifact_index(projects)
    explicit = set(artifact_ids or ())
    operations = []
    previous_at = {}
    for line in chosen:
        receipt = line.get("receipt") or {}
        op = line.get("operation_id")
        at = _epoch(line.get("timestamp"))
        where = line.get("project")
        cited = sorted(set(_ARTIFACT_ID.findall(json.dumps(line.get("arguments") or {})))
                       | {a for a in explicit if index.get(a, {}).get("project") == where})
        nearby = []
        if where and at is not None:
            lower = previous_at.get(where, float("-inf"))
            nearby = sorted(entry["id"] for entry in index.values()
                            if entry.get("project") == where
                            and lower < (entry.get("created") or 0) <= at
                            and entry["id"] not in cited)
            previous_at[where] = at
        operations.append({
            "operation_id": op,
            "timestamp": line.get("timestamp"),
            "status": line["status"],
            "capability": line.get("capability"),
            "project": where,
            "arguments": {k: _short(v) for k, v in (line.get("arguments") or {}).items()},
            "changes": changes(receipt.get("before"), receipt.get("after")),
            "revision_before": receipt.get("revision_before"),
            "revision_after": receipt.get("revision_after"),
            "source_file_modified": receipt.get("source_file_modified", False),
            "undo_of": line.get("undo_of"),
            "undone_by": undone_by.get(op),
            "parent_operation_id": line.get("parent_operation_id"),
            "job_id": line.get("job_id") or (started.get(op) or {}).get("job_id"),
            "error": (line.get("error") or {}).get("message"),
            "evidence": {"cited": cited, "nearby": nearby},
        })
    used = sorted({a for entry in operations for a in entry["evidence"]["cited"]
                   + entry["evidence"]["nearby"]} | explicit)
    return {
        "title": "Plexora agent session",
        "generated_at": now_iso(),
        "audit_log": str(audit.path),
        "filter": {"since": since, "operation_ids": sorted(wanted) or None,
                   "project": project},
        "operations": operations,
        "artifacts": {art: {**index.get(art, {"id": art}),
                            "path": str(_artifact_path(art) or "")} for art in used},
        "counts": {status: sum(1 for entry in operations if entry["status"] == status)
                   for status in (*_STATUSES, "running")},
    }


# -- rendering -----------------------------------------------------------


def _value(value):
    return "—" if value is None else str(value)


def _summary_lines(report):
    counts = ", ".join(f"{n} {status}" for status, n in report["counts"].items() if n)
    lines = [f"Generated {report['generated_at']} from `{report['audit_log']}`.",
             f"{len(report['operations'])} operations ({counts or 'none'}).",
             "Evidence marked *nearby* is a heuristic: renders made for the same project "
             "between the previous operation and this one."]
    return lines


def _operation_lines(entry):
    head = f"`{entry['operation_id']}` · {entry['timestamp']} · **{entry['capability']}**"
    if entry["project"]:
        head += f" on `{entry['project']}`"
    head += f" — {entry['status']}"
    lines = [head]
    if entry["undo_of"]:
        lines.append(f"Undoes `{entry['undo_of']}`.")
    if entry["undone_by"]:
        lines.append(f"Later undone by `{entry['undone_by']}`.")
    if entry["parent_operation_id"]:
        lines.append(f"Part of `{entry['parent_operation_id']}`.")
    if entry["job_id"]:
        lines.append(f"Ran as job `{entry['job_id']}`.")
    if entry["arguments"]:
        lines.append("Arguments: " + ", ".join(f"`{k}`={v}" for k, v in
                                                entry["arguments"].items()))
    for change in entry["changes"]:
        lines.append(f"- {change['field']}: {_value(change['before'])} → "
                     f"{_value(change['after'])}")
    if entry["revision_before"] is not None or entry["revision_after"] is not None:
        lines.append(f"Revision {_value(entry['revision_before'])} → "
                     f"{_value(entry['revision_after'])}.")
    if entry["source_file_modified"]:
        lines.append("**The source file was modified.**")
    if entry["error"]:
        lines.append(f"Error: {entry['error']}")
    return lines


def to_markdown(report, directory=None) -> str:
    """Markdown, linking each artifact relative to `directory` (the folder the
    report is written into), or by absolute path when there is none."""
    out = [f"# {report['title']}", ""]
    out += [*_summary_lines(report), ""]
    for entry in report["operations"]:
        lines = _operation_lines(entry)
        out.append("## " + lines[0])
        out += lines[1:]
        for kind in ("cited", "nearby"):
            for art in entry["evidence"][kind]:
                path = report["artifacts"].get(art, {}).get("path")
                if not path:
                    out.append(f"- {kind}: `{art}` (no longer stored)")
                    continue
                link = os.path.relpath(path, directory) if directory else path
                out.append(f"- {kind}: ![{art}]({Path(link).as_posix()})")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def to_html(report) -> str:
    """One self-contained page: the pictures embedded as base64."""
    esc = html.escape
    embedded = 0
    body = [f"<h1>{esc(report['title'])}</h1>"]
    body += [f"<p>{esc(line)}</p>" for line in _summary_lines(report)]
    for entry in report["operations"]:
        lines = _operation_lines(entry)
        body.append(f"<section><h2>{esc(lines[0])}</h2>")
        body += [f"<p>{esc(line)}</p>" for line in lines[1:]]
        for kind in ("cited", "nearby"):
            for art in entry["evidence"][kind]:
                path = report["artifacts"].get(art, {}).get("path")
                if path and Path(path).is_file() and embedded < MAX_EMBEDDED:
                    data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
                    embedded += 1
                    body.append(f'<figure><img alt="{esc(art)}" '
                                f'src="data:image/png;base64,{data}">'
                                f"<figcaption>{esc(kind)}: {esc(art)}</figcaption></figure>")
                else:
                    body.append(f"<p>{esc(kind)}: {esc(art)}</p>")
        body.append("</section>")
    style = ("body{font:14px/1.5 system-ui,sans-serif;max-width:60rem;margin:2rem auto;"
             "padding:0 1rem;color:#222;background:#fff}h2{font-size:1rem;margin-top:2rem}"
             "img{max-width:100%;border:1px solid #ddd}figure{margin:.5rem 0}"
             "@media (prefers-color-scheme:dark){body{color:#ddd;background:#161616}}")
    return ("<!doctype html><html><head><meta charset=\"utf-8\">"
            f"<title>{esc(report['title'])}</title><style>{style}</style></head><body>"
            + "".join(body) + "</body></html>\n")


def write(report, fmt="md", path=None) -> Path:
    """Write the report; default `<agent_root>/reports/<utc>.<fmt>`."""
    from plexora import paths

    if path is None:
        folder = paths.agent_root() / "reports"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = folder / f"session_{stamp}.{fmt}"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = to_html(report) if fmt == "html" else to_markdown(report, path.parent)
    path.write_text(text, encoding="utf-8")
    return path
