"""The closed vocabulary of quality control: one place, every word.

Scan, detectors, the session engine, cell modules, the exports' column names
and the skills all read their words from here, so an artifact class or a
reason code cannot drift between the region an agent confirmed and the flag
column a downstream notebook reads.
"""

from __future__ import annotations

#: Bumped when what a stored scan / cell measurement / result means changes.
SCAN_VERSION = "1"
CELLS_VERSION = "1"
RESULT_VERSION = "1"

ARTIFACT_CLASSES = (
    "out_of_focus",
    "tissue_fold",
    "tissue_damage_or_detachment",
    "debris_or_foreign_object",
    "air_bubble_or_coverslip",
    "slide_or_tissue_edge",
    "antibody_aggregate",
    "illumination_or_shading",
    "saturation_or_clipping",
    "excessive_background",
    "autofluorescence",
    "bleedthrough_or_crosstalk",
    "stitching_or_tile_seam",
    "cross_cycle_registration_error",
    "cycle_specific_tissue_loss",
    "empty_or_failed_channel",
    "other_technical",
    "uncertain_manual_review",
)

#: How a class reads in a sentence and in a category label.
CLASS_WORDS = {
    "out_of_focus": "out of focus",
    "tissue_fold": "tissue fold",
    "tissue_damage_or_detachment": "tissue damage or detachment",
    "debris_or_foreign_object": "debris or foreign object",
    "air_bubble_or_coverslip": "air bubble or coverslip artifact",
    "slide_or_tissue_edge": "slide or tissue edge",
    "antibody_aggregate": "antibody aggregate",
    "illumination_or_shading": "uneven illumination",
    "saturation_or_clipping": "saturation",
    "excessive_background": "excessive background",
    "autofluorescence": "autofluorescence",
    "bleedthrough_or_crosstalk": "bleed-through",
    "stitching_or_tile_seam": "tile seam",
    "cross_cycle_registration_error": "cross-cycle misregistration",
    "cycle_specific_tissue_loss": "tissue loss in a cycle",
    "empty_or_failed_channel": "failed channel",
    "other_technical": "technical artifact",
    "uncertain_manual_review": "manual review",
}

#: Alert hues, shared by the evidence sheets, the viewer overlay, the ROI
#: categories and the report legend.
CLASS_COLORS = {
    "out_of_focus": "#f59e0b",
    "tissue_fold": "#ef4444",
    "tissue_damage_or_detachment": "#dc2626",
    "debris_or_foreign_object": "#a855f7",
    "air_bubble_or_coverslip": "#06b6d4",
    "slide_or_tissue_edge": "#64748b",
    "antibody_aggregate": "#ec4899",
    "illumination_or_shading": "#eab308",
    "saturation_or_clipping": "#f97316",
    "excessive_background": "#84cc16",
    "autofluorescence": "#22c55e",
    "bleedthrough_or_crosstalk": "#14b8a6",
    "stitching_or_tile_seam": "#3b82f6",
    "cross_cycle_registration_error": "#6366f1",
    "cycle_specific_tissue_loss": "#b91c1c",
    "empty_or_failed_channel": "#7f1d1d",
    "other_technical": "#9ca3af",
    "uncertain_manual_review": "#fbbf24",
}

SCOPES = ("all_channels", "channel", "channels", "cycle", "cycles")
SEVERITIES = ("minor", "moderate", "severe")
#: Ordered: a stricter preset only ever moves an action to the right.
ACTIONS = ("ignore", "warn", "exclude")
STRICTNESS = ("lenient", "standard", "strict", "custom")
CONFIDENCE = ("high", "moderate", "low", "manual_review")
#: What an agent says of its own judgment, and the number each word stands for.
AI_CONFIDENCE = {"sure": 0.9, "fairly_sure": 0.65, "unsure": 0.3}

CELL_REASONS = ("counterstain_low", "counterstain_high", "area_small", "area_large",
                "morphology", "cycle_loss", "cycle_gain", "channel_outlier_bright",
                "channel_outlier_dim")
REGION_REASONS = tuple(f"region:{c}" for c in ARTIFACT_CLASSES)
#: The obsm["plexora_qc_flags"] columns, in order.
REASONS = CELL_REASONS + REGION_REASONS

