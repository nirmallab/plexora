"""Benchmark automatic quality control against known truth.

    plexora ai bench qc --synthetic all --agent oracle

Scenes come from `plexora.ai.qc_scenes`: one per injected artifact plus a
mixed one and a clean one. Two arms:

- **detect**: the deterministic scan and detectors alone -- every candidate
  taken as confirmed under its class hint. The ceiling of what numbers find,
  and their false alarms.
- **session**: a full QC session driven by a scripted agent (`QCTruthAgent`)
  that answers from the truth: `oracle` as a careful expert, `lazy` saying
  every channel is clean, `noisy:P` wrong with probability P.

Scores per scene: region recall and precision (a truth region is found when a
region of an accepted class overlaps it at IoU >= `MATCH_IOU` on the map
grid), the mean IoU of matched regions, the pixel-level IoU of the written
(traced) outline and of its envelope, and the share of written pixels on no
artifact (`excess_fraction`: the valid tissue a region takes), channel-status accuracy, cell
precision and recall against the cells the truth says are bad, the false
removal of cells the truth says are fine, and the cost: packets, vision
tokens, seconds. Written as `results.json` + `summary.md`.
"""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import time
from pathlib import Path

import numpy as np

SCENARIOS = {
    "clean": (),
    "blur": ("blur_local",),
    "saturation": ("saturation",),
    "aggregates": ("aggregates",),
    "fold": ("fold",),
    "damage": ("dark_region",),
    "seams": ("tile_seams",),
    "dropout": ("cycle_dropout",),
    "failed_channel": ("empty_channel",),
    "mixed": ("saturation", "fold", "cycle_dropout", "aggregates"),
}
AGENT_STYLES = ("oracle", "lazy", "noisy")
ARMS = ("detect", "session")
MATCH_IOU = 0.3

#: Classes that count as finding a truth region of each class (a fold is
#: often called autofluorescence; lost tissue, damage).
ACCEPTED = {
    "out_of_focus": {"out_of_focus"},
    "saturation_or_clipping": {"saturation_or_clipping"},
    "antibody_aggregate": {"antibody_aggregate", "debris_or_foreign_object"},
    "tissue_fold": {"tissue_fold", "autofluorescence", "air_bubble_or_coverslip"},
    "tissue_damage_or_detachment": {"tissue_damage_or_detachment",
                                    "cycle_specific_tissue_loss"},
    "stitching_or_tile_seam": {"stitching_or_tile_seam"},
    "cycle_specific_tissue_loss": {"cycle_specific_tissue_loss",
                                   "tissue_damage_or_detachment"},
    "illumination_or_shading": {"illumination_or_shading"},
}


def _grid_truth(mask, grid, cover=0.3):
    ny, nx = grid["shape"]
    s = grid["cell_full_px"]
    out = np.zeros((ny, nx), dtype=bool)
    for iy in range(ny):
        for ix in range(nx):
            block = mask[int(iy * s):int((iy + 1) * s), int(ix * s):int((ix + 1) * s)]
            out[iy, ix] = bool(block.size) and block.mean() >= cover
    return out


