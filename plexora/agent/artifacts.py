"""Where rendered evidence is kept, so a receipt or a report can point at it.

`<data_root>/.agent/artifacts/<project>/<artifact_id>.png` with a `.json`
sidecar holding the manifest and when it was made. Content-addressed: the id is
a digest of the image and its manifest, so rendering the same thing twice
stores it once and the id itself says "these are the same picture".

Bounded (`ARTIFACT_BUDGET_BYTES`, `ARTIFACT_MAX_AGE_DAYS`) by `sweep`, oldest
first. Nothing here is a source of truth -- every artifact can be rendered
again from its manifest.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

from plexora.agent.limits import ARTIFACT_BUDGET_BYTES, ARTIFACT_MAX_AGE_DAYS

ID_PATTERN = re.compile(r"^art_[0-9a-f]{20}$")
_LOCK = threading.Lock()


def root() -> Path:
    from plexora import paths

    return paths.agent_root() / "artifacts"


def _safe(project: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(project or "_"))[:120] or "_"


def artifact_id(png: bytes, manifest: dict) -> str:
    digest = hashlib.sha256()
    digest.update(png)
    digest.update(json.dumps(manifest, sort_keys=True, default=str).encode("utf-8"))
    return "art_" + digest.hexdigest()[:20]


def _atomic(path: Path, data: bytes):
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def put(project: str, png: bytes, manifest: dict, *, kind: str = "render") -> dict:
    """Store an image and its manifest; returns `{id, uri, path, bytes, kind}`."""
    art = artifact_id(png, manifest)
    folder = root() / _safe(project)
    with _LOCK:
        folder.mkdir(parents=True, exist_ok=True)
        image_path = folder / f"{art}.png"
        if not image_path.exists():
            _atomic(image_path, png)
            sidecar = {"id": art, "project": project, "kind": kind,
                       "created": time.time(), "bytes": len(png), "manifest": manifest}
            _atomic(folder / f"{art}.json",
                    json.dumps(sidecar, default=str, separators=(",", ":")).encode("utf-8"))
    return describe_entry(art, project, len(png), kind)


def describe_entry(art, project, size, kind):
    return {"id": art, "uri": f"plexora://artifact/{art}", "project": project,
            "path": str(root() / _safe(project) / f"{art}.png"), "bytes": size, "kind": kind}


def _find(art: str) -> Path | None:
    if not ID_PATTERN.match(str(art)):
        return None
    base = root()
    if not base.exists():
        return None
    for folder in base.iterdir():
        candidate = folder / f"{art}.json"
        if candidate.exists():
            return candidate
    return None


def get(art: str) -> tuple[bytes, dict]:
    """(png, sidecar). KeyError when there is no such artifact."""
    sidecar_path = _find(art)
    if sidecar_path is None:
        raise KeyError(f"no artifact {art!r}")
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    png = sidecar_path.with_suffix(".png").read_bytes()
    return png, sidecar


def list_artifacts(project: str | None = None, limit: int = 50) -> list:
    base = root()
    if not base.exists():
        return []
    folders = [base / _safe(project)] if project else [p for p in base.iterdir() if p.is_dir()]
    found = []
    for folder in folders:
        if not folder.exists():
            continue
        for sidecar_path in folder.glob("art_*.json"):
            try:
                sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            found.append({"id": sidecar["id"], "project": sidecar.get("project"),
                          "kind": sidecar.get("kind"), "created": sidecar.get("created"),
                          "bytes": sidecar.get("bytes"),
                          "uri": f"plexora://artifact/{sidecar['id']}"})
    found.sort(key=lambda entry: entry.get("created") or 0, reverse=True)
    return found[:limit]


def sweep(*, budget=ARTIFACT_BUDGET_BYTES, max_age_days=ARTIFACT_MAX_AGE_DAYS, now=None) -> int:
    """Delete artifacts past the age limit, then oldest-first past the budget.
    Returns how many were removed."""
    base = root()
    if not base.exists():
        return 0
    now = now or time.time()
    entries = []
    for png in base.glob("*/art_*.png"):
        try:
            stat = png.stat()
        except OSError:
            continue
        entries.append((stat.st_mtime, stat.st_size, png))
    entries.sort()
    removed = 0
    total = sum(size for _, size, _ in entries)
    for mtime, size, png in entries:
        too_old = now - mtime > max_age_days * 86400
        if not too_old and total <= budget:
            continue
        for path in (png, png.with_suffix(".json")):
            try:
                path.unlink()
            except OSError:
                pass
        total -= size
        removed += 1
    return removed


def as_capture_scene(manifest: dict) -> dict:
    """A Figure Builder capture scene for a render, so a figure can adopt the
    exact field an agent showed: the region in full-resolution pixels and the
    channels with their resolved windows. The overlays are listed, not
    reproduced -- a figure re-renders pixels, not cell layers."""
    bounds = manifest["bounds_fullres"]
    channels = []
    for channel in manifest.get("channels", []):
        colour = channel["color"].lstrip("#")
        channels.append({
            "key": channel["key"], "fullname_at_capture": channel["name"],
            "visible": True, "window": list(channel["window"]),
            "color": {"r": int(colour[0:2], 16), "g": int(colour[2:4], 16),
                      "b": int(colour[4:6], 16)},
        })
    return {
        "datasource": manifest["project"],
        "viewport": {"x": bounds["x"], "y": bounds["y"], "w": bounds["width"],
                     "h": bounds["height"]},
        "channels": channels,
        "core_overlays": {"agent_overlays": manifest.get("overlays", [])},
    }
