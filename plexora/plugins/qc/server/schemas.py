"""The closed vocabulary of quality control: one place, every word.

Scan, detectors, the session engine, cell modules, the exports' column names
and the skills all read their words from here, so an artifact class or a
reason code cannot drift between the region an agent confirmed and the flag
column a downstream notebook reads.
"""

from __future__ import annotations

import re
import zlib

#: Bumped when what a stored scan / cell measurement / result means changes.
SCAN_VERSION = "4"
CELLS_VERSION = "3"
RESULT_VERSION = "2"

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
    "segmentation_error",
    "tissue_artifact",
    "staining_artifact",
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
    "segmentation_error": "segmentation error",
    "tissue_artifact": "tissue or acquisition artifact",
    "staining_artifact": "staining or signal artifact",
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
    "segmentation_error": "#a855f7",
    "tissue_artifact": "#eab308",
    "staining_artifact": "#22c55e",
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

# -- what a cell's call rests on -------------------------------------------------
#
# Two levels. A CELL reason says the whole cell is unreadable -- the tissue
# under it is folded, torn or gone, the nucleus it was segmented from is not a
# nucleus, the object is not one cell -- so every one of its measurements goes
# with it. A MARKER reason says one channel's value for that cell cannot be
# trusted (an aggregate on it, background over it, that channel out of focus)
# and leaves the cell, and its other markers, alone. Nothing that concerns one
# channel ever fails a whole cell.

CELL_REASONS = ("counterstain_low", "counterstain_high", "area_small", "area_large",
                "morphology", "cycle_loss", "cycle_gain",
                "seg_under", "seg_over", "seg_small", "seg_large", "seg_irregular")
#: The cell reasons Segmentation QC's cell modules call, from the mask read
#: against the DNA stain (`segqc`): one label holding two nuclei, one nucleus
#: cut across labels, and a size or roundness far from the mask's own.
SEG_REASONS = ("seg_under", "seg_over", "seg_small", "seg_large", "seg_irregular")
REGION_REASONS = tuple(f"region:{c}" for c in ARTIFACT_CLASSES)
#: The obsm["plexora_qc_flags"] columns, in order.
REASONS = CELL_REASONS + REGION_REASONS
#: Why one marker's value is flagged for a cell: a channel-scoped region it
#: sits in (and, for a class that raises signal, is bright in), or a value the
#: agent judged an artifact on the cells themselves.
MARKER_REASONS = ("extreme_value",) + REGION_REASONS

#: Classes that damage the tissue itself -- every channel of the cells in them
#: is unreadable, and a cell lost in any cycle has an incomplete profile --
#: so their regions fail whole cells whatever channels they were seen in.
PHYSICAL_CLASSES = ("tissue_fold", "tissue_damage_or_detachment",
                    "cycle_specific_tissue_loss", "slide_or_tissue_edge", "tissue_artifact")
#: Classes whose regions fail whole cells: the physical ones, and a region
#: where the mask itself failed -- a cell drawn wrong is wrong in every channel.
WHOLE_CELL_CLASSES = PHYSICAL_CLASSES + ("segmentation_error",)
#: Classes whose artifact ADDS signal to a channel. A cell in one of their
#: channel-scoped regions has that marker flagged only when the region's cells
#: are brighter in it than the rest of the tissue's (a test on the region) and
#: the cell itself is (a cutoff on the cell): a region the cells' own values do
#: not bear out flags none of them.
SIGNAL_RAISING_CLASSES = ("antibody_aggregate", "excessive_background", "autofluorescence",
                          "saturation_or_clipping", "bleedthrough_or_crosstalk",
                          "debris_or_foreign_object")

#: [cal] the test a signal-raising region's cells must pass before any of them
#: has that marker flagged -- fixed across presets, so the presets stay nested
#: (a preset moves only the per-cell cutoff, `cells.marker_quantile`).
#:
#: The region's cells are compared with the cells AROUND it (a ring
#: `ring_cells` cell diameters wide, or `ring_share` of the region's radius if
#: wider, outside every region of that marker): the same tissue, so a marker's
#: positive population does not pass for an artifact, nor hide one. The region
#: is borne out -- and its cells may be flagged -- when more of them stand
#: out above the ring's `tail_quantile` than the ring predicts, at p <=
#: `alpha` (one-sided binomial) AND by `min_tail_ratio` times: a p-value over
#: thousands of cells is tiny for a trivial difference, and a 3x excess keeps
#: at least two flags in three real. Whether the region is brighter as a
#: whole is also measured (one-sided rank test; `min_superiority` is the
#: chance a cell inside is brighter than one around it, 0.64 a medium effect)
#: and reported, but a region only brighter as a whole flags no cell: no cell
#: of it stands out. At least `min_cells` cells must sit above the ring's
#: median. A ring of fewer than `min_reference` cells is replaced by every
#: cell outside the marker's regions.
MARKER_EVIDENCE = {"alpha": 1e-3, "min_superiority": 0.64, "min_tail_ratio": 3.0,
                   "min_cells": 5, "min_reference": 50, "tail_quantile": 0.99,
                   "ring_cells": 10.0, "ring_share": 0.5}

