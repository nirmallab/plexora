"""Run the 3-D benchmark: datasets x scenarios, one JSON per run.

    python tools/bench3d/run.py --label before [--datasets small,ims] [--scenarios all]
                                [--reps 7] [--headed] [--port N] [--data DIR] [--out DIR]

Results go to docs/internal/3d/perf/<sha12>[-dirty]-<label>.json (or `--out`) with the
commit, the WebGL renderer, the display period F and every scenario's raw
values; `report.py` turns two of them into the before/after tables. A server is
started for the run (serve.py) unless `--port` names one already up. After a
dataset's scenarios, what its derived files hold on disk is recorded too
(`disk`: the 3-D copy, `bricks_diskMB`; an import's pyramid copy,
`pyramid_diskMB`).
"""

from __future__ import annotations

import argparse
import base64
import json
import platform
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
from serve import bench_home, start, stop  # noqa: E402

OUT = ROOT / "docs" / "internal" / "3d" / "perf"

#: Which scenarios each dataset runs, and what they need to know about it.
DATASETS = {
    "small": {"scenarios": ["open", "channels", "zsteps", "jump", "held", "sync", "panes_resize",
                            "zreveal", "raildrag", "panesync",
                            "volume", "volume_flash", "clip", "area_slide", "zoom", "contrast", "tour", "memory"],
              "params": {"channels": [0, 1], "extra": 2, "z0": 6}},
    "twoplane": {"scenarios": ["flash"], "params": {}},
    "ims": {"scenarios": ["open", "channels", "zsteps", "jump", "held", "sync", "panes_resize",
                          "volume", "volume_flash", "clip", "area_slide", "zoom", "contrast", "tour", "memory"],
            "params": {"channels": [0, 1, 2], "extra": 3, "z0": 85, "crossings": [63, 127]}},
    "meso": {"scenarios": ["open", "zsteps", "jump", "held", "sync", "panes_resize", "volume",
                           "volume_flash", "zoom", "contrast", "tour"],
             "params": {"channels": [0], "z0": 958}},
    "medium": {"scenarios": ["open", "channels", "zsteps", "jump", "held", "sync", "panes_resize",
                             "zreveal", "raildrag", "panesync",
                             "volume", "volume_flash", "zoom", "tour"],
               "params": {"channels": [0, 1], "extra": 2, "z0": 90, "crossings": [31, 63]}},
    "large": {"scenarios": ["open", "zsteps", "zreveal", "sync", "volume", "tour"],
              "params": {"channels": [0, 1], "extra": 2, "z0": 1990}},
    "xenium": {"scenarios": ["open", "zsteps", "zoom"], "params": {"channels": [0], "z0": 5}},
    "flat": {"scenarios": ["parity2d"], "params": {}},
    "mediumz": {"scenarios": ["open", "zsteps", "jump", "held", "volume", "volume_flash", "zoom"],
                "params": {"channels": [0, 1], "extra": 2, "z0": 64}},
    "flatstack": {"scenarios": ["open", "zsteps", "jump", "held", "sync", "panes_resize",
                                "zreveal", "raildrag", "panesync", "volume",
                                "volume_flash", "zoom"],
                  "params": {"channels": [0, 1], "extra": 2, "z0": 50}},
}


def _size_mb(path: Path) -> float:
    if path.is_file():
        return path.stat().st_size / 1e6
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1e6


def disk_metrics(data: Path, dataset: str) -> dict:
    """What the dataset's derived files hold on disk: its 3-D copy (every
    `volume_v1` store under the data root with the dataset in its path) and
    the pyramid copy its import recorded, if any."""
    metrics = {}
    stores = [p for p in data.rglob("volume_v1") if p.is_dir() and dataset in p.parts]
    if stores:
        metrics["bricks_diskMB"] = [round(sum(_size_mb(p) for p in stores), 1)]
    try:
        entry = json.loads((data / "config.json").read_text(encoding="utf-8")).get(dataset) or {}
    except Exception:
        entry = {}
    pyramid = entry.get("imagePyramid")
    if pyramid and Path(pyramid).exists():
        metrics["pyramid_diskMB"] = [round(_size_mb(Path(pyramid)), 1)]
    return metrics


def git(*args) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()


