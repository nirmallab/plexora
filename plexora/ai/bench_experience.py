"""`plexora ai bench experience`: what finished gating sessions teach, as ledger lines.

Reads the experience record (`plexora/plugins/gating/server/experience.py`)
-- one decision row per marker a session handled, and the corrections that
followed -- joins each decision with what its project says now (the gate
stored, whether the user changed it since, the labels clicked), and writes
markdown a person reads to decide what to change:

- **gates the user moved**: per marker, how far and which way from the
  session's final gate, and from its scored start (a start that is always
  low for a marker across projects is a `scoring` or markers.yaml finding);
- **the classifier's layers**: adopted, reverted or discarded per marker
  role, what each removed or added, by signal grade;
- **the classifier check**: fired, which way, how many rows the look
  confirmed, and whether the user's later gate lies the way it moved;
- a version column on every line, so a superseded version is left out with
  `--exclude-version`.

Nothing here changes a parameter. A finding becomes a `[cal]` constant or a
markers.yaml caveat by hand, with a VERSION bump of the owning module and a
ledger row in docs/internal/bench/AUTOGATE_STEP_LEDGER.md citing a bench on
the frozen label sets.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path


def _version_tag(row) -> str:
    versions = row.get("versions") or {}
    gating = versions.get("gating") or []
    classifier = versions.get("classifier") or {}
    return "/".join(str(v) for v in gating[:3]) + (
        f" clf:{classifier.get('rules')}/{classifier.get('autopick')}" if classifier else "")


def _excluded(row, exclude) -> bool:
    if not exclude:
        return False
    tag = _version_tag(row)
    return any(e and e in tag for e in exclude)


def collect(projects=None, exclude_version=(), observe=True) -> dict:
    """{"decisions": [...], "corrections": [...]} with each decision joined to
    its project's state now (`observed`), when the project can be opened."""
    from plexora.plugins.gating.server import experience

    rows = []
    if projects:
        for project in projects:
            rows += experience.read(project)
    else:
        rows = experience.read()
    decisions = [r for r in rows if r["row"] == "decision"
                 and not _excluded(r, exclude_version)]
    corrections = [r for r in rows if r["row"] == "correction"]
    if observe:
        from plexora.agent import AgentSession

        session = AgentSession()
        handles = {}
        for row in decisions:
            project = row.get("project")
            if project not in handles:
                try:
                    handles[project] = session.data(project)
                except Exception:  # noqa: BLE001 -- a project since removed
                    handles[project] = None
            ds = handles[project]
            if ds is None or row.get("marker") not in ds.table.markers:
                continue
            try:
                row["observed"] = experience.observe(ds, row["marker"])
            except Exception:  # noqa: BLE001
                continue
    return {"decisions": decisions, "corrections": corrections}


def _latest(decisions):
    """The last decision per (project, marker): an earlier session's gate on
    a marker re-gated since is not what the user corrected."""
    out = {}
    for row in sorted(decisions, key=lambda r: r.get("at") or ""):
        out[(row.get("project"), row.get("marker"))] = row
    return list(out.values())