class QCTruthAgent:
    """Answers QC packets from a scene's truth (the masks it reads from the
    session record stand in for looking at the pictures)."""

    def __init__(self, truth, style="oracle", seed=0):
        self.truth = truth
        self.style, _, p = style.partition(":")
        self.p = float(p) if p else 0.25
        self.rng = np.random.default_rng(seed)
        self._grids = {}

    def _record(self, session_id):
        from plexora.plugins.qc.server.engine import store

        return store().load(session_id)

    def _grid_mask(self, region, grid):
        key = (region["name"], tuple(grid["shape"]))
        if key not in self._grids:
            self._grids[key] = _grid_truth(region["mask"], grid)
        return self._grids[key]

    def _match(self, record, unit):
        from plexora.plugins.qc.server import candidates as cand

        grid = record["scan"][unit["project"]]["grid"]
        mask = cand.decode_mask(unit["mask"])
        best = (0.0, None)
        for region in self.truth["regions"]:
            truth = self._grid_mask(region, grid)
            inter = np.logical_and(mask, truth).sum()
            if not inter:
                continue
            score = max(inter / np.logical_or(mask, truth).sum(), inter / max(1, mask.sum()))
            if set(region["channels"]) & set(unit.get("channels") or []) and score > best[0]:
                best = (score, region)
        return best

    def _flip(self):
        return self.style == "noisy" and self.rng.random() < self.p

    def _variant_ious(self, record, unit, region):
        """IoU with the truth of each outline the session offers."""
        grid = record["scan"][unit["project"]]["grid"]
        truth = self._grid_mask(region, grid)
        out = {}
        for name, variant in (unit.get("variants") or {}).items():
            mask = geometry_to_grid(variant["geometry"], grid)
            union = np.logical_or(mask, truth).sum()
            out[name] = np.logical_and(mask, truth).sum() / union if union else 0.0
        return out

    def _confirm(self, record, unit):
        """One candidate's judgment (the fields of a confirm answer)."""
        truth = self.truth
        score, region = self._match(record, unit)
        if truth["channels"].get(unit.get("channel")) == "failed":
            return {"verdict": "artifact", "artifact_class": "empty_or_failed_channel",
                    "severity": "severe", "boundary": "covers", "scope": "channel",
                    "confidence": "sure"}
        real = region is not None and score >= 0.2
        if self._flip():
            real = not real
        if not real or self.style == "lazy":
            return {"verdict": "not_artifact", "confidence": "sure"}
        klass = (region or {}).get("class") or unit.get("class_hint")
        scope = None
        boundary = "covers"
        if region is not None:
            scope = "all_channels" if len(region["channels"]) >= 5 else \
                ("channel" if len(region["channels"]) == 1 else None)
            ious = self._variant_ious(record, unit, region)
            if ious and max(ious.values()) > ious.get("standard", 0.0) + 0.1:
                boundary = "too_large" if ious.get("tight", 0) > ious.get(
                    "standard", 0) else "too_small"
        return {"verdict": "artifact", "artifact_class": klass, "severity": "severe",
                "boundary": boundary, "scope": scope, "exclude_recommended": True,
                "confidence": "sure"}

    def answer(self, packet, session_id):
        kind = packet["kind"]
        ev = packet["evidence"]
        record = self._record(session_id)
        truth = self.truth
        if kind == "channel_audit":
            verdicts = {}
            for row in ev["rows"]:
                name = row["channel"]
                bad = [r for r in truth["regions"] if name in r["channels"]] or \
                    truth["channels"].get(name) == "failed"
                if self.style == "lazy" or (not bad and not self._flip()):
                    verdicts[name] = {"verdict": "clean"}
                    continue
                named = []
                for c in row.get("candidates") or []:
                    unit = record["units"][f"{ev['project']}::candidate::{c['id']}"]
                    score, _region = self._match(record, unit)
                    if score >= 0.2 or truth["channels"].get(name) == "failed":
                        named.append(c["label"])
                verdicts[name] = {"verdict": "suspicious", "where": named or ["elsewhere"]}
            return {"kind": kind, "verdicts": verdicts}
        units = [record["units"][f"{ref['project']}::{ref['type']}::{ref['id']}"]
                 for ref in packet["units"]]
        unit = units[0]
        if kind == "artifact_confirm":
            if len(units) > 1:
                # A batched first look: one judgment per sheet row, by label.
                return {"kind": kind, "verdicts": {u["label"]: self._confirm(record, u)
                                                   for u in units}}
            return {"kind": kind, **self._confirm(record, unit)}
        if kind == "cell_modules":
            return {"kind": kind, "modules": {
                u["module"]: {"low": "accept", "high": "accept", "confidence": "sure"}
                for u in units}}
        if kind == "artifact_scope":
            _score, region = self._match(record, unit)
            wanted = set(region["channels"]) if region else set()
            best = max(ev["options"], key=lambda o: len(wanted & set(o["channels"]))
                       / max(1, len(wanted | set(o["channels"]))))
            return {"kind": kind, "chosen": best["id"], "confidence": "sure"}
        if kind == "artifact_localize":
            _score, region = self._match(record, unit)
            letters = {a["variant"]: a["id"] for a in ev.get("alternatives") or []}
            if region is None or not letters:
                return {"kind": kind, "chosen": "current", "confidence": "fairly_sure"}
            ious = self._variant_ious(record, unit, region)
            best = max((n for n in ious if n in letters), key=lambda n: ious[n],
                       default="standard")
            return {"kind": kind, "chosen": letters.get(best, "current"),
                    "confidence": "fairly_sure"}
        if kind == "artifact_grid":
            chosen = list(ev.get("pre_selected") or [])[:64] or [ev.get("allowed", ["A1"])[0]]
            return {"kind": kind, "cells": chosen, "confidence": "fairly_sure"}
        if kind == "final_qc_review":
            return {"kind": kind, "verdict": "consistent"}
        if kind in ("cell_intensity", "cell_area", "cycle_stability", "channel_outlier"):
            return {"kind": kind, "low": "accept", "high": "accept", "confidence": "sure"}
        raise ValueError(kind)