#: What each reason means, for `uns["plexora_qc"]["definitions"]` and the report.
REASON_DEFINITIONS = {
    "counterstain_low": "nuclear counterstain far below the image's cells (debris, "
                        "out-of-focus or lost nucleus)",
    "counterstain_high": "nuclear counterstain far above the image's cells (clumped nuclei, "
                         "saturation); dense chromatin is also biology, so this only warns",
    "area_small": "segmented object much smaller than the image's cells",
    "area_large": "segmented object much larger than the image's cells; excludes only "
                  "with a shape that says merged (low solidity), otherwise warns",
    "morphology": "segmented shape implausible for one cell (low solidity, an impossible "
                  "nucleus-to-cell ratio, low segmentation confidence); elongation alone "
                  "is never a reason -- fibroblasts and smooth muscle are elongated",
    "cycle_loss": "nuclear stain much weaker in the last cycle than the first (cell lost "
                  "or moved during cycling)",
    "cycle_gain": "nuclear stain much stronger in the last cycle than the first (a "
                  "neighbour moved in, or misregistration); only warns",
    "seg_under": "one mask label holds the DNA of two nuclei (Segmentation QC's "
                 "under-segmentation score, judged against its neighbourhood)",
    "seg_over": "one nucleus cut across two mask labels (Segmentation QC's "
                "over-segmentation score)",
    "seg_small": "mask label far smaller than the mask's own median (robust z on log "
                 "area); excludes only where size alone is allowed to",
    "seg_large": "mask label far larger than the mask's own median; excludes only where "
                 "the under-segmentation score says merged, otherwise warns",
    "seg_irregular": "mask label far less round than the mask's own; only warns -- "
                     "elongated cells are biology",
    **{f"region:{c}": f"inside a QC region of class {CLASS_WORDS[c]}"
       for c in ARTIFACT_CLASSES},
}

#: What each marker reason means for the marker it is attached to.
MARKER_REASON_DEFINITIONS = {
    "extreme_value": "the marker far brighter than this image's positive cells reach, on "
                     "cells the agent looked at and judged an artifact (aggregate specks, "
                     "saturation, debris on the cell)",
    **{f"region:{c}": (f"inside a QC region of class {CLASS_WORDS[c]} seen in this channel"
                       + (", and brighter in it than the tissue outside the region's"
                          if c in SIGNAL_RAISING_CLASSES else ""))
       for c in ARTIFACT_CLASSES},
}

#: [cal] which reason is a cell's primary one when it has several (first wins).
PRIMARY_ORDER = (
    "region:cycle_specific_tissue_loss", "region:tissue_damage_or_detachment",
    "region:empty_or_failed_channel", "region:out_of_focus", "region:saturation_or_clipping",
    "region:antibody_aggregate", "region:stitching_or_tile_seam",
    "region:cross_cycle_registration_error", "region:segmentation_error",
    "region:slide_or_tissue_edge", "region:tissue_fold", "region:tissue_artifact",
    "region:air_bubble_or_coverslip", "region:debris_or_foreign_object",
    "region:illumination_or_shading", "region:excessive_background",
    "region:autofluorescence", "region:bleedthrough_or_crosstalk",
    "region:staining_artifact", "region:other_technical",
    "region:uncertain_manual_review",
    "counterstain_low", "cycle_loss", "cycle_gain", "counterstain_high", "area_large",
    "area_small", "morphology",
    "seg_under", "seg_over", "seg_large", "seg_small", "seg_irregular",
)

# -- ROIs ----------------------------------------------------------------------

# -- the five categories ------------------------------------------------------
#
# What a user sees and draws in. Every finding -- a region of any class, a
# cell reason, a marker reason -- belongs to exactly one of five categories;
# the fine class (a fold, an aggregate, a merged cell) is its subtype, kept in
# `roi_meta`, the candidate, the region's name and notes, and every export, so
# grouping never loses what was found. `uncertain_manual_review` is not a
# category: it is "Needs review", listed after the five.

