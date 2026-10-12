"""Video Studio's HTTP surface: the licence guard, then the codes the client acts on.

The plugin is Paid as a whole (`plugin:video_studio`), so the first thing pinned
is that core's blueprint guard answers every API route 403 with a machine-
readable licence body on a licence without it -- while the plugin's static
files still load, so the Tools menu can badge the tool rather than break.

With the entitlement, the status codes are the contract, as in Figure Builder:

    400  the request is wrong; retrying it unchanged fails the same way
    404  no such video or export
    409  somebody else saved first, or frames arrived out of order
    422  a track that would interpolate acquired data (code "integrity"), a
         format no encoder here writes, a stored video this build cannot read

and an export runs end to end over HTTP at a tiny size, PNG frames in a zip.
"""

import io
import json
import time
import zipfile
from pathlib import Path

import pytest

import plexora
from plexora.plugins.video_studio.server import encode_jobs, encoders, schema
from plexora.server import plugins as plugin_registry
from tests.helpers import ALL_CONFIRMED, image_spec, project, use_data_root

API = "/plugins/video_studio/api"
STATIC = Path(__file__).resolve().parent.parent / "static"
W, H, FPS = 96, 64, 10
STACK_FACTS = {"depth": 4, "image_size": [400, 300]}


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """The app with a data root of its own and a flat two-channel demo project.

    The plugin is not installed here: `create_app` did that at import time, and
    Flask refuses to register a blueprint twice. Skipped rather than failed
    when it is absent, so a core-only run does not report this file as broken.
    """
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({
        "demo": project("demo", image=image_spec(channels=("DNA", "CD3"), width=400,
                                                 height=300),
                        confirmed=ALL_CONFIRMED).to_entry(),
    }), encoding="utf-8")
    if plugin_registry.find(plexora.app, "video_studio") is None:  # pragma: no cover
        pytest.skip("video_studio is not installed")
    encoders.reset_for_tests()
    yield plexora.app.test_client()
    # Before this test's data root is taken away: an export still running
    # would otherwise write into the next test's root.
    encode_jobs.drain()
    encoders.reset_for_tests()


@pytest.fixture
def client(app_client, license_issuer):
    """The same client on a licence that includes Video Studio."""
    license_issuer.install(license_issuer.issue(ent=["ai", "plugin:video_studio"]))
    return app_client


def _create(client, **body):
    body.setdefault("preset", "z_sweep")
    body.setdefault("datasource", "demo")
    body.setdefault("facts", STACK_FACTS)
    body.setdefault("params", {"ms_per_plane": 100, "hold_ends_s": 0})
    response = client.post(f"{API}/videos", json=body)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["video_id"]


def _rgba(count):
    return b"".join(bytes([i * 50 % 256, 90, 180, 255]) * (W * H) for i in range(count))


def _wait_done(client, video_id, export_id, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"{API}/videos/{video_id}/exports/{export_id}").get_json()
        if body["status"] not in ("running", "finishing"):
            return body
        time.sleep(0.02)
    raise AssertionError("the export did not finish")


# -- the licence ---------------------------------------------------------------------------

UNLICENSED = [
    ("get", "/state?datasource=demo"), ("get", "/encoders"), ("get", "/facts/demo"),
    ("post", "/presets/build"), ("get", "/videos"), ("post", "/videos"),
    ("get", "/videos/vid_000000000000"), ("put", "/videos/vid_000000000000"),
    ("delete", "/videos/vid_000000000000"), ("post", "/videos/vid_000000000000/ops"),
    ("post", "/videos/vid_000000000000/duplicate"),
    ("get", "/videos/vid_000000000000/document"),
    ("get", "/videos/vid_000000000000/posters/kf_1"),
    ("put", "/videos/vid_000000000000/posters/kf_1"),
    ("get", "/videos/vid_000000000000/exports"), ("post", "/videos/vid_000000000000/exports"),
    ("post", "/videos/vid_000000000000/exports/exp_1/frames"),
    ("post", "/videos/vid_000000000000/exports/exp_1/finish"),
    ("get", "/videos/vid_000000000000/exports/exp_1"),
    ("delete", "/videos/vid_000000000000/exports/exp_1"),
    ("get", "/videos/vid_000000000000/exports/exp_1/download"),
    ("get", "/videos/vid_000000000000/exports/exp_1/manifest"),
    ("post", "/videos/vid_000000000000/exports/record"),
]