def summarise(collected) -> dict:
    decisions = _latest(collected["decisions"])
    corrections = collected["corrections"]
    moved = []
    for row in decisions:
        seen = row.get("observed") or {}
        if row.get("final") is None or seen.get("low") is None:
            continue
        mine = seen.get("session_id") == row.get("session_id")
        by_hand = seen.get("method") == "manual" or (mine and seen.get("edited_since"))
        if not by_hand:
            continue
        moved.append({"project": row["project"], "marker": row["marker"],
                      "final": row["final"], "start": row.get("start"),
                      "user": seen["low"], "delta": seen["low"] - row["final"],
                      "check": (row.get("check") or {}).get("direction"),
                      "version": _version_tag(row)})
    by_marker = defaultdict(list)
    for entry in moved:
        by_marker[entry["marker"]].append(entry)
    verdicts = defaultdict(lambda: defaultdict(int))
    for row in corrections:
        if row.get("what", "").startswith("classifier_"):
            verdicts[row["marker"]][row["what"].split("_", 1)[1]] += 1
    layers = defaultdict(lambda: {"made": 0, "removed": 0, "added": 0, "low_signal": 0,
                                  "withheld": 0})
    for row in decisions:
        layer = row.get("classifier") or {}
        if not layer.get("status"):
            continue
        key = row.get("role") or "unknown"
        entry = layers[key]
        if layer["status"] == "ready":
            entry["made"] += 1
            entry["removed"] += int(layer.get("n_removed") or 0)
            entry["added"] += int(layer.get("n_added") or 0)
            entry["low_signal"] += int(layer.get("grade") == "low")
        elif layer["status"] == "withheld":
            entry["withheld"] += 1
    checks = []
    for row in decisions:
        check = row.get("check") or {}
        seen = row.get("observed") or {}
        if not check.get("direction"):
            continue
        toward = None
        if seen.get("low") is not None and check.get("from") is not None:
            toward = (seen["low"] > check["from"]) == (check["direction"] == "up")
        checks.append({"project": row["project"], "marker": row["marker"],
                       "direction": check["direction"], "from": check["from"],
                       "confirmed": check.get("confirmed"), "final": row.get("final"),
                       "user": seen.get("low"), "toward_user": toward})
    return {"n_decisions": len(decisions), "n_corrections": len(corrections),
            "moved": moved, "by_marker": dict(by_marker), "verdicts":
            {m: dict(v) for m, v in verdicts.items()}, "layers": dict(layers),
            "checks": checks}


def _n(value):
    return "-" if value is None else f"{value:.4g}"


def to_markdown(summary) -> str:
    lines = ["# What finished gating sessions say", "",
             f"{summary['n_decisions']} marker decisions (the latest per project and marker), "
             f"{summary['n_corrections']} corrections recorded.", "",
             "Read-only: nothing here changes a parameter. A finding goes into a `[cal]` "
             "constant or markers.yaml by hand, with a VERSION bump and bench evidence.", ""]
    lines += ["## Gates the user moved", "",
              "| marker | projects | mean change from the session's gate | each (final -> user) |",
              "|---|---|---|---|"]
    for marker, entries in sorted(summary["by_marker"].items()):
        mean = sum(e["delta"] for e in entries) / len(entries)
        each = "; ".join(f"{e['project']}: {_n(e['final'])} -> {_n(e['user'])}"
                         for e in entries[:6])
        lines.append(f"| {marker} | {len(entries)} | {mean:+.3g} | {each} |")
    if not summary["by_marker"]:
        lines.append("| - | 0 | - | none moved by hand |")
    lines += ["", "## The classifier's layers", "",
              "| marker role | layers | cells removed | cells added | low signal | withheld |",
              "|---|---|---|---|---|---|"]
    for role, entry in sorted(summary["layers"].items()):
        lines.append(f"| {role} | {entry['made']} | {entry['removed']} | {entry['added']} | "
                     f"{entry['low_signal']} | {entry['withheld']} |")
    lines += ["", "| marker | adopted | reverted | discarded |", "|---|---|---|---|"]
    for marker, counts in sorted(summary["verdicts"].items()):
        lines.append(f"| {marker} | {counts.get('adopted', 0)} | {counts.get('reverted', 0)} | "
                     f"{counts.get('discarded', 0)} |")
    lines += ["", "## The classifier check", ""]
    checks = summary["checks"]
    if checks:
        toward = sum(1 for c in checks if c["toward_user"] is True)
        away = sum(1 for c in checks if c["toward_user"] is False)
        lines.append(f"{len(checks)} checks; the user's gate now lies the way the check "
                     f"looked in {toward}, the other way in {away}.")
        lines += ["", "| project | marker | direction | from | rows confirmed | final | user |",
                  "|---|---|---|---|---|---|---|"]
        for c in checks[:50]:
            lines.append(f"| {c['project']} | {c['marker']} | {c['direction']} | "
                         f"{_n(c['from'])} | {c['confirmed'] if c['confirmed'] is not None else '-'} | "
                         f"{_n(c['final'])} | {_n(c['user'])} |")
    else:
        lines.append("No classifier check fired.")
    return "\n".join(lines) + "\n"


def command(projects=None, out=None, exclude_version=(), as_json=False) -> int:
    summary = summarise(collect(projects, exclude_version))
    text = json.dumps(summary, indent=1, default=str) if as_json else to_markdown(summary)
    if out:
        Path(out).write_text(text, encoding="utf-8")
        print(f"Wrote {out}")
    else:
        print(text)
    return 0
