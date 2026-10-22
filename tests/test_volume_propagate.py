"""Magic select through a z-stack (plexora/vision/propagate.py).

An outline on one plane is carried up and down the stack until the object
ends. The models are stood in for -- `FakeSamBackend` for MobileSAM, a
recorder of known masks for SAM 2's video predictor -- so what is pinned is the
part this code owns: the guards that decide where an object ends, that SAM 2
is stopped at the first plane past it (rather than run to the stack's ends),
which engine answers, the route's refusals, and the region it becomes.
"""

import json

import numpy as np
import pytest

from plexora.datasource import register_image_datasource
from plexora.vision import propagate, sam, sam_backend, sam_video
from plexora.vision import segment as segmenter
from tests import volume_fixtures as vf

#: Plexora 3D's analysis paths (plexora/licensing/manifest.py `volume`): these
#: tests exercise them, so they run on a Pro test licence.
pytestmark = pytest.mark.pro


def _disc(radius, centre=(48, 48), shape=(96, 96)):
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]]
    return (yy - centre[0]) ** 2 + (xx - centre[1]) ** 2 <= radius ** 2


@pytest.fixture(autouse=True)
def _clean():
    segmenter.forget()
    yield
    segmenter.forget()


# -- the guards ----------------------------------------------------------------------


def test_each_guard_says_why_a_direction_ended():
    guard = propagate._Guard(5)
    guard.begin(_disc(10))
    assert guard(6, _disc(9), 3.0, _disc(10))
    cases = [
        (np.zeros((96, 96), bool), 3.0, _disc(10), "object_ended"),
        (_disc(9), -2.0, _disc(10), "object_ended"),          # SAM 2: not there
        (_disc(10, centre=(48, 80)), None, _disc(10), "lost"),
        (_disc(21), None, _disc(19), "grew"),
        (_disc(3), None, _disc(4), "shrank"),
        (_disc(10, centre=(48, 56)), None, _disc(10), "drifted"),
    ]
    for got, score, previous, reason in cases:
        guard.stopped.clear()
        assert not guard(7, got, score, previous)
        assert guard.stopped == {"increasing": reason}


def test_a_ghost_past_the_pole_is_where_the_object_fades():
    """Axial blur leaves a dimmer copy of an object for planes past its pole;
    the guard stops where it stands out by less than FADE_MIN of the start."""
    def plane(brightness):
        frame = np.full((96, 96, 3), 20, np.uint8)
        frame[_disc(10)] = brightness
        return frame

    guard = propagate._Guard(5)
    guard.begin(_disc(10), plane(220))
    assert guard(6, _disc(10), None, _disc(10), plane(180))       # 0.8 of the start
    assert not guard(7, _disc(10), None, _disc(10), plane(60))    # 0.2: a ghost
    assert guard.stopped == {"increasing": "faded"}
    # An object that barely stood out on its own plane is never judged by it.
    flat = propagate._Guard(5)
    flat.begin(_disc(10), plane(23))
    assert flat(6, _disc(10), None, _disc(10), plane(20))


# -- SAM 2's path, stopped where the object ends ---------------------------------------


def test_sam2_is_stopped_at_the_first_plane_past_the_object(monkeypatch):
    depth, start = 21, 10
    radius = {z: 20 * np.sqrt(max(0.0, 1 - ((z - start) / 5.0) ** 2)) for z in range(depth)}
    computed = []

    def fake_carry(frames, first, *, box=None, points=None, labels=None, mask=None,
                   forward=True, backward=True, until=None):
        for step in ([1] if forward else []) + ([-1] if backward else []):
            z = first
            while 0 <= z < len(frames):
                computed.append(z)
                inside = radius[z] >= 1
                got = _disc(radius[z]) if inside else np.zeros((96, 96), bool)
                if not until(z, got, 4.0 if inside else -8.0):
                    break
                z += step
        return {}

    monkeypatch.setattr(sam_video, "available", lambda: True)
    monkeypatch.setattr(sam_video, "carry", fake_carry)
    frames = [np.zeros((96, 96, 3), np.uint8)] * depth
    kept, stopped, engine = propagate.carry_frames(
        frames, start, box=np.array([28, 28, 68, 68], np.float32), engine="sam2")
    assert engine == "sam2"
    assert sorted(kept) == list(range(6, 15))
    assert stopped == {"increasing": "object_ended", "decreasing": "object_ended"}
    # One plane past each pole, never the rest of the stack.
    assert max(computed) == 15 and min(computed) == 5


