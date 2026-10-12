"""A server-side export: frames in from the browser, a file and its manifest out.

The browser renders every frame and, when it cannot encode the video itself,
streams them here a chunk at a time. The job's status codes are what the
browser's export loop acts on, so each one is pinned:

    409  a chunk that does not start where the last ended (a dropped chunk
         would be a video silently shorter than its manifest), or finish
         before every frame arrived
    400  a chunk of the wrong size, or more frames than the export started with
    410  anything after the job is over
    503  the writer is behind -- backpressure, not buffering; send it again
    422  no writer here can produce the format
    429  too many exports at once

Every export runs on a daemon thread that writes into the video's directory, so
an autouse fixture drains them while this test's data root is still in force.
Exports are tiny (96x64, 10 fps, PNG frames in a zip) -- the writer under test
here is the job, not the codec.
"""

import io
import json
import threading
import time
import zipfile

import pytest
from PIL import Image

from plexora.plugins.video_studio.server import (encode_jobs, encoders, manifest, presets,
                                                 repository, schema, writers)
from plexora.plugins.video_studio.server.encode_jobs import JobError

W, H, FPS = 96, 64, 10
FRAME_BYTES = W * H * 4
FACTS = {"datasource": "demo", "image_size": [400, 300], "depth": 6, "frames": 1,
         "channels": [{"key": "DNA", "name": "DNA"}, {"key": "CD3", "name": "CD3"}]}


@pytest.fixture(autouse=True)
def _drain_exports(plexora_data_root):
    """Stop every export thread before the data root is taken away."""
    encoders.reset_for_tests()
    yield
    encode_jobs.drain()
    encoders.reset_for_tests()


@pytest.fixture
def video():
    """A saved six-plane z-sweep: 0.6 s, so six frames at 10 fps."""
    doc = presets.build("z_sweep", FACTS, None, {"ms_per_plane": 100, "hold_ends_s": 0})
    video_id = repository.create(doc)
    return video_id, repository.load(video_id)


def _output(doc, **overrides):
    return schema.normalize_output({**doc["output"], "width": W, "height": H, "fps": FPS,
                                    "format": "png", **overrides})


def _start(video, **overrides):
    video_id, doc = video
    output = _output(doc, **overrides)
    if "writer" in overrides:
        output["writer"] = overrides["writer"]
    frames = schema.frame_count(doc["duration_s"], output["fps"])
    return encode_jobs.start(video_id, doc, output, frames=frames, name="clip")


def _frames(first, count):
    return b"".join(bytes([(first + i) * 30 % 256, 80, 160, 255]) * (W * H)
                    for i in range(count))


def _pngs(first, count, size=(W, H)):
    """Length-prefixed PNGs, as the browser sends them over a slow link."""
    out = b""
    for i in range(count):
        buffer = io.BytesIO()
        Image.new("RGBA", size, ((first + i) * 30 % 256, 10, 200, 255)).save(buffer, "PNG")
        data = buffer.getvalue()
        out += len(data).to_bytes(4, "big") + data
    return out


def _push(video_id, export_id, first, count, payload, **kwargs):
    """Push as the browser does: a 503 means wait a moment and send it again."""
    deadline = time.monotonic() + 10
    while True:
        try:
            return encode_jobs.push(video_id, export_id, first, count, payload, **kwargs)
        except JobError as exc:
            if exc.status != 503 or time.monotonic() > deadline:
                raise
            time.sleep(0.02)


