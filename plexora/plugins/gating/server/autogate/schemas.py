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
PROFILE_VERSION = "5"

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
                 "no_positive_population", "not_binary", "insufficient_information")

#: The terminal states whose gate was accepted.
ACCEPTED_STATES = ("accepted", "accepted_low_confidence")

#: The terminal states whose gate is written empty -- low == high at the
#: column's maximum, so no cell reads positive: a failed stain, or a stain
#: that worked in an image with no positive cell.
EMPTY_GATE_STATES = ("technically_failed", "no_positive_population")
#: Closed for review, and written all the same at the best gate reached,
#: tagged `needs_review`: automatic gating leaves no gated marker without a
#: value.
REVIEW_WRITE_STATES = ("manual_review_recommended", "not_binary", "insufficient_information")

#: The terminal states that write a gate (apply mode, or a propose-mode commit).
WRITTEN_STATES = ACCEPTED_STATES + EMPTY_GATE_STATES + REVIEW_WRITE_STATES

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
#: A look's verdict on whether what it saw fits the sample's tissue and
#: disease (`biology`): a conflict or an ambiguity caps confidence; agreement,
#: with every other line of evidence, lets it rise.
BIOLOGY_FIT = ("consistent", "conflicts", "ambiguous", "not_judged")
#: `pixel_setup` (what one pixel is worth, for an image that does not say) is
#: set-up too, but the agent answers it from its own look, and the bulk pass
#: runs meanwhile: profiling needs no pixel size, only the pictures do.
SETUP_KINDS = USER_SETUP_KINDS + ("pixel_setup", "panel_context")
LOOK_KINDS = ("t1_strip", "t2_confirm", "t3_biological", "t4_candidates")
CHECK_KINDS = ("qc_confirm", "regression_confirm", "transfer_check")

#: What the agent is doing, as a viewer shows it (`engine.phase_for`).
PHASES = ("planning", "analyzing", "inspecting", "thinking", "validating", "waiting",
          "summarizing")

#: The `gating.session` events a viewer is told about (`_announce`).
SESSION_EVENTS = ("started", "control", "issued", "phase", "answered", "unit_closed",
                  "needs_setup", "limit_reached", "limit_answered", "finished", "report")

#: What the viewer's agent panel says the agent is doing, one line per packet
#: (`packets.narrate`): for the user watching, in the first person, never
#: the question put to the agent (that is the prompt, and stays with it).
#: Keys are `kind` or `kind:situation`; `{marker}`, `{partner}`,
#: `{references}` and `{n}` are filled in.
NARRATION = {
    "t2_confirm": "I'm evaluating {marker} expression across the tissue and at its current "
                  "threshold.",
    "t2_confirm:partner": "I'm evaluating {marker} across the tissue, using {partner} to "
                          "check which cells should carry it.",
    "t2_confirm:partner_exclusive": "I'm evaluating {marker} across the tissue, using "
                                    "{partner} to rule out cells that should not carry it.",
    "t2_confirm:tissue": "Reviewing {marker}'s tissue-scale pattern before judging the cells "
                         "at its threshold.",
    "t2_confirm:within": "Gating {marker} only among {partner}-positive cells, and checking "
                         "that threshold in the tissue.",
    "t3_biological": "Adding {references} as a complementary marker to check where "
                     "{marker}-positive cells sit.",
    "t4_candidates:up": "The {marker} threshold calls too many cells positive; comparing the "
                        "cells between candidate thresholds above it.",
    "t4_candidates:down": "The {marker} threshold misses real positives; comparing the cells "
                          "between candidate thresholds below it.",
    "regression_confirm": "Checking whether the {marker} threshold is consistent across "
                          "representative tissue regions.",
    "qc_confirm": "Checking whether {marker} staining is real anywhere in the tissue.",
    "t1_strip": "Reviewing {n} markers whose populations split cleanly, at a glance.",
    "transfer_check": "Checking that the {marker} threshold holds on the other images.",
    "panel_context": "Reading the panel to learn which markers belong together.",
    "expression_setup": "Working out which expression values to gate on.",
    "pixel_setup": "Estimating this image's pixel size from the size of its nuclei.",
}

