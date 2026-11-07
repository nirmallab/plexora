"""Record which Plexora kernel sources this package's ports follow.

    python kernels/tools/fingerprints.py           # rewrite src/plexora_kernels/_fingerprints.py
    python kernels/tools/fingerprints.py --check   # exit 1 if a recorded fingerprint is stale

Run from the Plexora checkout the ports were updated against, after the port
of every changed kernel has been changed to match (and the agreement tests,
tests/test_kernels_package.py, pass). The fingerprint is Plexora's own
(`plexora.server.utils.jit.kernel_fingerprints`: a kernel's tokens without
comments, layout or docstring), so a kernel whose code changes is no longer
taken from an older build of this package -- jit.py falls back to numba for
its whole module until a release records the new one.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
TARGET = HERE.parent / "src" / "plexora_kernels" / "_fingerprints.py"

#: Every Plexora module with ported kernels, and its source file.
MODULES = {
    "plexora.server.utils.label_kernels": "plexora/server/utils/label_kernels.py",
    "plexora.server.volume.measure": "plexora/server/volume/measure.py",
    "plexora.server.utils.cell_features": "plexora/server/utils/cell_features.py",
    "plexora.plugins.qc.server.segqc.kernels": "plexora/plugins/qc/server/segqc/kernels.py",
    "plexora.plugins.gating.server.autogate.kernels": "plexora/plugins/gating/server/autogate/kernels.py",
    "plexora.server.surface.nets": "plexora/server/surface/nets.py",
    "plexora.server.surface.mesh": "plexora/server/surface/mesh.py",
}


def compute() -> dict:
    sys.path.insert(0, str(REPO))
    import importlib.util

    spec = importlib.util.spec_from_file_location("_plexora_jit", REPO / "plexora/server/utils/jit.py")
    jit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(jit)
    return {module: jit.kernel_fingerprints((REPO / path).read_text(encoding="utf-8"))
            for module, path in MODULES.items()}


def render(found: dict, commit: str) -> str:
    lines = ['"""Which Plexora kernel sources these ports follow (written by',
             'kernels/tools/fingerprints.py; do not edit by hand)."""', "",
             "#: The Plexora commit the fingerprints were read from.",
             f"PORTED_FROM = {commit!r}", "",
             "#: {module: {kernel: fingerprint}} (plexora.server.utils.jit.kernel_fingerprints).",
             "FINGERPRINTS = {"]
    for module in sorted(found):
        lines.append(f"    {module!r}: {{")
        for name in sorted(found[module]):
            lines.append(f"        {name!r}: {found[module][name]!r},")
        lines.append("    },")
    lines.append("}")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    found = compute()
    if args.check:
        namespace: dict = {}
        exec(TARGET.read_text(encoding="utf-8"), namespace)
        recorded = namespace["FINGERPRINTS"]
        stale = {m: sorted(n for n in set(found[m]) | set(recorded.get(m, {}))
                           if found[m].get(n) != recorded.get(m, {}).get(n))
                 for m in found}
        stale = {m: names for m, names in stale.items() if names}
        if stale:
            print(f"stale: {stale}")
            return 1
        print("fingerprints current")
        return 0
    try:
        commit = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short=12", "HEAD"],
                                capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = "unknown"
    TARGET.write_text(render(found, commit), encoding="utf-8", newline="\n")
    print(f"wrote {TARGET} ({sum(len(v) for v in found.values())} kernels, from {commit})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
