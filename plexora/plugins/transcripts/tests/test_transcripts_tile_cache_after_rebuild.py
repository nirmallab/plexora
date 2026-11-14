"""A browser that drew a transcript layer draws the REBUILT layer afterwards.

A molecule tile goes out with `Cache-Control: private, max-age=31536000`, so
a browser that already holds one answers the next request for the same url
from its own cache, without asking the server -- the ETag is never compared
while that max-age is fresh. Rebuilding the layer from the same vendor file
(the panel's Build button after the layer's transform, its `min_qv` or its
gene panel changed; a sample deleted and imported again under the same name;
an upgrade that changes how tiles are written) keeps the source file's mtime,
the cache version and every url the viewer asks for. Unless the url itself
names the build, that browser goes on drawing the old molecules: at the old
positions, and after a vocabulary change under another gene's name.

The test plays the browser: it asks for tile 0_0 the way the client does (the
url comes from the real TranscriptsApi / TranscriptLayer code, run in node
by tests/js/transcript_tile_url_probe.mjs), keeps the answer as an HTTP cache
would, rebuilds the layer, and asks again.
"""

import gzip
import json
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import tifffile

import plexora
from plexora.server.models import transcript_tiles as tt
from plexora.server.models.project import ImageSpec, LayerSpec, Project

from tests.helpers import use_data_root


#: The plugin's routes need `transcriptomics:xenium`; on Free they 403.
pytestmark = pytest.mark.pro

REPO_ROOT = Path(__file__).resolve().parents[4]
PROBE = REPO_ROOT / "tests" / "js" / "transcript_tile_url_probe.mjs"

GENES = ["EPCAM", "CD3E", "PTPRC"]


def _build(source, *, genes, gene_index, x, y):
    tt.build("xen", "tx", genes=genes, gene_index=gene_index, x=x, y=y,
             expected=tt.expected_manifest(source, width=256, height=256,
                                           tile_size=128, layer_id="tx"))


def _decode(raw):
    return np.frombuffer(gzip.decompress(raw), dtype=tt.POINT_DTYPE)


def _drawn(records, genes):
    """What the viewer would put on screen: (gene name, x, y) per molecule."""
    return sorted((genes[int(r["gene"])], float(r["x"]), float(r["y"]))
                  for r in records)


def _fresh_for(response):
    match = re.search(r"max-age=(\d+)", response.headers.get("Cache-Control", ""))
    no_store = "no-store" in response.headers.get("Cache-Control", "") \
        or "no-cache" in response.headers.get("Cache-Control", "")
    return 0 if (match is None or no_store) else int(match.group(1))


def _client_urls(tmp_path, manifests):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    spec = tmp_path / "manifests.json"
    spec.write_text(json.dumps({"manifests": manifests}), encoding="utf-8")
    proc = subprocess.run([node, str(PROBE), str(spec)], capture_output=True,
                          text=True, cwd=REPO_ROOT, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    line = next(l for l in proc.stdout.splitlines() if l.startswith("URLS "))
    urls = json.loads(line[len("URLS "):])
    assert all(urls), f"the layer did not ask for tile 0_0: {urls}"
    return urls


@pytest.mark.parametrize("change", ["moved", "vocabulary_reordered"])
def test_a_rebuilt_layer_is_not_drawn_from_tiles_the_browser_kept(
        tmp_path, monkeypatch, change):
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    image = tmp_path / "morphology.ome.tif"
    tifffile.imwrite(image, np.zeros((1, 256, 256), dtype=np.uint16))
    Project(
        name="xen",
        image=ImageSpec(src=str(image), width=256, height=256, max_level=1,
                        tile_width=256, tile_height=256,
                        channels=({"name": "c0", "fullname": "DAPI", "src": "/t/0"},)),
    ).with_layer(LayerSpec(id="tx", kind="points", label="Transcripts")).save()

    # ONE vendor file, never touched: a rebuild from it keeps its mtime.
    source = tmp_path / "transcripts.parquet"
    source.write_bytes(b"stand-in; only its size and mtime are read")

    rng = np.random.default_rng(7)
    n = 400
    gene_index = rng.integers(0, len(GENES), size=n).astype(np.uint16)
    x = rng.uniform(0, 100, size=n).astype(np.float32)
    y = rng.uniform(0, 100, size=n).astype(np.float32)
    _build(source, genes=GENES, gene_index=gene_index, x=x, y=y)

    client = plexora.app.test_client()
    browser_cache = {}

    def browser_get(url):
        """A private HTTP cache: a fresh entry is answered without a request."""
        if url in browser_cache and browser_cache[url][1] > 0:
            return browser_cache[url][0]
        response = client.get(url)
        assert response.status_code == 200
        browser_cache[url] = (response.get_data(), _fresh_for(response))
        return response.get_data()

    first = client.get("/plugins/transcripts/manifest?datasource=xen&layer=tx").get_json()

    if change == "moved":
        # The layer's registration was corrected: every molecule shifts.
        genes2, gene_index2 = GENES, gene_index
        x2, y2 = x + 20.0, y + 15.0
    else:
        # Rebuilt from the run folder, whose gene_panel.json lists the genes
        # in another order: the same molecules, every index renumbered.
        genes2 = list(reversed(GENES))
        gene_index2 = (len(GENES) - 1 - gene_index).astype(np.uint16)
        x2, y2 = x, y

    # The first page load draws tile 0_0 (the url the real client builds).
    url_before = _client_urls(tmp_path, [first])[0]
    browser_get(url_before)

    _build(source, genes=genes2, gene_index=gene_index2,
           x=x2.astype(np.float32), y=y2.astype(np.float32))
    second = client.get("/plugins/transcripts/manifest?datasource=xen&layer=tx").get_json()
    assert second["status"] == "ready"

    # The next page load, after the rebuild, asks for tile 0_0 again.
    url_after = _client_urls(tmp_path, [second])[0]
    shown = _drawn(_decode(browser_get(url_after)), second["genes"])
    truth = _drawn(_decode(client.get(url_after).get_data()), second["genes"])

    assert shown == truth, (
        "the browser drew tile 0_0 from the bytes it kept before the rebuild: "
        f"the client asked for {url_after!r} both times "
        f"(before: {url_before!r}) and the first answer was cacheable for "
        f"{browser_cache[url_before][1]} s, so the rebuilt tile was never "
        f"requested. First molecule shown {shown[0]}, rebuilt {truth[0]}.")
