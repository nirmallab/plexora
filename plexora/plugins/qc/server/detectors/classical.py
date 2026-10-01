"""The classical detectors: robust statistics on the scan's maps.

Each says where a known kind of artifact would show in the maps, as a mask
and a class hint; none decides anything. Their cut-points (`DETECT`, `[cal]`)
are deliberately permissive -- a missed artifact is worse than a candidate
the agent dismisses at a glance, and the channel audit catches what they miss.
"""

from __future__ import annotations

import numpy as np

from plexora.plugins.qc.server.detectors.base import (Candidate, DetectorRequires,
                                                        severity_from_z)

VERSION = "1"

#: [cal] every cut-point the classical detectors use.
DETECT = {
    "focus_log2": -1.0, "focus_log2_max": -3.0,
    "saturation": 0.002, "saturation_max": 0.2,
    "compact_fraction": 0.005, "compact_z": 3.0, "compact_z_max": 10.0,
    "diffuse_z": 3.0, "diffuse_z_max": 8.0, "diffuse_min_cells": 4,
    "illumination_r2": 0.4, "illumination_range": 0.4, "illumination_dev": 0.25,
    "background_ratio": 2.0, "background_z": 2.0,
    "seam_z": 4.0, "seam_z_max": 12.0,
    "dark_z": -2.5, "dark_z_max": -6.0, "dark_min_cells": 4,
    "registration_um": 2.0, "registration_px": 4.0,
    "loss_fraction": 0.5, "loss_min_cells": 3,
    "edge_excess": 0.5,
    "min_tissue": 0.5,
    "core_tissue": 0.9,
    # focus is judged only where a channel has structure to blur
    "focus_min_contrast": 0.35,
    "aggregate_closing": 1,
}


def components(mask, min_cells=1):
    from scipy import ndimage

    labels, n = ndimage.label(mask, structure=np.ones((3, 3), dtype=bool))
    out = []
    for index in range(1, n + 1):
        part = labels == index
        if part.sum() >= min_cells:
            out.append(part)
    return out


def _closed(mask, cells):
    from scipy import ndimage

    if cells <= 0 or not mask.any():
        return mask
    return ndimage.binary_closing(mask, structure=np.ones((3, 3), dtype=bool),
                                  iterations=int(cells)) | mask


def _mean(values, mask):
    data = values[mask & np.isfinite(values)]
    return float(data.mean()) if data.size else 0.0


def _candidate(detector, klass, scope, channels, mask, severity, *, metric, context,
               metrics=None, alternatives=(), cycles=(), strength=None):
    return Candidate(detector=detector.name, detector_version=detector.version,
                     class_hint=klass, scope_hint=scope, channels=tuple(channels),
                     mask=mask.astype(bool), score=float(severity), severity=float(severity),
                     cycles=tuple(cycles), metrics={k: _round(v) for k, v in
                                                    (metrics or {}).items()},
                     primary_metric=metric, evidence_channels=tuple(channels[:3]),
                     alternatives=list(alternatives),
                     strength=None if strength is None else float(strength))


def _round(value):
    if isinstance(value, float):
        return float(f"{value:.4g}")
    return value


def _scope_for(context, channel):
    if channel == context.nuclear or (context.cycle_of(channel) and _is_cycle_nuclear(
            context, channel)):
        return "cycle" if len(context.cycles.get("cycles") or []) > 1 else "all_channels"
    return "channel"


def _is_cycle_nuclear(context, channel):
    for cycle in context.cycles.get("cycles") or []:
        if cycle.get("nuclear") == channel:
            return True
    return False


def _cycle_channels(context, channel):
    index = context.cycle_of(channel)
    for cycle in context.cycles.get("cycles") or []:
        if cycle["index"] == index:
            return list(cycle["channels"])
    return context.channels


class _Detector:
    name = ""
    version = VERSION
    classes_hint: tuple = ()
    requires = DetectorRequires()

    def available(self, context):
        return True, None