CATEGORIES = (
    {"id": "blur_focus", "words": "Blur / focus issue", "color": "#f97316",
     "default_class": "out_of_focus",
     "groups": ("out-of-focus fields", "blur in one channel or all of them",
                "blur across the whole tissue")},
    {"id": "registration", "words": "Registration issue", "color": "#3b82f6",
     "default_class": "cross_cycle_registration_error",
     "groups": ("a cycle offset from the reference", "local shifts",
                "a whole cycle shifted", "nuclei doubled at their edges")},
    {"id": "segmentation", "words": "Segmentation issue", "color": "#a855f7",
     "default_class": "segmentation_error",
     "groups": ("merged cells", "split nuclei", "cells too large or too small",
                "implausible shapes", "a mask that misses its nuclei")},
    {"id": "tissue_acquisition", "words": "Tissue / acquisition artifact", "color": "#eab308",
     "default_class": "tissue_artifact",
     "groups": ("folds", "tears and detachment", "debris", "bubbles", "tissue edges",
                "tile seams", "uneven illumination", "saturation",
                "tissue lost in a cycle")},
    {"id": "staining_signal", "words": "Staining / signal artifact", "color": "#22c55e",
     "default_class": "staining_artifact",
     "groups": ("antibody aggregates", "high background", "autofluorescence",
                "bleed-through", "failed channels", "artifact-bright values")},
)
CATEGORY_IDS = tuple(c["id"] for c in CATEGORIES)
CATEGORY_WORDS = {c["id"]: c["words"] for c in CATEGORIES}
CATEGORY_COLORS = {c["id"]: c["color"] for c in CATEGORIES}
CATEGORY_DEFAULT_CLASS = {c["id"]: c["default_class"] for c in CATEGORIES}
#: "Needs review": the region the agent could not settle.
REVIEW = {"id": "review", "words": "Needs review", "color": "#94a3b8",
          "class": "uncertain_manual_review"}

#: Every class's category (`review` for the one that is not a finding yet).
CLASS_CATEGORY = {
    "out_of_focus": "blur_focus",
    "cross_cycle_registration_error": "registration",
    "segmentation_error": "segmentation",
    "tissue_fold": "tissue_acquisition",
    "tissue_damage_or_detachment": "tissue_acquisition",
    "debris_or_foreign_object": "tissue_acquisition",
    "air_bubble_or_coverslip": "tissue_acquisition",
    "slide_or_tissue_edge": "tissue_acquisition",
    "illumination_or_shading": "tissue_acquisition",
    "saturation_or_clipping": "tissue_acquisition",
    # The seam detector measures an intensity step at a tile border, not a
    # misalignment: a seam is how the image was acquired.
    "stitching_or_tile_seam": "tissue_acquisition",
    "cycle_specific_tissue_loss": "tissue_acquisition",
    "other_technical": "tissue_acquisition",
    "tissue_artifact": "tissue_acquisition",
    "antibody_aggregate": "staining_signal",
    "excessive_background": "staining_signal",
    "autofluorescence": "staining_signal",
    "bleedthrough_or_crosstalk": "staining_signal",
    "empty_or_failed_channel": "staining_signal",
    "staining_artifact": "staining_signal",
    "uncertain_manual_review": "review",
}

#: Every cell and marker reason's category. A counterstain far from the
#: image's cells is a mask that does not sit on one nucleus; a cell lost
#: between cycles is tissue that detached; one that gained stain is, most
#: often, a neighbour moved into it -- misregistration.
CELL_REASON_CATEGORY = {
    "counterstain_low": "segmentation", "counterstain_high": "segmentation",
    "area_small": "segmentation", "area_large": "segmentation",
    "morphology": "segmentation",
    **{reason: "segmentation" for reason in SEG_REASONS},
    "cycle_loss": "tissue_acquisition", "cycle_gain": "registration",
    "extreme_value": "staining_signal",
    **{f"region:{c}": CLASS_CATEGORY[c] for c in ARTIFACT_CLASSES},
}

#: The classes an agent may name. The two generic ones are what a region
#: drawn by hand in a category is until someone says more; an agent always
#: says what it saw.
AGENT_CLASSES = tuple(c for c in ARTIFACT_CLASSES
                      if c not in ("tissue_artifact", "staining_artifact"))

#: How a cell or marker reason reads on a row. Region reasons read from the class.
REASON_WORDS = {
    "counterstain_low": "Low counterstain",
    "counterstain_high": "High counterstain",
    "area_small": "Too small",
    "area_large": "Too large",
    "morphology": "Implausible shape",
    "cycle_loss": "Lost across cycles",
    "cycle_gain": "Gained across cycles",
    "seg_under": "Merged cells",
    "seg_over": "Split nucleus",
    "seg_small": "Too small for the mask",
    "seg_large": "Too large for the mask",
    "seg_irregular": "Irregular shape",
    "extreme_value": "Artifact-bright value",
}


def category_of_class(artifact_class):
    """The category a class belongs to: one of the five, or "review"."""
    return CLASS_CATEGORY.get(artifact_class, "tissue_acquisition")


def category_of_reason(reason):
    return CELL_REASON_CATEGORY.get(reason, "tissue_acquisition")


def default_class(category):
    """The class a region drawn in a category is until someone says more."""
    if category == REVIEW["id"]:
        return REVIEW["class"]
    return CATEGORY_DEFAULT_CLASS.get(category, CUSTOM_CLASS if is_custom(category) else None)


