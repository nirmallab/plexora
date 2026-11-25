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
    "dropout": ("cycle_dropout",),
    "failed_channel": ("empty_channel",),
    "mixed": ("saturation", "fold", "cycle_dropout", "aggregates"),
    # What QC finds about a channel that is no region (`qc_scenes.
    # CHANNEL_FINDING_ARTIFACTS`).
    "edge_rim": ("edge_rim",),
    "broad_background": ("broad_background",),
    # A sixteen-channel panel of four cycles (`qc_scenes.cyclic_scene`):
    # twelve stain checks and a registration review per cycle -- the width a
    # run's lanes fill.
    "cyclic": ("cycle_fold",),
}
#: Scenarios drawn by another scene builder than `qc_scene` (name in qc_scenes).
SCENE_BUILDERS = {"cyclic": "cyclic_scene"}


def _builder(scenario):
    from plexora.ai import qc_scenes

    name = SCENE_BUILDERS.get(scenario)
    return getattr(qc_scenes, name) if name else None


class SchemaKeyedCache:
    """Mixed in ahead of `spatialagents.testing.FakeGateway`: its prompt-cache
    emulation keyed on the answer schema, as the provider's is (a format prompt
    sits ahead of the system prompt, so each `output_schema` writes the prefix
    once). spatialagents' own FakeGateway does this after 0.2.6 (a62d0d4); drop
    this when Plexora pins a release that has it."""

    def _usage(self, request: dict) -> dict:
        import hashlib

        from spatialagents.testing import _marked, _tokens, _unmarked
        from spatialagents.wire import canonical

        tools = [request["tools"]] if request.get("tools") else []
        segments = tools + [request.get("system") or []] + list(request["messages"])
        running = hashlib.sha256()
        keys = []
        for index, segment in enumerate(segments):
            if index == len(tools) and request.get("output_schema") is not None:
                running.update(canonical({"output_schema": request["output_schema"]}).encode())
            running.update(canonical(_unmarked(segment)).encode())
            keys.append(running.hexdigest())
        tokens = [_tokens(segment) for segment in segments]
        with self.lock:
            hit = max((i + 1 for i, key in enumerate(keys) if key in self.seen_prefixes), default=0)
            for segment, key in zip(segments, keys):
                if _marked(segment):
                    self.seen_prefixes.add(key)
        return {"input_uncached": 3, "cache_read": sum(tokens[:hit]), "cache_write_5m": sum(tokens[hit:]),
                "cache_write_1h": 0}


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