class FocusDetector(_Detector):
    name = "focus"
    classes_hint = ("out_of_focus",)
    requires = DetectorRequires(maps=("focus_rel",))

    def run(self, context):
        out = []
        # Core tissue only: a cell half on glass has a glass edge's contrast.
        tissue = context.tissue_fraction() >= DETECT["core_tissue"]
        for channel in context.usable():
            rel = context.map(channel, "focus_rel")
            contrast = context.map(channel, "contrast")
            with np.errstate(divide="ignore", invalid="ignore"):
                log2 = np.log2(np.maximum(rel, 1e-6))
            # A sparse marker's empty cells have nothing to blur: judged on
            # cells with structure, then those regions are closed over.
            structured = np.nan_to_num(contrast) >= DETECT["focus_min_contrast"]
            if structured[tissue].mean() < 0.5:
                continue
            mask = tissue & structured & (log2 <= DETECT["focus_log2"])
            mask = _closed(mask, 1) & tissue
            for part in components(mask, context.cells_for_um2(10_000)):
                depth = float(np.nanmedian(log2[part]))
                severity = float(np.clip((DETECT["focus_log2"] - depth)
                                         / (DETECT["focus_log2"] - DETECT["focus_log2_max"]),
                                         0.05, 1.0))
                scope = _scope_for(context, channel)
                channels = _cycle_channels(context, channel) if scope == "cycle" else \
                    (context.channels if scope == "all_channels" else [channel])
                if scope in ("cycle", "all_channels") and channel not in channels:
                    channels = [channel, *channels]
                median = context.map(channel, "median")
                dim = float(np.nanmedian(context.robust_z(np.log1p(np.maximum(median, 0)))
                                         [part]))
                alternatives = ["tissue_damage_or_detachment"] if dim <= -2.0 else []
                out.append(_candidate(self, "out_of_focus", scope, channels, part, severity,
                                      metric=f"{channel}::focus_rel", context=context,
                                      metrics={"focus_log2": depth, "intensity_z": dim,
                                               "channel": channel},
                                      alternatives=alternatives,
                                      cycles=(context.cycle_of(channel),)
                                      if context.cycle_of(channel) else (),
                                      strength=-depth))
        return out


class SaturationDetector(_Detector):
    name = "saturation"
    classes_hint = ("saturation_or_clipping",)

    def run(self, context):
        out = []
        tissue = context.tissue_fraction() >= 0.25
        for channel in context.usable():
            sat = context.map(channel, "saturation")
            mask = tissue & (np.nan_to_num(sat) >= DETECT["saturation"])
            for part in components(mask, 1):
                level = _mean(sat, part)
                severity = float(np.clip(level / DETECT["saturation_max"], 0.1, 1.0))
                out.append(_candidate(self, "saturation_or_clipping", "channel", [channel],
                                      part, severity, metric=f"{channel}::saturation",
                                      context=context,
                                      metrics={"saturated_fraction": level,
                                               "channel": channel},
                                      strength=level / DETECT["saturation"]))
        return out


class AggregateDetector(_Detector):
    """Compact bright specks in one marker. Not on an autofluorescence /
    blank channel (`class_rules.is_af_channel`): it is unstained, its
    texture is the tissue's own glow and expected -- it stays evidence for
    the other classes (diffuse brightness, the autofluorescence rule)."""

    name = "aggregate"
    classes_hint = ("antibody_aggregate", "debris_or_foreign_object")

    def run(self, context):
        from plexora.plugins.qc.server.class_rules import is_af_channel

        out = []
        tissue = context.tissue_fraction() >= 0.25
        for channel in context.markers():
            if is_af_channel(channel):
                continue
            compact = np.nan_to_num(context.map(channel, "bright_compact"))
            z = context.robust_z(compact, core=False, floor=DETECT["compact_fraction"])
            mask = tissue & (compact >= DETECT["compact_fraction"]) & (z >= DETECT["compact_z"])
            # Aggregates are scattered specks: nearby cells are one region.
            mask = _closed(mask, DETECT["aggregate_closing"]) & tissue
            for part in components(mask, 1):
                zz = float(np.nanmax(z[part]))
                severity = max(0.1, severity_from_z(zz, DETECT["compact_z"],
                                                    DETECT["compact_z_max"]))
                out.append(_candidate(self, "antibody_aggregate", "channel", [channel], part,
                                      severity, metric=f"{channel}::bright_compact",
                                      context=context,
                                      metrics={"compact_fraction": _mean(compact, part),
                                               "z": zz, "channel": channel},
                                      alternatives=["debris_or_foreign_object"],
                                      strength=zz))
        return out


