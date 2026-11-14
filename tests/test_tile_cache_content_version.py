"""A tile the browser kept must never stand in for different pixels.

Channel and label tiles go out with `Cache-Control: private, max-age=31536000`
(server/routes/data_routes.py, generate_png), so for a year the browser -- and
the desktop app's WebView2 cache, which outlives the app -- draws a tile it has
seen at an address without asking the server again. The ETag that carries
`load_generation` is only ever read when the browser revalidates, which a fresh
year-long entry never does.

That is safe only if the address changes whenever the pixels behind it do. The
address is `/generated/data/<project>/<key>/<level>/<x>_<y>.png`, where `key`
is the image file's stem plus the channel index, or the mask file's stem; the
client adds a version only to label tiles, and only a node mask ever moves it
(viewerManager.reloadLabelLayer). So re-registering a project with a new image
of the same file name, or attaching a re-run segmentation whose file keeps its
name (MCMICRO writes `cell.ome.tif` on every run), leaves every address where
it was -- and the browser keeps drawing the previous pixels.

The `_Browser` below is the HTTP cache the viewer's page has: a response whose
`Cache-Control` allows it is reused for its max-age with no request; otherwise
the browser asks again, with the ETag it holds. The tests assert what the user
must see: the tile the server serves NOW, at the address the client builds from
the project's current `/config`.
"""

import re
import time

import numpy as np
import tifffile

import plexora
from plexora import datasource
from plexora.server.models import data_model
from tests.helpers import use_data_root

#: The client's label tile revision (services/tileSource.js LABEL_TILE_REVISION);
#: at boot `segmentationVersion` is unset for a local mask, so the label tile's
#: query is just this.
LABEL_TILE_REVISION = "3"

_DATA_MODEL_GLOBALS = (
    "source", "_loaded_source", "config", "channels", "seg", "zarray",
    "datasource", "ball_tree", "metadata",
)


def _fresh_seconds(response):
    """How long a browser may reuse `response` without asking (0: must ask)."""
    control = response.headers.get("Cache-Control", "")
    if "no-cache" in control or "no-store" in control:
        return 0
    found = re.search(r"max-age=(\d+)", control)
    return int(found.group(1)) if found else 0


class _Browser:
    """The page's HTTP cache, keyed by URL, as a browser keeps it."""

    def __init__(self, client):
        self.client = client
        self.entries = {}

    def get(self, url):
        cached = self.entries.get(url)
        if cached is not None and cached["fresh"] > 0:
            return cached["body"]
        headers = {}
        if cached is not None and cached["etag"]:
            headers["If-None-Match"] = cached["etag"]
        response = self.client.get(url, headers=headers)
        if response.status_code == 304:
            body = cached["body"]
        else:
            assert response.status_code == 200, (url, response.status_code)
            body = response.data
        self.entries[url] = {"body": body, "fresh": _fresh_seconds(response),
                             "etag": response.headers.get("ETag")}
        return body


def _own_data_model(monkeypatch):
    for name in _DATA_MODEL_GLOBALS:
        if hasattr(data_model, name):
            monkeypatch.setattr(data_model, name, None)
    monkeypatch.setattr(data_model, "_gmm_cache", {})
    monkeypatch.setattr(data_model, "_image_stats_cache", {})


def _write_image(path, seed):
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    tifffile.imwrite(path, rng.integers(1, 4000, size=(2, 512, 512), dtype=np.uint16))
    return path


def _write_mask(path, offset):
    """Square cells on a grid, shifted by `offset` pixels: a different mask."""
    path.parent.mkdir(parents=True, exist_ok=True)
    labels = np.zeros((512, 512), dtype=np.uint32)
    label = 0
    for row in range(8):
        for column in range(8):
            label += 1
            y0, x0 = row * 64 + 8 + offset, column * 64 + 8 + offset
            labels[y0:y0 + 40, x0:x0 + 40] = label
    tifffile.imwrite(path, labels)
    return path


def _imagedata(client, name):
    return client.get("/config").get_json()[name]["imageData"]


