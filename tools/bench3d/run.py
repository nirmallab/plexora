"""Run the 3-D benchmark: datasets x scenarios, one JSON per run.

    python tools/bench3d/run.py --label before [--datasets small,ims] [--scenarios all]
                                [--reps 7] [--headed] [--port N] [--data DIR]

Results go to docs/internal/3d/perf/<sha12>[-dirty]-<label>.json with the
commit, the WebGL renderer, the display period F and every scenario's raw
values; `report.py` turns two of them into the before/after tables. A server is
started for the run (serve.py) unless `--port` names one already up.
"""

from __future__ import annotations

import argparse
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
    "small": {"scenarios": ["open", "channels", "zsteps", "sync", "volume", "zoom", "contrast",
                            "tour", "memory"],
              "params": {"channels": [0, 1], "extra": 2, "z0": 6}},
    "twoplane": {"scenarios": ["flash"], "params": {}},
    "ims": {"scenarios": ["open", "channels", "zsteps", "sync", "volume", "zoom", "contrast",
                          "tour", "memory"],
            "params": {"channels": [0, 1, 2], "extra": 3, "z0": 85, "crossings": [63, 127]}},
    "meso": {"scenarios": ["open", "zsteps", "sync", "volume", "zoom", "contrast", "tour"],
             "params": {"channels": [0], "z0": 958}},
    "medium": {"scenarios": ["open", "channels", "zsteps", "sync", "volume", "zoom", "tour"],
               "params": {"channels": [0, 1], "extra": 2, "z0": 90, "crossings": [31, 63]}},
    "large": {"scenarios": ["open", "zsteps", "sync", "volume", "tour"],
              "params": {"channels": [0, 1], "extra": 2, "z0": 1990}},
    "xenium": {"scenarios": ["open", "zsteps", "zoom"], "params": {"channels": [0], "z0": 5}},
    "flat": {"scenarios": ["parity2d"], "params": {}},
}


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
        result = json.loads(out.read_text())
    except Exception as error:
        return {"error": f"no result: {error!r}"}
    result["wall_s"] = round(time.time() - t0, 1)
    return result


def main(argv=None):
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
    args = parser.parse_args(argv)

    wanted = None if args.scenarios == "all" else set(args.scenarios.split(","))
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
            for scenario in spec["scenarios"]:
                if wanted and scenario not in wanted:
                    continue
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
    finally:
        if server:
            stop(server["pid"])
    OUT.mkdir(parents=True, exist_ok=True)
    name = f"{sha}{'-dirty' if dirty else ''}-{args.label}.json"
    path = OUT / name
    # Screenshots stay out of the repository: kept beside the bench's runs.
    for dataset, scenarios in record["results"].items():
        parity = scenarios.get("parity2d")
        if parity and parity.get("extra", {}).get("png"):
            png = bench_home() / "parity" / f"{sha}-{args.label}-{dataset}.png"
            png.parent.mkdir(parents=True, exist_ok=True)
            import base64
            png.write_bytes(base64.b64decode(parity["extra"].pop("png")))
            parity["extra"]["png"] = f"$PLEXORA_BENCH_HOME/parity/{png.name}"
    path.write_text(json.dumps(record, indent=1))
    print(path)


if __name__ == "__main__":
    main()