def test_every_route_is_covered_by_the_unlicensed_list():
    """So a route added later cannot slip past the licence test unnoticed."""
    samples = {"<video_id>": "vid_000000000000", "<export_id>": "exp_1", "<kf_id>": "kf_1",
               "<datasource>": "demo"}
    routes = set()
    for rule in plexora.app.url_map.iter_rules():
        if not rule.rule.startswith(API):
            continue
        path = rule.rule[len(API):]
        for placeholder, value in samples.items():
            path = path.replace(placeholder, value)
        routes |= {(method.lower(), path) for method in rule.methods - {"HEAD", "OPTIONS"}}
    assert routes == {(method, path.split("?")[0]) for method, path in UNLICENSED}


@pytest.mark.parametrize("method, path", UNLICENSED)
def test_without_the_entitlement_every_api_route_is_403(app_client, method, path):
    response = getattr(app_client, method)(f"{API}{path}", json={})
    assert response.status_code == 403, path
    body = response.get_json()
    assert body["success"] is False
    assert body["license"]["code"] == "license_required"
    assert body["license"]["entitlement"] == "plugin:video_studio"


def test_paid_alone_does_not_unlock_video_studio(app_client, paid_license):
    assert app_client.get(f"{API}/videos").status_code == 403


def test_the_static_files_load_without_the_entitlement(app_client):
    """The page loads the plugin's assets for the menu badge; refusing them
    would break the page rather than explain the lock."""
    response = app_client.get("/plugins/video_studio/static/videoEasing.js")
    assert response.status_code != 403
    if (STATIC / "videoEasing.js").is_file():
        assert response.status_code == 200


# -- what the bar opens on -----------------------------------------------------------------


def test_state_describes_the_presets_encoders_and_videos_for_this_image(client):
    body = client.get(f"{API}/state?datasource=demo").get_json()
    assert body["success"] is True
    assert body["facts"]["datasource"] == "demo"
    assert body["facts"]["image_size"] == [400, 300]
    assert [c["key"] for c in body["facts"]["channels"]] == ["DNA", "CD3"]
    assert len(body["presets"]) == 15
    # A flat image: the bar opens on a zoom, and the rotation says why it is off.
    assert body["default_preset"] == "zoom"
    orbit = next(p for p in body["presets"] if p["name"] == "orbit")
    assert orbit["available"] is False and orbit["reason"]
    assert {"ffmpeg", "cv2", "formats"} <= set(body["encoders"])
    assert body["videos"] == []
    assert body["limits"]["max_side"] == schema.MAX_OUTPUT_SIDE
    assert body["limits"]["formats"] == list(schema.FORMATS)
    assert [g["name"] for g in body["groups"]] == ["3d", "stack", "plane", "channels", "story"]


def test_state_with_no_datasource_still_answers(client):
    body = client.get(f"{API}/state").get_json()
    assert body["success"] is True and body["default_preset"] == "zoom"


def test_encoders_and_facts(client):
    assert {row["format"] for row in client.get(f"{API}/encoders").get_json()["formats"]} >= {
        "png", "gif"}
    assert client.get(f"{API}/facts/demo").get_json()["facts"]["depth"] == 1
    assert client.get(f"{API}/facts/nope").status_code == 404


def test_a_preset_previews_without_saving(client):
    response = client.post(f"{API}/presets/build", json={
        "preset": "z_sweep", "datasource": "demo", "facts": STACK_FACTS})
    assert response.status_code == 200
    body = response.get_json()
    assert body["document"]["tracks"][0]["kind"] == "acquired_z"
    assert body["document"]["source"]["depth"] == 4
    assert body["warnings"] == []
    assert client.get(f"{API}/videos").get_json()["videos"] == []


@pytest.mark.parametrize("body", [
    {"preset": "nope", "datasource": "demo"},
    # The demo image is flat: a z-sweep is refused, not built of nothing.
    {"preset": "z_sweep", "datasource": "demo"},
])
def test_a_preset_that_cannot_be_built_is_400(client, body):
    assert client.post(f"{API}/presets/build", json=body).status_code == 400


def test_a_body_that_is_not_an_object_is_400(client):
    for data in ("[1, 2]", "not json"):
        response = client.post(f"{API}/presets/build", data=data,
                               content_type="application/json")
        assert response.status_code == 400


# -- the library and the document ------------------------------------------------------------


def test_a_video_is_created_from_a_preset_and_read_back(client):
    video_id = _create(client)
    body = client.get(f"{API}/videos/{video_id}").get_json()
    assert body["document"]["video_id"] == video_id
    assert body["document"]["revision"] == 0
    assert body["document"]["provenance"]["preset"]["name"] == "z_sweep"
    assert body["warnings"] == [] and body["exports"] == []
    listed = client.get(f"{API}/videos?datasource=demo").get_json()["videos"]
    assert [v["video_id"] for v in listed] == [video_id]
    assert client.get(f"{API}/videos?datasource=other").get_json()["videos"] == []


