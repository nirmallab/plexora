"""What compiling the kernels costs a desktop process before its first request.

    python kernels/tools/bench_startup.py [--runs 3]

Each run is a fresh interpreter that imports Plexora's kernel modules and calls
`jit.prime()` (what `data_model.prime_hot_code` and the MCP server do at
start): with plexora-kernels (nothing to compile), with numba and a warm cache
(compiled code loaded from disk), and with numba and an empty cache (an LLVM
compile per kernel, a new install's first start). One JSON line: seconds per
mode, and whether numba was imported.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

PROBE = """
import json, sys, time
t = time.perf_counter()
from plexora.server.utils import jit
primed = jit.prime()
took = time.perf_counter() - t
print(json.dumps({"s": round(took, 3), "primed": len(primed), "numba": "numba" in sys.modules,
                  "compiled": jit.kernels_module() is not None}))
"""


def run(env) -> dict:
    done = subprocess.run([sys.executable, "-c", PROBE], cwd=REPO, env={**os.environ, **env},
                          capture_output=True, text=True, timeout=600, check=True)
    return json.loads(done.stdout.strip().splitlines()[-1])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args(argv)
    out = {"compiled": [], "numba_warm": [], "numba_cold": []}
    with tempfile.TemporaryDirectory(prefix="pxk-numba-warm-") as warm:
        run({"PLEXORA_KERNELS": "0", "NUMBA_CACHE_DIR": warm})  # fill the cache
        for _ in range(args.runs):
            out["compiled"].append(run({}))
            out["numba_warm"].append(run({"PLEXORA_KERNELS": "0", "NUMBA_CACHE_DIR": warm}))
            with tempfile.TemporaryDirectory(prefix="pxk-numba-cold-") as cold:
                out["numba_cold"].append(run({"PLEXORA_KERNELS": "0", "NUMBA_CACHE_DIR": cold}))
    summary = {mode: {"best_s": min(r["s"] for r in rows), "runs_s": [r["s"] for r in rows],
                      "numba_imported": rows[0]["numba"], "primed": rows[0]["primed"]}
               for mode, rows in out.items()}
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
