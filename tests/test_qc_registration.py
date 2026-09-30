"""Registration Check: the state rules, the mismatch field, the capabilities and routes."""

import json

import numpy as np
import pytest

from plexora.agent.errors import AgentError
from plexora.plugins.qc.server import registration as reg

NAMES = ["DNA_1", "CD3", "CD8", "DNA_2", "CD20", "DNA_3"]


# -- state ---------------------------------------------------------------------------------


def test_the_first_two_nuclear_channels_are_the_default_pair():
    state = reg.resolve(reg.default_state(), NAMES)
    assert (state["reference"], state["comparison"]) == ("DNA_1", "DNA_2")
    assert reg.status_of(state, ["DNA_1", "DNA_2", "DNA_3"]) == "inactive"


def test_step_moves_only_the_comparison_and_wraps():
    state = reg.resolve(reg.default_state(), NAMES)
    state = reg.step(state, NAMES, "next")
    assert (state["reference"], state["comparison"]) == ("DNA_1", "DNA_3")
    state = reg.step(state, NAMES, "next")
    assert state["comparison"] == "DNA_2"
    state = reg.step(state, NAMES, "prev")
    assert state["comparison"] == "DNA_3"
    assert reg.step(state, NAMES, "next", wrap=False)["comparison"] == "DNA_3"


def test_the_users_reference_is_respected():
    state = reg.resolve(reg.default_state(), NAMES)
    state = reg.apply_update(state, NAMES, reference="DNA_2")
    # The comparison was the new reference: it moves to the next candidate.
    assert (state["reference"], state["comparison"]) == ("DNA_2", "DNA_3")
    # Any channel may be the reference, not only a nuclear one.
    state = reg.apply_update(state, NAMES, reference="CD3")
    assert state["reference"] == "CD3" and state["comparison"] == "DNA_3"
    with pytest.raises(AgentError):
        reg.apply_update(state, NAMES, reference="DNA_1", comparison="DNA_1")
    with pytest.raises(AgentError):
        reg.apply_update(state, NAMES, comparison="nope")


def test_a_manual_rule_and_user_colours():
    state = reg.apply_update(reg.default_state(), NAMES,
                             rule={"mode": "manual", "channels": ["CD3", "CD8"]})
    assert (state["reference"], state["comparison"]) == ("CD3", "CD8")
    state = reg.apply_update(state, NAMES, colors={"comparison": "#00ff00"})
    assert state["colors"]["comparison"] == {"color": "#00ff00", "user_set": True}
    state = reg.apply_update(state, NAMES, reset_colors=True)
    assert state["colors"]["comparison"]["user_set"] is False
    pattern = reg.apply_update(reg.default_state(), NAMES,
                               rule={"mode": "manual", "pattern": "^CD"})
    assert (pattern["reference"], pattern["comparison"]) == ("CD3", "CD8")


def test_flicker_turns_back_on_at_each_activation():
    state = reg.apply_update(reg.default_state(), NAMES, active=True, flicker=False)
    assert state["flicker"] is False
    state = reg.apply_update(state, NAMES, active=False)
    assert state["reference"] == "DNA_1"
    state = reg.apply_update(state, NAMES, active=True)
    assert state["flicker"] is True


def test_one_nuclear_channel_needs_a_second():
    names = ["DAPI", "CD3"]
    state = reg.apply_update(reg.default_state(), names, active=True)
    assert state["comparison"] is None
    assert reg.status_of(state, ["DAPI"]) == "needs_second_channel"


# -- the field -----------------------------------------------------------------------------


def _scene(size=512, seed=0):
    from scipy import ndimage

    rng = np.random.default_rng(seed)
    plane = np.zeros((size, size), np.float32)
    ys, xs = rng.integers(0, size, 1200), rng.integers(0, size, 1200)
    plane[ys, xs] = rng.uniform(500, 1500, ys.size)
    return ndimage.gaussian_filter(plane, 2.5) * 40 + rng.random((size, size)).astype(
        np.float32) * 5


def _prep(plane):
    from scipy import ndimage

    return ndimage.gaussian_filter(np.log1p(plane), 1.0)