# -- the scene on disk ----------------------------------------------------------------------


def register(data_root, name, artifacts, *, size=1024, grid=40, seed=0):
    """Write a QC scene's files and register it; returns what was made."""
    from scipy import ndimage

    from plexora.ai.bench_data import _pyramid
    from plexora.ai.qc_scenes import qc_scene
    from plexora.server.models.project import (ROLE_NAMES, ColumnGroups, ColumnRoles,
                                               DataSpec, ImageSpec, Project,
                                               SegmentationSpec)

    data_root = Path(data_root)
    folder = data_root / f"_{name}_files"
    folder.mkdir(parents=True, exist_ok=True)
    image, labels, cells, channels, truth = qc_scene(size=size, grid=grid,
                                                     artifacts=artifacts, seed=seed)
    image_path = _pyramid(folder / "image.ome.tif", image, levels=4, pixel_size=1.0)
    mask_path = _pyramid(folder / "mask.tif", labels, levels=4, ome=False)
    ids = np.arange(1, len(cells) + 1)
    areas = np.bincount(labels.ravel(), minlength=len(cells) + 1)
    means = {c: ndimage.mean(image[i].astype(np.float64), labels, ids)
             for i, c in enumerate(channels)}
    lines = [",".join(["CellID", "X_centroid", "Y_centroid", "Area", *channels])]
    for index, cell in enumerate(cells):
        lines.append(",".join([str(cell["id"]), f"{cell['x']:.3f}", f"{cell['y']:.3f}",
                               str(int(areas[cell["id"]]))]
                              + [f"{means[c][index]:.4f}" for c in channels]))
    csv_path = folder / "cells.csv"
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    dataset = DataSpec(type="csv", src=str(csv_path),
                       roles=ColumnRoles(cell_id="CellID", x="X_centroid", y="Y_centroid"),
                       columns=ColumnGroups(markers=tuple(channels), metadata=("Area",)),
                       single_image=True)
    image_spec = ImageSpec(
        src=str(image_path), kind="ome_tiff",
        channels=tuple({"name": c, "fullname": c, "src": f"/generated/data/x/{c}/"}
                       for c in channels),
        width=size, height=size, max_level=3, tile_width=128, tile_height=128,
        num_channels=len(channels))
    confirmed = ("table", "segmentation", "markers", "features") + tuple(
        f"role:{role}" for role in ROLE_NAMES)
    record = dataclasses.replace(Project(
        name=name, image=image_spec,
        segmentation=SegmentationSpec(derived=str(mask_path), source=str(mask_path)),
        dataset=dataset, confirmed=confirmed))
    config_path = data_root / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    config[name] = record.to_entry()
    config_path.write_text(json.dumps(config), encoding="utf-8")
    # The cells truth calls bad: in an excluding region (all but the whole-
    # channel classes), or lost.
    bad = set()
    for region in truth["regions"]:
        if region["class"] in ("illumination_or_shading", "stitching_or_tile_seam"):
            continue
        for cell in cells:
            if region["mask"][int(cell["y"]), int(cell["x"])]:
                bad.add(cell["id"])
    bad |= {cid for cid, reasons in truth["cells"].items() if reasons}
    return {"name": name, "cells": cells, "truth": truth, "bad_cells": bad,
            "channels": channels}


# -- scoring ---------------------------------------------------------------------------------


