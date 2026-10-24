"""Labels: human display windows, the one thing the model learns from.

The user sets windows in Plexora. `pull_plexora` reads them from a project's
`channelList` table (read-only), attaches each channel's IntensityDomain, and
stores them under $AUTOCONTRAST_HOME/labels/<image_id>.parquet. Windows still
at Plexora's default (the whole range) are not labels and are skipped.

`manifest.csv` holds per-image metadata (specimen, patient, tissue, study,
instrument, batch) for the user to fill; splits are by specimen, so two images
of one specimen never land on both sides of a split.
"""

from __future__ import annotations

import ast
import csv
import hashlib
import io as _io
import json
import os
import pickle
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from biological_autocontrast.config import GAMMA, LABELS_VERSION, Config, home

DECISIONS = ("accept", "edit", "reject", "no_signal")
MANIFEST_COLUMNS = ("image_id", "image_path", "specimen", "patient", "tissue", "study",
                    "instrument", "batch", "notes")
SOURCE_PLEXORA = "plexora:channelList"


class _SafeUnpickler(pickle.Unpickler):
    """channelList blobs are lists of plain dicts; refuse anything that would
    construct an object, so a tampered database cannot run code here."""

    _ALLOWED = {("numpy", "dtype"), ("numpy.core.multiarray", "scalar"),
                ("numpy._core.multiarray", "scalar")}

    def find_class(self, module, name):
        if (module, name) in self._ALLOWED:
            return super().find_class(module, name)
        raise pickle.UnpicklingError(f"refusing {module}.{name} in a channelList blob")


def _unpickle(blob):
    return _SafeUnpickler(_io.BytesIO(blob)).load()


def labels_dir() -> Path:
    return home() / "labels"


def manifest_path() -> Path:
    return home() / "manifest.csv"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def label_id(image_id, fingerprint, channel_index, z, t, source) -> str:
    raw = f"{image_id}|{fingerprint}|{channel_index}|{z}|{t}|{source}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def resolve_project(project, data_root=None):
    """(data_root, project name). `project` may be a name, a project folder, or its .db."""
    candidate = Path(str(project)).expanduser()
    if candidate.suffix == ".db" and candidate.exists():
        return candidate.parent.parent, candidate.stem
    if candidate.is_dir() and (candidate / f"{candidate.name}.db").exists():
        return candidate.parent, candidate.name
    if data_root:
        return Path(data_root).expanduser(), str(project)
    env = os.environ.get("PLEXORA_DATA_PATH")
    if env:
        return Path(env).expanduser(), str(project)
    from plexora.paths import data_root as plexora_root

    return Path(plexora_root()), str(project)