#: The caption under the evidence thumbnail in the panel, by image role.
EVIDENCE_LABELS = {
    "t2_collage": "Cells around the {marker} threshold",
    "t3_collage": "{marker} beside {references}",
    "flips_collage": "Cells between candidate {marker} thresholds",
    "context_sheet": "{marker} across the tissue",
    "strip": "Markers at their thresholds",
}

#: What a session does when a marker reaches its allowance (looks, images,
#: candidate rounds) while the evidence still says to keep going. `ask`: the
#: marker waits while the user is asked in the viewer (and the calling agent
#: is told), the rest of the session goes on; `extend`: another allowance is
#: granted without asking, for automated runs; `stop`: the marker is flagged
#: for manual review at once. However the policy reads, a marker is never
#: accepted because it ran out: stopping short of a conclusion is always
#: `manual_review_recommended`, with the best gate reached written and
#: tagged `needs_review`. `max_extensions` bounds the extra allowances a marker can get.
LIMIT_POLICIES = ("ask", "extend", "stop")
LIMIT_DEFAULTS = {"on_limit": "ask", "max_extensions": 3}
#: The environment a command line sets them through (`plexora mcp serve
#: --gating-on-limit`, `plexora ai bench gating --on-limit`), so automated
#: runs do not stop on a viewer default.
LIMIT_ENV = {"on_limit": "PLEXORA_GATING_ON_LIMIT",
             "max_extensions": "PLEXORA_GATING_MAX_EXTENSIONS"}
#: What each limit is called where the user reads it.
LIMIT_WORDS = {"budget": "its allowance of looks for this marker",
               "rounds": "its rounds of candidate thresholds"}
LIMIT_DECISIONS = ("continue", "stop")

#: How a marker's compartment shapes the strategy. `image_led`: a cell mean
#: over a nuclear-dilated mask under-represents the stain, so a confident look
#: that a whole-image check agrees with outranks the distribution's shape
#: (`IMAGE_LED_RELAXED` stop capping confidence), and a DNA correlation is the
#: nucleus-based mask, never `nuclear_bleed`. `nuclear_bleed_expected`: a
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

#: How a session came by an image's pixel size, when the image states none:
#: the estimate looked right, the agent adjusted it by eye, or the user said.
#: Only `user_stated` is written to the project.
PIXEL_BASES = ("estimate_confirmed", "adjusted", "user_stated")

#: A session's pixel-size set-up, per image and overall.
PIXEL_STATES = ("pending", "applied", "asked", "unavailable")

#: A T4 answer that picks no candidate: the current gate was right, or no
#: boundary between the rows separates stained from unstained cells.
T4_CHOICES = ("keep", "none_separates")

#: What an agent says about its own judgment: three words, each a fixed
#: number the confidence rule reads (`ENGINE`'s cut-points sit between them),
#: so the same judgment always lands in the same state -- never a float an
#: agent picks to the second decimal.
AI_CONFIDENCE = {"sure": 0.9, "fairly_sure": 0.65, "unsure": 0.3}

#: How a T4 interval row is judged: the cells that flip across it.
INTERVAL_VERDICTS = ("mostly_positive", "mostly_negative", "mixed")

#: [cal] the engine's own cut-points (`engine.ENGINE` is this dict).
ENGINE = {"t2_min_confidence": 0.6, "t4_rounds": 4, "strip_batch": 8,
          "strip_cells": 6, "invalid_answers": 2,
          "high_ai": 0.75, "moderate_ai": 0.5, "delta_high": 0.5, "delta_moderate": 1.5,
          # a session's default ceiling: numbers, a look, a reference, candidates
          "max_tier_default": 4,
          # a partner pair this contradictory is listed for review by gating_qc
          "needs_review_contradiction": 0.5,
          # markers one delegated worker answers before handing back
          # (`plexora.ai.delegation`): each packet adds ~4.5k tokens of context
          # that every later call re-reads (live run lsp11385: 46k -> 480k over
          # 91 packets, 29.6M cache reads), against a worker's fixed start
          # (its brief, skill and guide); the cost per marker is flat from
          # three to six
          "markers_per_worker": 4}


def hard(flags) -> list:
    return [flag for flag in flags or () if flag in HARD_FLAGS]


def soft(flags) -> list:
    return [flag for flag in flags or () if flag not in HARD_FLAGS]