def test_a_video_is_created_from_a_whole_document_with_a_title(client):
    preview = client.post(f"{API}/presets/build", json={
        "preset": "zoom", "datasource": "demo"}).get_json()["document"]
    response = client.post(f"{API}/videos", json={"document": preview, "title": "Imported"})
    assert response.status_code == 200
    document = response.get_json()["document"]
    assert document["title"] == "Imported"
    assert document["tracks"][0]["kind"] == "camera2d"


def test_an_unknown_video_is_404_and_an_invalid_id_is_400(client):
    for method in ("get", "delete"):
        response = getattr(client, method)(f"{API}/videos/vid_000000000000")
        assert response.status_code == 404
        assert response.get_json()["error"] == "unknown_video"
    assert client.post(f"{API}/videos/vid_000000000000/ops",
                       json={"base_revision": 0, "operations": [{"op": "set_loop"}]}
                       ).status_code == 404
    for bad in ("vid_XYZ", "config", "vid_0123"):
        assert client.get(f"{API}/videos/{bad}").status_code == 400, bad


def test_put_replaces_the_document_and_a_stale_put_is_409(client):
    video_id = _create(client)
    document = client.get(f"{API}/videos/{video_id}").get_json()["document"]
    response = client.put(f"{API}/videos/{video_id}",
                          json={"base_revision": 0, "document": {**document, "title": "Put"}})
    assert response.status_code == 200
    assert response.get_json()["revision"] == 1
    assert response.get_json()["document"]["title"] == "Put"
    stale = client.put(f"{API}/videos/{video_id}",
                       json={"base_revision": 0, "document": {**document, "title": "Stale"}})
    assert stale.status_code == 409
    assert stale.get_json() == {"success": False, "error": "stale_revision", "revision": 1}
    assert client.put(f"{API}/videos/{video_id}", json={"document": document}).status_code == 400


def test_ops_return_the_inverse_which_undoes_them(client):
    video_id = _create(client)
    response = client.post(f"{API}/videos/{video_id}/ops", json={
        "base_revision": 0, "operations": [{"op": "set_title", "title": "Edited"},
                                           {"op": "set_loop", "loop": True}]})
    assert response.status_code == 200
    body = response.get_json()
    assert body["revision"] == 1
    assert body["document"]["title"] == "Edited" and body["document"]["loop"] is True
    undone = client.post(f"{API}/videos/{video_id}/ops", json={
        "base_revision": 1, "operations": body["inverse"], "origin": "undo"}).get_json()
    assert undone["revision"] == 2
    assert undone["document"]["loop"] is False
    assert undone["document"]["title"].startswith("Z-Stack Sweep")


def test_a_stale_edit_is_409_and_a_bad_one_is_400(client):
    video_id = _create(client)
    client.post(f"{API}/videos/{video_id}/ops",
                json={"base_revision": 0, "operations": [{"op": "set_loop", "loop": True}]})
    stale = client.post(f"{API}/videos/{video_id}/ops",
                        json={"base_revision": 0, "operations": [{"op": "set_loop"}]})
    assert stale.status_code == 409 and stale.get_json()["revision"] == 1
    bad = client.post(f"{API}/videos/{video_id}/ops",
                      json={"base_revision": 1, "operations": [{"op": "explode"}]})
    assert bad.status_code == 400 and "explode" in bad.get_json()["error"]
    assert client.post(f"{API}/videos/{video_id}/ops",
                       json={"base_revision": 1, "operations": []}).status_code == 400


def test_an_operation_that_is_not_an_object_is_400(client):
    video_id = _create(client)
    response = client.post(f"{API}/videos/{video_id}/ops",
                           json={"base_revision": 0, "operations": ["set_loop"]})
    assert response.status_code == 400


def test_an_interpolated_plane_is_422_with_code_integrity(client):
    video_id = _create(client)
    for keyframe in ({"t": 0.1, "value": 2.5}, {"t": 0.1, "value": 2, "ease": "linear"}):
        response = client.post(f"{API}/videos/{video_id}/ops", json={
            "base_revision": 0, "operations": [{"op": "add_track", "track": {
                "kind": "acquired_z", "keyframes": [keyframe]}}]})
        assert response.status_code == 422, keyframe
        assert response.get_json()["code"] == "integrity"
    assert client.get(f"{API}/videos/{video_id}").get_json()["document"]["revision"] == 0


