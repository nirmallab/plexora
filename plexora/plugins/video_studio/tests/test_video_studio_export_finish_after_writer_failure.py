"""Finish answers even when the writer dies while the queue is full.

The browser streams frames until every one has arrived, then posts finish. With
the encoder behind (the normal backpressure state) the queue between the route
and the writer is full when finish arrives, so finish waits for room for its
end-of-frames marker. If the writer then fails on the chunk it is writing, its
thread exits without taking anything more off the queue: finish must still
answer -- 410, the export is failed -- instead of holding a request thread, and
the page's render cover, forever.
"""

import threading

import pytest

from plexora.plugins.video_studio.server import encode_jobs, writers
from plexora.plugins.video_studio.server.encode_jobs import JobError
from plexora.plugins.video_studio.tests.test_video_studio_export_jobs import (  # noqa: F401
    _drain_exports, _frames, _start, video)


def test_finish_answers_when_the_writer_fails_while_the_queue_is_full(video, monkeypatch):
    video_id, _ = video
    entered, release = threading.Event(), threading.Event()

    def failing_write(self, rgba):
        entered.set()
        assert release.wait(20), "the test never released the writer"
        raise writers.WriterError("ffmpeg stopped: [Errno 28] No space left on device")

    monkeypatch.setattr(writers.PngZipWriter, "write", failing_write)
    assert encode_jobs.QUEUE_CHUNKS == 2
    export_id = _start(video)["export_id"]
    job = encode_jobs._JOBS[export_id]
    finish_thread = None
    try:
        # The writer takes the first chunk and is busy with it...
        encode_jobs.push(video_id, export_id, 0, 2, _frames(0, 2))
        assert entered.wait(10)
        # ...while the last two chunks fill the queue: every frame has arrived.
        encode_jobs.push(video_id, export_id, 2, 2, _frames(2, 2))
        encode_jobs.push(video_id, export_id, 4, 2, _frames(4, 2))
        with pytest.raises(JobError) as info:
            encode_jobs.push(video_id, export_id, 6, 1, _frames(6, 1))
        assert info.value.status == 400

        outcome = {}

        def call_finish():
            try:
                outcome["result"] = encode_jobs.finish(video_id, export_id, {})
            except JobError as exc:
                outcome["error"] = exc

        finish_thread = threading.Thread(target=call_finish, daemon=True,
                                         name="audit-video-finish")
        finish_thread.start()
        # finish has passed its checks and is waiting for room in the queue.
        for _ in range(500):
            if job["status"] == "finishing":
                break
            threading.Event().wait(0.01)
        assert job["status"] == "finishing"

        # Now the writer fails on the chunk it was writing; its thread exits.
        release.set()
        job["_thread"].join(10)
        assert not job["_thread"].is_alive()
        assert job["status"] == "failed"

        finish_thread.join(5)
        assert not finish_thread.is_alive(), (
            "finish() is still blocked on queue.put(None) after the writer thread died "
            "with the queue full; job status is " + repr(job["status"]))
        if "error" in outcome:
            assert outcome["error"].status == 410
        else:
            assert outcome["result"]["status"] == "failed"
    finally:
        release.set()
        # Unblock a finish stuck on the full queue so no thread outlives the test.
        if finish_thread is not None and finish_thread.is_alive():
            while finish_thread.is_alive():
                try:
                    job["_queue"].get_nowait()
                except Exception:  # noqa: BLE001 - queue.Empty while it wakes
                    pass
                finish_thread.join(0.05)
