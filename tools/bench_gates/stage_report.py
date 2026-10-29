"""Where a gating session's time went, from what it recorded.

    python tools/bench_gates/stage_report.py <session id or folder> [...] \
        [--data-root DIR] [--json out.json]

A session id is looked up under `<data root>/.agent/sessions/gating/`
(`--data-root`, else PLEXORA_DATA_PATH, else the platform default); a folder
is read as it is. Nothing is written to the session.

From `decisions.jsonl`:

- the wall clock, the bulk pass (`calibrated`, `profiled`: milliseconds per
  image and marker) and when the first packet went out;
- per packet kind, issued -> answered (the reader: a model call and its
  driver) and the gap from an answer to the next packet while none was out
  (the server: applying the answer, choosing and drawing the next);
- the stages the engine timed (`ms` on `issued`, `answered`, `profiled`:
  choose, build, sample, plan, render, store, apply, regression, lineage,
  write, settle), summed over the session;
- the mirror (`mirrored`: milliseconds per script, looks dropped as stale);
- the most packets out at once, and how many answers were refused as stale.

From the harness's trace (`<data root>/.agent/ai/trace.sqlite`), when the
session was driven by it: model-call latency by task, and `gating_next` /
`gating_answer` tool-call latency.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path


def data_root(arg=None) -> Path:
    if arg:
        return Path(arg).expanduser()
    if os.environ.get("PLEXORA_DATA_PATH"):
        return Path(os.environ["PLEXORA_DATA_PATH"]).expanduser()
    from plexora import paths

    return paths.agent_root().parent


def session_folder(name, root) -> Path:
    path = Path(name).expanduser()
    if path.is_dir():
        return path
    return root / ".agent" / "sessions" / "gating" / name


def _events(folder):
    out = []
    with open(folder / "decisions.jsonl", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def _epoch(stamp):
    """Seconds since the epoch of an ISO time the record holds, or None."""
    import datetime as dt

    if not stamp:
        return None
    try:
        return dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _spread(values):
    if not values:
        return {"n": 0}
    return {"n": len(values), "sum_s": round(sum(values), 1),
            "median_s": round(statistics.median(values), 2),
            "max_s": round(max(values), 2)}


def report(folder, root=None) -> dict:
    events = _events(folder)
    record = {}
    if (folder / "session.json").is_file():
        record = json.loads((folder / "session.json").read_text(encoding="utf-8"))
    out = {"session_id": folder.name, "state": record.get("state"),
           "options": {k: (record.get("options") or {}).get(k)
                       for k in ("engine", "mode", "classifier", "mirror_delay_ms",
                                 "display_check", "seed")},
           "units": len(record.get("units") or {}),
           "outstanding_peak": int(record.get("outstanding_peak") or 0),
           "reissued": int(record.get("reissued") or 0)}
    if not events:
        return out
    t0, t1 = events[0]["t"], events[-1]["t"]
    created = _epoch(record.get("created_at"))
    if created is not None and created < t0:
        # The session's start: its first event is only logged once the bulk
        # pass has calibrated the image.
        t0 = created
    out["wall_s"] = round(t1 - t0, 1)
    issued = {e["packet_id"]: e for e in events if e.get("event") == "issued"}
    answered = {e["packet_id"]: e for e in events if e.get("event") in ("answered", "replayed")}
    first = min((e["t"] for e in issued.values()), default=None)
    out["first_packet_s"] = round(first - t0, 1) if first is not None else None
    bulk = [e for e in events if e.get("event") in ("profiled", "calibrated")]
    out["bulk"] = {
        "calibrate_s": round(sum((e.get("ms") or {}).get("calibrate", 0)
                                 for e in bulk) / 1e3, 1),
        "profile_s": _spread([(e.get("ms") or {}).get("profile", 0) / 1e3
                              for e in bulk if e.get("event") == "profiled"]),
        "last_profiled_s": round(max((e["t"] for e in bulk), default=t0) - t0, 1)}
    reader = defaultdict(list)
    for packet_id, done in answered.items():
        if packet_id in issued and done.get("event") == "answered":
            reader[issued[packet_id].get("kind")].append(done["t"] - issued[packet_id]["t"])
    out["reader_by_kind"] = {k: _spread(v) for k, v in sorted(reader.items())}
    # The server's gap: an answer to the next packet, while nothing was out.
    flow = sorted((e for e in events if e.get("event") in ("issued", "answered")),
                  key=lambda e: e["t"])
    gaps, held, peak = [], 0, 0
    for index, event in enumerate(flow):
        if event["event"] == "issued":
            held += 1
            peak = max(peak, held)
            continue
        held = max(0, held - 1)
        following = next((e for e in flow[index + 1:] if e["event"] == "issued"), None)
        if following is not None and held == 0:
            gaps.append(following["t"] - event["t"])
    out["server_gap"] = _spread(gaps)
    out["peak_out_in_log"] = peak
    stages = Counter()
    for event in events:
        if event.get("event") in ("issued", "answered", "replayed", "profiled"):
            for stage, ms in (event.get("ms") or {}).items():
                stages[f"{event['event'] if event['event'] != 'replayed' else 'answered'}."
                       f"{stage}"] += float(ms)
    out["stages_s"] = {k: round(v / 1e3, 1) for k, v in sorted(stages.items())}
    mirrored = [e for e in events if e.get("event") == "mirrored"]
    out["mirror"] = {"scripts": len(mirrored),
                     "send": _spread([(e.get("ms") or {}).get("mirror", 0) / 1e3
                                      for e in mirrored]),
                     "dropped": sum(int(e.get("dropped") or 0) for e in mirrored),
                     "status": dict(Counter(e.get("status") for e in mirrored))}
    out["packets"] = dict(Counter(e.get("kind") for e in issued.values()))
    out["packets_total"] = len(issued)
    per_marker = Counter()
    for event in issued.values():
        for unit in event.get("units") or []:
            per_marker[unit.get("marker")] += 1
    out["packets_per_marker"] = dict(per_marker.most_common())
    trace = _trace(root, folder.name) if root else None
    if trace:
        out["trace"] = trace
    return out


def _trace(root, session_id):
    path = root / ".agent" / "ai" / "trace.sqlite"
    if not path.is_file():
        return None
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        runs = [r[0] for r in db.execute("select run_id from runs where session_id = ?",
                                         (session_id,))]
        if not runs:
            return None
        marks = ",".join("?" * len(runs))
        models = defaultdict(list)
        tokens = Counter()
        for task, latency, uncached, read, write, output in db.execute(
                f"select task, latency_ms, input_uncached, cache_read, cache_write, "
                f"output_tokens from model_calls where run_id in ({marks})", runs):
            models[task or "?"].append((latency or 0) / 1e3)
            tokens["input_uncached"] += uncached or 0
            tokens["cache_read"] += read or 0
            tokens["cache_write"] += write or 0
            tokens["output"] += output or 0
        tools = defaultdict(list)
        for tool, latency in db.execute(
                f"select tool, latency_ms from tool_calls where run_id in ({marks})", runs):
            tools[tool].append((latency or 0) / 1e3)
        return {"runs": runs, "model_calls": {k: _spread(v) for k, v in models.items()},
                "tokens": dict(tokens),
                "tool_calls": {k: _spread(v) for k, v in tools.items()
                               if k.startswith("gating_")}}
    finally:
        db.close()


def show(rep):
    print("=" * 78)
    print(f"{rep['session_id']}  state={rep.get('state')}  units={rep.get('units')}  "
          f"options={rep.get('options')}")
    if "wall_s" not in rep:
        print("  no events")
        return
    print(f"  wall {rep['wall_s']} s; first packet at {rep['first_packet_s']} s; "
          f"bulk: calibrate {rep['bulk']['calibrate_s']} s, profiles {rep['bulk']['profile_s']}, "
          f"last profiled at {rep['bulk']['last_profiled_s']} s")
    print(f"  packets {rep['packets_total']} {rep['packets']}; peak out "
          f"{rep['outstanding_peak']} (log {rep['peak_out_in_log']}); reissued {rep['reissued']}")
    print("  reader (issued -> answered):")
    for kind, row in rep["reader_by_kind"].items():
        print(f"    {kind:22s} {row}")
    print(f"  server gap (answered -> next issued, none out): {rep['server_gap']}")
    print("  engine stages (s, summed):")
    for stage, seconds in rep["stages_s"].items():
        print(f"    {stage:24s} {seconds:8.1f}")
    print(f"  mirror: {rep['mirror']}")
    if rep.get("trace"):
        trace = rep["trace"]
        print(f"  trace: model calls {trace['model_calls']}")
        print(f"         tokens {trace['tokens']}")
        print(f"         tool calls {trace['tool_calls']}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("sessions", nargs="+")
    parser.add_argument("--data-root")
    parser.add_argument("--json")
    args = parser.parse_args(argv)
    root = data_root(args.data_root)
    reports = [report(session_folder(name, root), root) for name in args.sessions]
    for rep in reports:
        show(rep)
    if args.json:
        Path(args.json).write_text(json.dumps(reports, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
