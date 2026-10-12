"""The record that travels with every exported video.

A video of tissue is evidence only if a reader can tell exactly what it shows.
`video-manifest.json` sits beside every export (and in the video's database)
and says, machine-readably:

* what made it -- Plexora's version, the encoder, its codec and settings;
* what it shows -- the source image, its fingerprint, pixel size and z/t spacing;
* the output -- size, rate, frame count, duration, and whether it is complete;
* for every frame, the acquired timepoint, plane, slab and view mode it shows
  (computed here from the project, and compared with what the browser applied:
  `acquired_verified` is False if the two ever disagree);
* every display transition (fades, window changes, cross-fades), so a fade is
  never mistaken for biology;
* whether any frame was rendered before the viewer had settled
  (`unresolved_frames` -- zero, or the export was a debug run);
* the full project tracks, so the video can be made again.

The browser sends a draft (its per-frame record and timings); this module adds
what only the server knows and fixes the shape.
"""

from __future__ import annotations

import platform
from datetime import datetime, timezone

from plexora.plugins.video_studio.server import schema

FILENAME = "video-manifest.json"
MANIFEST_VERSION = 1


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _plexora_version():
    try:
        from plexora import __version__  # noqa: PLC0415

        return __version__
    except Exception:  # noqa: BLE001
        return "unknown"


def build(document: dict, draft: dict, *, output: dict, writer: dict, frames: int,
          complete: bool, error: str | None = None, render_s: float | None = None) -> dict:
    draft = draft if isinstance(draft, dict) else {}
    fps = int(output.get("fps") or document["output"]["fps"])
    plan = schema.acquired_plan(document, fps)
    client_frames = draft.get("frames") if isinstance(draft.get("frames"), list) else []
    verified = _verify(plan, client_frames)
    n = len(plan)
    source = document["source"]
    return {
        "manifest_version": MANIFEST_VERSION,
        "created_at": _now(),
        "software": {
            "plexora": _plexora_version(),
            "plugin": "video_studio",
            "plugin_version": draft.get("plugin_version") or "",
            "browser": str(draft.get("user_agent") or "")[:300],
            "server": f"{platform.system()} {platform.release()}",
            "encoder": writer,
        },
        "video": {"video_id": document["video_id"], "revision": document["revision"],
                  "title": document["title"],
                  "preset": (document.get("provenance") or {}).get("preset")},
        "source": {
            "datasource": source["datasource"],
            "fingerprint": source["fingerprint"],
            "image_size": source["image_size"],
            "depth": source["depth"],
            "frames": source["frames"],
            "pixel_size_um": source["pixel_size"],
            "spacing": source["spacing"],
        },
        "output": {
            "width": output.get("width"), "height": output.get("height"), "fps": fps,
            "format": output.get("format"), "quality": output.get("quality"),
            "transparent": bool(output.get("transparent")),
            "overlays": {k: v for k, v in (output.get("overlays") or {}).items() if v},
            "frame_count": n,
            "frames_written": int(frames),
            "duration_s": round(n / fps, 6),
            "timestamps": "frame k is presented at k / fps seconds",
            "content_time": ("loop: k x duration / N" if document["loop"]
                             else "k x duration / (N - 1)"),
            "complete": bool(complete) and int(frames) == n,
            "error": error,
        },
        "integrity": {
            "acquired_interpolated": False,
            "acquired_verified": verified,
            "unresolved_frames": [int(k) for k in draft.get("unresolved_frames") or []],
            "display_transitions": schema.display_transitions(document),
        },
        "frames": [{"k": f["k"], "t_acquired": f["t"], "z_acquired": f["z"],
                    "mode": f["mode"], "slab": f["slab"]} for f in plan],
        "timing": {
            "render_s": render_s if render_s is not None else draft.get("render_s"),
            "per_frame_ms": draft.get("per_frame_ms"),
            "retries": draft.get("retries", 0),
            "readiness_waits_ms": draft.get("readiness_waits_ms"),
        },
        "scene_at_start": document["base"],
        "tracks": document["tracks"],
        "duration_s": document["duration_s"],
        "loop": document["loop"],
    }


def _verify(plan: list, client_frames: list):
    """True when the browser applied exactly the acquired indices the project
    names for every frame, False on any disagreement, None when it did not say."""
    if not client_frames:
        return None
    if len(client_frames) != len(plan):
        return False
    for ours, theirs in zip(plan, client_frames):
        if not isinstance(theirs, dict):
            return False
        for key in ("t", "z"):
            if key not in theirs:
                continue
            try:
                if int(theirs[key]) != int(ours[key]):
                    return False
            except (TypeError, ValueError):
                # A frame the browser described unreadably is a disagreement,
                # never a reason to lose a finished export.
                return False
    return True