class _AgentText:
    """The literal text the scripted agent receives in the session arm --
    every `qc_next` / `qc_answer` result, the start result, the status calls
    the bench makes and the finish result -- summed into a rough token count
    (chars/4) and broken down by decision-packet kind (how many of each kind
    were shown, and how large each packet's own JSON was). Image counts and
    vision tokens are not re-derived from this text: the session already
    charges each packet's images (`budgets.packet_cost`) into its own
    `used.images` / `used.pixels`, read back from the final `qc_session_status`
    call instead."""

    def __init__(self):
        self.chars = 0
        self.by_kind = {}

    def see(self, result):
        """One tool result the agent just received (the whole payload, not
        just its packet) -- added to the running total; if it carries a
        packet (directly, from `qc_next`, or under `next`, from `qc_answer`),
        tally that packet's own size under its kind too.

        `_images` is carried on the raw in-process result this bench calls
        `invoke()` to get (`SessionTools.packet_result` / `.apply`), but it is
        never text the agent reads: `plexora.mcp.tools.split_images` pops it
        off every real tool result and attaches it as actual image content
        instead (`plexora/agent/jobs.py` strips it from a job's stored result
        the same way). Its bytes are what `vision_tokens` already prices, so
        it is excluded here rather than double-counted as text."""
        text_only = {k: v for k, v in result.items() if k != "_images"}
        self.chars += len(json.dumps(text_only, default=str))
        nxt = result.get("next")
        packet = result.get("packet") or (nxt.get("packet") if isinstance(nxt, dict) else None)
        if packet:
            entry = self.by_kind.setdefault(packet.get("kind", "?"), {"count": 0, "chars": 0})
            entry["count"] += 1
            entry["chars"] += len(json.dumps(packet, default=str))

    def cost(self, *, vision_tokens) -> dict:
        text_tokens_est = round(self.chars / 4)
        return {"text_chars": self.chars, "text_tokens_est": text_tokens_est,
                "total_tokens_est": text_tokens_est + vision_tokens,
                "cost_by_kind": self.by_kind}


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
                    if isinstance(c, str):  # a label; described once in ev["candidates"]
                        c = {"label": c, "id": ev["candidates"][c]["id"]}
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
            return {"kind": kind, "cells": chosen, "severity": "moderate",
                    "confidence": "fairly_sure"}
        if kind == "final_qc_review":
            return {"kind": kind, "verdict": "consistent"}
        if kind == "score_review":
            return self._score_review(unit, ev)
        if kind == "visual_scan":
            # The bench draws nothing by eye: the detectors are what it scores.
            return {"kind": kind, "status": "nothing_found"}
        if kind == "stain_check":
            # The synthetic images carry no stain truth: every stain reads real.
            return {"kind": kind, "expectation": "", "observed": "", "structure": "yes",
                    "verdict": "real", "why": "the bench scores artifacts, not stains"}
        raise ValueError(kind)

    #: The class of the painted region each image check should find.
    CHECK_TRUTH = {"blur": "out_of_focus", "registration": "cross_cycle_registration_error"}

    def _score_review(self, unit, ev):
        """A check's rows by where their places fall: inside a painted region
        of the check's class (in its channel) reads as the artifact. A lazy
        agent calls every row normal; a noisy one flips rows."""
        if unit.get("check") == "artifacts":
            # The Artifact Detector (on by default) reviews every channel: any
            # painted physical artifact is what its places should show.
            from plexora.plugins.qc.server import schemas

            regions = [r for r in self.truth["regions"]
                       if schemas.category_of_class(r["class"]) == "tissue_acquisition"]
        else:
            target = self.CHECK_TRUTH.get(unit.get("check"))
            regions = [r for r in self.truth["regions"] if r["class"] == target
                       and unit.get("channel") in r["channels"]]
        strata = {}
        for stratum, places in ((unit.get("shown") or {}).get("places") or {}).items():
            inside = 0
            for place in places:
                x, y = int(place["x"]), int(place["y"])
                inside += any(0 <= y < r["mask"].shape[0] and 0 <= x < r["mask"].shape[1]
                              and r["mask"][y, x] for r in regions)
            share = inside / max(1, len(places))
            verdict = "artifact" if share >= 0.6 else ("normal" if share <= 0.2 else "mixed")
            if self.style == "lazy":
                verdict = "normal"
            elif self._flip():
                verdict = {"artifact": "normal", "normal": "artifact"}.get(verdict, verdict)
            strata[stratum] = verdict
        answer = {"kind": "score_review", "strata": strata, "threshold": "accept",
                  "severity": "severe", "confidence": "sure"}
        if (ev.get("global") or {}).get("possible"):
            everywhere = unit.get("check") == "blur" and self.truth.get("global_blur")
            answer["whole_tissue"] = "artifact" if everywhere and self.style != "lazy" \
                else "normal"
        return answer


# -- the scene on disk ----------------------------------------------------------------------


