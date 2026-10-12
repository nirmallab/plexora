"""The record that travels with every exported video.

A video of tissue is evidence only if a reader can tell exactly what each frame
shows. `manifest.build` computes the acquired timepoint and plane of every frame
from the project (never trusting the browser for it), compares them with what
the browser says it applied, and states whether the file is complete, which
frames were rendered before the viewer settled, and every display transition
-- so a fade is never mistaken for biology. These tests pin each of those
fields against `schema.acquired_plan`, the one source of truth for them.
"""

import pytest

from plexora.plugins.video_studio.server import manifest, schema

SOURCE = {"datasource": "demo", "image_size": [800, 600], "depth": 8, "frames": 3,
          "pixel_size": 0.65, "spacing": {"z": {"value": 2.0, "unit": "µm"}},
          "fingerprint": {"image_width": 800}}
WRITER = {"writer": "PngZipWriter", "encoder": "zipfile+Pillow", "codec": "PNG"}


def _document(*, loop=False, fps=10):
    return schema.normalize_project({
        "video_id": "vid_0123456789ab", "revision": 3, "title": "Sweep", "source": SOURCE,
        "duration_s": 1.6, "loop": loop,
        "tracks": [
            {"track_id": "trk_z", "kind": "acquired_z", "mode": "procedural",
             "procedural": {"generator": "sweep", "params": {"from": 0, "to": 7}}},
            {"track_id": "trk_t", "kind": "acquired_t",
             "keyframes": [{"t": 0, "value": 0}, {"t": 0.8, "value": 2}]},
            {"track_id": "trk_dna", "kind": "channel", "target": "DNA",
             "keyframes": [{"t": 0, "value": {"presence": 0}},
                           {"t": 0.5, "value": {"presence": 1}}]},
        ],
        "output": {"fps": fps, "width": 640, "height": 480, "format": "png",
                   "overlays": {"scalebar": True, "label": False}},
        "provenance": {"preset": {"name": "z_sweep", "params": {}}},
    })


def _build(document=None, draft=None, *, frames=None, complete=True, **kwargs):
    document = document or _document()
    output = kwargs.pop("output", document["output"])
    n = schema.frame_count(document["duration_s"], output["fps"])
    return manifest.build(document, draft or {}, output=output, writer=WRITER,
                          frames=n if frames is None else frames, complete=complete, **kwargs)


def _client_frames(document, fps=10):
    return [{"k": f["k"], "t": f["t"], "z": f["z"]}
            for f in schema.acquired_plan(document, fps)]


def test_the_frame_list_is_the_acquired_plan():
    doc = _document()
    record = _build(doc)
    plan = schema.acquired_plan(doc, 10)
    assert record["output"]["frame_count"] == 16 == len(plan)
    assert len(record["frames"]) == 16
    assert record["frames"] == [{"k": f["k"], "t_acquired": f["t"], "z_acquired": f["z"],
                                 "mode": f["mode"], "slab": f["slab"]} for f in plan]
    # The sweep reaches every plane in order; the timepoint steps at 0.8 s.
    zs = [f["z_acquired"] for f in record["frames"]]
    assert zs == sorted(zs) and set(zs) == set(range(8))
    assert {f["t_acquired"] for f in record["frames"]} == {0, 2}


def test_the_output_rate_wins_over_the_documents():
    record = _build(output={**_document()["output"], "fps": 30})
    assert record["output"]["fps"] == 30
    assert record["output"]["frame_count"] == 48 == len(record["frames"])
    assert record["output"]["duration_s"] == pytest.approx(1.6)


def test_complete_only_when_every_frame_was_written():
    assert _build()["output"]["complete"] is True
    assert _build(frames=15)["output"]["complete"] is False
    assert _build(frames=15)["output"]["frames_written"] == 15
    assert _build(complete=False)["output"]["complete"] is False
    failed = _build(frames=4, complete=False, error="ffmpeg exited with status 1")
    assert failed["output"]["error"] == "ffmpeg exited with status 1"


def test_acquired_verified_is_none_when_the_browser_did_not_say():
    assert _build()["integrity"]["acquired_verified"] is None
    assert _build(draft={"frames": "not a list"})["integrity"]["acquired_verified"] is None