#: What each reason means, for `uns["plexora_qc"]["definitions"]` and the report.
REASON_DEFINITIONS = {
    "counterstain_low": "nuclear counterstain far below the image's cells (debris, "
                        "out-of-focus or lost nucleus)",
    "counterstain_high": "nuclear counterstain far above the image's cells (clumped or "
                         "over-segmented nuclei, saturation)",
    "area_small": "segmented object much smaller than the image's cells",
    "area_large": "segmented object much larger than the image's cells (merged cells)",
    "morphology": "segmented shape implausible for a cell (eccentricity, solidity, "
                  "nucleus-to-cell ratio)",
    "cycle_loss": "nuclear stain much weaker in the last cycle than the first (cell lost "
                  "or moved during cycling)",
    "cycle_gain": "nuclear stain much stronger in the last cycle than the first",
    "channel_outlier_bright": "a marker far brighter than every other cell, in a spatial "
                              "cluster the agent judged an artifact",
    "channel_outlier_dim": "a marker far dimmer than every other cell, in a spatial cluster",
    **{f"region:{c}": f"inside a QC region of class {CLASS_WORDS[c]}"
       for c in ARTIFACT_CLASSES},
}

#: [cal] which reason is a cell's primary one when it has several (first wins).
PRIMARY_ORDER = (
    "region:cycle_specific_tissue_loss", "region:tissue_damage_or_detachment",
    "region:empty_or_failed_channel", "region:out_of_focus", "region:saturation_or_clipping",
    "region:antibody_aggregate", "region:stitching_or_tile_seam",
    "region:cross_cycle_registration_error", "region:slide_or_tissue_edge",
    "region:tissue_fold", "region:air_bubble_or_coverslip", "region:debris_or_foreign_object",
    "region:illumination_or_shading", "region:excessive_background",
    "region:autofluorescence", "region:bleedthrough_or_crosstalk", "region:other_technical",
    "region:uncertain_manual_review",
    "counterstain_low", "cycle_loss", "cycle_gain", "counterstain_high", "area_large",
    "area_small", "morphology", "channel_outlier_bright", "channel_outlier_dim",
)

# -- ROIs ----------------------------------------------------------------------

#: One ROI category per artifact class, with a fixed id: a strictness change
#: renames a region (its action is in the name), never moves it.
ROI_CATEGORY_PREFIX = "qc_"
ROI_SORT_ORDER = 900


def roi_category_id(artifact_class) -> str:
    return f"{ROI_CATEGORY_PREFIX}{artifact_class}"


def roi_category_label(artifact_class) -> str:
    words = CLASS_WORDS.get(artifact_class, artifact_class.replace("_", " "))
    return f"QC: {words[:1].upper()}{words[1:]}"


def class_of_category(category_id):
    """The artifact class a `qc_*` category id stands for, or None."""
    if not isinstance(category_id, str) or not category_id.startswith(ROI_CATEGORY_PREFIX):
        return None
    name = category_id[len(ROI_CATEGORY_PREFIX):]
    return name if name in ARTIFACT_CLASSES else None


ACTION_WORDS = {"exclude": "exclude", "warn": "warn", "ignore": "noted"}


def roi_name(action, artifact_class, channels) -> str:
    where = ", ".join(channels[:4]) + (f" +{len(channels) - 4}" if len(channels) > 4 else "") \
        if channels else "all channels"
    return f"QC {ACTION_WORDS.get(action, action)}: {CLASS_WORDS.get(artifact_class, artifact_class)}" \
           f" · {where}"


def action_of_name(name):
    """The action a QC ROI's name carries, or None when it carries none."""
    text = str(name or "")
    for action, word in ACTION_WORDS.items():
        if text.startswith(f"QC {word}:"):
            return action
    return None


# -- the session --------------------------------------------------------------

CHANNEL_STATES = ("pending", "scanned", "awaiting_audit", "audit_uncertain",
                  "clean", "flagged", "failed_channel", "manual_review_recommended",
                  "skipped_no_image", "skipped_brightfield")
CANDIDATE_STATES = ("awaiting_confirm", "awaiting_scope", "awaiting_localize", "awaiting_grid",
                    "confirmed_exclude", "confirmed_warn", "confirmed_noted", "dismissed",
                    "merged", "manual_review_recommended", "user_kept", "skipped_locked")
CELL_STATES = ("pending", "scanned", "awaiting_look", "decided", "skipped_no_table",
               "skipped_not_applicable", "manual_review_recommended")
FINAL_STATES = ("pending", "awaiting_review", "reviewed", "manual_review_recommended")

TERMINAL_STATES = ("clean", "flagged", "failed_channel", "manual_review_recommended",
                   "skipped_no_image", "skipped_brightfield",
                   "confirmed_exclude", "confirmed_warn", "confirmed_noted", "dismissed",
                   "merged", "user_kept", "skipped_locked",
                   "decided", "skipped_no_table", "skipped_not_applicable", "reviewed")
