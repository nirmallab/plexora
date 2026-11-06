"""CPython's reference for P9: the oracle gating session on the fixture, in
the conda env with real threads, as tests/test_gating_session.py runs it.

    python truth_p9.py --fixture P9S [--runs 3] --out FILE

Each run is its own process on a fresh copy of the fixture's data root (the
data root is fixed per process), with a throwaway licence (page/py/p9_boot's
`licence`, the suite's issuer). The dump is page/py/p9_run's, the same code
the browser's worker runs. Writes {fixture, runs: [summary...], dump (run 1),
self: compare_p9 of run 1 against each later run, median times}.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = HERE.parent / "page" / "py"
CACHE = Path(os.environ.get("PLEXORA_BENCH_HOME") or "C:/plexora-bench") / "browser_node" / "cache"


def one(fixture_dir: Path, out: Path, prototype=None):
    """One session in this process (the child's side)."""
    root = Path(tempfile.mkdtemp(prefix="p9-truth-"))
    try:
        shutil.copy(fixture_dir / "root" / "config.json", root / "config.json")
        sys.path.insert(0, str(PY))
        import p9_boot

        p9_boot.environment(str(root))
        t0 = time.perf_counter()
        from plexora import paths

        paths.reset()
        p9_boot.licence(str(root / ".biocognia"))
        import p9_run

        p9_run.import_check()
        imported = time.perf_counter() - t0
        info = json.loads((fixture_dir / "info.json").read_text(encoding="utf-8"))
        if prototype == "metacache":
            p9_boot.prototype_metadata_cache()
        dump = p9_run.session(str(root), info)
        dump["summary"]["times"]["import"] = imported
        out.write_text(json.dumps(dump), encoding="utf-8")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--out", required=True)
    parser.add_argument("--prototype", choices=("metacache",), default=None,
                        help="a product change prototyped on both sides (page/py/p9_boot.py)")
    parser.add_argument("--one", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    fixture_dir = CACHE / f"p9-{args.fixture}"
    if args.one:
        return one(fixture_dir, Path(args.one), args.prototype)
    sys.path.insert(0, str(HERE))
    from compare_p9 import compare

    dumps = []
    with tempfile.TemporaryDirectory(prefix="p9-truth-runs-") as scratch:
        for i in range(args.runs):
            target = Path(scratch) / f"run{i}.json"
            run = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--fixture",
                                  args.fixture, "--out", args.out, "--one", str(target),
                                  *(["--prototype", args.prototype] if args.prototype else [])],
                                 capture_output=True, text=True)
            if run.returncode != 0:
                raise SystemExit(f"run {i} failed:\n{run.stderr[-4000:]}")
            dumps.append(json.loads(target.read_text(encoding="utf-8")))
            if os.environ.get("P9_KEEP"):
                shutil.copy(target, Path(args.out).with_name(f"run{i}.json"))
    times = [d["summary"]["times"] for d in dumps]
    median = {k: statistics.median(t[k] for t in times) for k in times[0]}
    record = {
        "fixture": args.fixture,
        "prototype": args.prototype,
        "key": json.loads((fixture_dir / "key.json").read_text(encoding="utf-8")),
        "runs": [d["summary"] for d in dumps],
        "median": median,
        "self": [compare(dumps[0], d) for d in dumps[1:]],
        "dump": dumps[0],
    }
    Path(args.out).write_text(json.dumps(record), encoding="utf-8")
    print(json.dumps({"median": median, "packets": dumps[0]["summary"]["packets"],
                      "units": dumps[0]["units"],
                      "self": [s["verdict"] for s in record["self"]]}, indent=1))


if __name__ == "__main__":
    main()
