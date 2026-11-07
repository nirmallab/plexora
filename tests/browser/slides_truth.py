"""Whole-slide tiles as brightfield.py cuts them over native OpenSlide, for
tests/test_browser_slides.py: `brightfield.open_rgb` with the website's page
slide (`providers.browser.page_slide`) replaced by openslide-python's
`OpenSlide` -- the same levels, origins, compositing and dyadic resampling the
website runs, over native OpenSlide instead of openslide-js -- and each tile
as `data_model.read_tile` cuts it (`brightfield.rgb_region`). For an MRXS
that is exactly the desktop's own path; for NDPI and SVS the desktop reads
with tifffile instead (L2.md measures that difference).

Run by an interpreter that has openslide-python and openslide-bin (the
plexora env does not; `PLEXORA_OPENSLIDE_PYTHON` names one), with this
checkout's `plexora/server/utils/brightfield.py` loaded as it is: the package
roots are stood in for (plexora/__init__ builds the Flask app), as are the two
providers modules it asks whether a path is remote and what the page reads.

    <python with openslide> -I tests/browser/slides_truth.py SPEC.json OUT_DIR

SPEC: {name: {"path": slide, "tile": [w, h], "tiles": [{"level", "tx", "ty"}]}}.
Writes OUT_DIR/<name>/<level>_<tx>_<ty>.npy ((h, w, 3) uint8) and
OUT_DIR/truth.json ({name: {"levels": [[h, w]], "openslide": version}}).
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _stand_ins():
    for name, where in (("plexora", "plexora"), ("plexora.server", "plexora/server"),
                        ("plexora.server.utils", "plexora/server/utils"),
                        ("plexora.server.providers", "plexora/server/providers")):
        module = types.ModuleType(name)
        module.__path__ = [str(ROOT / where)]
        sys.modules[name] = module
    base = types.ModuleType("plexora.server.providers.base")
    base.is_remote_locator = lambda path: False
    sys.modules["plexora.server.providers.base"] = base
    browser = types.ModuleType("plexora.server.providers.browser")

    class PageSlideError(RuntimeError):
        pass

    browser.PageSlideError = PageSlideError
    sys.modules["plexora.server.providers.browser"] = browser
    return browser


def main():
    browser = _stand_ins()
    import numpy as np
    import openslide
    from plexora.server.utils import brightfield

    # What the page reads with openslide-js, read with native OpenSlide.
    browser.page_slide = lambda path: openslide.OpenSlide(str(path))
    browser.PageSlide = openslide.OpenSlide

    spec = json.loads(Path(sys.argv[1]).read_text())
    out = Path(sys.argv[2])
    record = {}
    for name, slide in spec.items():
        pyramid = brightfield.open_rgb(slide["path"])
        tw, th = slide["tile"]
        folder = out / name
        folder.mkdir(parents=True, exist_ok=True)
        for tile in slide["tiles"]:
            level, tx, ty = tile["level"], tile["tx"], tile["ty"]
            rgb = brightfield.rgb_region(pyramid[level], slice(ty * th, ty * th + th),
                                         slice(tx * tw, tx * tw + tw))
            np.save(folder / f"{level}_{tx}_{ty}.npy", np.ascontiguousarray(rgb))
        record[name] = {"levels": pyramid.level_shapes, "base_levels": pyramid.base_levels,
                        "openslide": openslide.__library_version__}
    (out / "truth.json").write_text(json.dumps(record), newline="\n")


if __name__ == "__main__":
    main()