class DiffuseBrightDetector(_Detector):
    """Bright patches larger than cells, judged across channels: in the
    nuclear stain too, a fold; in most channels, autofluorescence; in one,
    background."""

    name = "diffuse_bright"
    classes_hint = ("tissue_fold", "autofluorescence", "excessive_background")

    def run(self, context):
        tissue = context.tissue_fraction() >= 0.25
        per = {}
        for channel in context.usable():
            z = context.map(channel, "bright_diffuse")
            saturated = np.nan_to_num(context.map(channel, "saturation")) >= DETECT["saturation"]
            per[channel] = tissue & (np.nan_to_num(z) >= DETECT["diffuse_z"]) & ~saturated
        if not per:
            return []
        union = np.logical_or.reduce(list(per.values()))
        out = []
        markers = context.markers()
        for part in components(union, DETECT["diffuse_min_cells"]):
            involved = [c for c, m in per.items() if (m & part).sum() >= 0.3 * part.sum()]
            if not involved:
                continue
            # The region is where most of the involved channels agree, not
            # the union of every channel's spread.
            count = np.sum([per[c] for c in involved], axis=0)
            agreed = part & (count >= max(1, int(np.ceil(0.5 * len(involved)))))
            if agreed.sum() >= DETECT["diffuse_min_cells"]:
                part = agreed
            nuclear_bright = any(c == context.nuclear or _is_cycle_nuclear(context, c)
                                 for c in involved)
            share = len([c for c in involved if c in markers]) / max(1, len(markers))
            if nuclear_bright and share >= 0.5:
                klass, scope, channels = "tissue_fold", "all_channels", context.channels
                alternatives = ["air_bubble_or_coverslip", "debris_or_foreign_object"]
            elif share >= 0.5:
                klass, scope, channels = "autofluorescence", "channels", involved
                alternatives = ["tissue_fold", "excessive_background"]
            else:
                klass, scope = "excessive_background", "channel" if len(involved) == 1 \
                    else "channels"
                channels = involved
                alternatives = ["antibody_aggregate", "autofluorescence"]
            zz = max(float(np.nanmax(context.map(c, "bright_diffuse")[part])) for c in involved)
            severity = max(0.1, severity_from_z(zz, DETECT["diffuse_z"], DETECT["diffuse_z_max"]))
            out.append(_candidate(self, klass, scope, channels, part, severity,
                                  metric=f"{involved[0]}::bright_diffuse", context=context,
                                  metrics={"z": zz, "channels_involved": len(involved)},
                                  alternatives=alternatives, strength=zz))
        return out


class IlluminationDetector(_Detector):
    name = "illumination"
    classes_hint = ("illumination_or_shading",)

    def run(self, context):
        out = []
        tissue = context.tissue_fraction() >= 0.25
        for channel in context.usable():
            summary = context.channel_meta(channel)["summary"]
            ill = summary.get("illumination") or {}
            if (ill.get("r2") or 0) <= DETECT["illumination_r2"] or \
                    (ill.get("range_log") or 0) <= DETECT["illumination_range"]:
                continue
            fitted = context.map(channel, "illumination_fit")
            centre = float(np.nanmedian(fitted[tissue]))
            mask = tissue & (np.abs(fitted - centre) >= DETECT["illumination_dev"])
            if mask.sum() < 2:
                continue
            severity = float(np.clip((ill["range_log"] - DETECT["illumination_range"]) / 1.5,
                                     0.2, 1.0))
            out.append(_candidate(self, "illumination_or_shading", "channel", [channel], mask,
                                  severity, metric=f"{channel}::illumination_fit",
                                  context=context,
                                  metrics={"r2": ill["r2"], "range_log": ill["range_log"],
                                           "channel": channel}))
        return out


