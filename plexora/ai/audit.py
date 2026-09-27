"""`plexora ai audit`: what agents did, from the terminal.

Reads `<data_root>/.agent/audit.jsonl` -- one line per attempted mutation --
and prints it as a table, or writes the same session report `session_report`
writes (`--report`).
"""

from __future__ import annotations

import json


def _row(line) -> str:
    receipt = line.get("receipt") or {}
    what = line.get("capability") or "?"
    detail = ""
    if line.get("error"):
        detail = (line["error"] or {}).get("message", "")
    elif receipt:
        changed = [f"{k}: {receipt['before'].get(k)} -> {v}"
                   for k, v in (receipt.get("after") or {}).items()
                   if isinstance(receipt.get("before"), dict)
                   and receipt["before"].get(k) != v
                   and isinstance(v, (int, float, str, bool, type(None)))]
        detail = "; ".join(changed[:3])
    if line.get("undo_of"):
        detail = f"undo of {line['undo_of']}" + (f"; {detail}" if detail else "")
    if line.get("job_id"):
        detail = f"job {line['job_id']}" + (f"; {detail}" if detail else "")
    stamp = str(line.get("timestamp", ""))[:19].replace("T", " ")
    return (f"{stamp}  {line.get('status', '?'):<9}  {what:<28}  "
            f"{line.get('project') or '-':<16}  {line.get('operation_id', '')}"
            + (f"\n{'':>21}{detail}" if detail else ""))


def audit_command(*, since=None, project=None, limit=50, report=None, fmt=None,
                  as_json=False, out=print) -> int:
    from plexora.agent.audit import AuditLog

    audit = AuditLog()
    if report is not None:
        from pathlib import Path

        from plexora.agent import report as reports

        fmt = fmt or ("html" if str(report).endswith(".html") else "md")
        built = reports.build(audit, since=since, project=project)
        path = reports.write(built, fmt=fmt, path=Path(report))
        out(f"Wrote {len(built['operations'])} operations to {path}")
        return 0
    lines = [line for line in audit.entries(since=since)
             if project is None or line.get("project") == project]
    lines = lines[-limit:] if limit else lines
    if as_json:
        for line in lines:
            out(json.dumps(line, default=str, ensure_ascii=False))
        return 0
    if not lines:
        out(f"No agent operations in {audit.path}" + (f" since {since}" if since else ""))
        return 0
    out(f"{audit.path}  ({len(lines)} shown)")
    for line in lines:
        out(_row(line))
    return 0