CONFIRMED_STATES = ("confirmed_exclude", "confirmed_warn", "confirmed_noted")
#: Candidate states whose region is written as an ROI.
WRITTEN_STATES = CONFIRMED_STATES + ("manual_review_recommended",)

SESSION_STATES = ("created", "bulk_running", "deciding", "done", "cancelled", "rolled_back",
                  "failed")
FINISHED_STATES = ("done", "cancelled", "rolled_back", "failed")

ASKS = {"audit_uncertain": "artifact_confirm", "awaiting_confirm": "artifact_confirm",
        "awaiting_scope": "artifact_scope", "awaiting_localize": "artifact_localize",
        "awaiting_grid": "artifact_grid", "awaiting_review": "final_qc_review"}
CELL_KINDS = {"counterstain_intensity": "cell_intensity", "segmentation_area": "cell_area",
              "cycle_stability": "cycle_stability", "channel_outlier": "channel_outlier"}

SETUP_KINDS = ("pixel_setup",)
LOOK_KINDS = ("channel_audit", "artifact_confirm", "artifact_localize", "artifact_grid",
              "cell_intensity", "cell_area", "cycle_stability", "channel_outlier")
CHECK_KINDS = ("artifact_scope", "final_qc_review")
PACKET_KINDS = SETUP_KINDS + LOOK_KINDS + CHECK_KINDS
#: Looks a unit's allowance pays for; the audit, scope and final review are
#: bounded by the state machine itself.
BUDGETED_KINDS = ("artifact_confirm", "artifact_localize", "artifact_grid", "cell_intensity",
                  "cell_area", "cycle_stability", "channel_outlier")

PHASES = ("planning", "analyzing", "inspecting", "thinking", "validating", "waiting",
          "summarizing")
SESSION_EVENTS = ("started", "control", "issued", "phase", "answered", "unit_closed",
                  "limit_reached", "limit_answered", "finished", "needs_setup")

LIMIT_POLICIES = ("ask", "extend", "stop")
LIMIT_DEFAULTS = {"on_limit": "ask", "max_extensions": 2}
LIMIT_ENV = {"on_limit": "PLEXORA_QC_ON_LIMIT", "max_extensions": "PLEXORA_QC_MAX_EXTENSIONS"}
LIMIT_WORDS = {"budget": "its allowance of looks for this candidate",
               "rounds": "its rounds of outlines for this candidate"}
LIMIT_DECISIONS = ("continue", "stop")

#: Per unit, unless the session says otherwise: a confirm at three levels, a
#: localisation and a grid round fit in it.
QC_UNIT_DEFAULT = {"packets": 5, "images": 10, "pixels": 8_000_000, "chars": 40_000}

#: [cal] the engine's own cut-points.
ENGINE = {
    "audit_batch": 8,              # channels per audit sheet
    "audit_sheets_per_packet": 2,
    "candidate_min_score": 0.05,   # below this a detector candidate is never pursued
    "candidates_per_channel": 8,
    "candidates_per_session": 40,
    "force_confirm_score": 0.85,   # a candidate this strong is looked at even on a clean row
    "merge_iou": 0.7,
    "confirm_levels": 3,
    "localize_rounds": 1,
    "grid_rounds": 2,
    "grid_side": 8,
    "cell_rounds": 2,
    "offset_step_mad": 0.5,
    "final_reopens": 1,
    "invalid_answers": 2,
    "large_region_fraction": 0.3,
}

NARRATION = {
    "pixel_setup": "Estimating this image's pixel size from the size of its nuclei.",
    "channel_audit": "I'm reviewing {n} channels for staining and imaging problems.",
    "artifact_confirm": "I'm checking a suspected {class_words} in {channel}.",
    "artifact_scope": "Working out how many channels the {class_words} at {channel} reaches.",
    "artifact_localize": "Refining where the {class_words} in {channel} begins and ends.",
    "artifact_grid": "Marking the {class_words} in {channel} on a grid.",
    "cell_intensity": "Checking cells with unusually weak or strong nuclear staining.",
    "cell_area": "Checking unusually large and small segmented cells.",
    "cycle_stability": "Comparing first and last nuclear cycles.",
    "channel_outlier": "Looking at the brightest and dimmest {channel} cells.",
    "final_qc_review": "Reviewing the whole QC picture before I close.",
}