def test_duplicate_and_delete(client):
    video_id = _create(client)
    response = client.post(f"{API}/videos/{video_id}/duplicate")
    assert response.status_code == 200
    copy_id = response.get_json()["video_id"]
    assert copy_id != video_id
    assert response.get_json()["document"]["title"].endswith(" copy")
    named = client.post(f"{API}/videos/{video_id}/duplicate", json={"title": "Variant"})
    assert named.get_json()["document"]["title"] == "Variant"
    assert client.delete(f"{API}/videos/{copy_id}").get_json() == {"success": True}
    assert client.get(f"{API}/videos/{copy_id}").status_code == 404
    assert client.post(f"{API}/videos/vid_000000000000/duplicate").status_code == 404


def test_the_document_downloads_as_a_json_attachment(client):
    video_id = _create(client, title="My sweep: v2")
    response = client.get(f"{API}/videos/{video_id}/document")
    assert response.status_code == 200
    assert response.mimetype == "application/json"
    disposition = response.headers["Content-Disposition"]
    assert disposition.startswith("attachment;")
    assert 'filename="My sweep_ v2.plexvid"' in disposition
    body = json.loads(response.get_data(as_text=True))
    assert body["plexora_video"] == schema.SCHEMA_VERSION
    assert body["document"]["video_id"] == video_id
    # Re-importable as it stands.
    reimported = client.post(f"{API}/videos", json={"document": body["document"]})
    assert reimported.status_code == 200
    assert reimported.get_json()["video_id"] != video_id


def test_posters_round_trip(client):
    video_id = _create(client)
    url = f"{API}/videos/{video_id}/posters/kf_1"
    assert client.get(url).status_code == 404
    put = client.put(url, data=b"\x89PNG poster", content_type="image/png")
    assert put.status_code == 200
    got = client.get(url)
    assert got.status_code == 200 and got.data == b"\x89PNG poster"
    assert got.mimetype == "image/png"
    assert client.put(url, data=b"", content_type="image/png").status_code == 400


# -- exports ---------------------------------------------------------------------------------


def _start_export(client, video_id, **output):
    output = {"width": W, "height": H, "fps": FPS, "format": "png", **output}
    return client.post(f"{API}/videos/{video_id}/exports", json={"output": output})


def test_an_export_runs_end_to_end_over_http(client):
    video_id = _create(client)  # four planes at 100 ms: four frames at 10 fps
    started = _start_export(client, video_id)
    assert started.status_code == 200, started.get_json()
    job = started.get_json()
    export_id = job["export_id"]
    assert job["frames_expected"] == 4 and job["writer"] == "pngzip"

    frames_url = f"{API}/videos/{video_id}/exports/{export_id}/frames"
    pushed = client.post(frames_url, data=_rgba(4), content_type="application/octet-stream",
                         headers={"X-Plexora-First-Frame": "0", "X-Plexora-Frames": "4"})
    assert pushed.status_code == 200, pushed.get_json()
    assert pushed.get_json()["frames_received"] == 4

    finished = client.post(f"{API}/videos/{video_id}/exports/{export_id}/finish",
                           json={"manifest": {"frames": [{"k": k, "z": k} for k in range(4)]}})
    assert finished.status_code == 200
    status = _wait_done(client, video_id, export_id)
    assert status["status"] == "done" and status["frames_written"] == 4

    download = client.get(f"{API}/videos/{video_id}/exports/{export_id}/download")
    assert download.status_code == 200
    assert download.headers["Content-Disposition"].startswith("attachment;")
    with zipfile.ZipFile(io.BytesIO(download.data)) as archive:
        assert len(archive.namelist()) == 4
    download.close()

    record = client.get(f"{API}/videos/{video_id}/exports/{export_id}/manifest").get_json()
    assert len(record["manifest"]["frames"]) == 4
    assert [f["z_acquired"] for f in record["manifest"]["frames"]] == [0, 1, 2, 3]
    assert record["manifest"]["integrity"]["acquired_verified"] is True
    exports = client.get(f"{API}/videos/{video_id}/exports").get_json()["exports"]
    assert [(e["export_id"], e["status"]) for e in exports] == [(export_id, "done")]
    assert client.get(f"{API}/videos/{video_id}").get_json()["exports"][0]["available"] is True