def test_identical_planes_have_nothing_highlighted():
    plane = _prep(_scene())
    tissue = np.ones(plane.shape, bool)
    field = reg.mismatch_field(plane, plane.copy(), tissue, 64)
    stats, state = reg.evaluate(field, 1.0, None, None)
    assert stats["highlighted_pct"] == 0.0 and stats["pattern"] == "none"
    assert stats["denominator"] == "evaluated_tissue"
    again = reg.mismatch_field(plane, plane.copy(), tissue, 64)
    assert np.array_equal(field["dx"], again["dx"])


def test_a_global_shift_is_recovered_and_widespread():
    raw = _scene()
    tissue = np.ones(raw.shape, bool)
    field = reg.mismatch_field(_prep(raw), _prep(np.roll(raw, (3, -2), axis=(0, 1))),
                               tissue, 64)
    assert abs(field["global"]["dy"] - 3) < 0.3 and abs(field["global"]["dx"] + 2) < 0.3
    stats, _ = reg.evaluate(field, 1.0, None, {"threshold_px": 2.0})
    assert stats["highlighted_pct"] > 90 and stats["pattern"] == "widespread"
    # Every block reads the same displacement.
    assert stats["residual_px"]["p90"] - stats["residual_px"]["p50"] < 0.5


def test_a_local_shift_is_highlighted_where_it_is():
    raw = _scene()
    moved = raw.copy()
    moved[128:384, 64:320] = np.roll(raw, (6, 0), axis=(0, 1))[128:384, 64:320]
    tissue = np.ones(raw.shape, bool)
    tissue[:, 448:] = False
    field = reg.mismatch_field(_prep(raw), _prep(moved), tissue, 64)
    stats, state = reg.evaluate(field, 1.0, None, None)
    truth = np.zeros_like(state, dtype=bool)
    truth[2:6, 1:5] = True
    lit = state == reg.HIGHLIGHTED
    assert (lit & truth).sum() / (lit | truth).sum() > 0.7
    assert stats["pattern"] == "isolated"
    assert (state[:, 7] == reg.NOT_EVALUATED).all()


def test_a_cell_that_changed_is_dense_and_scattered_noise_is_not():
    from scipy import ndimage

    rng = np.random.default_rng(1)
    a = np.zeros((256, 256), np.float32)
    ys, xs = np.mgrid[0:256, 0:256]
    for cy in range(12, 256, 24):
        for cx in range(12, 256, 24):
            a[(ys - cy) ** 2 + (xs - cx) ** 2 <= 49] = 0.9
    b = a.copy()
    # One nucleus is a different shape in the second cycle ...
    b[96:120, 96:120] = 0.0
    b[100:122, 104:118] = 0.9
    # ... and a few pixels here and there are just brighter.
    b[rng.integers(0, 256, 60), rng.integers(0, 256, 60)] += 0.5
    a, b = (ndimage.gaussian_filter(np.clip(p, 0, 1), 0.8) for p in (a, b))
    lit = np.clip((np.abs(a - b) - reg.DISAGREE_LOW)
                  / (reg.DISAGREE_HIGH - reg.DISAGREE_LOW), 0, 1)
    dense = reg.dense_mismatch(a, b, lit, 40) & (lit > 0)
    assert dense[96:124, 96:124].any()
    away = dense.copy()
    away[70:150, 70:150] = False
    assert not away.any()


def test_the_mismatch_map_heats_where_disagreement_crowds():
    a = np.zeros((240, 240), np.float32)
    ys, xs = np.mgrid[0:240, 0:240]
    for cy in range(10, 240, 20):
        for cx in range(10, 240, 20):
            a[(ys - cy) ** 2 + (xs - cx) ** 2 <= 36] = 0.9
    b = a.copy()
    # Two cells changed in the second cycle, top left; the rest agree.
    b[40:80, 40:80] = np.roll(a, (7, 5), axis=(0, 1))[40:80, 40:80]
    mapped = reg.mismatch_map(a, b, 8, 10)
    share = mapped["share"]
    assert share.shape == (24, 24)
    assert share[4:8, 4:8].max() > 0.3
    assert share[14:, 14:].max() < 0.05
    assert reg._dense_pct(mapped) > 0
    spots = reg._hotspots(mapped, 2.0, None)
    assert spots and 60 <= spots[0]["x"] <= 180 and 60 <= spots[0]["y"] <= 180
    assert reg._dense_pct(reg.mismatch_map(a, a.copy(), 8, 10)) == 0


