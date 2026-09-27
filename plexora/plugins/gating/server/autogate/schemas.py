"""The vocabulary automatic gating speaks: versions, classes, strata, flags.

The evidence itself travels as plain JSON-safe dicts -- it is produced by table
operations, which must survive `json.dumps` to run on a data node -- and this
module fixes the names those dicts use, so the engine, the answer models, the
tools, the skills and the tests all spell a class, a state or a flag the same
way. Every other module derives its tuples and Literals from these; nothing
restates them (tests/test_ai_skills.py checks the skills against them).
"""

from __future__ import annotations

#: Bumped whenever a profile's numbers change meaning; part of every cache key.
PROFILE_VERSION = "3"

#: What a marker's distribution looks like, first match wins (see profile.classify).
CLASSES = ("degenerate", "saturated", "unimodal", "bimodal", "weakly_bimodal",
           "long_tail", "continuous")

#: Tiers of decision, cheapest first.
TIERS = ("T1", "T2", "T3", "T4", "T5")

#: Cells are sampled by where they sit relative to the gate, in the fit's space.
STRATA = ("clear_negative", "low_background", "just_below", "borderline", "just_above",
          "moderate_positive", "strong_positive", "extreme")

#: A technical flag that on its own sends a marker to confirmation.
HARD_FLAGS = ("saturated", "empty_channel", "illumination_gradient", "no_values")

#: Flags that lower confidence but decide nothing alone.
#: `weak_signal` is here, not above: CyCIF mean intensities carry a high
#: background, and a real stain (E-cadherin tracing every gland) routinely sits
#: under 2x it in linear units.
SOFT_FLAGS = ("weak_signal", "pileup", "size_correlated", "nuclear_bleed", "edge_enriched",
              "edge_depleted", "near_zero_plane", "spatially_clustered",
              "isolated_positives", "log_ambiguous", "no_image_channel", "sparse",
              "estimators_disagree", "unstable_fit")

#: Artifacts an agent may name when it looks at the cells.
ARTIFACTS = ("image_quality", "segmentation", "saturation", "bleedthrough",
             "autofluorescence", "tissue_fold", "uneven_staining", "edge")

#: Terminal states of one marker in a run.
TERMINAL_STATES = ("accepted", "accepted_low_confidence", "manual_review_recommended",
                   "technically_failed", "not_binary", "insufficient_information",
                   "skipped_locked", "skipped_excluded", "skipped_manual", "skipped_no_marker")

#: The states `gating_qc` lists for a person to look at.
REVIEW_STATES = ("manual_review_recommended", "accepted_low_confidence", "technically_failed")

#: The terminal states whose gate was accepted.
ACCEPTED_STATES = ("accepted", "accepted_low_confidence")

#: A marker waiting on something: the audit sheet, a look, a check.
WAITING_STATES = ("accepted_t1", "qc_confirm", "awaiting_t2", "awaiting_t3", "awaiting_t4",
                  "awaiting_regression", "regression_confirm", "transfer_check")

#: Before any decision: not profiled yet, or (a dataset) waiting for the reference.
OPEN_STATES = ("pending", "transfer_pending")

#: A session's own states; the last four end it.
SESSION_STATES = ("created", "bulk_running", "deciding", "done", "cancelled", "rolled_back",
                  "failed")
FINISHED_STATES = ("done", "cancelled", "rolled_back", "failed")

#: What `gating_next` answers, besides a finished session's own state.
NEXT_STATES = ("decision", "bulk_running", "decided", "paused")

CONFIDENCE = ("high", "moderate", "low", "manual_review", "failed_qc")

#: A T4 answer that picks no candidate: the current gate was right, or no
#: boundary between the rows separates stained from unstained cells.
T4_CHOICES = ("keep", "none_separates")

#: [cal] the engine's own cut-points (`engine.ENGINE` is this dict).
ENGINE = {"t2_min_confidence": 0.6, "t4_rounds": 2, "strip_batch": 8,
          "strip_cells": 6, "contradiction_render": 0.5, "invalid_answers": 2,
          "high_ai": 0.75, "moderate_ai": 0.5, "delta_high": 0.5, "delta_moderate": 1.5,
          # a session's default ceiling: numbers, a look, a reference, candidates
          "max_tier_default": 4,
          # a look that goes on without a settled direction caps the agent's say
          "ai_confidence_cap": 0.3,
          # a partner pair this contradictory is listed for review by gating_qc
          "needs_review_contradiction": 0.5,
          # markers per conversation before an agent should start a fresh one
          "markers_per_conversation": 15}


def hard(flags) -> list:
    return [flag for flag in flags or () if flag in HARD_FLAGS]


def soft(flags) -> list:
    return [flag for flag in flags or () if flag not in HARD_FLAGS]
