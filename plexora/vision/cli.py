"""`plexora ai segment install | status | remove`: magic select's weights.

In the viewer the setup is automatic; this is for a headless machine (a data
node, a cluster login node, a container build) and for checking what is
installed and which device it runs on.
"""

from __future__ import annotations

import sys


def _bar(done, total, name, *, width=30):
    fraction = done / total if total else 0.0
    filled = int(round(width * fraction))
    sys.stdout.write(f"\r  [{'#' * filled}{'.' * (width - filled)}] "
                     f"{fraction * 100:5.1f}%  {done / 1e6:6.1f}/{total / 1e6:.1f} MB"
                     f"  {name or ''}   ")
    sys.stdout.flush()


def segment_command(args) -> int:
    import os

    from plexora import paths
    from plexora.vision import sam, sam_backend, sam_weights

    if getattr(args, "model_dir", None):
        os.environ[paths.ENV_SEGMENT_MODEL_DIR] = args.model_dir
    action = getattr(args, "segment_command", None) or "status"

    if action == "status":
        status = sam.status()
        print(f"Magic select: {status.state}")
        print(f"  weights:  {status.weights['dir']}  ({status.weights['source']})")
        for name, row in status.weights["files"].items():
            mark = "ok" if row["ok"] else ("wrong size" if row["present"] else "missing")
            print(f"    {name:28s} {mark}")
        runtime = "installed" if status.runtime["installed"] else "missing"
        print(f"  runtime:  onnxruntime {runtime}")
        if status.runtime["installed"]:
            try:
                ort = sam_backend.import_runtime()
                print(f"            {ort.__version__}; providers: "
                      f"{', '.join(ort.get_available_providers())}")
            except sam_backend.SamRuntimeMissing as exc:
                print(f"            {exc}")
        print(f"  model:    {status.model['name']} v{status.model['version']} "
              f"({status.model['license']})")
        if getattr(args, "check_device", False) and status.state == "ready":
            backend = sam.session()
            print(f"  device:   encoder on {backend.device['encoder']}, "
                  f"decoder on {backend.device['decoder']}")
            if backend.device.get("fallback"):
                print(f"            fell back: {backend.device['fallback']}")
        if status.hint and status.state != "ready":
            print(f"  next:     {status.hint}")
        return 0 if status.state == "ready" else 1

    if action == "install":
        if not sam.enabled():
            print(f"{sam.ENV_SWITCH}=0 switches magic select off.", file=sys.stderr)
            return 2
        print(f"Installing magic select's model into {sam_weights.model_dir()} "
              f"(~{sam_weights.TOTAL_BYTES / 1e6:.0f} MB, once).")
        try:
            sam.ensure_installed(progress=_bar, force=getattr(args, "force", False))
        except Exception as exc:
            print(f"\nFailed: {exc}", file=sys.stderr)
            return 1
        print("\nVerified and installed.")
        return 0

    if action == "remove":
        gone = sam.remove()
        print("Removed: " + (", ".join(gone) if gone else "nothing was installed"))
        return 0

    print("Usage: plexora ai segment install|status|remove", file=sys.stderr)
    return 2


def add_parser(subs) -> None:
    segment = subs.add_parser(
        "segment", help="Magic select's model: install it on a headless machine, "
                        "check it, or remove it.")
    segment_subs = segment.add_subparsers(dest="segment_command")
    for name, text in (("install", "Download and verify the model (once)."),
                       ("status", "What is installed and which device it runs on."),
                       ("remove", "Delete the downloaded model.")):
        sub = segment_subs.add_parser(name, help=text)
        sub.add_argument("--model-dir", default=None,
                         help="Use this directory instead of the configured one.")
        if name == "install":
            sub.add_argument("--force", action="store_true",
                             help="Download again even when the files verify.")
        if name == "status":
            sub.add_argument("--device", dest="check_device", action="store_true",
                             help="Also load the model and report the device it runs on.")