def score_regions(regions, truth, grid):
    """(recall, precision, mean IoU of matches) of predicted regions
    [{class, mask}] against the truth's regions."""
    scored = [r for r in truth["regions"] if r["class"] in ACCEPTED]
    matched_truth, ious = set(), []
    used = set()
    for t_index, region in enumerate(scored):
        truth_mask = _grid_truth(region["mask"], grid)
        best = (0.0, None)
        for p_index, predicted in enumerate(regions):
            if predicted["class"] not in ACCEPTED[region["class"]]:
                continue
            inter = np.logical_and(predicted["mask"], truth_mask).sum()
            union = np.logical_or(predicted["mask"], truth_mask).sum()
            iou = inter / union if union else 0.0
            if iou > best[0]:
                best = (iou, p_index)
        if best[0] >= MATCH_IOU:
            matched_truth.add(t_index)
            used.add(best[1])
            ious.append(best[0])
    recall = len(matched_truth) / len(scored) if scored else None
    # A predicted region is right when most of it lies on a real artifact --
    # a second outline of the same fold is redundant, not false.
    every = np.zeros(tuple(grid["shape"]), dtype=bool)
    for region in truth["regions"]:
        every |= _grid_truth(region["mask"], grid, cover=0.1)
    on = [p for p in regions if p["mask"].any()
          and np.logical_and(p["mask"], every).sum() >= 0.5 * p["mask"].sum()]
    precision = len(on) / len(regions) if regions else (None if not scored else 0.0)
    return recall, precision, (float(np.mean(ious)) if ious else None)


def geometry_to_grid(geometry, grid):
    """A GeoJSON polygon rasterised onto the scan's map grid."""
    from plexora.plugins.qc.server import polygons

    return polygons.geometry_to_grid(geometry, grid)


def geometry_to_pixels(geometry, size):
    """A GeoJSON polygon as a full-resolution mask of a (width, height) image."""
    import cv2
    from shapely.geometry import MultiPolygon, shape

    width, height = size
    canvas = np.zeros((int(height), int(width)), dtype=np.uint8)
    if not geometry:
        return canvas.astype(bool)
    found = shape(geometry)

    def points(coords):
        ring = np.asarray(coords, dtype=np.float64) - 0.5
        return np.round(ring * 16).astype(np.int32).reshape(-1, 1, 2)

    for polygon in (found.geoms if isinstance(found, MultiPolygon) else [found]):
        cv2.fillPoly(canvas, [points(polygon.exterior.coords)], 1, shift=4)
        for hole in polygon.interiors:
            cv2.fillPoly(canvas, [points(hole.coords)], 0, shift=4)
    return canvas.astype(bool)


def score_regions_px(regions, truth, size) -> dict:
    """Pixel-level scores of predicted regions [{class, geometry,
    envelope_geometry}]: the mean IoU of each truth artifact with its best
    written region (`region_iou_px`) and with that region's envelope
    (`envelope_iou_px`), and `excess_fraction` -- the share of written pixels
    on no artifact at all, the valid tissue a region takes."""
    scored = [r for r in truth["regions"] if r["class"] in ACCEPTED]
    every = np.zeros((int(size[1]), int(size[0])), dtype=bool)
    for region in truth["regions"]:
        every |= region.get("pixels", region["mask"])
    written = [(r, geometry_to_pixels(r.get("geometry"), size),
                geometry_to_pixels(r.get("envelope_geometry") or r.get("geometry"), size))
               for r in regions if r.get("geometry")]
    ious, envelope_ious = [], []
    for region in scored:
        target = region.get("pixels", region["mask"])
        best = (0.0, 0.0)
        for predicted, mine, envelope in written:
            if predicted["class"] not in ACCEPTED[region["class"]]:
                continue
            union = np.logical_or(mine, target).sum()
            iou = np.logical_and(mine, target).sum() / union if union else 0.0
            if iou > best[0]:
                union = np.logical_or(envelope, target).sum()
                best = (iou, np.logical_and(envelope, target).sum() / union if union else 0.0)
        if best[0] > 0:
            ious.append(best[0])
            envelope_ious.append(best[1])
    union = np.zeros_like(every)
    for _predicted, mine, _envelope in written:
        union |= mine
    return {"region_iou_px": float(np.mean(ious)) if ious else None,
            "envelope_iou_px": float(np.mean(envelope_ious)) if envelope_ious else None,
            "excess_fraction": float((union & ~every).sum() / union.sum())
            if union.any() else None}


def _regions_of_result(session, project, result, grid):
    """Predicted regions from the active result's ROIs: map masks of the
    envelopes (whether a region was found is judged on the grid, where the
    agent localised it) and the written and envelope geometries (how
    tightly, in pixels)."""
    out = []
    for candidate in (result.get("candidates") or {}).values():
        if candidate.get("action") not in ("exclude", "warn") or not candidate.get("geometry") \
                or candidate.get("state") == "merged":
            continue
        envelope = candidate.get("envelope_geometry") or candidate["geometry"]
        out.append({"class": candidate["class"], "mask": geometry_to_grid(envelope, grid),
                    "geometry": candidate["geometry"], "envelope_geometry": envelope})
    return out