def read_plexora_project(project, data_root=None) -> dict:
    """{data_root, project, image_path, channels (zarray order), windows [...], db_mtime}.
    Opens the project database read-only."""
    root, name = resolve_project(project, data_root)
    config_path = root / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(f"no Plexora config.json under {root}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if name not in config:
        raise KeyError(f"{name!r} is not a project in {config_path}")
    entry = config[name]
    image_data = entry.get("imageData") or []
    if isinstance(image_data, str):
        image_data = ast.literal_eval(image_data)
    # data_model.real_channels: 'Area' is Plexora's mask placeholder, never an
    # image channel, so a position in this list is the zarray index.
    channels = [d.get("fullname") or d.get("name") for d in image_data
                if (d.get("fullname") or d.get("name")) != "Area"]
    db_path = root / name / f"{name}.db"
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    uri = "file:" + str(db_path).replace("?", "%3f") + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        columns = [r[1] for r in db.execute("PRAGMA table_info(channelList)")]
        rows = db.execute("SELECT * FROM channelList").fetchall()
    windows = []
    for row in rows:
        record = dict(zip(columns, row))
        for value in record.values():
            if isinstance(value, (bytes, bytearray)):
                items = _unpickle(bytes(value))
                if isinstance(items, list):
                    windows.extend(i for i in items if isinstance(i, dict))
    image_path = entry.get("channelFile")
    image = (entry.get("resources") or {}).get("image") or {}
    if not image_path and image.get("provider") == "node":
        # Held by a data node: read through the node's raw region route.
        from biological_autocontrast.io import register_plexora_root

        register_plexora_root(root)
        image_path = f"node://{image['node']}/{image['resource_id']}"
    return {"data_root": str(root), "project": name, "image_path": image_path,
            "image_kind": entry.get("image_kind"), "channels": channels, "windows": windows,
            "db_path": str(db_path), "db_mtime": db_path.stat().st_mtime}


def _is_default(start, end, dom) -> bool:
    """Plexora's untouched window: [0, 65535|65536] or the whole domain."""
    if start <= max(0.0, dom.offset) and end >= dom.full_scale:
        return True
    return start <= 0 and end >= 65535


def _record(*, image_id, image_path, fingerprint, channel_index, channel_name, file_channel_name,
            z, t, dom, low, high, decision, source, annotator, weight=1.0, proposal=None,
            rationale_tags=(), rationale_text="", extra=None):
    from biological_autocontrast.domain import to_norm

    low, high = float(low), float(high)
    if not high > low:
        raise ValueError(f"{channel_name}: window max {high:g} is not above min {low:g}")
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}")
    record = {
        "label_id": label_id(image_id, fingerprint, channel_index, z, t, source),
        "image_id": image_id, "image_path": str(image_path), "image_fingerprint": fingerprint,
        "channel_index": int(channel_index), "channel_name": channel_name,
        "file_channel_name": file_channel_name, "z": int(z), "t": int(t),
        **{k: v for k, v in dom.to_dict().items() if k not in ("version", "warnings")},
        "domain_version": dom.version, "domain_warnings": json.dumps(dom.warnings),
        "display_min_raw": low, "display_max_raw": high,
        "display_min_norm": to_norm(low, dom), "display_max_norm": to_norm(high, dom),
        "gamma": GAMMA, "decision": decision,
        "proposal_min_raw": (proposal or {}).get("min_raw"),
        "proposal_max_raw": (proposal or {}).get("max_raw"),
        "proposal_method": (proposal or {}).get("method"),
        "rationale_tags": json.dumps(list(rationale_tags)), "rationale_text": rationale_text,
        "source": source, "annotator": annotator, "created_at": _now(), "weight": float(weight),
        "label_version": LABELS_VERSION,
    }
    record.update(extra or {})
    return record


def _domains(image_path, indices, z=0, t=0, assume_bits=None, cfg=None):
    from biological_autocontrast import features as feat
    from biological_autocontrast.io import open_image

    cfg = cfg or Config()
    out = {}
    with open_image(image_path) as image:
        ctx = feat.ImageContext.build(image, z, t, cfg)
        for c in indices:
            f = feat.extract_channel(ctx, c, assume_bits=assume_bits, with_patches=False)
            out[c] = (f.domain, image.channels[c])
        fingerprint = image.fingerprint()
    return out, fingerprint


def _write_image_labels(image_id, records, replace_source):
    import pandas as pd

    path = labels_dir() / f"{_safe(image_id)}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(records)
    if path.exists():
        old = pd.read_parquet(path)
        if replace_source is not None:
            old = old[old["source"] != replace_source]
        else:
            old = old[~old["label_id"].isin(frame["label_id"])]
        frame = pd.concat([old, frame], ignore_index=True)
    tmp = path.with_suffix(".tmp.parquet")
    frame.to_parquet(tmp, index=False)
    tmp.replace(path)
    return path


def _safe(name) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(name))


def ensure_manifest(image_id, image_path):
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = read_manifest()
    if any(r["image_id"] == image_id for r in rows):
        return path
    rows.append({"image_id": image_id, "image_path": str(image_path), "specimen": image_id,
                 **{k: "" for k in MANIFEST_COLUMNS if k not in ("image_id", "image_path", "specimen")}})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in MANIFEST_COLUMNS})
    return path