def category_words(category):
    if category == REVIEW["id"]:
        return REVIEW["words"]
    return CATEGORY_WORDS.get(category, str(category or "").replace("_", " "))


def category_color(category):
    if category == REVIEW["id"]:
        return REVIEW["color"]
    return CATEGORY_COLORS.get(category) or (custom_color(category) if is_custom(category)
                                             else "#9ca3af")


def public_categories() -> list:
    """The five, for the picker and the vocabulary route: id, words, colour,
    what each groups, and the classes in it."""
    return [{"id": c["id"], "words": c["words"], "color": c["color"],
             "default_class": c["default_class"], "groups": list(c["groups"]),
             "help": ", ".join(c["groups"]),
             "classes": [k for k in ARTIFACT_CLASSES if CLASS_CATEGORY[k] == c["id"]]}
            for c in CATEGORIES]


# -- ROIs ----------------------------------------------------------------------

#: One ROI category per category above, with a fixed id: a strictness change
#: renames a region (its action is in the name), never moves it. A project QC
#: wrote before the five existed has one category per class
#: (`qc_tissue_fold`, "QC: Tissue fold"); `roi_link.migrate_categories` moves
#: their regions into the five on the first write.
ROI_CATEGORY_PREFIX = "qc_"
ROI_SORT_ORDER = 900


#: A category the user named themselves ("QC: Pen mark") is `qc_custom_<slug>`.
#: It is its own group of regions everywhere the panel draws them, and a
#: technical artifact to everything that reasons by class (strictness, the
#: cells' reasons, the report), so no rule has to learn a class it never had.
CUSTOM_PREFIX = "custom_"
CUSTOM_CLASS = "other_technical"
CUSTOM_COLORS = ("#e879f9", "#2dd4bf", "#fb7185", "#a3e635", "#60a5fa", "#fbbf24",
                 "#c084fc", "#34d399", "#f472b6", "#38bdf8")
MAX_CUSTOM_WORDS = 60


def is_custom(key) -> bool:
    return isinstance(key, str) and key.startswith(CUSTOM_PREFIX) \
        and re.fullmatch(r"[a-z0-9_]{1,48}", key[len(CUSTOM_PREFIX):]) is not None


def custom_key(words) -> str | None:
    """`custom_<slug>` of what the user typed, or None when nothing in it can
    name a category."""
    slug = re.sub(r"[^a-z0-9]+", "_", str(words or "").casefold()).strip("_")[:48].strip("_")
    return f"{CUSTOM_PREFIX}{slug}" if slug else None


def custom_color(key) -> str:
    """A custom category's own colour, the same every time for the same name."""
    return CUSTOM_COLORS[zlib.crc32(key.encode()) % len(CUSTOM_COLORS)]


def _as_key(key):
    """A category key from what a caller holds: one of the five, "review", a
    custom key -- or a class id, which stands for its category."""
    if key in CATEGORY_IDS or key == REVIEW["id"] or is_custom(key):
        return key
    if key in CLASS_CATEGORY:
        return CLASS_CATEGORY[key]
    return None


def roi_category_id(key) -> str:
    """`qc_<category>` of a category, "review", a custom key or a class."""
    return f"{ROI_CATEGORY_PREFIX}{_as_key(key) or key}"


def roi_category_label(key) -> str:
    """"QC: Blur / focus issue", "QC: Needs review" -- a class is labelled by
    its category."""
    key = _as_key(key) or key
    words = category_words(key)
    return f"QC: {words[:1].upper()}{words[1:]}"


def legacy_class_of_category_id(category_id):
    """The class of a pre-categories `qc_<class>` id, or None."""
    if not isinstance(category_id, str) or not category_id.startswith(ROI_CATEGORY_PREFIX):
        return None
    name = category_id[len(ROI_CATEGORY_PREFIX):]
    return name if name in ARTIFACT_CLASSES else None


def category_key(category_id):
    """What a `qc_*` category groups its regions by: one of the five,
    "review", or the custom category's key (a legacy `qc_<class>` id reads as
    its class's category). None for a category that is not QC's."""
    if not isinstance(category_id, str) or not category_id.startswith(ROI_CATEGORY_PREFIX):
        return None
    name = category_id[len(ROI_CATEGORY_PREFIX):]
    if name in CATEGORY_IDS or name == REVIEW["id"] or is_custom(name):
        return name
    if name in ARTIFACT_CLASSES:
        return CLASS_CATEGORY[name]
    return None


def class_of_category(category_id):
    """The artifact class a `qc_*` category id stands for, or None: a legacy
    `qc_<class>` id its class, one of the five its default class, a custom
    one `CUSTOM_CLASS`."""
    legacy = legacy_class_of_category_id(category_id)
    if legacy:
        return legacy
    key = category_key(category_id)
    return default_class(key) if key else None