def score_cells(failing, made):
    bad = made["bad_cells"]
    total = {c["id"] for c in made["cells"]}
    good = total - bad
    tp = len(failing & bad)
    return {"cell_recall": tp / len(bad) if bad else None,
            "cell_precision": tp / len(failing) if failing else (None if not bad else 0.0),
            "false_removal": len(failing & good) / len(good) if good else None}


def run_scene(session, made, arm, agent_style, *, seed=0, cell_um=25.0):
    from plexora.agent import invoke, jobs
    from plexora.plugins.qc.server import candidates as cand
    from plexora.plugins.qc.server import results, scan
    from plexora.plugins.qc.server.detectors import DetectorContext, run_all

    project = made["name"]
    started = time.monotonic()
    row = {"arm": arm, "agent": agent_style if arm == "session" else None}
    if arm == "detect":
        result, _ = scan.load_or_run(session, project, params={"cell_um": cell_um})
        raw, _skipped = run_all(DetectorContext(result, project=project))
        built = cand.build(raw, result, project=project)
        regions = [{"class": c.class_hint, "mask": c.mask} for c in built["ranked"]]
        grid = result.grid
        row.update(packets=0, vision_tokens=0, candidates=len(built["ranked"]))
        recall, precision, iou = score_regions(regions, made["truth"], grid)
        row.update(region_recall=recall, region_precision=precision, region_iou=iou)
    else:
        answer = invoke(session, "qc_session_start", {"project": project,
                                                      "map_cell_um": cell_um,
                                                      "on_limit": "extend", "seed": seed,
                                                      "agent": f"bench_{agent_style}"})
        if not answer["ok"]:
            if answer["error"]["code"] == "license_required":
                row.update(skipped="the session arm needs a Paid licence (ai:qc:session)")
                row["seconds"] = round(time.monotonic() - started, 2)
                return row
            raise RuntimeError(answer["error"])
        sid = answer["result"]["session_id"]
        jobs.drain(600)
        agent = QCTruthAgent(made["truth"], agent_style, seed=seed)
        packets = 0
        state = invoke(session, "qc_next", {"session_id": sid, "wait_s": 20})["result"]
        while state.get("state") == "decision" and packets < 400:
            packets += 1
            packet = state["packet"]
            reply = invoke(session, "qc_answer", {"session_id": sid,
                                                  "packet_id": packet["packet_id"],
                                                  "answer": agent.answer(packet, sid)})
            if not reply["ok"]:
                raise RuntimeError(reply["error"])
            state = reply["result"]["next"]
        invoke(session, "qc_session_finish", {"session_id": sid})
        status = invoke(session, "qc_session_status", {"session_id": sid})["result"]
        document = results.load(project)
        result = results.active(document)
        from plexora.plugins.qc.server.engine import store

        grid = store().load(sid)["scan"][project]["grid"]
        regions = _regions_of_result(session, project, result, grid)
        recall, precision, iou = score_regions(regions, made["truth"], grid)
        row.update(score_regions_px(regions, made["truth"], grid["image_size"]))
        cells = results.cells(project)
        failing = set(cells.filter(~cells["pass"])["cell_id"].to_list()) \
            if cells is not None and cells.height else set()
        truth_channels = made["truth"]["channels"]
        channel_states = {u["id"]: u["state"] for u in status["units"]
                          if u.get("type") == "channel"}
        right = sum(1 for name, want in truth_channels.items()
                    if (want == "clean") == (channel_states.get(name) == "clean"))
        from plexora.agent.sessions.budget import vision_tokens

        row.update(packets=packets, vision_tokens=vision_tokens(
                       (status.get("used") or {}).get("pixels", 0)),
                   region_recall=recall, region_precision=precision, region_iou=iou,
                   channel_accuracy=right / len(truth_channels) if truth_channels else None,
                   replayed=status.get("replayed"), **score_cells(failing, made))
        # Reruns from the memo: each scene is scored once, from scratch.
        invoke(session, "qc_session_finish", {"session_id": sid, "action": "rollback"})
    row["seconds"] = round(time.monotonic() - started, 2)
    return row


