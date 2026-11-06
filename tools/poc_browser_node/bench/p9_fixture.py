"""P9's fixture: tests/autogate_fixtures.py's synthetic gating project, made
once per size into the bench cache.

    python p9_fixture.py --fixture P9S|P9M [--force]

Writes ``$PLEXORA_BENCH_HOME/browser_node/cache/p9-<fixture>/``:

- ``root/`` -- the data root make_gating_project fills: ``_gsynth_files/``
  (``image.ome.tif``, a 3-level 128-px-tiled uint16 OME-TIFF with SubIFDs;
  ``mask.tif``, its label pyramid; ``cells.csv``) and ``config.json`` naming
  them by their paths here (CPython's reference copies it into a fresh root
  per run);
- ``config.browser.json`` -- the same entry with each path under
  ``/data/_gsynth_files/``, where the page mounts the user's picked files;
- ``info.json`` -- what the Oracle answers from: the cells (with their
  painted marker values) and the truth, as make_gating_project returned them;
- ``key.json`` -- what made it, so a changed size or seed remakes it.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent.parent
CACHE = Path(os.environ.get("PLEXORA_BENCH_HOME") or "C:/plexora-bench") / "browser_node" / "cache"
#: tests/test_gating_session.py's sizes: S is make_gating_project's default
#: (the suite's most common call), M the oracle test's own (grid 32, 1280 px,
#: five markers).
FIXTURES = {
    "P9S": {"grid": 16, "size": 768, "markers": ["CD3", "CD8", "CD20"], "seed": 0},
    "P9M": {"grid": 32, "size": 1280, "markers": ["CD3", "CD8", "CD20", "CD4", "FOXP3"], "seed": 0},
}
BROWSER_FOLDER = "/data/_gsynth_files"


def _relocate(value, folder):
    if isinstance(value, dict):
        return {k: _relocate(v, folder) for k, v in value.items()}
    if isinstance(value, list):
        return [_relocate(v, folder) for v in value]
    if isinstance(value, str):
        for form in (str(folder), str(folder).replace("\\", "/")):
            if value.startswith(form):
                return BROWSER_FOLDER + value[len(form):].replace("\\", "/")
    return value


def make(name, force=False):
    key = FIXTURES[name]
    out = CACHE / f"p9-{name}"
    if (out / "key.json").exists() and not force:
        if json.loads((out / "key.json").read_text(encoding="utf-8")) == key:
            return out
    if out.exists():
        shutil.rmtree(out)
    root = out / "root"
    root.mkdir(parents=True)
    sys.path.insert(0, str(REPO))
    from tests.autogate_fixtures import make_gating_project

    info = make_gating_project(root, grid=key["grid"], size=key["size"],
                               markers=tuple(key["markers"]), seed=key["seed"])
    folder = root / f"_{info['name']}_files"
    config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    (out / "config.browser.json").write_text(json.dumps(_relocate(config, folder)),
                                             encoding="utf-8")
    cells = [{k: (v.item() if hasattr(v, "item") else v) for k, v in c.items()}
             for c in info["cells"]]
    (out / "info.json").write_text(json.dumps({
        "name": info["name"], "cells": cells,
        "truth": {m: [int(i) for i in ids] for m, ids in info["truth"].items()},
        "log_transformed": info["log_transformed"],
        "files": sorted(p.name for p in folder.iterdir()),
    }), encoding="utf-8")
    (out / "key.json").write_text(json.dumps(key), encoding="utf-8")
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--fixture", choices=sorted(FIXTURES), required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    out = make(args.fixture, args.force)
    files = out / "root" / "_gsynth_files"
    print(json.dumps({"dir": str(out), "files": {p.name: p.stat().st_size for p in files.iterdir()}}))


if __name__ == "__main__":
    main()
