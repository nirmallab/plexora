"""Several tiles in one request (server/tile_batch.py, /generated/batch, /node/v1/batch).

What has to hold: a batch answers each tile with exactly what its own GET
answers -- status and bytes -- in the order asked; only tile routes are
dispatched, at most `MAX_TILES`; a node's batch is behind the node's token
like any of its routes, and each tile in it is checked against the token in
its own query; and the server says how long it took (`Server-Timing`), which
is how the browser tells a slow link from a slow server.
"""

import json
import struct

import numpy as np
import pytest
import tifffile

import plexora
from plexora.datasource import register_image_datasource
from plexora.server import tile_batch


def _split(body):
    (length,) = struct.unpack("<I", body[:4])
    header = json.loads(body[4:4 + length])
    out, at = [], 4 + length
    for meta in header["tiles"]:
        out.append((meta, body[at:at + meta["bytes"]]))
        at += meta["bytes"]
    assert at == len(body)
    return out


def _image(tmp_path, name="batch"):
    rng = np.random.default_rng(3)
    data = rng.poisson(60, (2, 600, 700)).astype(np.uint16)
    data[:, 100:300, 100:300] += 3000
    path = tmp_path / f"{name}.ome.tif"
    tifffile.imwrite(path, data, photometric="minisblack", ome=True,
                     metadata={"axes": "CYX", "Channel": {"Name": ["DNA", "CD3"]}})
    register_image_datasource(name, path)
    return path


def test_a_batch_answers_each_tile_as_its_own_request_would(tmp_path):
    _image(tmp_path)
    client = plexora.app.test_client()
    # Channels by key; the last tile is off the image.
    paths = ["/generated/data/batch/batch_0/0/0_0.png",
             "/generated/data/batch/batch_1/0/0_0.png?q=hd",
             "/generated/data/batch/batch_0/0/9_9.png"]
    alone = [client.get(path) for path in paths]
    response = client.post("/generated/batch", data=json.dumps({"tiles": paths}),
                           content_type="text/plain")
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.headers["Server-Timing"].startswith("total;dur=")
    together = _split(response.data)
    assert [meta["status"] for meta, _ in together] == [r.status_code for r in alone]
    for (meta, body), single in zip(together, alone):
        if single.status_code == 200:
            assert body == single.data and meta["etag"] == single.headers.get("ETag")


def test_only_tiles_and_only_so_many(tmp_path):
    _image(tmp_path, "refuse")
    client = plexora.app.test_client()
    post = lambda tiles: client.post("/generated/batch", data=json.dumps({"tiles": tiles}))
    assert post(["/settings"]).status_code == 400
    assert post(["/generated/data/refuse/../../etc/0/0_0.png"]).status_code == 400
    assert post([]).status_code == 400
    many = ["/generated/data/refuse/DNA/0/0_0.png"] * (tile_batch.MAX_TILES + 1)
    assert post(many).status_code == 400
    assert client.post("/generated/batch", data="not json").status_code == 400


def test_a_nodes_batch_is_behind_its_token_and_so_is_each_tile(tmp_path):
    from plexora.server.node.app import create_node_app

    path = _image(tmp_path, "nodebatch")
    app = create_node_app([f"image:slide={path}"], token="sekrit", log=lambda *a, **k: None)
    client = app.test_client()
    tile = "/node/v1/image/slide/tile/slide_0/0/0_0.png?tw=1024&th=1024"
    alone = client.get(f"{tile}&t=sekrit")
    assert alone.status_code == 200, alone.get_data(as_text=True)
    body = json.dumps({"tiles": [f"{tile}&t=sekrit", tile]})
    assert client.post("/node/v1/batch", data=body).status_code == 403   # no token on the batch
    response = client.post("/node/v1/batch?t=sekrit", data=body)
    assert response.status_code == 200
    (first, first_body), (second, _) = _split(response.data)
    assert first["status"] == 200 and first_body == alone.data
    assert second["status"] == 403, "a tile without the token is refused inside the batch too"
    assert client.post("/node/v1/batch?t=sekrit",
                       data=json.dumps({"tiles": ["/node/v1/health?t=sekrit"]})).status_code == 400


@pytest.mark.parametrize("path", ["/generated/layer/ds/lay/ch/2/3_4.png?view=z3",
                                  "/generated/data/ds/ch/0/12_7.png?q=hd&view=z4-9max&w=0,1"])
def test_the_tile_routes_it_dispatches(path):
    assert tile_batch.PRIMARY.match(path)