def run_one(port, dataset, scenario, reps, headed, params, timeout=1800):
    out = bench_home() / "runs" / f"{dataset}-{scenario}-{int(time.time() * 1000)}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["node", str(HERE / "drive.mjs"), "--base", f"http://127.0.0.1:{port}",
           "--dataset", dataset, "--scenario", scenario, "--reps", str(reps),
           "--out", str(out), "--params", json.dumps(params)]
    if headed:
        cmd.append("--headed")
    t0 = time.time()
    try:
        subprocess.run(cmd, cwd=str(HERE), timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {"error": f"timed out after {timeout} s"}
    try:
        result = json.loads(out.read_text(encoding="utf-8"))
    except Exception as error:
        return {"error": f"no result: {error!r}"}
    result["wall_s"] = round(time.time() - t0, 1)
    return result


def write_record(record: dict, out_dir: Path) -> Path:
    """The run's JSON, `<sha12>[-dirty]-<label>.json` in `out_dir`; screenshots
    (base64 in a scenario's `extra`) moved out beside the bench's runs first."""
    sha, label = record["sha"], record["label"]
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"{sha}{'-dirty' if record['dirty'] else ''}-{label}.json"
    path = out_dir / name
    # Screenshots stay out of the repository: kept beside the bench's runs.
    for dataset, scenarios in record["results"].items():
        parity = scenarios.get("parity2d")
        if parity and parity.get("extra", {}).get("png"):
            png = bench_home() / "parity" / f"{sha}-{label}-{dataset}.png"
            png.parent.mkdir(parents=True, exist_ok=True)
            png.write_bytes(base64.b64decode(parity["extra"].pop("png")))
            parity["extra"]["png"] = f"$PLEXORA_BENCH_HOME/parity/{png.name}"
        # Any scenario's screenshots (`extra.shots`: {name: base64 PNG}).
        for scenario, result in scenarios.items():
            shots = (result.get("extra") or {}).get("shots") if isinstance(result, dict) else None
            if not isinstance(shots, dict):
                continue
            for shot, data in list(shots.items()):
                if not isinstance(data, str) or data.startswith("$"):
                    continue
                png = bench_home() / "shots" / f"{sha}-{label}-{dataset}-{scenario}-{shot}.png"
                png.parent.mkdir(parents=True, exist_ok=True)
                png.write_bytes(base64.b64decode(data))
                shots[shot] = f"$PLEXORA_BENCH_HOME/shots/{png.name}"
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, indent=1))
    return path


def main(argv=None):
    # Scenario names, paths and the server's own lines can carry characters a
    # Windows console's code page cannot print.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--label", required=True)
    parser.add_argument("--datasets", default="small,twoplane,ims,meso,flat")
    parser.add_argument("--scenarios", default="all")
    parser.add_argument("--reps", type=int, default=7)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--data", default=str(bench_home() / "data"))
    parser.add_argument("--env", action="append", default=[], help="K=V for the server")
    parser.add_argument("--tree", default=None,
                        help="serve this checkout (a detached worktree of an older commit, say)")
    parser.add_argument("--out", default=str(OUT),
                        help="where the run's JSON goes (default docs/internal/3d/perf; a smoke "
                             "run belongs under $PLEXORA_BENCH_HOME instead)")
    args = parser.parse_args(argv)

    # Named scenarios run in the order named (the server's caches are warmed by
    # whatever ran before: a "cold" scenario named first is cold); `all` runs
    # each dataset's own list in its order.
    wanted = None if args.scenarios == "all" else [s for s in args.scenarios.split(",") if s]
    tree = Path(args.tree).expanduser() if args.tree else ROOT
    sha = git("-C", str(tree), "rev-parse", "HEAD")[:12]
    dirty = bool(git("-C", str(tree), "status", "--porcelain", "--untracked-files=no"))
    record = {
        "label": args.label, "sha": sha, "dirty": dirty, "headed": args.headed,
        "date": time.strftime("%Y-%m-%dT%H:%M:%S"), "reps": args.reps,
        "machine": {"platform": platform.platform(), "processor": platform.processor(),
                    "python": platform.python_version()},
        "results": {},
    }
    server = None
    port = args.port
    if not port:
        extra = dict(item.split("=", 1) for item in args.env)
        server = start(Path(args.data), 0, extra, root=tree)
        port = server["port"]
        record["server_env"] = sorted(extra)
    try:
        for dataset in args.datasets.split(","):
            spec = DATASETS[dataset]
            for scenario in (spec["scenarios"] if wanted is None
                             else [s for s in wanted if s in spec["scenarios"]]):
                reps = args.reps + (2 if scenario in ("zsteps", "volume") else 0)
                print(f"{dataset}/{scenario} ...", flush=True)
                result = run_one(port, dataset, scenario, reps, args.headed, spec["params"])
                record["results"].setdefault(dataset, {})[scenario] = result
                record.setdefault("renderer", result.get("renderer"))
                if result.get("F"):
                    record.setdefault("F", {})[dataset] = result["F"]
                status = result.get("error") or result.get("skipped") or "ok"
                print(f"  {status if isinstance(status, str) else 'ok'} "
                      f"({result.get('wall_s', '?')} s)", flush=True)
            disk = disk_metrics(Path(args.data).expanduser(), dataset)
            if disk:
                record["results"].setdefault(dataset, {})["disk"] = {"metrics": disk}
    finally:
        # The record first: what was measured survives a server that will not
        # stop (or an interrupt), and a partial run still says how far it got.
        path = write_record(record, Path(args.out).expanduser())
        print(path, flush=True)
        if server:
            try:
                stop(server["pid"])
            except Exception as error:  # noqa: BLE001 -- the record is written
                print(f"stopping the server (pid {server['pid']}) failed: {error!r}", flush=True)


if __name__ == "__main__":
    main()