def _label_words(label):
    text = str(label or "").strip()
    if not text.casefold().startswith("qc:"):
        return None
    words = text[3:].strip()
    if words.casefold().endswith("(qc)"):
        words = words[:-4].strip()
    return words.casefold()


def class_of_label(label):
    """The artifact class a category label names ("QC: Out of focus", or one
    of the five: "QC: Blur / focus issue" is its default class), or None --
    how a region the user drew in a QC category made by hand (or by
    `create_roi`, which mints its own id) is still recognised."""
    folded = _label_words(label)
    if folded is None:
        return None
    for klass, klass_words in CLASS_WORDS.items():
        if folded in (klass_words.casefold(), klass.casefold(), klass.replace("_", " ")):
            return klass
    category = category_of_label(label)
    return default_class(category) if category else None


def category_of_label(label):
    """The category a label names -- one of the five ("QC: Registration
    issue"), "review", or a legacy class label's category -- or None."""
    folded = _label_words(label)
    if folded is None:
        return None
    for key in (*CATEGORY_IDS, REVIEW["id"]):
        words = category_words(key).casefold()
        if folded in (words, key, key.replace("_", " ")):
            return key
    for klass, klass_words in CLASS_WORDS.items():
        if folded in (klass_words.casefold(), klass.casefold(), klass.replace("_", " ")):
            return CLASS_CATEGORY[klass]
    return None


def is_legacy_label(label):
    """A pre-categories label ("QC: Tissue fold"): it names a class, not one
    of the five."""
    folded = _label_words(label)
    if folded is None or category_of_label(label) is None:
        return False
    return not any(folded in (category_words(k).casefold(), k, k.replace("_", " "))
                   for k in (*CATEGORY_IDS, REVIEW["id"]))


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
#: A check unit: one of the image checks (blur, registration, segmentation)
#: on one channel or comparison, scored in the bulk stage and settled by one
#: `score_review` look at tiles sampled across its score distribution.
CHECK_STATES = ("pending", "scanned", "awaiting_score_review", "decided",
                "skipped_not_applicable", "manual_review_recommended")

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
        "awaiting_grid": "artifact_grid", "awaiting_review": "final_qc_review",
        "awaiting_score_review": "score_review"}
CELL_KINDS = {"counterstain_intensity": "cell_intensity", "segmentation_area": "cell_area",
              "cycle_stability": "cycle_stability", "channel_outlier": "channel_outlier",
              "seg_under": "cell_segmentation", "seg_over": "cell_segmentation",
              "seg_size": "cell_segmentation", "seg_shape": "cell_segmentation"}

# -- the image checks inside a session -----------------------------------------

#: The local checks, each a continuous score over the tissue: the Blur
#: Score per 40 um tile, the registration mismatch share per ~6.5 um block,
#: the density of cells Segmentation QC flags, and the Artifact Detector's
#: object scores (one unit per category; opt-in, see QCChecks.artifacts).
CHECKS = ("blur", "registration", "segmentation", "artifacts")
CHECK_WORDS = {"blur": "blur", "registration": "registration mismatch",
               "segmentation": "segmentation problems",
               "artifacts": "tissue and acquisition artifacts"}
#: Which scan detector a check supersedes when it runs (the detector runs
#: after all when the check fails). The Artifact Detector confirms
#: saturation at full resolution per channel; the scan's dark and
#: diffuse-bright detectors also hint at other things, so they stay.
CHECK_SUPERSEDES = {"blur": "focus", "registration": "registration",
                    "artifacts": "saturation"}
#: The class a check's region is, until an agent says otherwise (an
#: artifact object carries its own: fold, tear, debris or saturation).
CHECK_CLASS = {"blur": "out_of_focus", "registration": "cross_cycle_registration_error",
               "segmentation": "segmentation_error", "artifacts": "tissue_artifact"}
#: The rows of a score-review sheet, in order: tiles well below the bar, just
#: below it, just above it, far above it, the heart of the largest flagged
#: regions, and -- when the whole tissue may be affected -- the tissue itself.
SCORE_STRATA = ("clear_good", "borderline_below", "borderline_above", "strongly_abnormal",
                "clustered", "global")
STRATUM_WORDS = {"clear_good": "clearly fine", "borderline_below": "just below the bar",
                 "borderline_above": "just above the bar",
                 "strongly_abnormal": "far above the bar",
                 "clustered": "inside the largest flagged regions",
                 "global": "the whole tissue"}
#: What a row of tiles shows, in the agent's words.
SCORE_VERDICTS = ("artifact", "normal", "mixed", "cannot_tell")
THRESHOLD_VERDICTS = ("accept", "too_lenient", "too_aggressive", "cannot_tell")
#: Where a threshold came from: the check's own rule, a value the user typed,
#: steps the user moved it from the automatic one, or steps an agent moved it
#: after looking.
THRESHOLD_SOURCES = ("auto", "user", "user_relative", "agent_refined")
#: One step of a threshold: tighter flags more, looser flags less.
ADJUST = {"tighter": 1, "looser": -1}

