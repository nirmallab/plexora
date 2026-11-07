"""A desktop project, opened on the website: the page asks once for the folder
that holds its image, finds the file by its identity and draws it
(tests/browser/find_files.mjs, in Chrome).

The project is made here the way the desktop app makes one in the library it
shares with the website: registered there, so its file's identity is recorded
(server/models/sources.py).
"""

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import tifffile

from plexora import paths

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "browser" / "find_files.mjs"
SITE = ROOT / "plexora" / "client" / "dist" / "static" / "index.html"
PLAYWRIGHT = ROOT / "tools" / "poc_browser_node" / "node_modules" / "playwright-core"

pytestmark = pytest.mark.skipif(
    not shutil.which("node") or not SITE.is_file() or not PLAYWRIGHT.is_dir(),
    reason="needs node, a static build (python -m plexora.client.static_build) and playwright-core")


def test_the_website_finds_a_desktop_projects_image(tmp_path, monkeypatch):
    library = tmp_path / "Documents" / "Plexora" / "projects"
    library.mkdir(parents=True)
    image = tmp_path / "images" / "slide.ome.tif"
    image.parent.mkdir()
    data = (np.arange(2 * 512 * 640) % 3000).astype(np.uint16).reshape(2, 512, 640)
    with tifffile.TiffWriter(image, bigtiff=True, ome=True) as writer:
        writer.write(data, tile=(256, 256), subifds=1, photometric="minisblack", metadata={"axes": "CYX"})
        writer.write(data[:, ::2, ::2], tile=(256, 256), subfiletype=1, photometric="minisblack")
    monkeypatch.setenv("PLEXORA_LIBRARY_PATH", str(library))
    paths.reset()
    import plexora

    plexora.register_image_datasource("desktop_slide", str(image), data_dir=str(library))
    entry = json.loads((library / "config.json").read_text(encoding="utf-8"))["desktop_slide"]
    assert entry["sources"][str(image)]["size"] == image.stat().st_size
    spec = tmp_path / "entry.json"
    spec.write_text(json.dumps({"name": "desktop_slide", "entry": entry}), encoding="utf-8")
    done = subprocess.run(["node", str(DRIVER), "--entry", str(spec), "--image", str(image)],
                          capture_output=True, text=True, encoding="utf-8", timeout=600)
    record = json.loads(done.stdout[done.stdout.index("{"):])
    assert record["ok"], record
