"""A server-side export: frames in from the browser, a file out.

The browser renders every frame (the live viewer is the only thing that can
draw a frame exactly as the user sees it); when it cannot encode the video
itself it streams the frames here, in order, a chunk at a time:

    POST   .../exports                 -> {export_id, chunk_frames}
    POST   .../exports/<id>/frames     header X-Plexora-First-Frame, X-Plexora-Frames
    POST   .../exports/<id>/finish     {manifest}
    GET    .../exports/<id>            status
    DELETE .../exports/<id>            cancel
    GET    .../exports/<id>/download

Figure Builder's job registry, reshaped: in memory, one daemon thread per
export, a cancel event. Jobs are not persisted -- a browser that died mid-export
took the rest of the frames with it, so a job that outlived a restart would be
waiting for frames that will never come. What IS kept is the export's row in
the video's database and, on failure, the partial file.

**Backpressure, not buffering.** The queue between the route and the writer
holds two chunks. A third is refused with 503 and `Retry-After`, and the
browser waits: a slow encoder slows the render instead of filling the server's
memory with 4K frames.

**Frames are contiguous.** A chunk that does not start where the last one
ended is refused with 409 -- a dropped chunk would otherwise be a video that is
silently a second shorter than its manifest says.
"""

from __future__ import annotations

import json
import queue
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone

from plexora.plugins.video_studio.server import encoders, manifest, repository, writers

MAX_JOBS = 10
QUEUE_CHUNKS = 2
#: Frames per chunk the browser is told to send, by frame size: about 32 MB.
CHUNK_BYTES = 32 * 1024 * 1024
MAX_CHUNK_BYTES = 64 * 1024 * 1024
#: A finished job's record is dropped after this; its files stay in exports/.
FINISHED_TTL_S = 48 * 3600
#: A running job that has heard nothing for this long is presumed abandoned.
IDLE_TIMEOUT_S = 15 * 60

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