def test_export_frame_errors_answer_the_codes_the_export_loop_acts_on(client):
    video_id = _create(client)
    export_id = _start_export(client, video_id).get_json()["export_id"]
    url = f"{API}/videos/{video_id}/exports/{export_id}/frames"

    def push(first, count, data):
        return client.post(url, data=data, content_type="application/octet-stream",
                           headers={"X-Plexora-First-Frame": str(first),
                                    "X-Plexora-Frames": str(count)})

    assert push(1, 1, _rgba(1)).status_code == 409
    assert push(0, 2, _rgba(1)).status_code == 400
    assert push(0, 9, _rgba(9)).status_code == 400
    assert client.post(url, data=_rgba(1), headers={"X-Plexora-First-Frame": "zero",
                                                    "X-Plexora-Frames": "1"}).status_code == 400
    assert client.post(f"{API}/videos/{video_id}/exports/{export_id}/finish",
                       json={}).status_code == 409
    assert push(0, 4, _rgba(4)).status_code == 200
    assert client.post(f"{API}/videos/{video_id}/exports/{export_id}/finish",
                       json={}).status_code == 200
    assert push(4, 1, _rgba(1)).status_code == 410


def test_a_busy_encoder_answers_503_with_retry_after(client, monkeypatch):
    video_id = _create(client)
    export_id = _start_export(client, video_id).get_json()["export_id"]

    def busy(*args, **kwargs):
        raise encode_jobs.JobError("the encoder is busy", 503)

    monkeypatch.setattr(encode_jobs, "push", busy)
    response = client.post(f"{API}/videos/{video_id}/exports/{export_id}/frames",
                           data=_rgba(1), headers={"X-Plexora-First-Frame": "0",
                                                   "X-Plexora-Frames": "1"})
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"


def test_a_format_no_writer_here_produces_is_422(client):
    video_id = _create(client)
    response = _start_export(client, video_id, writer="telepathy")
    assert response.status_code == 422
    assert client.post(f"{API}/videos/vid_000000000000/exports",
                       json={"output": {}}).status_code == 404


def test_an_export_with_a_null_output_uses_the_videos_own_output(client):
    video_id = _create(client)
    response = client.post(f"{API}/videos/{video_id}/exports", json={"output": None})
    assert response.status_code == 200


def test_a_cancelled_export_is_gone(client):
    video_id = _create(client)
    export_id = _start_export(client, video_id).get_json()["export_id"]
    assert client.delete(f"{API}/videos/{video_id}/exports/{export_id}").get_json() == {
        "success": True}
    assert _wait_done(client, video_id, export_id)["status"] == "cancelled"
    assert client.get(f"{API}/videos/{video_id}/exports/{export_id}/download"
                      ).status_code == 404
    exports = client.get(f"{API}/videos/{video_id}/exports").get_json()["exports"]
    assert [e["status"] for e in exports] == ["cancelled"]


def test_unknown_exports_are_404(client):
    video_id = _create(client)
    base = f"{API}/videos/{video_id}/exports/exp_000000000000"
    assert client.get(base).status_code == 404
    assert client.delete(base).status_code == 404
    assert client.get(f"{base}/download").status_code == 404
    assert client.get(f"{base}/manifest").status_code == 404
    assert client.post(f"{base}/finish", json={}).status_code == 404
    assert client.post(f"{base}/frames", data=b"x", headers={
        "X-Plexora-First-Frame": "0", "X-Plexora-Frames": "1"}).status_code == 404


def test_an_export_the_browser_encoded_is_recorded_with_its_manifest(client):
    video_id = _create(client)
    response = client.post(f"{API}/videos/{video_id}/exports/record", json={
        "output": {"width": 1280, "height": 720, "fps": FPS, "format": "mp4"},
        "frames": 4, "bytes": 123456,
        "manifest": {"frames": [{"k": k, "z": k} for k in range(4)],
                     "encoder": {"encoder": "WebCodecs", "codec": "avc1.640028"}}})
    assert response.status_code == 200
    body = response.get_json()
    export_id = body["export_id"]
    assert export_id.startswith("exp_")
    assert body["manifest"]["output"]["complete"] is True
    assert body["manifest"]["software"]["encoder"]["encoder"] == "WebCodecs"
    assert body["manifest"]["integrity"]["acquired_verified"] is True
    (row,) = client.get(f"{API}/videos/{video_id}/exports").get_json()["exports"]
    assert (row["export_id"], row["status"], row["bytes"]) == (export_id, "done", 123456)
    # The file stays where the browser saved it.
    assert row["available"] is False
    stored = client.get(f"{API}/videos/{video_id}/exports/{export_id}/manifest").get_json()
    assert stored["manifest"]["output"]["width"] == 1280

    short = client.post(f"{API}/videos/{video_id}/exports/record",
                        json={"output": {"fps": FPS}, "frames": 2}).get_json()
    assert short["manifest"]["output"]["complete"] is False
    statuses = {e["export_id"]: e["status"]
                for e in client.get(f"{API}/videos/{video_id}/exports").get_json()["exports"]}
    assert statuses[short["export_id"]] == "failed"