SETUP_KINDS = ("pixel_setup",)
#: `cell_modules` is several cell modules judged in one packet (answered per
#: module with the single kinds' fields); `artifact_confirm` may likewise
#: carry several candidates (answered per candidate label).
LOOK_KINDS = ("channel_audit", "artifact_confirm", "artifact_localize", "artifact_grid",
              "cell_intensity", "cell_area", "cycle_stability", "channel_outlier",
              "cell_modules", "score_review", "cell_segmentation")
CHECK_KINDS = ("artifact_scope", "final_qc_review")
PACKET_KINDS = SETUP_KINDS + LOOK_KINDS + CHECK_KINDS
#: Looks a unit's allowance pays for; the audit, scope and final review are
#: bounded by the state machine itself.
BUDGETED_KINDS = ("artifact_confirm", "artifact_localize", "artifact_grid", "cell_intensity",
                  "cell_area", "cycle_stability", "channel_outlier", "cell_modules",
                  "score_review", "cell_segmentation")

PHASES = ("planning", "analyzing", "inspecting", "thinking", "validating", "waiting",
          "summarizing")
SESSION_EVENTS = ("started", "control", "issued", "phase", "answered", "unit_closed",
                  "limit_reached", "limit_answered", "finished", "needs_setup")

LIMIT_POLICIES = ("ask", "extend", "stop")
LIMIT_DEFAULTS = {"on_limit": "ask", "max_extensions": 2}
LIMIT_ENV = {"on_limit": "PLEXORA_QC_ON_LIMIT", "max_extensions": "PLEXORA_QC_MAX_EXTENSIONS"}
#: Set to 0 to write confirmed regions as their envelopes (the map-grid outline
#: the agent judged) instead of tracing the artifact's pixels inside them.
REFINE_ENV = "PLEXORA_QC_REFINE"
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
    "force_confirm_score": 0.85,   # a candidate this strong is looked at even on a clean row...
    "overview_small_fraction": 0.02,  # ...when it is this small a share of the tissue, or of
                                   # a class an audit tile cannot show (OVERVIEW_BLIND)
    # What "strong" and "too small for a tile" mean for that exemption
    # (transitions.overview_blind / _forced). `score` saturates at 1.0 -- on a
    # 40-channel 45k px image nearly every candidate ties there, so a clean
    # row exempted all of them. "Very strong" is therefore also a rank: the
    # candidate's unbounded detector strength (a robust z, `Candidate.strength`)
    # is in the top `force_confirm_top_share` of its detector's candidates in
    # this scan. "Too small" is measured on the tile itself: the candidate's
    # map cells drawn at the audit tile's scale (sheets.TILE_PX across the
    # grid) cover at most `overview_blind_tile_px` tile pixels, or
    # `overview_fine_tile_px` for specks (OVERVIEW_BLIND), which a tile shows
    # only once they are a patch. Above `overview_small_fraction` of the
    # tissue the tile settles any class but OVERVIEW_BLIND_ANY_SIZE.
    "force_confirm_top_share": 0.1,
    "overview_blind_tile_px": 4.0,   # a 2x2-pixel dot on the 256 px tile
    "overview_fine_tile_px": 16.0,   # a 4x4-pixel patch of specks
    # Raw candidates per detector and channel kept before the merge (the
    # strongest by `Candidate.strength`); the rest are counted in the
    # residual as `capped`. 5,400 aggregate specks over 40 channels made the
    # merge and the ranking the slow part of a live scan, for specks the
    # per-channel cap (`candidates_per_channel`) then dropped anyway.
    "raw_per_channel": 64,
    # Bright compact cells in many stained markers at once, but not in the
    # unstained / autofluorescence channels: dense real tissue (an immune
    # infiltrate, an epidermis), not debris -- debris glows in (nearly) every
    # channel, the unstained ones included. A merged aggregate group in at
    # least `dense_tissue_min_markers` markers is that signature when the
    # image has an autofluorescence channel and less than
    # `dense_tissue_af_share` of the region is bright in every one of them,
    # or, without one, when it is bright in less than `debris_marker_share`
    # of the stained markers. Its score is scaled by `dense_tissue_factor`
    # (so it is not pursued on a clean row) and the reason recorded.
    "dense_tissue_min_markers": 6,
    "dense_tissue_af_share": 0.25,
    "debris_marker_share": 0.9,
    "dense_tissue_factor": 0.5,
    "confirm_batch": 4,            # first looks at candidates, one sheet row each, per packet
    "cell_batch": 6,               # cell modules judged in one packet (two images at most)
    "merge_iou": 0.7,
    "merge_contain": 0.8,          # ...or this share of the smaller inside the larger
    "confirm_levels": 3,
    "localize_rounds": 1,
    "grid_rounds": 2,
    "grid_side": 8,
    "cell_rounds": 2,
    # One too_lenient / too_aggressive answer moves a cutoff by the larger of
    # this many MADs and this share of its distance from the median, never
    # nearer the median than `offset_min_keep` of that distance (see
    # cells.modules.step_size). share * the two steps allowed stays < 1, which
    # keeps every preset nested after the same moves.
    "offset_step_mad": 1.0,
    "offset_step_share": 0.25,
    "offset_min_keep": 0.4,
    "final_reopens": 1,
    "invalid_answers": 2,
    "large_region_fraction": 0.3,
    # The image checks. A score review may move its bar this many times (one
    # step each, `offset_steps`), never more than `adjust_max_steps` from the
    # automatic threshold -- the same bound the free path's `adjust` has. A
    # step is the larger of `score_step_mad` MADs of the score and the
    # check's floor. Rows hold `score_per_stratum` tiles at least
    # `score_spacing_um` apart; a flagged region needs `score_min_region_cells`
    # score cells. A region at least `score_direct_confirm_margin_steps`
    # above the bar is decided by the review itself when its rows say
    # artifact; the rest are confirmed one by one. When nothing can be told,
    # at most `check_max_manual_regions` regions are written for review. Up to
    # `check_confirm_per_channel` of a check's regions get a look (four to a
    # sheet) before the rest go to manual review.
    "score_rounds": 2,
    "adjust_max_steps": 2,
    "score_step_mad": 1.0,
    "score_step_floor": {"blur": 0.05, "registration": 0.05, "segmentation": 0.10,
                         "artifacts": 0.10},
    "score_per_stratum": 6,
    "score_spacing_um": 60,
    "score_min_region_cells": {"blur": 4, "registration": 6, "segmentation": 3,
                               "artifacts": 1},
    "score_direct_confirm_margin_steps": 1,
    "max_registration_pairs": 6,
    "check_max_manual_regions": 8,
    "check_confirm_per_channel": 16,
    # A check's to-confirm regions are probed first (check_candidates.
    # hold_for_probes): its `check_confirm_probe` strongest (by score) get a
    # look; the rest wait. When every probe comes back the same verdict --
    # all not artifacts, or all artifacts of one class -- that verdict is
    # applied to the rest (`extrapolated`, with the probes as its source);
    # otherwise the rest are released and confirmed one by one. A group no
    # larger than the probe count is confirmed as it is.
    "check_confirm_probe": 4,
    # The Registration Check's one-cycle places (nuclei in one cycle only:
    # tissue lost, or debris) become `cycle_specific_tissue_loss` candidates
    # of the cycle that lost them: the largest `check_one_cycle_regions`
    # regions of at least `check_one_cycle_min_cells` map cells.
    "check_one_cycle_regions": 8,
    "check_one_cycle_min_cells": 24,
    # Segmentation QC's cell modules: one step of a score cutoff (under /
    # over, 0..1) and of a robust-z cutoff (size, shape).
    "seg_step_score": 0.05,
    "seg_step_z": 0.5,
}