def test_acquired_verified_is_true_when_the_browser_applied_the_plan():
    doc = _document()
    record = _build(doc, {"frames": _client_frames(doc)})
    assert record["integrity"]["acquired_verified"] is True
    assert record["integrity"]["acquired_interpolated"] is False
    # A frame record that names only some fields is checked on those.
    partial = [{"k": f["k"]} for f in _client_frames(doc)]
    assert _build(doc, {"frames": partial})["integrity"]["acquired_verified"] is True


def test_acquired_verified_is_false_on_any_disagreement():
    doc = _document()
    wrong_plane = _client_frames(doc)
    wrong_plane[5]["z"] += 1
    wrong_time = _client_frames(doc)
    wrong_time[-1]["t"] = 1
    not_a_record = _client_frames(doc)
    not_a_record[0] = "frame 0"
    for frames in (wrong_plane, wrong_time, not_a_record, _client_frames(doc)[:-1]):
        assert _build(doc, {"frames": frames})["integrity"]["acquired_verified"] is False


def test_a_malformed_frame_record_from_the_browser_is_a_disagreement_not_a_crash():
    doc = _document()
    for bad in (None, "plane three", [3]):
        frames = _client_frames(doc)
        frames[2]["z"] = bad
        assert _build(doc, {"frames": frames})["integrity"]["acquired_verified"] is False


def test_unresolved_frames_are_carried_as_integers():
    record = _build(draft={"unresolved_frames": [3, "7", 9.0]})
    assert record["integrity"]["unresolved_frames"] == [3, 7, 9]
    assert _build()["integrity"]["unresolved_frames"] == []


def test_display_transitions_are_declared():
    record = _build()
    assert record["integrity"]["display_transitions"] == [
        {"track_id": "trk_dna", "kind": "channel", "target": "DNA", "fields": ["presence"],
         "from_s": 0.0, "to_s": 0.5}]


def test_the_timestamps_and_content_time_are_stated():
    record = _build()
    assert record["output"]["timestamps"] == "frame k is presented at k / fps seconds"
    assert record["output"]["content_time"] == "k x duration / (N - 1)"
    looped = _build(_document(loop=True))
    assert looped["output"]["content_time"] == "loop: k x duration / N"
    assert looped["loop"] is True


def test_what_made_it_and_what_it_shows():
    doc = _document()
    record = _build(doc, {"plugin_version": "20261011", "user_agent": "Chrome" * 100,
                          "per_frame_ms": 12.5, "retries": 2, "render_s": 99},
                    render_s=1.25)
    assert record["manifest_version"] == manifest.MANIFEST_VERSION
    assert record["created_at"].endswith("Z")
    software = record["software"]
    assert software["plugin"] == "video_studio" and software["plugin_version"] == "20261011"
    assert software["encoder"] == WRITER
    assert len(software["browser"]) == 300
    assert software["plexora"]
    assert record["video"] == {"video_id": "vid_0123456789ab", "revision": 3, "title": "Sweep",
                               "preset": {"name": "z_sweep", "params": {}}}
    assert record["source"] == {"datasource": "demo", "fingerprint": {"image_width": 800},
                                "image_size": [800, 600], "depth": 8, "frames": 3,
                                "pixel_size_um": 0.65,
                                "spacing": {"z": {"value": 2.0, "unit": "µm"}}}
    out = record["output"]
    assert (out["width"], out["height"], out["format"]) == (640, 480, "png")
    # Only the overlays actually drawn are listed.
    assert out["overlays"] == {"scalebar": True}
    # The server's own render time wins over the browser's claim.
    assert record["timing"]["render_s"] == 1.25
    assert record["timing"]["per_frame_ms"] == 12.5 and record["timing"]["retries"] == 2
    assert _build(doc, {"render_s": 4.5})["timing"]["render_s"] == 4.5
    # Enough to make the video again.
    assert record["tracks"] == doc["tracks"]
    assert record["scene_at_start"] == doc["base"]
    assert record["duration_s"] == 1.6