def test_the_threshold_changes_numbers_not_the_field():
    raw = _scene()
    field = reg.mismatch_field(_prep(raw), _prep(np.roll(raw, (0, 3), axis=(0, 1))),
                               np.ones(raw.shape, bool), 64)
    low, _ = reg.evaluate(field, 1.0, 1.0, {"threshold_um": 2.0})
    high, _ = reg.evaluate(field, 1.0, 1.0, {"threshold_um": 5.0})
    assert low["highlighted_pct"] > 90 and high["highlighted_pct"] == 0.0
    assert low["unit"] == "um" and low["global_shift_um"] == pytest.approx(3.0, abs=0.3)


# -- capabilities and routes ---------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    import plexora
    from tests.qc_fixtures import make_qc_project

    make_qc_project(tmp_path, size=512, grid=20, artifacts=("misregistration",), table=False,
                    mask=False)
    return plexora.app.test_client()


def _post(client, path, body):
    return client.post(path, data=json.dumps({"datasource": "qcsynth", **body})).get_json()


def test_the_panel_turns_it_on_steps_and_computes(client):
    channels = client.get("/plugins/qc/registration/channels?datasource=qcsynth").get_json()
    assert channels["ok"] and channels["candidates"] == ["DNA_1", "DNA_2"]
    state = client.get("/plugins/qc/state?datasource=qcsynth").get_json()
    assert state["checks"]["registration"]["status"] == "inactive"
    revision = state["revision"]
    on = _post(client, "/plugins/qc/registration/set", {"active": True})
    assert on["ok"] and on["registration"]["status"] == "ready"
    assert on["registration"]["flicker"] is True
    assert (on["registration"]["reference"], on["registration"]["comparison"]) == \
        ("DNA_1", "DNA_2")
    assert on["receipt"]["undo_hint"]["tool"] == "set_registration_check"
    # The check's state is beside the QC document: its revision does not move.
    assert client.get("/plugins/qc/state?datasource=qcsynth").get_json()["revision"] == revision
    stepped = _post(client, "/plugins/qc/registration/step", {"direction": "next"})
    assert stepped["ok"] and stepped["registration"]["comparison"] == "DNA_2"
    computed = _post(client, "/plugins/qc/registration/compute", {})
    assert computed["ok"] and not computed["reused"]
    stats = computed["stats"]
    assert 5 < stats["highlighted_pct"] < 60 and stats["pattern"] == "isolated"
    assert len(computed["overlay"]["state"]) == computed["overlay"]["grid"]["nx"] * \
        computed["overlay"]["grid"]["ny"]
    # The mismatch map rides along: bytes for the panel only when asked.
    lean = computed["overlay"]["mismatch"]
    assert "share" not in lean and lean["grid"]["nx"] > computed["overlay"]["grid"]["nx"]
    assert stats["dense_mismatch_pct"] > 0 and stats["mismatch_hotspots"]
    again = _post(client, "/plugins/qc/registration/compute", {"include_map": True})
    assert again["reused"] and again["fingerprint"] == computed["fingerprint"]
    import base64

    mapped = again["overlay"]["mismatch"]
    share = np.frombuffer(base64.b64decode(mapped["share"]), np.uint8)
    assert share.size == mapped["grid"]["nx"] * mapped["grid"]["ny"] and share.max() > 0
    read = client.get("/plugins/qc/registration?datasource=qcsynth").get_json()
    assert read["registration"]["last"]["stats"]["highlighted_pct"] == \
        stats["highlighted_pct"]
    paused = _post(client, "/plugins/qc/registration/set", {"flicker": False})
    assert paused["registration"]["flicker"] is False
    off = _post(client, "/plugins/qc/registration/set", {"active": False})
    assert off["registration"]["status"] == "inactive"
    bad = client.post("/plugins/qc/registration/set",
                      data=json.dumps({"datasource": "qcsynth", "reference": "nope"}))
    assert bad.status_code == 400