class JobError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def start(video_id: str, document: dict, output: dict, *, frames: int, name: str) -> dict:
    """Open a writer for an export of `frames` frames. Returns the job's public
    description; raises JobError when no writer can produce the format."""
    entry = encoders.pick(output["format"], output.get("writer"))
    if entry is None:
        raise JobError(f"this server cannot write {output['format']!r}", 422)
    export_id = "exp_" + uuid.uuid4().hex[:12]
    directory = repository.exports_dir(video_id) / export_id
    try:
        writer = writers.open_writer(entry, directory, name, output["width"], output["height"],
                                     output["fps"], output)
    except writers.WriterError as exc:
        shutil.rmtree(directory, ignore_errors=True)
        raise JobError(str(exc), 422) from exc
    frame_bytes = output["width"] * output["height"] * 4
    job = {
        "export_id": export_id,
        "video_id": video_id,
        "status": "running",
        "frames_expected": int(frames),
        "frames_received": 0,
        "frames_written": 0,
        "chunk_frames": max(1, min(120, CHUNK_BYTES // max(1, frame_bytes))),
        "format": output["format"],
        "writer": entry["writer"],
        "label": entry["label"],
        "name": writer.path.name,
        "started_at": _now(),
        "finished_at": None,
        "error": None,
        "error_tail": "",
        "bytes": 0,
        "_writer": writer,
        "_queue": queue.Queue(maxsize=QUEUE_CHUNKS),
        "_cancel": threading.Event(),
        "_finish": None,
        "_output": output,
        "_document": document,
        "_directory": directory,
        "_touched": time.monotonic(),
        "_started": time.monotonic(),
    }
    with _LOCK:
        _sweep()
        running = sum(1 for j in _JOBS.values() if j["status"] in ("running", "finishing"))
        if running >= MAX_JOBS:
            writer.abort()
            shutil.rmtree(directory, ignore_errors=True)
            raise JobError("too many exports are running; wait for one to finish", 429)
        _JOBS[export_id] = job
    repository.record_export(video_id, export_id, status="running", output=output)
    thread = threading.Thread(target=_run, args=(job,), name=f"video-export-{export_id}",
                              daemon=True)
    job["_thread"] = thread
    thread.start()
    return describe(job)


def _job(video_id, export_id):
    with _LOCK:
        job = _JOBS.get(export_id)
    if job is None or job["video_id"] != video_id:
        raise JobError("no such export", 404)
    return job


def push(video_id: str, export_id: str, first: int, count: int, payload: bytes, *,
         frame_format: str = "rgba") -> dict:
    """Queue one chunk of frames. 409 out of order, 503 when the writer is
    behind (try again), 410 once the job is over."""
    job = _job(video_id, export_id)
    if job["status"] != "running":
        raise JobError(f"this export is {job['status']}", 410)
    if first != job["frames_received"]:
        raise JobError(f"expected frame {job['frames_received']}, got {first}", 409)
    if count < 1 or job["frames_received"] + count > job["frames_expected"]:
        raise JobError("more frames than the export was started with", 400)
    if len(payload) > MAX_CHUNK_BYTES:
        raise JobError("chunk too large", 413)
    frames = _split(payload, count, job, frame_format)
    try:
        job["_queue"].put_nowait(frames)
    except queue.Full:
        raise JobError("the encoder is busy", 503) from None
    job["frames_received"] += count
    job["_touched"] = time.monotonic()
    return describe(job)


def _split(payload: bytes, count: int, job: dict, frame_format: str) -> list:
    """`payload` cut into `count` frames: raw RGBA back to back, or PNGs each
    prefixed by its length (4 bytes, big-endian) for a link where raw frames
    would be too heavy."""
    if frame_format == "png":
        frames, offset = [], 0
        for _ in range(count):
            if offset + 4 > len(payload):
                raise JobError("truncated PNG chunk")
            size = int.from_bytes(payload[offset:offset + 4], "big")
            offset += 4
            frames.append(("png", payload[offset:offset + size]))
            offset += size
        return frames
    size = job["_output"]["width"] * job["_output"]["height"] * 4
    if len(payload) != size * count:
        raise JobError(f"expected {count} frames of {size} bytes, got {len(payload)} bytes")
    view = memoryview(payload)
    return [("rgba", view[i * size:(i + 1) * size]) for i in range(count)]


def finish(video_id: str, export_id: str, client_manifest: dict) -> dict:
    job = _job(video_id, export_id)
    if job["status"] != "running":
        raise JobError(f"this export is {job['status']}", 410)
    if job["frames_received"] != job["frames_expected"]:
        raise JobError(f"{job['frames_received']} of {job['frames_expected']} frames arrived",
                       409)
    job["_finish"] = client_manifest if isinstance(client_manifest, dict) else {}
    job["status"] = "finishing"
    job["_queue"].put(None)
    return describe(job)


def cancel(video_id: str, export_id: str) -> bool:
    try:
        job = _job(video_id, export_id)
    except JobError:
        return False
    job["_cancel"].set()
    try:
        job["_queue"].put_nowait(None)
    except queue.Full:
        pass
    return True


def _decode_png(data: bytes, width: int, height: int) -> bytes:
    import io  # noqa: PLC0415

    from PIL import Image  # noqa: PLC0415

    image = Image.open(io.BytesIO(data)).convert("RGBA")
    if image.size != (width, height):
        raise writers.WriterError(f"a {image.size[0]}x{image.size[1]} frame in a "
                                  f"{width}x{height} export")
    return image.tobytes()


def _run(job: dict):
    writer = job["_writer"]
    output = job["_output"]
    try:
        while True:
            if job["_cancel"].is_set():
                break
            try:
                chunk = job["_queue"].get(timeout=5)
            except queue.Empty:
                if time.monotonic() - job["_touched"] > IDLE_TIMEOUT_S:
                    job["error"] = "no frames arrived for 15 minutes; the export was abandoned"
                    job["_cancel"].set()
                continue
            if chunk is None:
                break
            for kind, data in chunk:
                if job["_cancel"].is_set():
                    break
                if kind == "png" and isinstance(writer, writers.PngZipWriter):
                    writer.write_png(bytes(data))
                elif kind == "png":
                    writer.write(_decode_png(bytes(data), output["width"], output["height"]))
                else:
                    writer.write(bytes(data))
                job["frames_written"] += 1
        if job["_cancel"].is_set():
            writer.abort()
            shutil.rmtree(job["_directory"], ignore_errors=True)
            job["status"] = "cancelled"
            repository.record_export(job["video_id"], job["export_id"], status="cancelled")
            return
        path = writer.close()
        record = manifest.build(job["_document"], job["_finish"] or {}, output=output,
                                writer=writer.describe(), frames=job["frames_written"],
                                complete=True,
                                render_s=round(time.monotonic() - job["_started"], 3))
        manifest_path = job["_directory"] / manifest.FILENAME
        manifest_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        job["bytes"] = writers.file_size(path)
        job["status"] = "done"
        repository.record_export(job["video_id"], job["export_id"], status="done",
                                 manifest=record, path=path, size=job["bytes"])
    except Exception as exc:  # noqa: BLE001 - a job must never take the server with it
        writer.abort()
        job["status"] = "failed"
        job["error"] = str(exc)
        job["error_tail"] = getattr(exc, "tail", "")
        partial = _keep_partial(job)
        try:
            repository.record_export(job["video_id"], job["export_id"], status="failed",
                                     path=partial,
                                     size=writers.file_size(partial) if partial else None)
        except Exception:  # noqa: BLE001 - the video itself may have been deleted
            pass
        try:
            record = manifest.build(job["_document"], job["_finish"] or {}, output=output,
                                    writer=writer.describe(), frames=job["frames_written"],
                                    complete=False, error=str(exc))
            (job["_directory"] / manifest.FILENAME).write_text(json.dumps(record, indent=2),
                                                               encoding="utf-8")
            repository.record_export(job["video_id"], job["export_id"], status="failed",
                                     manifest=record, path=partial,
                                     size=writers.file_size(partial) if partial else None)
        except Exception:  # noqa: BLE001 - the video itself may have been deleted
            pass
    finally:
        job["finished_at"] = _now()
        job["_touched"] = time.monotonic()
        job["_document"] = None


def _keep_partial(job):
    """Whatever the writer managed to write, kept as `partial.<ext>`."""
    directory = job["_directory"]
    for candidate in sorted(directory.glob("partial.*")) + sorted(directory.glob("*.*")):
        if candidate.name != manifest.FILENAME and candidate.stat().st_size > 0:
            return candidate
    return None


def get(video_id, export_id):
    try:
        return describe(_job(video_id, export_id))
    except JobError:
        return None


def describe(job):
    out = {key: value for key, value in job.items() if not key.startswith("_")}
    out["progress"] = {"done": job["frames_written"], "total": job["frames_expected"]}
    return out


def download_path(video_id, export_id):
    try:
        job = _job(video_id, export_id)
    except JobError:
        job = None
    if job is not None:
        if job["status"] != "done":
            return None
        return job["_directory"] / job["name"]
    # A finished export from an earlier session: its row knows the file.
    for row in repository.list_exports(video_id):
        if row["export_id"] == export_id and row["available"] and row["status"] == "done":
            return repository.exports_dir(video_id) / export_id / row["name"]
    return None


def drain(timeout=10.0):
    """Cancel every export and wait for its thread (tests, data-root switches)."""
    with _LOCK:
        jobs = list(_JOBS.values())
    for job in jobs:
        job["_cancel"].set()
        try:
            job["_queue"].put_nowait(None)
        except queue.Full:
            pass
    for job in jobs:
        thread = job.get("_thread")
        if thread is not None:
            thread.join(timeout)
    with _LOCK:
        _JOBS.clear()


def _sweep():
    now = time.monotonic()
    for export_id, job in list(_JOBS.items()):
        if job["status"] in ("running", "finishing"):
            continue
        if now - job["_touched"] > FINISHED_TTL_S or len(_JOBS) > MAX_JOBS * 4:
            _JOBS.pop(export_id, None)