def _wait(video_id, export_id, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = encode_jobs.get(video_id, export_id)
        if job["status"] not in ("running", "finishing"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"export {export_id} still {job['status']} after {timeout}s")


def _status(info):
    return info.value.status


# -- the whole path ------------------------------------------------------------------------


def test_an_export_streams_in_order_and_finishes_with_a_file_and_a_manifest(video):
    video_id, doc = video
    job = _start(video)
    export_id = job["export_id"]
    assert job["status"] == "running"
    assert job["frames_expected"] == 6 == schema.frame_count(doc["duration_s"], FPS)
    assert job["writer"] == "pngzip" and job["name"] == "clip.zip"
    assert job["chunk_frames"] >= 1
    assert not any(key.startswith("_") for key in job)
    assert repository.list_exports(video_id)[0]["status"] == "running"

    for first in (0, 2, 4):
        pushed = _push(video_id, export_id, first, 2, _frames(first, 2))
        assert pushed["frames_received"] == first + 2
    plan = schema.acquired_plan(doc, FPS)
    client = {"frames": [{"k": f["k"], "t": f["t"], "z": f["z"]} for f in plan],
              "plugin_version": "test", "unresolved_frames": []}
    finishing = encode_jobs.finish(video_id, export_id, client)
    assert finishing["status"] == "finishing"

    done = _wait(video_id, export_id)
    assert done["status"] == "done", done
    assert done["frames_written"] == 6
    assert done["progress"] == {"done": 6, "total": 6}
    assert done["bytes"] > 0 and done["finished_at"]

    path = encode_jobs.download_path(video_id, export_id)
    assert path == repository.exports_dir(video_id) / export_id / "clip.zip"
    with zipfile.ZipFile(path) as archive:
        assert len(archive.namelist()) == 6
    beside = json.loads((path.parent / manifest.FILENAME).read_text(encoding="utf-8"))
    stored = repository.export_manifest(video_id, export_id)
    assert stored == beside
    assert len(stored["frames"]) == stored["output"]["frame_count"] == 6
    assert [f["z_acquired"] for f in stored["frames"]] == [f["z"] for f in plan]
    assert stored["output"]["complete"] is True
    assert stored["integrity"]["acquired_verified"] is True
    assert stored["software"]["encoder"]["writer"] == "PngZipWriter"
    (row,) = repository.list_exports(video_id)
    assert (row["status"], row["name"], row["available"]) == ("done", "clip.zip", True)
    assert row["bytes"] == done["bytes"]


def test_a_finished_export_is_still_downloadable_after_its_job_record_is_gone(video):
    video_id, _ = video
    export_id = _start(video)["export_id"]
    encode_jobs.push(video_id, export_id, 0, 6, _frames(0, 6))
    encode_jobs.finish(video_id, export_id, {})
    assert _wait(video_id, export_id)["status"] == "done"
    path = encode_jobs.download_path(video_id, export_id)
    assert path is not None and path.is_file()
    # A restart forgets the job; the video's database still knows the file.
    encode_jobs._JOBS.clear()
    assert encode_jobs.get(video_id, export_id) is None
    assert encode_jobs.download_path(video_id, export_id) == path
    assert encode_jobs.download_path(video_id, "exp_nope") is None


def test_png_frames_are_accepted_length_prefixed(video):
    video_id, _ = video
    export_id = _start(video)["export_id"]
    payload = _pngs(0, 3)
    encode_jobs.push(video_id, export_id, 0, 3, payload, frame_format="png")
    _push(video_id, export_id, 3, 3, _pngs(3, 3), frame_format="png")
    encode_jobs.finish(video_id, export_id, {})
    assert _wait(video_id, export_id)["status"] == "done"
    with zipfile.ZipFile(encode_jobs.download_path(video_id, export_id)) as archive:
        names = sorted(archive.namelist())
        assert len(names) == 6
        # The zip writer stores the browser's PNG bytes as they came.
        first_size = int.from_bytes(payload[:4], "big")
        assert archive.read(names[0]) == payload[4:4 + first_size]


def test_png_frames_are_decoded_for_a_writer_that_needs_pixels(video):
    video_id, _ = video
    export_id = _start(video, format="gif", writer="pillow")["export_id"]
    encode_jobs.push(video_id, export_id, 0, 6, _pngs(0, 6), frame_format="png")
    encode_jobs.finish(video_id, export_id, {})
    done = _wait(video_id, export_id)
    assert done["status"] == "done" and done["writer"] == "pillow"
    with Image.open(encode_jobs.download_path(video_id, export_id)) as gif:
        assert gif.n_frames == 6


def test_a_png_frame_of_the_wrong_size_fails_the_export_and_says_why(video):
    video_id, _ = video
    export_id = _start(video, format="gif", writer="pillow")["export_id"]
    encode_jobs.push(video_id, export_id, 0, 6, _pngs(0, 6, size=(32, 32)), frame_format="png")
    done = _wait(video_id, export_id)
    assert done["status"] == "failed"
    assert "32x32" in done["error"]
    (row,) = repository.list_exports(video_id)
    assert row["status"] == "failed"
    assert repository.export_manifest(video_id, export_id)["output"]["complete"] is False


def test_a_truncated_png_chunk_is_refused(video):
    video_id, _ = video
    export_id = _start(video)["export_id"]
    with pytest.raises(JobError) as info:
        encode_jobs.push(video_id, export_id, 0, 3, _pngs(0, 2), frame_format="png")
    assert _status(info) == 400
    assert encode_jobs.get(video_id, export_id)["frames_received"] == 0


# -- refusals ------------------------------------------------------------------------------


def test_a_chunk_out_of_order_is_409(video):
    video_id, _ = video
    export_id = _start(video)["export_id"]
    with pytest.raises(JobError) as info:
        encode_jobs.push(video_id, export_id, 1, 1, _frames(1, 1))
    assert _status(info) == 409
    encode_jobs.push(video_id, export_id, 0, 2, _frames(0, 2))
    for first in (0, 1, 3):
        with pytest.raises(JobError) as info:
            encode_jobs.push(video_id, export_id, first, 1, _frames(first, 1))
        assert _status(info) == 409
    assert encode_jobs.get(video_id, export_id)["frames_received"] == 2


def test_a_chunk_of_the_wrong_size_is_400(video):
    video_id, _ = video
    export_id = _start(video)["export_id"]
    for payload in (_frames(0, 2)[:-1], _frames(0, 3), b""):
        with pytest.raises(JobError) as info:
            encode_jobs.push(video_id, export_id, 0, 2, payload)
        assert _status(info) == 400
    assert encode_jobs.get(video_id, export_id)["frames_received"] == 0


def test_more_frames_than_the_export_started_with_is_400(video):
    video_id, _ = video
    export_id = _start(video)["export_id"]
    with pytest.raises(JobError) as info:
        encode_jobs.push(video_id, export_id, 0, 7, _frames(0, 7))
    assert _status(info) == 400
    encode_jobs.push(video_id, export_id, 0, 5, _frames(0, 5))
    with pytest.raises(JobError) as info:
        encode_jobs.push(video_id, export_id, 5, 2, _frames(5, 2))
    assert _status(info) == 400
    with pytest.raises(JobError) as info:
        encode_jobs.push(video_id, export_id, 5, 0, b"")
    assert _status(info) == 400


def test_a_chunk_over_the_size_limit_is_413(video, monkeypatch):
    video_id, _ = video
    export_id = _start(video)["export_id"]
    monkeypatch.setattr(encode_jobs, "MAX_CHUNK_BYTES", FRAME_BYTES)
    with pytest.raises(JobError) as info:
        encode_jobs.push(video_id, export_id, 0, 2, _frames(0, 2))
    assert _status(info) == 413


def test_finish_before_every_frame_arrived_is_409(video):
    video_id, _ = video
    export_id = _start(video)["export_id"]
    encode_jobs.push(video_id, export_id, 0, 4, _frames(0, 4))
    with pytest.raises(JobError) as info:
        encode_jobs.finish(video_id, export_id, {})
    assert _status(info) == 409
    assert "4 of 6" in str(info.value)
    assert encode_jobs.get(video_id, export_id)["status"] == "running"


def test_anything_after_finish_is_410(video):
    video_id, _ = video
    export_id = _start(video)["export_id"]
    encode_jobs.push(video_id, export_id, 0, 6, _frames(0, 6))
    encode_jobs.finish(video_id, export_id, {})
    for call in (lambda: encode_jobs.push(video_id, export_id, 6, 1, _frames(6, 1)),
                 lambda: encode_jobs.finish(video_id, export_id, {})):
        with pytest.raises(JobError) as info:
            call()
        assert _status(info) == 410
    assert _wait(video_id, export_id)["status"] == "done"
    with pytest.raises(JobError) as info:
        encode_jobs.push(video_id, export_id, 6, 1, _frames(6, 1))
    assert _status(info) == 410


def test_an_unknown_export_or_another_videos_export_is_404(video):
    video_id, _ = video
    export_id = _start(video)["export_id"]
    for vid, eid in ((video_id, "exp_000000000000"), ("vid_000000000000", export_id)):
        with pytest.raises(JobError) as info:
            encode_jobs.push(vid, eid, 0, 1, _frames(0, 1))
        assert _status(info) == 404
        assert encode_jobs.get(vid, eid) is None
        assert encode_jobs.cancel(vid, eid) is False


def test_a_format_no_writer_here_produces_is_422(video):
    video_id, doc = video
    output = _output(doc)
    output["format"] = "avi"
    with pytest.raises(JobError) as info:
        encode_jobs.start(video_id, doc, output, frames=6, name="clip")
    assert _status(info) == 422
    with pytest.raises(JobError) as info:
        _start(video, writer="telepathy")
    assert _status(info) == 422
    assert repository.list_exports(video_id) == []


def test_a_writer_that_refuses_the_size_is_422_and_leaves_nothing(video):
    video_id, _ = video
    with pytest.raises(JobError) as info:
        _start(video, format="gif", writer="pillow", width=1200, height=800)
    assert _status(info) == 422
    exports = repository.exports_dir(video_id)
    assert not exports.exists() or list(exports.iterdir()) == []


def test_too_many_running_exports_is_429(video, monkeypatch):
    video_id, _ = video
    monkeypatch.setattr(encode_jobs, "MAX_JOBS", 2)
    _start(video)
    _start(video)
    with pytest.raises(JobError) as info:
        _start(video)
    assert _status(info) == 429
    # The refused export's directory and row are not left behind.
    assert len(list(repository.exports_dir(video_id).iterdir())) == 2
    assert len(repository.list_exports(video_id)) == 2


# -- backpressure ------------------------------------------------------------------------


def test_a_full_queue_is_503_and_the_same_chunk_succeeds_once_the_writer_catches_up(
        video, monkeypatch):
    video_id, _ = video
    entered, release = threading.Event(), threading.Event()
    real_write = writers.PngZipWriter.write

    def stalled_write(self, rgba):
        entered.set()
        assert release.wait(20), "the test never released the writer"
        return real_write(self, rgba)

    monkeypatch.setattr(writers.PngZipWriter, "write", stalled_write)
    monkeypatch.setattr(encode_jobs, "QUEUE_CHUNKS", 1)
    try:
        export_id = _start(video)["export_id"]
        encode_jobs.push(video_id, export_id, 0, 1, _frames(0, 1))
        # The writer has taken chunk one and is stuck writing it...
        assert entered.wait(10)
        # ...so the queue holds exactly one more chunk.
        encode_jobs.push(video_id, export_id, 1, 1, _frames(1, 1))
        with pytest.raises(JobError) as info:
            encode_jobs.push(video_id, export_id, 2, 1, _frames(2, 1))
        assert _status(info) == 503
        # Refused, not half-taken: the next chunk is still frame 2.
        assert encode_jobs.get(video_id, export_id)["frames_received"] == 2
    finally:
        release.set()
    deadline = time.monotonic() + 10
    while True:
        try:
            encode_jobs.push(video_id, export_id, 2, 4, _frames(2, 4))
            break
        except JobError as exc:
            assert exc.status == 503 and time.monotonic() < deadline
            time.sleep(0.02)
    encode_jobs.finish(video_id, export_id, {})
    assert _wait(video_id, export_id)["status"] == "done"
    assert encode_jobs.get(video_id, export_id)["frames_written"] == 6


# -- cancel, failure, drain ------------------------------------------------------------------


def test_cancel_removes_the_export_directory_and_records_cancelled(video):
    video_id, _ = video
    export_id = _start(video)["export_id"]
    encode_jobs.push(video_id, export_id, 0, 2, _frames(0, 2))
    directory = repository.exports_dir(video_id) / export_id
    assert directory.is_dir()
    assert encode_jobs.cancel(video_id, export_id) is True
    done = _wait(video_id, export_id)
    assert done["status"] == "cancelled"
    assert not directory.exists()
    (row,) = repository.list_exports(video_id)
    assert row["status"] == "cancelled" and row["available"] is False
    assert encode_jobs.download_path(video_id, export_id) is None
    with pytest.raises(JobError) as info:
        encode_jobs.push(video_id, export_id, 2, 1, _frames(2, 1))
    assert _status(info) == 410


def test_a_writer_failure_marks_the_export_failed_with_the_encoders_tail(video, monkeypatch):
    video_id, _ = video

    def broken(self, rgba):
        raise writers.WriterError("the encoder fell over", tail="last line of the log")

    monkeypatch.setattr(writers.PngZipWriter, "write", broken)
    export_id = _start(video)["export_id"]
    encode_jobs.push(video_id, export_id, 0, 2, _frames(0, 2))
    done = _wait(video_id, export_id)
    assert done["status"] == "failed"
    assert done["error"] == "the encoder fell over"
    assert done["error_tail"] == "last line of the log"
    assert encode_jobs.download_path(video_id, export_id) is None
    (row,) = repository.list_exports(video_id)
    assert row["status"] == "failed"
    record = repository.export_manifest(video_id, export_id)
    assert record["output"]["complete"] is False
    assert record["output"]["error"] == "the encoder fell over"
    assert (repository.exports_dir(video_id) / export_id / manifest.FILENAME).is_file()


def test_drain_cancels_every_running_export_and_forgets_them(video):
    video_id, _ = video
    first, second = _start(video)["export_id"], _start(video)["export_id"]
    encode_jobs.push(video_id, first, 0, 1, _frames(0, 1))
    encode_jobs.drain()
    assert encode_jobs.get(video_id, first) is None
    assert encode_jobs.get(video_id, second) is None
    statuses = {row["export_id"]: row["status"] for row in repository.list_exports(video_id)}
    assert statuses == {first: "cancelled", second: "cancelled"}