def test_an_agent_is_told_and_the_viewer_notified(tmp_path):
    from plexora.agent import AgentSession, registry
    from tests.qc_fixtures import make_qc_project

    make_qc_project(tmp_path, size=512, grid=20, artifacts=("global_shift",), table=False,
                    mask=False)
    registry.discover(["qc"])
    told = []
    session = AgentSession()

    def notify(project, plugin, kind, payload):
        told.append((plugin, kind))
        return True

    def run(tool, **args):
        answer = registry.invoke(session, tool, {"project": "qcsynth", **args}, notify=notify)
        assert answer["ok"], answer
        return answer["result"]

    assert run("detect_nuclear_channels")["suggested"] == {"reference": "DNA_1",
                                                            "comparison": "DNA_2"}
    run("set_registration_check", active=True)
    computed = run("compute_registration_mismatch", include_overlay=False)
    assert computed["stats"]["pattern"] == "widespread"
    assert computed["stats"]["global_shift_px"]["magnitude"] == pytest.approx(5.0, abs=0.5)
    assert ("qc", "qc.registration_set") in told and ("qc", "qc.registration") in told
    status = run("get_registration_check")
    assert status["registration"]["last"]["stats"]["pattern"] == "widespread"


def test_a_comparison_keeps_its_own_colour():
    state = reg.apply_update(reg.default_state(), NAMES, channel_colors={"DNA_3": "#00AAFF"})
    assert state["channel_colors"] == {"DNA_3": "#00aaff"}
    assert reg.default_state()["colors"]["reference"]["color"] == "#ff2d2d"
    assert reg.default_state()["colors"]["comparison"]["color"] == "#2bd46f"
    state = reg.apply_update(state, NAMES, channel_colors={"DNA_3": None})
    assert state["channel_colors"] == {}
    with pytest.raises(AgentError):
        reg.apply_update(state, NAMES, channel_colors={"nope": "#ffffff"})
    with pytest.raises(AgentError):
        reg.apply_update(state, NAMES, channel_colors={"DNA_2": "red"})
    state = reg.apply_update(state, NAMES, channel_colors={"DNA_2": "#123456"})
    assert reg.apply_update(state, NAMES, reset_colors=True)["channel_colors"] == {}


def test_every_comparison_is_scored_and_the_disagreement_is_drawn(client):
    from io import BytesIO

    from PIL import Image

    _post(client, "/plugins/qc/registration/set", {"active": True})
    # A score for a channel that is not the comparison leaves the state alone.
    scored = _post(client, "/plugins/qc/registration/compute",
                   {"comparison": "DNA_2", "include_overlay": False})
    assert scored["ok"] and scored["comparison"] == "DNA_2" and "overlay" not in scored
    refused = _post(client, "/plugins/qc/registration/compute", {"comparison": "DNA_1"})
    assert refused["ok"] is False
    base = "/plugins/qc/registration/disagreement?datasource=qcsynth"
    whole = client.get(f"{base}&box=0,0,512,512&max_px=128")
    assert whole.status_code == 200 and whole.mimetype == "image/png"
    picture = Image.open(BytesIO(whole.data))
    assert picture.mode == "RGBA" and max(picture.size) <= 128
    assert [float(v) for v in whole.headers["X-QC-Box"].split(",")] == [0, 0, 512, 512]
    # Misregistered: some nuclei light up, and most of the image does not.
    alpha = np.asarray(picture)[..., 3]
    assert 0 < (alpha > 0).mean() < 0.5
    # Zoomed in, the same place is read at full resolution.
    close = client.get(f"{base}&box=100,100,228,228&max_px=512")
    assert close.headers["X-QC-Level"] == "0"
    assert Image.open(BytesIO(close.data)).size == (128, 128)
    assert client.get(f"{base}&box=0,0,nan,1").status_code == 400
    assert client.get(f"{base}&box=900,900,950,950").status_code == 400
