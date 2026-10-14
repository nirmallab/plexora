"""The 3-D view's level payloads are built once and named (server/volume/serve.py).

What the browser uploads as a 3-D texture used to be re-read, re-quantized and
re-sent `no-store` for every channel switched back on, every return to the 3-D
view and every reload. Pinned here: the bytes are what they always were; a
request that already has them is answered 304 before a voxel is read; a request
naming its window may keep them for a year; two requests for one payload are
one read; and the route says where its time went.
"""

import threading

import numpy as np
import pytest

import plexora
from plexora.datasource import register_image_datasource
from plexora.server.models import data_model, views
from plexora.server.volume import serve
from tests import volume_fixtures as vf


@pytest.fixture()
def client():
    serve.clear()
    return plexora.app.test_client()


def _volume(tmp_path, name="vol", **kwargs):
    path, data = vf.write_ome_volume(tmp_path / f"{name}.ome.tif", axes="CZYX", channels=2,
                                     depth=5, levels=2, **kwargs)
    entry = register_image_datasource(name, path)
    data_model.load_datasource(name)
    return entry, data


def _count_reads(monkeypatch, datasource):
    source = views.current_source(datasource)
    calls = []
    original = source.read_block

    def counting(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(source, "read_block", counting)
    return calls


# -- the bytes ---------------------------------------------------------------------


@pytest.mark.parametrize("dtype", [np.uint8, np.uint16])
@pytest.mark.parametrize("voxel_format", ["u8", "f16"])
def test_the_lookup_table_gives_the_float_formula_byte_for_byte(dtype, voxel_format):
    rng = np.random.default_rng(1)
    top = np.iinfo(dtype).max
    block = rng.integers(0, top + 1, size=(3, 17, 23), dtype=dtype)
    qmin, qmax = 7.0, top * 0.61
    span = max(1.0, qmax - qmin)
    unit = np.clip((block.astype(np.float32) - qmin) / span, 0.0, 1.0)
    expected = unit.astype("<f2") if voxel_format == "f16" else np.rint(unit * 255).astype(np.uint8)
    got = serve.quantize(block, qmin, qmax, voxel_format)
    assert got.dtype == expected.dtype
    assert np.array_equal(got.view(np.uint8), expected.view(np.uint8))


def test_float_data_still_takes_the_formula():
    block = np.linspace(-5, 300, 60, dtype=np.float32).reshape(3, 4, 5)
    unit = np.clip((block - 0.0) / 255.0, 0, 1)
    assert np.array_equal(serve.quantize(block, 0.0, 255.0), np.rint(unit * 255).astype(np.uint8))


# -- names, 304s and how long a browser may keep them ---------------------------------


def test_a_level_carries_a_stable_tag_and_a_second_ask_is_not_read_again(tmp_path, client,
                                                                          monkeypatch):
    entry, _ = _volume(tmp_path)
    name = entry["imageData"][1]["fullname"]
    first = client.get(f"/volume/vol/level?channel={name}&level=0")
    tag = first.headers["ETag"]
    assert tag and first.headers["Cache-Control"] == "private, no-cache"
    assert client.get(f"/volume/vol/level?channel={name}&level=0").headers["ETag"] == tag
    other = client.get(f"/volume/vol/level?channel={name}&level=0&format=f16")
    assert other.headers["ETag"] != tag, "the format is part of the name"

    reads = _count_reads(monkeypatch, "vol")
    serve.clear()
    again = client.get(f"/volume/vol/level?channel={name}&level=0",
                       headers={"If-None-Match": tag})
    assert again.status_code == 304 and not again.data
    assert again.headers["ETag"] == tag and again.headers["X-Plexora-Level"] == "0"
    assert reads == [], "a revalidation must not read a single voxel"


def test_a_request_naming_its_window_gets_bytes_made_with_it_kept_for_a_year(tmp_path, client):
    entry, data = _volume(tmp_path)
    name = entry["imageData"][0]["fullname"]
    window = client.get(f"/volume/vol/level?channel={name}").headers["X-Plexora-Window"]
    named = client.get(f"/volume/vol/level?channel={name}&level=0&w={window}")
    assert named.headers["Cache-Control"] == "private, max-age=31536000, immutable"
    # Any window named is the window the bytes are made with: the address
    # fixes them (a provisional window's and the exact one's never collide).
    other = client.get(f"/volume/vol/level?channel={name}&level=0&w=0,1000")
    assert other.headers["Cache-Control"] == "private, max-age=31536000, immutable"
    assert other.headers["X-Plexora-Window"] == "0,1000"
    assert other.headers["ETag"] != named.headers["ETag"]
    voxels = np.frombuffer(other.data, np.uint8).reshape(data[0, 0].shape)
    expected = np.rint(np.clip(data[0, 0].astype(np.float32) / 1000.0, 0, 1) * 255).astype(np.uint8)
    assert np.array_equal(voxels, expected)


def test_the_route_says_where_its_time_went(tmp_path, client):
    entry, _ = _volume(tmp_path)
    name = entry["imageData"][0]["fullname"]
    timing = client.get(f"/volume/vol/level?channel={name}&level=0").headers["Server-Timing"]
    for phase in ("read;dur=", "quant;dur=", "total;dur="):
        assert phase in timing, timing


# -- one read per payload ------------------------------------------------------------


def test_two_asks_for_one_payload_are_one_read(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    reads = []

    def read():
        reads.append(1)
        started.set()
        release.wait(5)
        return np.arange(24, dtype=np.uint16).reshape(2, 3, 4)

    serve.clear()
    results = []
    first = threading.Thread(target=lambda: results.append(
        serve.level_payload(("k",), read, (0.0, 23.0), "u8")))
    first.start()
    assert started.wait(5)
    second = threading.Thread(target=lambda: results.append(
        serve.level_payload(("k",), read, (0.0, 23.0), "u8")))
    second.start()
    release.set()
    first.join(5)
    second.join(5)
    assert len(reads) == 1 and len(results) == 2 and results[0][0] == results[1][0]


def test_a_detail_is_a_range_of_planes_of_an_area(tmp_path, client):
    """The 3-D view's detail around where its camera looks: `box=` and `z=`."""
    entry, data = _volume(tmp_path)
    name = entry["imageData"][1]["fullname"]
    response = client.get(f"/volume/vol/level?channel={name}&level=0&box=8,16,40,32&z=1,3&w=0,4000")
    assert response.status_code == 200
    assert response.headers["X-Plexora-Z"] == "1,3"
    shape = tuple(int(v) for v in response.headers["X-Plexora-Shape"].split(","))
    x0, y0, x1, y1 = (int(v) for v in response.headers["X-Plexora-Box"].split(","))
    assert shape == (3, y1 - y0, x1 - x0)
    voxels = np.frombuffer(response.data, np.uint8).reshape(shape)
    unit = np.clip(data[0, 1, 1:4, y0:y1, x0:x1].astype(np.float32) / 4000.0, 0, 1)
    assert np.array_equal(voxels, np.rint(unit * 255).astype(np.uint8))
    whole = client.get(f"/volume/vol/level?channel={name}&level=0&box=8,16,40,32&w=0,4000")
    assert whole.headers["ETag"] != response.headers["ETag"]
