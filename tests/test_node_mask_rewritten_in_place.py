"""A cell mask rewritten at the same path while a data node keeps running.

Re-running segmentation into the same file is the normal way a mask changes:
mcmicro writes `cell.ome.tif` again, Cellpose overwrites its output. A node
that converted the first version into a label pyramid has to notice, when the
mask is shared again, that the file is not the one it converted -- otherwise it
keeps serving the old pyramid, and the viewer draws (and the node measures)
cells the new quantification table does not describe.
"""

from __future__ import annotations

import io
import os
import time

import numpy as np
import tifffile

TOKEN_HEADER = {"X-Plexora-Node-Token": "x"}


def _quiet(*_args, **_kwargs):
    """A node builder's log, silenced -- these tests read responses."""


def _write_mask(path, label):
    """One untiled plane, as a segmentation pipeline writes it, with a single
    cell numbered `label` in the top-left tile."""
    labels = np.zeros((512, 512), dtype=np.uint32)
    labels[10:60, 10:60] = label
    tifffile.imwrite(path, labels)


def _label_ids(png_bytes):
    """The label ids a mask tile carries, decoded the way the shader reads them."""
    from PIL import Image

    rgba = np.asarray(Image.open(io.BytesIO(png_bytes)).convert("RGBA"))
    return (rgba[..., 0].astype(np.uint32)
            | (rgba[..., 1].astype(np.uint32) << 8)
            | (rgba[..., 2].astype(np.uint32) << 16))


def _settled(client, resource_id, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.get(f"/node/v1/resources/{resource_id}/status",
                           headers=TOKEN_HEADER).get_json()["resource"]
        if state["state"] != "preparing":
            return state
        time.sleep(0.05)
    return state


def _served_ids(client):
    tile = client.get("/node/v1/seg/mask/tile/0/0_0", headers=TOKEN_HEADER)
    assert tile.status_code == 200, tile.get_data(as_text=True)
    return set(np.unique(_label_ids(tile.data)).tolist())


def test_a_mask_rewritten_in_place_is_served_anew_when_shared_again(tmp_path):
    """Sharing a mask the node already converted, after the file was rewritten
    with different cells, serves the new cells and bumps the generation -- not
    the pyramid converted from the old file."""
    from plexora.server.node.app import create_node_app

    mask = tmp_path / "cell.tif"
    _write_mask(mask, 7)
    past = time.time() - 600
    os.utime(mask, (past, past))

    app = create_node_app([], token="x", dynamic=True, log=_quiet)
    client = app.test_client()
    payload = {"kind": "segmentation", "id": "mask", "path": str(mask)}

    client.post("/node/v1/resources", json=payload, headers=TOKEN_HEADER)
    first = _settled(client, "mask")
    assert first["state"] == "ready", first.get("error")
    assert 7 in _served_ids(client)

    # The user re-runs segmentation into the same file: same shape, so the
    # same size on disk, different cell ids, newer modification time.
    _write_mask(mask, 9)
    now = time.time()
    os.utime(mask, (now, now))

    client.post("/node/v1/resources", json=payload, headers=TOKEN_HEADER)
    # What opening the project asks for besides (data_routes._node_mask_report).
    client.post("/node/v1/resources/mask/prepare", headers=TOKEN_HEADER)
    second = _settled(client, "mask")
    assert second["state"] == "ready", second.get("error")

    served = _served_ids(client)
    assert 9 in served and 7 not in served, (
        f"the node still serves the pyramid converted from the old mask: "
        f"labels {sorted(served)}")
    assert second["generation"] > first["generation"]
