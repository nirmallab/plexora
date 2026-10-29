"""Time automatic gating on a real image with no model calls, and check that
a change to the pipeline's speed changed none of its decisions.

    python tools/bench_gates/perf_bench.py run --project lsp11385 \
        --scratch C:/pxperf --markers CD45,Ki67,CD3e,... --out runs/base.json \
        [--lanes 1] [--latency cell_panel=12,qc_confirm=7,default=8] [--scale 1.0] \
        [--classifier layer] [--engine v2] [--seed 0] [--style oracle]
    python tools/bench_gates/perf_bench.py compare runs/base.json runs/new.json

`run` copies the project (its folder and its registry entry; the image and
mask stay where they are, read only) and the licence into `--scratch` once,
points PLEXORA_DATA_PATH there, and drives one propose-mode gating session
to the end with `plexora.ai.bench.TruthAgent` reading each packet: no model,
no credits, nothing written to the live install (a scratch root that is, holds
or sits inside a live data directory is refused). The reader's truth is each
marker's Auto (mixture) gate -- what matters here is the work the server does
per packet, not where the gates land. `--latency` holds each answer back for
a model call's typical seconds (by packet kind; `--scale` multiplies them), so
lanes (`--lanes N`: N readers, `parallel: N`) can be timed against the
critical path they shorten. The bulk pass is not drained first: packets start
while it runs, as in the viewer.

The record (`--out`) holds the stage report (`stage_report.py`), each
marker's final state and gate, and a fingerprint of every packet: its JSON
without what names the session or the run (`session_id`, artifact ids,
`briefed`, `progress`) and the bytes of its images.

`compare` says whether two records decided the same: every marker's state and
gate equal, and the packets equal -- in order for two single-lane runs, as
one set keyed by (kind, markers, look) otherwise -- then the time each took.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

#: Seconds a model call took per packet kind, median of the 2026-10-28
#: ImageLSP23362 run (Opus panels, Sonnet stain checks).
LATENCY = {"cell_panel": 11.7, "qc_confirm": 6.7, "default": 8.0}

#: Packet fields that name the session or the run, not what was decided.
VOLATILE = ("session_id", "packet_id", "briefed", "progress", "rerendered", "artifact_id",
            "slide")
#: Floats are compared to this many significant digits: a mixture fit's
#: reductions run on threaded BLAS, whose summation order -- and so the last
#: bits of a gate -- moves from one run of the same code to the next.
DIGITS = 9


def _canonical(value):
    if isinstance(value, float):
        return float(f"{value:.{DIGITS}g}")
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    return value


def parse_latency(text) -> dict:
    out = dict(LATENCY)
    for part in (text or "").split(","):
        if "=" in part:
            kind, seconds = part.split("=", 1)
            out[kind.strip()] = float(seconds)
    return out


def prepare(scratch: Path, source: Path, project: str) -> Path:
    """The scratch data root, holding a fresh copy of `project`: copied again
    for every run, since a session leaves its mark on the project (the
    expression source it confirmed, the display calibration, provenance,
    classifier layers) and the next run must start where the first did."""
    from plexora import paths
    from plexora.server.models.project import read_config, write_config

    scratch.mkdir(parents=True, exist_ok=True)
    source_config = read_config(paths.config_path(source))
    if project not in source_config:
        raise SystemExit(f"{project!r} is not registered in {source}")
    config_file = paths.config_path(scratch)
    config = read_config(config_file) if config_file.is_file() else {}
    config[project] = source_config[project]
    write_config(config_file, config)
    if (scratch / project).is_dir():
        shutil.rmtree(scratch / project)
    shutil.copytree(source / project, scratch / project)
    if (source / "license").is_dir() and not (scratch / "license").is_dir():
        shutil.copytree(source / "license", scratch / "license")
    return scratch


def fingerprint(packet, images) -> dict:
    clean = _canonical(packet)
    body = json.dumps(clean, sort_keys=True, default=str).encode("utf-8")
    units = packet.get("units") or []
    return {"packet_id": packet.get("packet_id"), "kind": packet.get("kind"),
            "markers": [u.get("marker") for u in units],
            "look": (packet.get("evidence") or {}).get("look"),
            "json": hashlib.sha256(body).hexdigest()[:16],
            "images": [hashlib.sha256(data).hexdigest()[:16] for data, _fmt in images]}


def truth_agent(project, markers, style, seed, cache=None):
    """(TruthAgent, {marker: gate}): each marker's Auto gate as the truth, read
    on the values the session gates (after its expression source is settled,
    so the gate and the panels' values are in one space). The gates are fitted
    once and kept in `cache` (a JSON file): fitting them again would take the
    driver tens of seconds before its first packet, every run."""
    import numpy as np

    from plexora.agent import AgentSession
    from plexora.ai import bench
    from plexora.plugins.gating.server import model

    session = AgentSession(table_limit=4)
    ds = session.data(project)
    kept = json.loads(Path(cache).read_text(encoding="utf-8")) \
        if cache and Path(cache).is_file() else {}
    truth, values, gates = {}, {}, {}
    for marker in markers:
        gate = kept.get(marker)
        if gate is None:
            gate = (model.fit_for(ds, marker) or {}).get("gate")
        if gate is None:
            continue
        values[marker] = np.asarray(ds.table.columns([marker])[marker], dtype=np.float32)
        gates[marker] = float(gate)
        truth[marker] = values[marker] > np.float32(gate)
    session.close()
    if cache:
        Path(cache).write_text(json.dumps({**kept, **gates}, indent=1), encoding="utf-8")
    return bench.TruthAgent(values, truth, style, seed=seed), gates


def packets_on_disk(root, session_id) -> list:
    """Every stored packet's fingerprint, read from the session's folder."""
    folder = Path(root) / ".agent" / "sessions" / "gating" / session_id / "packets"
    out = []
    for path in sorted(folder.glob("pk_*.json")):
        packet = json.loads(path.read_text(encoding="utf-8"))
        image_dir = folder / path.stem
        images = [(f.read_bytes(), f.suffix.lstrip(".")) for f in sorted(
            image_dir.iterdir(), key=lambda f: int(f.stem) if f.stem.isdigit() else 0)]             if image_dir.is_dir() else []
        out.append(fingerprint(packet, images))
    return out


def drive(session, project, markers, make_agent, *, lanes, latency, scale, options, emit):
    """One session to the end; returns (session id, seconds, gates read by)."""
    from plexora.agent import invoke
    from plexora.plugins.gating.server.autogate import engine as engines
    from plexora.plugins.gating.server.autogate import packets as packet_views

    def ok(answer):
        if not answer.get("ok"):
            raise RuntimeError(json.dumps(answer.get("error"), default=str)[:800])
        return answer["result"]

    started_at = time.perf_counter()
    started = ok(invoke(session, "gating_session_start", {
        "scope": "project", "project": project, "markers": list(markers), "mode": "propose",
        **options}))
    sid = started["session_id"]
    emit(f"session {sid}")
    agent, gates = make_agent()
    errors = []
    answered = {"n": 0}
    guard = threading.Lock()

    def lane(index):
        reader = {"reader": f"lane{index}", "parallel": lanes} if lanes > 1 else {}
        held = {}
        try:
            result = ok(invoke(session, "gating_next", {"session_id": sid, "wait_s": 30,
                                                        **reader}))
            for _ in range(2000):
                state = result.get("state")
                if state in ("busy", "bulk_running"):
                    result = ok(invoke(session, "gating_next", {"session_id": sid, "wait_s": 30,
                                                                **reader}))
                    continue
                if state != "decision":
                    return
                packet = packet_views.resolve_as_in(result["packet"], held)
                if packet.get("kind") == "cell_panel":
                    stored, _ = engines.store().read_packet(sid, packet["packet_id"])
                    packet = {**packet, "_panel": stored.get("_panel")}
                with guard:
                    answer = agent.answer(packet)
                pause = latency.get(packet.get("kind"), latency["default"]) * scale
                if pause:
                    time.sleep(pause)
                result = ok(invoke(session, "gating_answer", {
                    "session_id": sid, "packet_id": packet["packet_id"], "answer": answer,
                    **reader}))["next"]
                with guard:
                    answered["n"] += 1
        except Exception as exc:  # noqa: BLE001 -- reported, the run is void
            errors.append(f"lane{index}: {exc}")

    threads = [threading.Thread(target=lane, args=(i,), daemon=True) for i in range(lanes)]
    for thread in threads:
        thread.start()
        if lanes > 1:
            time.sleep(0.2)
    for thread in threads:
        thread.join()
    if errors:
        raise RuntimeError("; ".join(errors))
    seconds = time.perf_counter() - started_at
    ok(invoke(session, "gating_session_finish", {"session_id": sid, "action": "close"}))
    emit(f"decided in {seconds:.1f} s, {answered['n']} answers")
    return sid, seconds, gates


def run(args, emit=print) -> int:
    scratch = Path(args.scratch).expanduser().resolve() / "root"
    os.environ["PLEXORA_DATA_PATH"] = str(scratch)
    from run_arms import check_data_root

    check_data_root(scratch)
    source = Path(args.source).expanduser() if args.source else None
    if source is None:
        from platformdirs import user_data_dir

        source = Path(user_data_dir("plexora", appauthor=False))
    prepare(scratch, source, args.project)

    from plexora import paths

    if Path(paths.data_root()).resolve() != scratch:
        raise SystemExit(f"the data root is {paths.data_root()}, not {scratch}")
    from plexora.agent import AgentSession, jobs, registry
    from plexora.ai import bench
    from plexora.plugins.gating.server.autogate import engine as engines

    import stage_report

    registry.discover(["gating"])
    session = AgentSession(table_limit=4)
    markers = [m.strip() for m in args.markers.split(",")] if args.markers else \
        list(session.data(args.project).table.markers)
    options = {"engine": args.engine, "classifier": args.classifier, "seed": args.seed,
               "reuse_answers": False, "on_limit": "extend", "display_check": "off",
               "mirror": False, "agent": "bench:perf"}
    sid, seconds, gates = drive(
        session, args.project, markers,
        lambda: truth_agent(args.project, markers, args.style, args.seed,
                            cache=scratch.parent / f"truth_{args.project}.json"),
        lanes=args.lanes, latency=parse_latency(args.latency), scale=args.scale,
        options=options, emit=emit)
    jobs.drain(900)
    st = engines.store()
    folder = st.folder(sid)
    record = st.load(sid)
    packets = []
    for path in sorted((folder / "packets").glob("pk_*.json")):
        packet, images = st.read_packet(sid, path.stem)
        packets.append(fingerprint(packet, images))
    units = {u["marker"]: {"state": u.get("state"), "final": u.get("final"),
                           "proposed": u.get("proposed"), "confidence": u.get("confidence"),
                           "packets": len(u.get("packets") or [])}
             for u in record["units"].values()}
    out = {"project": args.project, "markers": markers, "lanes": args.lanes,
           "data_root": str(scratch),
           "latency": parse_latency(args.latency), "scale": args.scale,
           "options": options, "style": args.style, "seconds": round(seconds, 1),
           "session_id": sid, "code": bench.code_state(), "truth_gates": gates,
           "report": stage_report.report(folder, scratch), "units": units,
           "packets": packets}
    stage_report.show(out["report"])
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
        emit(f"wrote {args.out}")
    session.close()
    return 0


def _keyed(packets):
    out = {}
    for row in packets:
        key = (row["kind"], tuple(row["markers"]), row.get("look"))
        out.setdefault(key, []).append((row["json"], tuple(row["images"])))
    return out


def compare(a, b, emit=print, root=None) -> int:
    """0 when `b` decided exactly what `a` did. With the scratch root at hand
    (`root`, else the records' own), the packets are fingerprinted again from
    the sessions' folders, by this version of `fingerprint`."""
    for record in (a, b):
        where = root or record.get("data_root")
        if where and (Path(where) / ".agent" / "sessions" / "gating" /
                      record["session_id"]).is_dir():
            record["packets"] = packets_on_disk(where, record["session_id"])
    same = True
    for marker in sorted(set(a["units"]) | set(b["units"])):
        x, y = a["units"].get(marker), b["units"].get(marker)
        if x is None or y is None or any(x.get(k) != y.get(k)
                                         for k in ("state", "final", "proposed", "confidence")):
            same = False
            emit(f"  unit {marker}: {x} != {y}")
    if a["lanes"] == 1 and b["lanes"] == 1:
        pa, pb = a["packets"], b["packets"]
        if len(pa) != len(pb):
            same = False
            emit(f"  packets: {len(pa)} != {len(pb)}")
        for x, y in zip(pa, pb):
            if (x["json"], x["images"]) != (y["json"], y["images"]):
                same = False
                emit(f"  {x['packet_id']} {x['kind']} {x['markers']}: differs "
                     f"(json {x['json']} vs {y['json']}, images {x['images'] == y['images']})")
    else:
        ka, kb = _keyed(a["packets"]), _keyed(b["packets"])
        for key in sorted(set(ka) | set(kb), key=str):
            if sorted(ka.get(key, [])) != sorted(kb.get(key, [])):
                same = False
                emit(f"  {key}: {len(ka.get(key, []))} vs {len(kb.get(key, []))} packets, "
                     "or their contents differ")
    emit(f"decisions {'IDENTICAL' if same else 'DIFFER'}")
    ra, rb = a["report"], b["report"]
    emit(f"  wall  {a['seconds']:8.1f} s -> {b['seconds']:8.1f} s "
         f"({100 * (b['seconds'] - a['seconds']) / max(a['seconds'], 1e-9):+.0f} %)")
    for key in ("first_packet_s",):
        emit(f"  {key:14s} {ra.get(key)} -> {rb.get(key)}")
    emit(f"  server gap     {ra.get('server_gap')} -> {rb.get('server_gap')}")
    stages = sorted(set(ra.get("stages_s") or {}) | set(rb.get("stages_s") or {}))
    for stage in stages:
        emit(f"  {stage:24s} {(ra.get('stages_s') or {}).get(stage, 0):8.1f} -> "
             f"{(rb.get('stages_s') or {}).get(stage, 0):8.1f}")
    return 0 if same else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("run")
    one.add_argument("--project", required=True)
    one.add_argument("--scratch", required=True, help="a scratch folder; never a live root")
    one.add_argument("--source", help="the data root to copy the project from "
                                      "(default: the platform default)")
    one.add_argument("--markers")
    one.add_argument("--lanes", type=int, default=1)
    one.add_argument("--latency", default="")
    one.add_argument("--scale", type=float, default=0.0,
                     help="multiplies --latency (default 0: no simulated model time)")
    one.add_argument("--engine", default="v2")
    one.add_argument("--classifier", default="layer")
    one.add_argument("--seed", type=int, default=0)
    one.add_argument("--style", default="oracle")
    one.add_argument("--out")
    two = sub.add_parser("compare")
    two.add_argument("a")
    two.add_argument("b")
    two.add_argument("--root", help="the scratch data root the sessions are in")
    args = parser.parse_args(argv)
    if args.command == "compare":
        return compare(json.loads(Path(args.a).read_text(encoding="utf-8")),
                       json.loads(Path(args.b).read_text(encoding="utf-8")), root=args.root)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
