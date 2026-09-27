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
PROFILE_VERSION = "4"

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
                   "technically_failed", "no_positive_population", "not_binary",
                   "insufficient_information",
                   "skipped_locked", "skipped_excluded", "skipped_manual", "skipped_no_marker")

#: The states `gating_qc` lists for a person to look at.
REVIEW_STATES = ("manual_review_recommended", "accepted_low_confidence", "technically_failed",
                 "no_positive_population")

#: The terminal states whose gate was accepted.
ACCEPTED_STATES = ("accepted", "accepted_low_confidence")

#: The terminal states whose gate is written empty -- low == high at the
#: column's maximum, so no cell reads positive: a failed stain, or a stain
#: that worked in an image with no positive cell.
EMPTY_GATE_STATES = ("technically_failed", "no_positive_population")

#: The terminal states that write a gate (apply mode, or a propose-mode commit).
WRITTEN_STATES = ACCEPTED_STATES + EMPTY_GATE_STATES

#: The terminal states that skipped the marker without deciding it.
SKIPPED_STATES = tuple(s for s in TERMINAL_STATES if s.startswith("skipped_"))

#: A marker waiting on something: the audit sheet, a look, a check.
WAITING_STATES = ("accepted_t1", "qc_confirm", "awaiting_t2", "awaiting_t3", "awaiting_t4",
                  "awaiting_regression", "regression_confirm", "transfer_check")

#: Before any decision: not profiled yet, or (a dataset) waiting for the reference.
OPEN_STATES = ("pending", "transfer_pending")

#: A session's own states; the last four end it. `needs_setup` waits on the
#: user (which expression matrix to gate) before the bulk pass may start.
SESSION_STATES = ("created", "needs_setup", "bulk_running", "deciding", "done", "cancelled",
                  "rolled_back", "failed")
FINISHED_STATES = ("done", "cancelled", "rolled_back", "failed")

#: What `gating_next` answers, besides a finished session's own state.
NEXT_STATES = ("decision", "needs_setup", "bulk_running", "decided", "paused", "stopped")

#: Packet kinds, partitioned by what they are for (a test checks the union is
#: every packet builder): set-up before any marker (the ones that wait on the
#: user first), looks at a marker's cells, and whole-image checks.
USER_SETUP_KINDS = ("expression_setup",)
SETUP_KINDS = USER_SETUP_KINDS + ("panel_context",)
LOOK_KINDS = ("t1_strip", "t2_confirm", "t3_biological", "t4_candidates")
CHECK_KINDS = ("qc_confirm", "regression_confirm", "transfer_check")

#: What the agent is doing, as a viewer shows it (`engine.phase_for`).
PHASES = ("planning", "analyzing", "inspecting", "thinking", "validating", "summarizing")

#: The `gating.session` events a viewer is told about (`_announce`).
SESSION_EVENTS = ("started", "control", "issued", "phase", "answered", "unit_closed",
                  "needs_setup", "finished", "report")

#: How a marker's compartment shapes the strategy. `image_led`: a cell mean
#: over a nuclear-dilated mask under-represents the stain, so a confident look
#: that a whole-image check agrees with outranks the distribution's shape
#: (`IMAGE_LED_RELAXED` stop capping confidence). `nuclear_bleed_expected`: a
#: marker correlated with the DNA channel is not flagged for it.
COMPARTMENT_POLICY = {
    "nuclear": {
        "image_led": False, "nuclear_bleed_expected": True,
        "reading": "a nuclear marker: the mask covers the stain, so the cell's value is a fair "
                   "summary; expect the stain inside the blue nucleus"},
    "nuclear_cytoplasmic": {
        "image_led": False, "nuclear_bleed_expected": True,
        "reading": "nuclear and cytoplasmic: the mask covers most of the stain; expect it in "
                   "and around the nucleus"},
    "cytoplasmic": {
        "image_led": True, "nuclear_bleed_expected": False,
        "reading": "a cytoplasmic marker: a nucleus-based mask catches only part of the "
                   "stain, so a real positive can read lower than it looks; trust a clear "
                   "cytoplasmic ring or halo in the image over a borderline number"},
    "membrane": {
        "image_led": True, "nuclear_bleed_expected": False,
        "reading": "a membrane marker: a nucleus-based mask under-represents a ring at the "
                   "cell edge, so positives read lower than they look; trust a clear ring at "
                   "the outline over a borderline number, and judge the tissue pattern in the "
                   "fields first"},
    "extracellular": {
        "image_led": True, "nuclear_bleed_expected": False,
        "reading": "an extracellular marker: the stain lies between cells, so cell means are "
                   "a weak summary; judge the pattern in the fields and the whole image first"},
}

#: The soft flags an image-led marker's confident, checked look waives.
IMAGE_LED_RELAXED = ("unstable_fit", "estimators_disagree", "weak_signal", "log_ambiguous")

CONFIDENCE = ("high", "moderate", "low", "manual_review", "failed_qc")

#: A T4 answer that picks no candidate: the current gate was right, or no
#: boundary between the rows separates stained from unstained cells.
T4_CHOICES = ("keep", "none_separates")

#: [cal] the engine's own cut-points (`engine.ENGINE` is this dict).
ENGINE = {"t2_min_confidence": 0.6, "t4_rounds": 2, "strip_batch": 8,
          "strip_cells": 6, "invalid_answers": 2,
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
