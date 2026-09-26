"""The vocabulary automatic gating speaks: versions, classes, strata, flags.

The evidence itself travels as plain JSON-safe dicts -- it is produced by table
operations, which must survive `json.dumps` to run on a data node -- and this
module fixes the names those dicts use, so the engine, the skills and the
tests all spell a class or a flag the same way.
"""

from __future__ import annotations

#: Bumped whenever a profile's numbers change meaning; part of every cache key.
PROFILE_VERSION = "1"

#: What a marker's distribution looks like, first match wins (see profile.classify).
CLASSES = ("degenerate", "saturated", "unimodal", "bimodal", "weakly_bimodal",
           "long_tail", "continuous")

#: Tiers of decision, cheapest first.
TIERS = ("T1", "T2", "T3", "T4", "T5")

#: Cells are sampled by where they sit relative to the gate, in the fit's space.
STRATA = ("clear_negative", "low_background", "just_below", "borderline", "just_above",
          "moderate_positive", "strong_positive", "extreme")

#: A technical flag that on its own sends a marker to confirmation.
HARD_FLAGS = ("saturated", "empty_channel", "weak_signal", "illumination_gradient",
              "no_values")

#: Flags that lower confidence but decide nothing alone.
SOFT_FLAGS = ("pileup", "size_correlated", "nuclear_bleed", "edge_enriched",
              "edge_depleted", "near_zero_plane", "spatially_clustered",
              "isolated_positives", "log_ambiguous", "no_image_channel", "sparse",
              "estimators_disagree", "unstable_fit")

#: Artifacts an agent may name when it looks at the cells.
ARTIFACTS = ("image_quality", "segmentation", "saturation", "bleedthrough",
             "autofluorescence", "tissue_fold", "uneven_staining", "edge")

#: Terminal states of one marker in a run.
TERMINAL_STATES = ("accepted", "accepted_low_confidence", "manual_review_recommended",
                   "technically_failed", "not_binary", "insufficient_information",
                   "skipped_locked", "skipped_excluded", "skipped_no_marker")

CONFIDENCE = ("high", "moderate", "low", "manual_review", "failed_qc")


def hard(flags) -> list:
    return [flag for flag in flags or () if flag in HARD_FLAGS]


def soft(flags) -> list:
    return [flag for flag in flags or () if flag not in HARD_FLAGS]