def read_manifest() -> list:
    path = manifest_path()
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def pull_plexora(project, annotator, *, data_root=None, image_path=None, channels=None,
                 assume_bits=None, cfg=None) -> dict:
    """Labels from a Plexora project's saved windows. Returns a summary."""
    info = read_plexora_project(project, data_root)
    image_path = image_path or info["image_path"]
    if not image_path:
        raise ValueError(f"project {info['project']} names no image file")
    names = info["channels"]
    wanted, skipped = {}, []
    for window in info["windows"]:
        name = window.get("channel")
        if channels and name not in channels:
            continue
        if name not in names:
            skipped.append({"channel": name, "reason": "not an image channel of this project"})
            continue
        try:
            start, end = float(window["start"]), float(window["end"])
        except (KeyError, TypeError, ValueError):
            skipped.append({"channel": name, "reason": "no start/end"})
            continue
        wanted[names.index(name)] = (name, start, end)
    domains, fingerprint = _domains(image_path, sorted(wanted), assume_bits=assume_bits, cfg=cfg)
    image_id = info["project"]
    records = []
    for index in sorted(wanted):
        name, start, end = wanted[index]
        dom, file_name = domains[index]
        if _is_default(start, end, dom):
            skipped.append({"channel": name, "reason": f"default window {start:g}..{end:g}"})
            continue
        if not end > start:
            skipped.append({"channel": name, "reason": f"empty window {start:g}..{end:g}"})
            continue
        records.append(_record(
            image_id=image_id, image_path=image_path, fingerprint=fingerprint,
            channel_index=index, channel_name=name, file_channel_name=file_name, z=0, t=0,
            dom=dom, low=start, high=end, decision="edit", source=SOURCE_PLEXORA,
            annotator=annotator,
            extra={"project": info["project"], "data_root": info["data_root"],
                   "observed_at": datetime.fromtimestamp(info["db_mtime"], timezone.utc)
                   .isoformat(timespec="seconds")}))
    path = _write_image_labels(image_id, records, SOURCE_PLEXORA) if records else None
    ensure_manifest(image_id, image_path)
    return {"image_id": image_id, "image_path": image_path, "labels": len(records),
            "skipped": skipped, "written": str(path) if path else None,
            "manifest": str(manifest_path())}


def add_label(image_path, channel, low, high, *, annotator, decision="edit", image_id=None,
              z=0, t=0, assume_bits=None, rationale_tags=(), rationale_text="", weight=1.0,
              cfg=None) -> dict:
    from biological_autocontrast.io import open_image

    with open_image(image_path) as image:
        index = image.channel_index(int(channel) if str(channel).isdigit() else channel)
    domains, fingerprint = _domains(image_path, [index], z, t, assume_bits, cfg)
    dom, file_name = domains[index]
    image_id = image_id or Path(image_path).name
    if decision == "no_signal" and (low is None or high is None):
        low, high = dom.offset, dom.full_scale
    record = _record(image_id=image_id, image_path=image_path, fingerprint=fingerprint,
                     channel_index=index, channel_name=file_name, file_channel_name=file_name,
                     z=z, t=t, dom=dom, low=low, high=high, decision=decision, source="cli",
                     annotator=annotator, weight=weight, rationale_tags=rationale_tags,
                     rationale_text=rationale_text)
    _write_image_labels(image_id, [record], None)
    ensure_manifest(image_id, image_path)
    return record


def load_all():
    import pandas as pd

    files = sorted(labels_dir().glob("*.parquet"))
    files = [f for f in files if not f.name.endswith(".tmp.parquet")]
    if not files:
        return pd.DataFrame()
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


def build_index(out=None):
    """Every label joined with its manifest row -> labels_index.parquet."""
    import pandas as pd

    frame = load_all()
    manifest = pd.DataFrame(read_manifest())
    if not frame.empty and not manifest.empty:
        meta = manifest.drop(columns=["image_path"], errors="ignore")
        frame = frame.merge(meta, on="image_id", how="left")
    if not frame.empty:
        if "specimen" not in frame:
            frame["specimen"] = frame["image_id"]
        frame["specimen"] = frame["specimen"].replace("", np.nan).fillna(frame["image_id"])
    out = Path(out) if out else home() / "labels_index.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(out, index=False)
    return out, frame


def export_labels(out, fmt="csv"):
    frame = load_all()
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "csv":
        frame.to_csv(out, index=False)
    elif fmt == "json":
        out.write_text(frame.to_json(orient="records", indent=1), encoding="utf-8")
    elif fmt == "parquet":
        frame.to_parquet(out, index=False)
    else:
        raise ValueError(f"unknown format {fmt!r}")
    return out, len(frame)


#: Phase 2 hook: pairwise preferences ("A looks better than B"). Schema only.
PAIR_COLUMNS = ("label_id_a", "label_id_b", "preferred", "neither_ok", "annotator", "created_at")