def register(data_root, name, artifacts, *, size=1024, grid=40, seed=0, scene=None):
    """Write a QC scene's files and register it; returns what was made.
    `scene`: the builder (`qc_scenes.qc_scene` by default; `cyclic_scene`
    for a sixteen-channel, four-cycle panel)."""
    from scipy import ndimage

    from plexora.ai.bench_data import _pyramid
    from plexora.ai.qc_scenes import qc_scene
    from plexora.server.models.project import (ROLE_NAMES, ColumnGroups, ColumnRoles,
                                               DataSpec, ImageSpec, Project,
                                               SegmentationSpec)

    data_root = Path(data_root)
    folder = data_root / f"_{name}_files"
    folder.mkdir(parents=True, exist_ok=True)
    image, labels, cells, channels, truth = (scene or qc_scene)(size=size, grid=grid,
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
    # channel classes, and staining -- judged per channel, never a region),
    # or lost. A region out of focus in a cycle the cells were not segmented
    # from leaves the cells standing and makes that cycle's markers
    # unreliable in them (`bad_marker_cells`): focus is per acquisition.
    from plexora.plugins.qc.server import cycles as cycle_rules
    from plexora.plugins.qc.server import schemas

    segmentation = next((c for c in channels if cycle_rules.is_nuclear(c)), None)
    bad = set()
    bad_markers = {}
    for region in truth["regions"]:
        if region["class"] == "illumination_or_shading" \
                or region["class"] in schemas.STAINING_REGION_CLASSES:
            continue
        marker_level = region["class"] == "out_of_focus" \
            and segmentation not in (region.get("channels") or ())
        for cell in cells:
            if region["mask"][int(cell["y"]), int(cell["x"])]:
                if marker_level:
                    bad_markers.setdefault(cell["id"], set()).update(region["channels"])
                else:
                    bad.add(cell["id"])
    bad |= {cid for cid, reasons in truth["cells"].items() if reasons}
    return {"name": name, "cells": cells, "truth": truth, "bad_cells": bad,
            "bad_marker_cells": bad_markers, "channels": channels}


# -- scoring ---------------------------------------------------------------------------------


def score_regions(regions, truth, grid, *, skip=()):
    """(recall, precision, mean IoU of matches) of predicted regions
    [{class, mask}] against the truth's regions (but those of a class in
    `skip`: a session never outlines staining)."""
    scored = [r for r in truth["regions"] if r["class"] in ACCEPTED and r["class"] not in skip]
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


def score_regions_px(regions, truth, size, *, skip=()) -> dict:
    """Pixel-level scores of predicted regions [{class, geometry,
    envelope_geometry}]: the mean IoU of each truth artifact with its best
    written region (`region_iou_px`) and with that region's envelope
    (`envelope_iou_px`), and `excess_fraction` -- the share of written pixels
    on no artifact at all, the valid tissue a region takes."""
    scored = [r for r in truth["regions"] if r["class"] in ACCEPTED and r["class"] not in skip]
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
    candidates = result.get("candidates") or {}
    for candidate in candidates.values():
        if candidate.get("action") not in ("exclude", "warn") or not candidate.get("geometry") \
                or candidate.get("state") == "merged" or candidate.get("consolidated_into"):
            continue
        if candidate.get("findings"):
            # A consolidated ROI holds a whole category (one per category and
            # action): it is scored by its findings, each its own region -- the
            # outline the cells follow (`roi_link.membership_meta`) -- with the
            # envelope it was found in. Scored as one, a layer of three folds
            # would match each fold at a third of its IoU.
            for finding in candidate["findings"]:
                source = candidates.get(finding.get("candidate_id")) or {}
                if (source.get("action") or finding.get("action")) not in ("exclude", "warn"):
                    continue
                geometry = finding["geometry"]
                envelope = source.get("envelope_geometry") or geometry
                out.append({"class": finding["class"], "mask": geometry_to_grid(envelope, grid),
                            "geometry": geometry, "envelope_geometry": envelope})
            continue
        envelope = candidate.get("envelope_geometry") or candidate["geometry"]
        out.append({"class": candidate["class"], "mask": geometry_to_grid(envelope, grid),
                    "geometry": candidate["geometry"], "envelope_geometry": envelope})
    return out


def staining_scores(truth, channel_states, cells, findings=None) -> dict:
    """Staining is judged per channel: `staining_channel_recall` is the share
    of channels holding a painted staining artifact the session did not call
    clean; `failed_marker_flagged` the share of failed channels whose marker
    is unreliable in (nearly) every cell; `channel_finding_recall` the share
    of painted channel findings (a rim, a broad background) the result's
    `cells.channel_findings` holds, and `rim_cell_recall` / `_precision` the
    rim warnings against the painted rim cells. None where the scene has
    none of them."""
    from plexora.plugins.qc.server import schemas

    stained = {c for r in truth["regions"] if r["class"] in schemas.STAINING_REGION_CLASSES
               for c in r["channels"]}
    failed = [n for n, want in (truth.get("channels") or {}).items() if want == "failed"]
    out = {"staining_channel_recall": None, "failed_marker_flagged": None}
    if stained:
        out["staining_channel_recall"] = sum(
            1 for c in stained if channel_states.get(c) not in (None, "clean")) / len(stained)
    if failed:
        rows = cells["unreliable_markers"].to_list() if cells is not None and cells.height \
            else []
        hit = 0
        for name in failed:
            share = sum(1 for r in rows if name in (r or [])) / len(rows) if rows else 0.0
            hit += share >= 0.99
        out["failed_marker_flagged"] = hit / len(failed)
    painted = truth.get("channel_findings") or {}
    if painted:
        found = findings or {}
        got = 0
        for marker, kind in painted.items():
            entry = (found.get(marker) or {}).get(kind) or {}
            got += bool(entry.get("borne_out") if kind == "rim" else entry.get("flagged"))
        out["channel_finding_recall"] = got / len(painted)
    for marker, ids in (truth.get("rim_cells") or {}).items():
        warned = set()
        if cells is not None and cells.height and "marker_flags" in cells.columns:
            label = f"{marker}|{schemas.RIM_REASON}|warn"
            for cid, flags in zip(cells["cell_id"].to_list(), cells["marker_flags"].to_list()):
                if label in (flags or ()):
                    warned.add(int(cid))
        out["rim_cell_recall"] = len(warned & ids) / len(ids) if ids else None
        out["rim_cell_precision"] = len(warned & ids) / len(warned) if warned else None
    return out


def score_cells(failing, made):
    bad = made["bad_cells"]
    total = {c["id"] for c in made["cells"]}
    good = total - bad
    tp = len(failing & bad)
    return {"cell_recall": tp / len(bad) if bad else None,
            "cell_precision": tp / len(failing) if failing else (None if not bad else 0.0),
            "false_removal": len(failing & good) / len(good) if good else None}


#: The marker reasons a marker-level truth region is called by.
MARKER_TRUTH_REASONS = ("region:out_of_focus",)


def score_markers(cells, made):
    """Recall and precision of the (cell, marker) pairs the truth calls
    unreliable while the cell stands (`bad_marker_cells`): recall over every
    flag that excludes or warns; precision over the flags of the classes such
    truth is made of (`MARKER_TRUTH_REASONS`), so a scene's staining flags do
    not count against it."""
    truth = {(cid, m) for cid, markers in (made.get("bad_marker_cells") or {}).items()
             for m in markers}
    if cells is None or not cells.height or "marker_flags" not in cells.columns:
        return {"marker_recall": 0.0 if truth else None, "marker_precision": None}
    flagged, called = set(), set()
    for cid, entries in zip(cells["cell_id"].to_list(), cells["marker_flags"].to_list()):
        for entry in entries or ():
            marker, reason, status = (entry.split("|") + ["", ""])[:3]
            if status not in ("exclude", "warn"):
                continue
            flagged.add((int(cid), marker))
            if reason in MARKER_TRUTH_REASONS:
                called.add((int(cid), marker))
    return {"marker_recall": len(flagged & truth) / len(truth) if truth else None,
            "marker_precision": len(called & truth) / len(called) if called else None}


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
        agent_text = _AgentText()
        answer = invoke(session, "qc_session_start", {"project": project,
                                                      "map_cell_um": cell_um,
                                                      "on_limit": "extend", "seed": seed,
                                                      "agent": f"bench_{agent_style}"})
        if not answer["ok"]:
            if answer["error"]["code"] == "license_required":
                row.update(skipped="the session arm needs a Pro licence (ai:qc:session)")
                row["seconds"] = round(time.monotonic() - started, 2)
                return row
            raise RuntimeError(answer["error"])
        agent_text.see(answer["result"])
        sid = answer["result"]["session_id"]
        jobs.drain(600)
        agent = QCTruthAgent(made["truth"], agent_style, seed=seed)
        packets = 0
        next_answer = invoke(session, "qc_next", {"session_id": sid, "wait_s": 20})
        agent_text.see(next_answer["result"])
        state = next_answer["result"]
        while state.get("state") == "decision" and packets < 400:
            packets += 1
            packet = state["packet"]
            reply = invoke(session, "qc_answer", {"session_id": sid,
                                                  "packet_id": packet["packet_id"],
                                                  "answer": agent.answer(packet, sid)})
            if not reply["ok"]:
                raise RuntimeError(reply["error"])
            agent_text.see(reply["result"])
            state = reply["result"]["next"]
        finish_answer = invoke(session, "qc_session_finish", {"session_id": sid})
        agent_text.see(finish_answer["result"])
        status_answer = invoke(session, "qc_session_status", {"session_id": sid,
                                                              "units": "all",
                                                              "detail": "full"})
        agent_text.see(status_answer["result"])
        status = status_answer["result"]
        document = results.load(project)
        result = results.active(document)
        from plexora.plugins.qc.server.engine import store

        grid = store().load(sid)["scan"][project]["grid"]
        regions = _regions_of_result(session, project, result, grid)
        from plexora.plugins.qc.server import schemas

        staining = schemas.STAINING_REGION_CLASSES
        recall, precision, iou = score_regions(regions, made["truth"], grid, skip=staining)
        row.update(score_regions_px(regions, made["truth"], grid["image_size"],
                                    skip=staining))
        cells = results.cells(project)
        failing = set(cells.filter(~cells["pass"])["cell_id"].to_list()) \
            if cells is not None and cells.height else set()
        truth_channels = made["truth"]["channels"]
        channel_states = {u["id"]: u["state"] for u in status["units"]
                          if u.get("type") == "channel"}
        # A whole-tissue region (a fold, a cycle lost) reaches the channels it
        # covers without flagging them (`finalize.channel_effects`): reached
        # is not clean either.
        reached = {c["name"] for c in (result or {}).get("channels") or []
                   if c.get("reached_by")}
        right = sum(1 for name, want in truth_channels.items()
                    if (want == "clean") == (channel_states.get(name) == "clean"
                                             and name not in reached))
        row.update(staining_scores(made["truth"], channel_states, cells,
                                   findings=((result or {}).get("cells") or {})
                                   .get("channel_findings")))
        from plexora.agent.sessions.budget import vision_tokens

        used = status.get("used") or {}
        vtokens = vision_tokens(used.get("pixels", 0))
        row.update(packets=packets, vision_tokens=vtokens, images=used.get("images", 0),
                   region_recall=recall, region_precision=precision, region_iou=iou,
                   channel_accuracy=right / len(truth_channels) if truth_channels else None,
                   replayed=status.get("replayed"), **score_cells(failing, made),
                   **score_markers(cells, made),
                   **agent_text.cost(vision_tokens=vtokens))
        # Reruns from the memo: each scene is scored once, from scratch.
        invoke(session, "qc_session_finish", {"session_id": sid, "action": "rollback"})
    row["seconds"] = round(time.monotonic() - started, 2)
    return row


#: Median seconds a model call of each QC task took in the local trace's QC
#: runs (13 runs, 2026-10..11; `plexora ai trace`): what the `harness` arm's
#: stand-in model waits before it answers, scaled by `latency_scale`. A stain
#: check reads one large picture with high effort -- most of a run's model time.
TASK_LATENCY_S = {"qc.stain_check": 104.0, "qc.stain_reader_2": 104.0, "qc.stain_reader_3": 104.0,
                  "qc.artifact_inspection": 9.9, "qc.roi_refinement": 9.9, "qc.blur": 4.7,
                  "qc.registration": 4.8, "qc.artifacts": 4.8, "qc.planning": 7.1,
                  "qc.visual_scan": 11.1, "qc.final_review": 3.8}
DEFAULT_LATENCY_S = 8.0


def _task_of_wire(task):
    """`plexora.qc.stain_check` -> `qc.stain_check`."""
    return (task or "").split(".", 1)[1] if (task or "").count(".") >= 2 else (task or "")


def result_signature(project, session_id) -> dict:
    """What a QC run decided, without ids, labels or order: the regions of the
    active result (class, action, channels, area and box of the outline), each
    channel's state, each stain verdict and the failing cells. Two runs that
    decided the same thing have equal signatures."""
    import hashlib

    from plexora.plugins.qc.server import results
    from plexora.plugins.qc.server.engine import store

    record = store().load(session_id)
    result = results.active(results.load(project)) or {}
    def shape(geometry):
        """The outline's hash, its coordinates rounded to a tenth of a pixel."""
        def rounded(value):
            if isinstance(value, (list, tuple)):
                return [rounded(v) for v in value]
            return round(float(value), 1) if isinstance(value, (int, float)) else value
        coords = rounded((geometry or {}).get("coordinates"))
        return hashlib.sha1(json.dumps(coords).encode()).hexdigest()[:12]

    regions = []
    for candidate in (result.get("candidates") or {}).values():
        if candidate.get("state") == "merged" or candidate.get("consolidated_into"):
            continue
        regions.append([candidate.get("class"), candidate.get("action"), candidate.get("state"),
                        sorted(candidate.get("channels") or []), shape(candidate.get("geometry")),
                        sorted(shape(f.get("geometry")) for f in candidate.get("findings") or [])])
    regions.sort(key=lambda r: json.dumps(r))
    units = record.get("units") or {}
    channels = {u["id"]: u["state"] for u in units.values() if u.get("type") == "channel"}
    stains = {u.get("channel"): u.get("verdict") for u in units.values()
              if u.get("type") == "check" and u.get("check") == "stain"}
    # How each check ended: one skipped (failed to score) is a different run
    # even when no region shows it.
    checks = {u["id"]: u["state"] for u in units.values() if u.get("type") == "check"}
    cells = results.cells(project)
    failing = sorted(int(c) for c in cells.filter(~cells["pass"])["cell_id"].to_list()) \
        if cells is not None and cells.height else []
    return {"regions": regions, "channels": channels, "stains": stains, "checks": checks,
            "failing_cells": len(failing),
            "failing_hash": hashlib.sha1(json.dumps(failing).encode()).hexdigest()[:12]}


def run_harness(session, made, lanes, agent_style, *, seed=0, cell_um=25.0, latency_scale=0.02,
                jitter=0.0, calls_per_min=None) -> dict:
    """The `harness:N` arm: Plexora's own AI harness (`QCRun`) answering a QC
    session with N lanes, against a stand-in gateway whose model waits each
    task's real median latency (`TASK_LATENCY_S` x `latency_scale`, +-`jitter`
    share at random) and then answers from the truth (`QCTruthAgent`). What it
    measures: the run's wall time, its model calls, tokens and prefix writes,
    the packets out at once, and a signature of what it decided -- which must
    not depend on the lanes."""
    import random
    import threading

    from spatialagents.testing import FakeGateway as _Fake

    from plexora.ai.harness.decision import QCOptions, QCRun
    from plexora.ai.harness.gateway import GatewayClient, TokenSource
    from plexora.ai.harness.trace import TraceStore

    class Gateway(SchemaKeyedCache, _Fake):
        PRODUCT = "plexora"
        PRICES = {"gating": ("marker", 25), "qc": ("channel", 12)}

    agent = QCTruthAgent(made["truth"], agent_style, seed=seed)
    rng = random.Random(seed)
    lock = threading.Lock()
    busy = {"now": 0, "peak": 0, "model_s": 0.0}
    timeline: list = []
    started = time.monotonic()

    def brain(packet, body):
        task = _task_of_wire(body.get("task"))
        wait = TASK_LATENCY_S.get(task, DEFAULT_LATENCY_S) * latency_scale
        if jitter:
            with lock:
                wait *= 1.0 + rng.uniform(-jitter, jitter)
        with lock:
            busy["now"] += 1
            busy["peak"] = max(busy["peak"], busy["now"])
        began = time.monotonic() - started
        try:
            time.sleep(max(0.0, wait))
            return agent.answer(packet, body["context"]["session_id"])
        finally:
            with lock:
                busy["now"] -= 1
                busy["model_s"] += wait
                timeline.append([round(began, 2), round(time.monotonic() - started, 2),
                                 packet.get("packet_id"), packet.get("kind"),
                                 [u.get("id") for u in packet.get("units") or ()]])

    project = made["name"]
    with tempfile.TemporaryDirectory(prefix="pxbench-trace-", ignore_cleanup_errors=True) as tdir, \
            Gateway(brain) as gateway:
        client = GatewayClient(gateway.url, tokens=TokenSource("BIOCAI1.test"), sleep=lambda s: None)
        # Every arm asks the model afresh: answers are never replayed from
        # another arm's memo (`reuse_answers`).
        options = QCOptions(project=project, parallel_markers=None if lanes is None else max(1, int(lanes)),
                            start_options={"map_cell_um": cell_um, "on_limit": "extend",
                                           "seed": seed, "reuse_answers": False, "mirror": False})
        # The stand-in gateway has no per-device rate: the harness's pace
        # (`decision._Pace`, the real gateway's 120 a minute) is lifted, or
        # scaled-down runs would measure the pace, not the lanes.
        from plexora.plugins.qc.server import sheets

        panels0 = dict(sheets.PANEL_STATS)
        paced = os.environ.get("PLEXORA_AI_CALLS_PER_MIN")
        os.environ["PLEXORA_AI_CALLS_PER_MIN"] = str(calls_per_min or 100_000)
        try:
            summary = QCRun(options, gateway=client, trace=TraceStore(Path(tdir) / "trace.sqlite"),
                            session=session).run()
        finally:
            if paced is None:
                os.environ.pop("PLEXORA_AI_CALLS_PER_MIN", None)
            else:
                os.environ["PLEXORA_AI_CALLS_PER_MIN"] = paced
        calls = list(gateway.calls)
    wall = time.monotonic() - started
    if summary.get("status") != "done":
        return {"seconds": round(wall, 2), "status": summary.get("status"),
                "reason": summary.get("reason")}
    signature = result_signature(project, summary["session_id"])
    by_task: dict = {}
    for call in calls:
        task = _task_of_wire(call["body"].get("task"))
        by_task[task] = by_task.get(task, 0) + 1
    tokens = summary.get("tokens") or {}
    # A call that read nothing wrote the prefix (its system prompt is the
    # first breakpoint); the provider keys that prefix on the answer schema.
    schemas = {json.dumps(call["body"]["request"].get("output_schema"), sort_keys=True) for call in calls}
    prefix_writes = sum(1 for call in calls if not (call.get("usage") or {}).get("cache_read"))
    row = {"status": "done", "lanes": summary.get("parallel_markers") or 1, "packets": summary.get("packets"),
           "answer_schemas": len(schemas), "prefix_writes": prefix_writes,
           "model_calls": summary.get("model_calls"), "invalid_answers": summary.get("invalid_answers"),
           "wall_s": round(wall, 2), "model_s": round(busy["model_s"], 2),
           "peak_calls_out": busy["peak"], "peak_outstanding": summary.get("peak_outstanding"),
           "reissued": summary.get("reissued", 0), "workers": summary.get("workers"),
           "tokens_input": tokens.get("input"), "tokens_cache_read": tokens.get("cache_read"),
           "tokens_cache_write": tokens.get("cache_write"), "tokens_output": tokens.get("output"),
           "cache_read_share": (summary.get("cache") or {}).get("read_share"),
           "charged_micro": summary.get("charged_micro"), "calls_by_task": by_task,
           "signature": signature, "timeline": sorted(timeline),
           "panels": {k: sheets.PANEL_STATS[k] - panels0[k] for k in ("hits", "misses")}}
    from plexora.plugins.qc.server.engine import store

    # What did not run and why (a check skipped, say): two arms that differ
    # in their calls differ here first.
    record = store().load(summary["session_id"])
    row["not_run"] = sorted(f"{n.get('check')}:{n.get('unit')}: {n.get('reason')}"
                            for n in record.get("planning_notes") or [])
    row["checks"] = sorted(f"{u['id']}={u['state']} ({u.get('reason') or ''})"
                           for u in record["units"].values() if u.get("type") == "check")
    # How long building a packet held the session's lock (`decisions.jsonl`'s
    # `ms.build`): what the drawing outside the lock takes off it.
    builds = sorted(float((d.get("ms") or {}).get("build") or 0)
                    for d in store().decisions(summary["session_id"])
                    if d.get("event") == "issued" and (d.get("ms") or {}).get("build") is not None)
    row["build_ms"] = {"median": round(builds[len(builds) // 2], 1) if builds else None,
                       "total": round(sum(builds), 1)}
    from plexora.agent import invoke

    # Each scene is scored from scratch by the next arm too.
    invoke(session, "qc_session_finish", {"session_id": summary["session_id"], "action": "rollback"})
    return row


def run_synthetic(scenarios, agent_style="oracle", *, arms=ARMS, seed=0, size=1024,
                  grid=40, latency_scale=0.02, jitter=0.0) -> list:
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
                                seed=seed + index, scene=_builder(scenario))
                for position, arm in enumerate(arms):
                    if arm.startswith("harness"):
                        # A scene of its own per harness arm, the same pixels: the
                        # stain ledger and the scan caches an earlier arm left
                        # would otherwise answer for this one.
                        spec = arm.partition(":")[2]
                        lanes = None if spec == "auto" else int(spec or 1)
                        own = register(root, f"{name}_h{lanes}_{position}",
                                       SCENARIOS[scenario], size=size, grid=grid, seed=seed + index,
                                       scene=_builder(scenario))
                        row = {"arm": arm, "agent": agent_style,
                               **run_harness(session, own, lanes, agent_style, seed=seed + index,
                                             latency_scale=latency_scale, jitter=jitter)}
                    else:
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


#: The agent-facing cost: `_AgentText.cost` plus the images the session
#: itself charged (`run_scene`'s `used.images`), averaged per scene like
#: `packets` and `vision_tokens` are -- so arms and scenes compare like for
#: like. Only the session arm reports these (the detect arm shows a scan's
#: candidates, never a packet).
COST_KEYS = ("text_chars", "text_tokens_est", "images", "total_tokens_est")


def _by_kind_totals(rows) -> dict:
    """Every scene's `cost_by_kind` summed into one {kind: {count, chars}} for
    the arm -- the decision kinds that drove the agent-facing text cost."""
    out = {}
    for row in rows:
        for kind, stat in (row.get("cost_by_kind") or {}).items():
            entry = out.setdefault(kind, {"count": 0, "chars": 0})
            entry["count"] += stat["count"]
            entry["chars"] += stat["chars"]
    return out


def summarise(rows) -> dict:
    out = {}
    for arm in sorted({r["arm"] for r in rows}):
        mine = [r for r in rows if r["arm"] == arm]
        out[arm] = {key: _mean([r.get(key) for r in mine]) for key in (
            "region_recall", "region_precision", "region_iou", "region_iou_px",
            "envelope_iou_px", "excess_fraction", "channel_accuracy",
            "cell_recall", "cell_precision", "false_removal", "marker_recall",
            "marker_precision", "packets", "vision_tokens", "seconds", *COST_KEYS)}
        out[arm]["cost_by_kind"] = _by_kind_totals(mine)
    return out


def to_markdown(summary, rows, *, title) -> str:
    keys = ("region_recall", "region_precision", "region_iou", "region_iou_px",
            "envelope_iou_px", "excess_fraction", "channel_accuracy",
            "cell_recall", "cell_precision", "false_removal", "marker_recall",
            "marker_precision", "packets", "vision_tokens", "seconds")
    lines = [f"# {title}", "", "| arm | " + " | ".join(keys) + " |",
             "|---|" + "---|" * len(keys)]
    for arm, values in summary.items():
        lines.append(f"| {arm} | " + " | ".join("-" if values.get(k) is None else str(values[k])
                                                for k in keys) + " |")
    lines += ["", "## Cost", "",
             "Agent-facing text the session arm's scripted agent received -- every `qc_next` "
             "/ `qc_answer` result, the start result, the status calls the bench makes and the "
             "finish result -- as chars and chars/4 (`text_tokens_est`), the images the "
             "session charged to those packets, and an estimated total input token count "
             "(`text_tokens_est` + `vision_tokens`). Per scene, like `packets` and "
             "`vision_tokens` above; the detect arm never drives a session, so it shows `-`.",
             "", "| arm | " + " | ".join(COST_KEYS) + " |", "|---|" + "---|" * len(COST_KEYS)]
    for arm, values in summary.items():
        lines.append(f"| {arm} | " + " | ".join(
            "-" if values.get(k) is None else str(values[k]) for k in COST_KEYS) + " |")
    by_kind = {arm: values.get("cost_by_kind") or {} for arm, values in summary.items()}
    if any(by_kind.values()):
        lines += ["", "### Cost by packet kind (summed over every scene)", "",
                  "| arm | kind | count | chars |", "|---|---|---|---|"]
        for arm, kinds in by_kind.items():
            for kind, stat in sorted(kinds.items()):
                lines.append(f"| {arm} | {kind} | {stat['count']} | {stat['chars']} |")
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