#: What a channel-audit tile (the whole tissue in ~256 px) shows well: a
#: `clean` verdict on the row settles a candidate of these classes, however
#: strong its score -- a seam, shading, background or a failed stain spans the
#: tile. Any other class is still looked at closer when its score is at
#: least `force_confirm_score` and it covers at most `overview_small_fraction`
#: of the tissue (a small fold or patch of blur is a few pixels there), and
#: the classes in OVERVIEW_BLIND at any size: an antibody aggregate is specks,
#: a misregistration a sub-cell shift between cycles, neither visible on one
#: channel's tile.
OVERVIEW_VISIBLE = ("empty_or_failed_channel", "illumination_or_shading",
                    "stitching_or_tile_seam", "excessive_background", "autofluorescence",
                    "slide_or_tissue_edge")
OVERVIEW_BLIND = ("antibody_aggregate", "cross_cycle_registration_error")
#: ...and of those, the ones no single channel's tile shows at any size: a
#: misregistration is a shift between cycles. (Specks covering a large share
#: of the tissue are a texture the tile does show.)
OVERVIEW_BLIND_ANY_SIZE = ("cross_cycle_registration_error",)

NARRATION = {
    "pixel_setup": "Estimating this image's pixel size from the size of its nuclei.",
    "channel_audit": "I'm reviewing {n} channels for staining and imaging problems.",
    "artifact_confirm": "I'm checking a suspected {class_words} in {channel}.",
    "artifact_confirm_batch": "I'm checking {n} suspected artifacts side by side.",
    "artifact_scope": "Working out how many channels the {class_words} at {channel} reaches.",
    "artifact_localize": "Refining where the {class_words} in {channel} begins and ends.",
    "artifact_grid": "Marking the {class_words} in {channel} on a grid.",
    "cell_intensity": "Checking cells with unusually weak or strong nuclear staining.",
    "cell_modules": "Checking the cells beside {n} cell-QC cutoffs at once.",
    "cell_area": "Checking unusually large and small segmented cells.",
    "cycle_stability": "Comparing first and last nuclear cycles.",
    "channel_outlier": "Looking at the brightest {channel} cells.",
    "final_qc_review": "Reviewing the whole QC picture before I close.",
    "score_review": "I'm checking tiles across the {check_words} score in {channel}.",
    "cell_segmentation": "Checking cells the mask may have drawn wrong.",
}