class SeamDetector(_Detector):
    name = "seam"
    classes_hint = ("stitching_or_tile_seam",)

    def run(self, context):
        out = []
        tissue = context.tissue_fraction() >= 0.25
        for channel in context.usable():
            seam = context.map(channel, "seam")
            mask = tissue & (np.nan_to_num(seam) >= DETECT["seam_z"])
            if mask.sum() < 3:
                continue
            zz = float(np.nanmax(seam[mask]))
            severity = max(0.15, severity_from_z(zz, DETECT["seam_z"], DETECT["seam_z_max"]))
            scope = "all_channels" if _is_cycle_nuclear(context, channel) or \
                channel == context.nuclear else "channel"
            out.append(_candidate(self, "stitching_or_tile_seam", scope,
                                  context.channels if scope == "all_channels" else [channel],
                                  mask, severity, metric=f"{channel}::seam", context=context,
                                  metrics={"z": zz, "channel": channel}, strength=zz))
        return out


class DarkDetector(_Detector):
    """Tissue much darker than the rest in the nuclear stain and most markers:
    torn, detached or missing tissue."""

    name = "dark"
    classes_hint = ("tissue_damage_or_detachment",)

    def run(self, context):
        fraction = context.tissue_fraction()
        holes = context.scan.shared("tissue_holes")
        region = (fraction >= DETECT["core_tissue"]) |             ((holes if holes is not None else 0) >= 0.5)
        channels = context.usable()
        if not channels:
            return []
        zs = {}
        for channel in channels:
            median = context.map(channel, "median")
            zs[channel] = context.robust_z(np.log1p(np.maximum(median, 0)), core=False)
        reference = context.nuclear if context.nuclear in zs else None
        dark = [np.nan_to_num(z) <= DETECT["dark_z"] for z in zs.values()]
        share = np.mean(dark, axis=0)
        mask = region & (share >= 0.5)
        if reference is not None:
            mask &= np.nan_to_num(zs[reference]) <= DETECT["dark_z"]
        out = []
        for part in components(mask, DETECT["dark_min_cells"]):
            zz = float(np.nanmedian(zs[reference or channels[0]][part]))
            severity = max(0.2, severity_from_z(zz, abs(DETECT["dark_z"]),
                                                abs(DETECT["dark_z_max"])))
            out.append(_candidate(self, "tissue_damage_or_detachment", "all_channels",
                                  context.channels, part, severity,
                                  metric=f"{reference or channels[0]}::median",
                                  context=context, metrics={"z": zz},
                                  alternatives=["cycle_specific_tissue_loss", "out_of_focus"],
                                  strength=abs(zz)))
        return out


class EmptyChannelDetector(_Detector):
    name = "empty_channel"
    classes_hint = ("empty_or_failed_channel",)

    def run(self, context):
        out = []
        tissue = context.tissue_fraction() >= 0.25
        for meta in context.scan.channels:
            if not ({"empty_channel", "near_zero_plane"} & set(meta["flags"])):
                continue
            out.append(_candidate(self, "empty_or_failed_channel", "channel", [meta["name"]],
                                  tissue, 1.0, metric=f"{meta['name']}::median",
                                  context=context,
                                  metrics={"flags": ",".join(meta["flags"]),
                                           "channel": meta["name"]}))
        return out