def run_synthetic(scenarios, agent_style="oracle", *, arms=ARMS, seed=0, size=1024,
                  grid=40) -> list:
    from plexora import paths
    from plexora.agent import AgentSession, registry

    rows = []
    # Windows keeps an open file from being deleted: the readers are closed
    # below, and a straggler only leaves a temporary folder behind.
    with tempfile.TemporaryDirectory(prefix="plexora-bench-qc-",
                                     ignore_cleanup_errors=True) as root:
        previous = os.environ.get("PLEXORA_DATA_PATH")
        os.environ["PLEXORA_DATA_PATH"] = root
        paths.reset()
        try:
            registry.discover(["roi", "qc"])
            session = AgentSession(table_limit=4)
            for index, scenario in enumerate(scenarios):
                name = f"qcbench_{scenario}"
                made = register(root, name, SCENARIOS[scenario], size=size, grid=grid,
                                seed=seed + index)
                for arm in arms:
                    row = run_scene(session, made, arm, agent_style, seed=seed + index)
                    row["scenario"] = scenario
                    rows.append(row)
            session.close()
        finally:
            from plexora.agent import render
            from plexora.server.utils import source_image

            render.close_masks()
            source_image.close_readers()
            if previous is None:
                os.environ.pop("PLEXORA_DATA_PATH", None)
            else:
                os.environ["PLEXORA_DATA_PATH"] = previous
            paths.reset()
    return rows


def _mean(values):
    values = [v for v in values if v is not None]
    return round(float(np.mean(values)), 3) if values else None


def summarise(rows) -> dict:
    out = {}
    for arm in sorted({r["arm"] for r in rows}):
        mine = [r for r in rows if r["arm"] == arm]
        out[arm] = {key: _mean([r.get(key) for r in mine]) for key in (
            "region_recall", "region_precision", "region_iou", "region_iou_px",
            "envelope_iou_px", "excess_fraction", "channel_accuracy",
            "cell_recall", "cell_precision", "false_removal", "packets", "vision_tokens",
            "seconds")}
    return out


def to_markdown(summary, rows, *, title) -> str:
    keys = ("region_recall", "region_precision", "region_iou", "region_iou_px",
            "envelope_iou_px", "excess_fraction", "channel_accuracy",
            "cell_recall", "cell_precision", "false_removal", "packets", "vision_tokens",
            "seconds")
    lines = [f"# {title}", "", "| arm | " + " | ".join(keys) + " |",
             "|---|" + "---|" * len(keys)]
    for arm, values in summary.items():
        lines.append(f"| {arm} | " + " | ".join("-" if values[k] is None else str(values[k])
                                                for k in keys) + " |")
    if rows:
        lines += ["", "## Per scene", "", "| scene | arm | " + " | ".join(keys) + " |",
                  "|---|---|" + "---|" * len(keys)]
        for row in rows:
            lines.append(f"| {row['scenario']} | {row['arm']} | " + " | ".join(
                "-" if row.get(k) is None else str(round(row[k], 3)
                                                   if isinstance(row[k], float) else row[k])
                for k in keys) + " |")
    return "\n".join(lines) + "\n"


def bench_command(*, synthetic=None, agent="oracle", arms=ARMS, out=None, seed=0,
                  emit=print) -> int:
    if not synthetic:
        emit("Nothing to benchmark: pass --synthetic (" + ", ".join(SCENARIOS) + ", or all).")
        return 2
    unknown = [s for s in synthetic if s not in SCENARIOS]
    if unknown:
        emit(f"Unknown QC scenarios: {', '.join(unknown)} (known: {', '.join(SCENARIOS)})")
        return 2
    started = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    rows = run_synthetic(synthetic, agent, arms=arms, seed=seed)
    summary = summarise(rows)
    title = f"Synthetic QC benchmark ({agent} agent)"
    if out is None:
        from plexora import paths

        out = paths.agent_root() / "bench" / f"qc_{started}"
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps({"summary": summary, "rows": rows},
                                                 indent=1, default=str), encoding="utf-8")
    text = to_markdown(summary, rows, title=title)
    (out / "summary.md").write_text(text, encoding="utf-8")
    emit(to_markdown(summary, [], title=title).rstrip())
    skipped = sorted({r["skipped"] for r in rows if r.get("skipped")})
    for reason in skipped:
        emit(f"Skipped: {reason}")
    emit(f"\nWritten: {out / 'results.json'}\n         {out / 'summary.md'}")
    return 0