EVIDENCE_LABELS = {
    "audit_sheet": "whole-tissue view of each channel",
    "confirm_sheet": "the suspected region at three scales",
    "confirm_batch_sheet": "several suspected regions, one row each",
    "scope_sheet": "the same place across channels",
    "localize_sheet": "candidate outlines of the region",
    "grid_sheet": "the region on a labelled grid",
    "cell_collage": "cells beside the proposed cutoffs",
    "review_sheet": "every QC region on the tissue",
    "pixel_snapshots": "nuclei with a ten-micron ring",
    "score_sheet": "tiles sampled across a check's score range",
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
    "area.solidity_min": "up",
    "area.seg_conf_min": "up",
    "area.size_alone": "up",
    "cycle.abs_floor": "down",
    "cycle.k": "down",
    "outlier.k": "down",
    "cells.roi_overlap_fraction": "down",
    "cells.marker_quantile": "down",
}

#: [cal] the three presets. Custom tables are refused outside [lenient, strict].
STRICTNESS_PRESETS = {
    "lenient": {
        "artifact.exclude_min_severity": 2, "artifact.exclude_min_confidence": 0.65,
        "artifact.warn_min_severity": 1, "artifact.min_area_fraction_exclude": 0.002,
        "artifact.large_region_exclude": 0,
        "counterstain.low_k": 3.5, "counterstain.high_k": 4.0,
        "area.k": 4.0, "area.ratio_low": 0.02, "area.ratio_high": 0.98,
        "area.solidity_min": 0.5, "area.seg_conf_min": 0.2,
        "area.size_alone": 0, "cycle.abs_floor": 0.5, "cycle.k": 4.0, "outlier.k": 6.0,
        "cells.roi_overlap_fraction": 0.75, "cells.marker_quantile": 0.995,
    },
    "standard": {
        "artifact.exclude_min_severity": 1, "artifact.exclude_min_confidence": 0.65,
        "artifact.warn_min_severity": 0, "artifact.min_area_fraction_exclude": 0.001,
        "artifact.large_region_exclude": 0,
        "counterstain.low_k": 3.0, "counterstain.high_k": 3.5,
        "area.k": 3.5, "area.ratio_low": 0.05, "area.ratio_high": 0.95,
        "area.solidity_min": 0.6, "area.seg_conf_min": 0.35,
        "area.size_alone": 0, "cycle.abs_floor": 0.35, "cycle.k": 3.5, "outlier.k": 5.0,
        "cells.roi_overlap_fraction": 0.5, "cells.marker_quantile": 0.99,
    },
    "strict": {
        "artifact.exclude_min_severity": 0, "artifact.exclude_min_confidence": 0.3,
        "artifact.warn_min_severity": 0, "artifact.min_area_fraction_exclude": 0.0005,
        "artifact.large_region_exclude": 1,
        "counterstain.low_k": 2.5, "counterstain.high_k": 3.0,
        "area.k": 3.0, "area.ratio_low": 0.08, "area.ratio_high": 0.90,
        "area.solidity_min": 0.7, "area.seg_conf_min": 0.5,
        "area.size_alone": 1, "cycle.abs_floor": 0.25, "cycle.k": 3.0, "outlier.k": 4.0,
        "cells.roi_overlap_fraction": 0.25, "cells.marker_quantile": 0.975,
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


def _assert_categories():
    """Every class and every reason has a category, every default class
    exists, and every reason can be a cell's primary one."""
    for klass in ARTIFACT_CLASSES:
        if CLASS_CATEGORY.get(klass) not in (*CATEGORY_IDS, REVIEW["id"]):
            raise AssertionError(f"class {klass} has no category")
    for reason in (*REASONS, *MARKER_REASONS):
        if CELL_REASON_CATEGORY.get(reason) not in (*CATEGORY_IDS, REVIEW["id"]):
            raise AssertionError(f"reason {reason} has no category")
    for category in CATEGORIES:
        if category["default_class"] not in ARTIFACT_CLASSES \
                or CLASS_CATEGORY[category["default_class"]] != category["id"]:
            raise AssertionError(f"category {category['id']} has a stray default class")
    if set(PRIMARY_ORDER) != set(REASONS):
        raise AssertionError("PRIMARY_ORDER names every reason, once")
    if set(REASON_DEFINITIONS) != set(REASONS):
        raise AssertionError("every reason is defined")


_assert_categories()