class RegistrationDetector(_Detector):
    name = "registration"
    classes_hint = ("cross_cycle_registration_error",)
    requires = DetectorRequires(cycles=True)

    def available(self, context):
        cross = context.scan.meta.get("cross_cycle") or {}
        if not cross.get("available"):
            return False, cross.get("reason") or "no cycle structure"
        return True, None

    def run(self, context):
        cross = context.scan.meta.get("cross_cycle") or {}
        grid = context.scan.grid
        tissue = context.tissue_fraction() >= 0.25
        out = []
        for entry in cross.get("cycles") or []:
            shift_px = float(np.hypot(*entry["shift_px"]))
            shift_um = float(np.hypot(*entry["shift_um"])) if entry.get("shift_um") else None
            limit_hit = (shift_um is not None and shift_um >= DETECT["registration_um"]) or \
                (shift_um is None and shift_px >= DETECT["registration_px"])
            local_mask = np.zeros(tissue.shape, dtype=bool)
            factor = context.scan.meta.get("overview_factor") or 1.0
            for block in entry.get("local") or []:
                dy = (block["dy"] - entry["shift_px_overview"][0]) * factor
                dx = (block["dx"] - entry["shift_px_overview"][1]) * factor
                off_px = float(np.hypot(dx, dy))
                off_um = off_px * context.pixel_um if context.pixel_um else None
                if (off_um is not None and off_um >= DETECT["registration_um"]) or \
                        (off_um is None and off_px >= DETECT["registration_px"]):
                    x0, y0, x1, y1 = (v * factor / grid["cell_full_px"] for v in block["box"])
                    local_mask[int(y0):int(np.ceil(y1)), int(x0):int(np.ceil(x1))] = True
            channels = _channels_of_cycle(context, entry["cycle"])
            if limit_hit:
                severity = float(np.clip(shift_px / (4 * DETECT["registration_px"]), 0.2, 1.0))
                out.append(_candidate(self, "cross_cycle_registration_error", "cycle", channels,
                                      tissue, severity, metric="registration",
                                      context=context,
                                      metrics={"shift_px": shift_px, "shift_um": shift_um,
                                               "cycle": entry["cycle"]},
                                      cycles=(entry["cycle"],)))
            elif (local_mask & tissue).sum() >= 2:
                out.append(_candidate(self, "cross_cycle_registration_error", "cycle", channels,
                                      local_mask & tissue, 0.4, metric="registration",
                                      context=context, metrics={"cycle": entry["cycle"],
                                                                "local": True},
                                      cycles=(entry["cycle"],)))
        return out


def _channels_of_cycle(context, index, *, onward=False):
    out = []
    for cycle in context.cycles.get("cycles") or []:
        if cycle["index"] == index or (onward and cycle["index"] > index):
            out.extend(cycle["channels"])
    return out


class TissueLossDetector(_Detector):
    name = "tissue_loss"
    classes_hint = ("cycle_specific_tissue_loss",)
    requires = DetectorRequires(cycles=True)

    def available(self, context):
        cross = context.scan.meta.get("cross_cycle") or {}
        if not cross.get("available"):
            return False, cross.get("reason") or "no cycle structure"
        return True, None

    def run(self, context):
        cross = context.scan.meta.get("cross_cycle") or {}
        tissue = context.tissue_fraction() >= 0.25
        out = []
        for entry in cross.get("cycles") or []:
            loss = context.scan.shared(f"tissue_loss:c{entry['cycle']}")
            if loss is None:
                continue
            mask = tissue & (np.nan_to_num(loss) >= DETECT["loss_fraction"])
            for part in components(mask, DETECT["loss_min_cells"]):
                later = [e["cycle"] for e in cross["cycles"] if e["cycle"] >= entry["cycle"]
                         and context.scan.shared(f"tissue_loss:c{e['cycle']}") is not None
                         and (np.nan_to_num(context.scan.shared(f"tissue_loss:c{e['cycle']}"))
                              [part] >= DETECT["loss_fraction"]).mean() >= 0.5]
                cycles = tuple(later or [entry["cycle"]])
                channels = [c for k in cycles for c in _channels_of_cycle(context, k)]
                level = _mean(loss, part)
                out.append(_candidate(self, "cycle_specific_tissue_loss",
                                      "cycle" if len(cycles) == 1 else "cycles", channels,
                                      part, float(np.clip(level, 0.3, 1.0)),
                                      metric=f"tissue_loss:c{entry['cycle']}", context=context,
                                      metrics={"lost_fraction": level,
                                               "first_cycle": entry["cycle"]},
                                      alternatives=["tissue_damage_or_detachment"],
                                      cycles=cycles))
        return out


BUILTIN = (FocusDetector(), SaturationDetector(), AggregateDetector(), DiffuseBrightDetector(),
           IlluminationDetector(), SeamDetector(), DarkDetector(), EmptyChannelDetector(),
           RegistrationDetector(), TissueLossDetector())