def _channel_tile_url(client, name, index=0):
    """The address the viewer fetches a channel's top-left tile at: the
    channel's `src` from /config, a level and a tile, no query (the default,
    non-HD quality of a planar image)."""
    entries = [e for e in _imagedata(client, name) if e["fullname"] != "Area"]
    return f"{entries[index]['src']}0/0_0.png"


def _label_tile_url(client, name):
    """The label layer's top-left tile, as labelTileSource + getTileUrl build it."""
    src = _imagedata(client, name)[0]["src"]
    return f"{src}0/0_0.png?v={LABEL_TILE_REVISION}"


def _wait_for_mask(name, deadline_s=120):
    for _ in range(int(deadline_s * 10)):
        record = data_model._segmentation_jobs.get(name)
        if record is None or record.get("status") != "pending":
            return record
        time.sleep(0.1)
    raise AssertionError(f"mask conversion for {name} never finished")


def test_a_reregistered_image_is_not_drawn_from_the_previous_images_cached_tiles(
        tmp_path, monkeypatch):
    """Re-registering a project with a new image of the same file name (a
    reprocessed `registration/<sample>.ome.tif`) must not leave the browser
    drawing the old image's tiles: the channel's tile address has to change,
    or the cached tile has to be revalidated."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    use_data_root(monkeypatch, data_dir)
    _own_data_model(monkeypatch)
    client = plexora.app.test_client()
    browser = _Browser(client)

    first = _write_image(tmp_path / "run1" / "slide.tif", seed=1)
    datasource.register_image_datasource(name="s3_reimage", image=first, data_dir=data_dir)
    data_model.load_datasource("s3_reimage", reload=True)
    before = browser.get(_channel_tile_url(client, "s3_reimage"))

    second = _write_image(tmp_path / "run2" / "slide.tif", seed=2)
    datasource.register_image_datasource(name="s3_reimage", image=second, data_dir=data_dir)
    data_model.load_datasource("s3_reimage", reload=True)
    url = _channel_tile_url(client, "s3_reimage")
    served_now = client.get(url).data

    assert served_now != before, "the server itself still serves the first image"
    assert browser.get(url) == served_now, (
        f"the browser draws the first image's cached tile at {url}: the address "
        "did not change and the cached response is fresh for "
        f"{browser.entries[url]['fresh']} s, so the new pixels are never asked for")


def test_a_reattached_segmentation_of_the_same_file_name_is_not_drawn_from_the_old_masks_tiles(
        tmp_path, monkeypatch):
    """Attaching a re-run segmentation whose file keeps its name (MCMICRO's
    `cell.ome.tif`) on the edit page must not leave the browser drawing the
    previous mask's label tiles -- outlines of cells that no longer exist over
    the new table."""
    from plexora.server.routes.import_routes import attach_segmentation

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    use_data_root(monkeypatch, data_dir)
    _own_data_model(monkeypatch)
    client = plexora.app.test_client()
    browser = _Browser(client)

    image = _write_image(tmp_path / "slide.tif", seed=3)
    datasource.register_image_datasource(name="s3_remask", image=image, data_dir=data_dir)

    attach_segmentation("s3_remask", str(_write_mask(tmp_path / "run1" / "cell.tif", 0)))
    record = _wait_for_mask("s3_remask")
    assert record is None or record.get("status") != "error", record
    data_model.load_datasource("s3_remask", reload=True)
    before = browser.get(_label_tile_url(client, "s3_remask"))

    attach_segmentation("s3_remask", str(_write_mask(tmp_path / "run2" / "cell.tif", 20)))
    record = _wait_for_mask("s3_remask")
    assert record is None or record.get("status") != "error", record
    data_model.load_datasource("s3_remask", reload=True)
    url = _label_tile_url(client, "s3_remask")
    served_now = client.get(url).data

    assert served_now != before, "the server itself still serves the first mask"
    assert browser.get(url) == served_now, (
        f"the browser draws the previous mask's cached label tile at {url}: the "
        "address did not change and the cached response is fresh for "
        f"{browser.entries[url]['fresh']} s, so the new mask is never asked for")