def test_auto_takes_mobilesam_without_the_3d_extra(monkeypatch):
    monkeypatch.setattr(sam_video, "available", lambda: False)
    assert propagate.choose_engine("auto") == "mobile"
    with pytest.raises(segmenter.SegmentError) as refused:
        propagate.choose_engine("sam2")
    assert refused.value.code == "precondition_missing"
    with pytest.raises(segmenter.SegmentError):
        propagate.choose_engine("best")


# -- a project's planes, through the route --------------------------------------------------


def _ring(cx, cy, r, n=48):
    angles = np.linspace(0, 2 * np.pi, n, endpoint=False)
    ring = [[float(cx + r * np.cos(a)), float(cy + r * np.sin(a))] for a in angles]
    return {"type": "Polygon", "coordinates": [ring + [ring[0]]]}


@pytest.fixture
def stack(tmp_path):
    """12 planes of 128 x 128 with a bright column (radius 30) through all of
    them, over a dim noisy background: something that stands out on every
    plane, so only the stand-in model's growing outline can end a carry."""
    import tifffile

    rng = np.random.default_rng(0)
    planes = rng.integers(100, 140, size=(12, 128, 128)).astype(np.uint16)
    planes[:, _disc(30, centre=(64, 64), shape=(128, 128))] += 2000
    path = tmp_path / "vol.ome.tif"
    tifffile.imwrite(path, planes[None], ome=True, metadata={
        "axes": "CZYX", "PhysicalSizeX": 0.5, "PhysicalSizeY": 0.5, "PhysicalSizeZ": 2.0})
    register_image_datasource("vol", path)
    return planes


@pytest.fixture
def client():
    import plexora

    return plexora.app.test_client()


def test_an_outline_is_carried_through_the_route(stack, client):
    geometry = _ring(64, 64, 12)
    with sam.use_backend(sam_backend.FakeSamBackend()):
        answer = client.post("/segment/v1/carry", data=json.dumps({
            "datasource": "vol", "z": 6, "engine": "mobile", "geometry": geometry,
            "view": {"x": 0, "y": 0, "width": 128, "height": 128}}),
            content_type="application/json")
    assert answer.status_code == 200, answer.get_data(as_text=True)
    body = answer.get_json()
    assert body["engine"] == "mobile" and body["start"] == 6
    # The outline given is kept exactly on its own plane.
    assert body["planes"]["6"] == geometry
    zs = sorted(int(z) for z in body["planes"])
    assert zs == list(range(zs[0], zs[-1] + 1)) and len(zs) > 1
    assert body["range"] == [zs[0], zs[-1]]
    assert body["footprint"]["type"] in ("Polygon", "MultiPolygon")
    assert set(body["stopped"]) == {"increasing_z", "decreasing_z"}
    assert body["provenance"]["engine"] == "mobile"


def test_the_route_refuses_what_it_cannot_carry(stack, client, monkeypatch):
    def post(**body):
        payload = {"datasource": "vol", "z": 6, "geometry": _ring(64, 64, 12),
                   "view": {"x": 0, "y": 0, "width": 128, "height": 128}, **body}
        return client.post("/segment/v1/carry", data=json.dumps(payload),
                           content_type="application/json")

    monkeypatch.setattr(sam_video, "runtime_installed", lambda: False)
    with sam.use_backend(sam_backend.FakeSamBackend()):
        sam2 = post(engine="sam2")
        assert sam2.status_code == 409
        error = sam2.get_json()["error"]
        assert error["code"] == "precondition_missing" and "volume3d" in error["install"]
        assert post(z=40, engine="mobile").status_code == 400
        assert post(z="top").status_code == 400
    status = client.get("/segment/v1/status").get_json()
    assert status["through_planes"]["state"] == "not_installed_runtime"


def test_a_region_is_carried_into_a_shape_through_the_planes(stack):
    from plexora.agent import AgentSession
    from plexora.plugins.roi.server import service

    session = AgentSession()
    ds = session.image_data("vol")
    _before, _after, roi = service.create_roi(ds, category="Nucleus",
                                              geometry=_ring(64, 64, 12),
                                              extent={"kind": "plane", "z": 6})
    with sam.use_backend(sam_backend.FakeSamBackend()):
        _b, _a, before, after, result = service.carry_through_planes(
            ds, session, roi["id"], engine="mobile")
    assert before.get("solid") is None
    assert after["solid"]["kind"] == "contours" and after["solid"]["keys"] == [6]
    assert after["extent"]["kind"] == "slab"
    assert after["extent"]["z0"] <= 6 <= after["extent"]["z1"]
    assert result["start"] == 6 and "planes" not in result
