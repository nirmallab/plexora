"""Hold the website's kernels (tests/browser/kernels.mjs) to this machine's.

    python kernels/tools/compare_browser.py BROWSER.json [--size test]

Builds the same inputs here (tests/kernel_cases.py, same seed), runs them as
this interpreter has the kernels (the native port with plexora-kernels
installed; numba with PLEXORA_KERNELS=0), hashes inputs and outputs the way the
driver does, and prints, per case, whether the browser's inputs were the same
and whether its port's and its Python's outputs equal the desktop's. One JSON
line; `mismatch` lists every case whose inputs agreed and outputs did not.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("browser")
    parser.add_argument("--size", default=None)
    args = parser.parse_args(argv)
    browser = json.loads(Path(args.browser).read_text(encoding="utf-8").strip().splitlines()[-1])
    from tests import kernel_cases
    from tests.kernel_cases import digest, outputs

    cases = kernel_cases.build(args.size or browser["size"])
    cases = {k: v for k, v in cases.items() if k in browser["cases"]}
    inputs = {name: digest([a for a in a_ if isinstance(a, (np.ndarray, bytes))])
              for name, a_ in cases.items()}
    here = kernel_cases.run(cases, 1)
    report, mismatch = {}, []
    for name, entry in here.items():
        mine = digest(outputs(entry))
        theirs = browser["cases"][name]
        row = {"inputs_same": inputs[name] == browser["inputs"].get(name), "desktop": entry["impl"],
               "port_same": theirs["compiled"]["digest"] == mine}
        if "python" in theirs:
            row["python_same"] = theirs["python"]["digest"] == mine
        report[name] = row
        if row["inputs_same"] and not row["port_same"]:
            mismatch.append(name)
    print(json.dumps({"cases": report, "mismatch": mismatch,
                      "inputs_differ": [n for n, r in report.items() if not r["inputs_same"]]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