EVIDENCE_LABELS = {
    "audit_sheet": "whole-tissue view of each channel",
    "confirm_sheet": "the suspected region at three scales",
    "scope_sheet": "the same place across channels",
    "localize_sheet": "candidate outlines of the region",
    "grid_sheet": "the region on a labelled grid",
    "cell_collage": "cells beside the proposed cutoffs",
    "review_sheet": "every QC region on the tissue",
    "pixel_snapshots": "nuclei with a ten-micron ring",
}

# -- strictness ------------------------------------------------------------------

SEVERITY_RANK = {"minor": 0, "moderate": 1, "severe": 2}

#: Direction of each key: "up" = a larger value is stricter, "down" = smaller.
STRICTNESS_KEYS = {
    "artifact.exclude_min_severity": "down",
    "artifact.exclude_min_confidence": "down",
    "artifact.warn_min_severity": "down",
    "artifact.min_area_fraction_exclude": "down",
    "artifact.large_region_exclude": "up",
    "counterstain.low_k": "down",
    "counterstain.high_k": "down",
    "area.k": "down",
    "area.ratio_low": "up",
    "area.ratio_high": "down",
    "area.ecc_max": "down",
    "area.solidity_min": "up",
    "area.seg_conf_min": "up",
    "area.size_alone": "up",
    "cycle.abs_floor": "down",
    "cycle.k": "down",
    "outlier.k": "down",
    "outlier.dim_clustered_exclude": "up",
    "cells.roi_overlap_fraction": "down",
}

#: [cal] the three presets. Custom tables are refused outside [lenient, strict].
STRICTNESS_PRESETS = {
    "lenient": {
        "artifact.exclude_min_severity": 2, "artifact.exclude_min_confidence": 0.65,
        "artifact.warn_min_severity": 1, "artifact.min_area_fraction_exclude": 0.002,
        "artifact.large_region_exclude": 0,
        "counterstain.low_k": 3.5, "counterstain.high_k": 4.0,
        "area.k": 4.0, "area.ratio_low": 0.02, "area.ratio_high": 0.98,
        "area.ecc_max": 0.99, "area.solidity_min": 0.5, "area.seg_conf_min": 0.2,
        "area.size_alone": 0, "cycle.abs_floor": 0.5, "cycle.k": 4.0, "outlier.k": 6.0,
        "outlier.dim_clustered_exclude": 0, "cells.roi_overlap_fraction": 0.75,
    },
    "standard": {
        "artifact.exclude_min_severity": 1, "artifact.exclude_min_confidence": 0.65,
        "artifact.warn_min_severity": 0, "artifact.min_area_fraction_exclude": 0.001,
        "artifact.large_region_exclude": 0,
        "counterstain.low_k": 3.0, "counterstain.high_k": 3.5,
        "area.k": 3.5, "area.ratio_low": 0.05, "area.ratio_high": 0.95,
        "area.ecc_max": 0.98, "area.solidity_min": 0.6, "area.seg_conf_min": 0.35,
        "area.size_alone": 0, "cycle.abs_floor": 0.35, "cycle.k": 3.5, "outlier.k": 5.0,
        "outlier.dim_clustered_exclude": 0, "cells.roi_overlap_fraction": 0.5,
    },
    "strict": {
        "artifact.exclude_min_severity": 0, "artifact.exclude_min_confidence": 0.3,
        "artifact.warn_min_severity": 0, "artifact.min_area_fraction_exclude": 0.0005,
        "artifact.large_region_exclude": 1,
        "counterstain.low_k": 2.5, "counterstain.high_k": 3.0,
        "area.k": 3.0, "area.ratio_low": 0.08, "area.ratio_high": 0.90,
        "area.ecc_max": 0.97, "area.solidity_min": 0.7, "area.seg_conf_min": 0.5,
        "area.size_alone": 1, "cycle.abs_floor": 0.25, "cycle.k": 3.0, "outlier.k": 4.0,
        "outlier.dim_clustered_exclude": 1, "cells.roi_overlap_fraction": 0.25,
    },
}


def _assert_monotonic():
    lenient, standard, strict = (STRICTNESS_PRESETS[k] for k in ("lenient", "standard",
                                                                 "strict"))
    for key, direction in STRICTNESS_KEYS.items():
        values = (lenient[key], standard[key], strict[key])
        ordered = values[0] <= values[1] <= values[2] if direction == "up" \
            else values[0] >= values[1] >= values[2]
        if not ordered:
            raise AssertionError(f"strictness key {key} is not monotonic: {values}")
    for preset in STRICTNESS_PRESETS.values():
        if set(preset) != set(STRICTNESS_KEYS):
            raise AssertionError("every preset names every strictness key")


_assert_monotonic()
