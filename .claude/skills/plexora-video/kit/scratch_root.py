"""Build a scratch PLEXORA_DATA_PATH for filming, so nothing touches the real install.

    python scratch_root.py <dst> [project ...] [--keep-db]

Each named project's config entry is copied verbatim (its image paths are
absolute, so the pyramids are read in place) with its small derived files
(ball tree, thumbnail, centroids). Without --keep-db the project .db is left
out: the video starts with no gates, no saved channels, no ROIs. With no
projects the root is empty -- right for "add an image" tutorials.

The real root is wherever Plexora resolves it (PLEXORA_REAL_DATA_PATH, else
the platform default).
"""
import json
import os
import shutil
import sys
from pathlib import Path

from platformdirs import user_data_dir

args = [a for a in sys.argv[1:] if not a.startswith("--")]
keep_db = "--keep-db" in sys.argv
if not args:
    sys.exit(__doc__)
real = Path(os.environ.get("PLEXORA_REAL_DATA_PATH") or user_data_dir("plexora", appauthor=False))
dst = Path(args[0]).resolve()
dst.mkdir(parents=True, exist_ok=True)
cfg_real = json.loads((real / "config.json").read_text()) if (real / "config.json").exists() else {}
cfg = {}
for name in args[1:]:
    if name not in cfg_real:
        sys.exit(f"{name}: not in {real / 'config.json'}")
    cfg[name] = cfg_real[name]
    src, out = real / name, dst / name
    out.mkdir(exist_ok=True)
    for f in ("ball_tree.pickle", ".thumbnail-v2.webp") + ((f"{name}.db",) if keep_db else ()):
        if (src / f).exists() and not (out / f).exists():
            shutil.copy2(src / f, out / f)
    if (src / "centroids_v1").exists() and not (out / "centroids_v1").exists():
        shutil.copytree(src / "centroids_v1", out / "centroids_v1")
(dst / "config.json").write_text(json.dumps(cfg, indent=1))
print("scratch root:", dst, "projects:", list(cfg) or "none")
