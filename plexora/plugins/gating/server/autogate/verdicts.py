"""What a look's row words mean for the gate -- decided here, never by the agent.

A look answers one closed question per collage row about the cells around the
marked nucleus (`schemas.ROW_VERDICTS`): `own_stain`, `neighbour_only`,
`unstained`, `mixed` or `cannot_tell`. The agent never says which way the
gate should go; this table does, so two agents that see the cells alike move
the gate alike, and a rerun with the same answers moves it the same way.

First looks (`direction_from_rows`), rows `below` / `near` / `above` the gate,
evaluated in order:

    rows                                                       direction
    pattern `none`, every judged row unstained/neighbour_only  no_positives
    above unstained/neighbour_only and below own_stain         cannot_tell
    above unstained/neighbour_only/mixed                       up
    below own_stain                                            down
    near unstained                                             up
    near own_stain                                             down
    otherwise                                                  about_right

A first look's rows are the cells nearest the gate, never the clear
positives: rows that are all negative say "raise", and only a look that saw
no stained cell anywhere (`pattern: none`) says the marker has no positives.
At a right gate the cells AT it are the ones a stained neighbour lifts, so
`near: neighbour_only` is what a right gate looks like; `near: unstained`
puts the gate in plain background.

Looks at candidates (`MOVES`, `lattice.place`): going up the gate passes each
row whose cells are not positive, going down each row whose cells are.

With spatial evidence (`SessionOptions.spatial = decide`) a first look's
direction is first put to a like-for-like look when its words and the
image's numbers both point at spill-over or background (`contrast_trigger`),
and that look's rows -- the same intensity in different surroundings -- are
read by a second table (`spatial_case`): an error seen in every environment
moves the gate, one concentrated beside bright neighbours or in high
background holds it, flagged. Explanation words add evidence to a case; they
never set a direction.
"""

from __future__ import annotations

from plexora.plugins.gating.server.autogate import schemas

#: The words that say "these cells are not positive themselves".
NOT_OWN = ("unstained", "neighbour_only")

#: Which rows a look at candidates moves the gate past, by direction.
MOVES = {"up": NOT_OWN, "down": ("own_stain",)}

#: The words that say the opposite of moving, by direction: a first row of
#: these is a reason to look the other way once (`lattice.place`).
AGAINST = {"up": ("own_stain",), "down": NOT_OWN}

DIRECTIONS = ("up", "down", "about_right", "cannot_tell", "no_positives")


def direction_from_rows(rows, pattern=None) -> tuple:
    """(direction, why) for a first look's rows (see the module docstring).
    `direction` is one of `DIRECTIONS`; `why` names the rule that fired. Only
    `schemas.CORE_ROWS` are read: a spatial row says where cells sit, not
    which way the gate goes."""
    rows = {k: v for k, v in (rows or {}).items()
            if v in schemas.ROW_VERDICTS and k in schemas.CORE_ROWS}
    judged = {k: v for k, v in rows.items() if v != "cannot_tell"}
    above, below, near = rows.get("above"), rows.get("below"), rows.get("near")
    if pattern == "none" and "above" in judged and all(v in NOT_OWN for v in judged.values()):
        return "no_positives", "nothing stained anywhere"
    if above in NOT_OWN and below == "own_stain":
        return "cannot_tell", "stained below the gate but not above it"
    if above in NOT_OWN + ("mixed",):
        return "up", "the cells above the gate are not all positive"
    if below == "own_stain":
        return "down", "the cells below the gate are positive"
    if near == "unstained":
        return "up", "the cells at the gate are background"
    if near == "own_stain":
        return "down", "the cells at the gate are positive"
    if not judged:
        return "cannot_tell", "no row could be judged"
    return "about_right", "positive above, not below"


def legacy_direction(direction) -> str | None:
    """The engine's older words for a direction (`too_low` raises the gate)."""
    return {"up": "too_low", "down": "too_high"}.get(direction, direction)


# -- spatial evidence (`SessionOptions.spatial = decide`) -------------------------------

#: The explanation words that point at spill-over, background, weak expression.
SPILL_WORDS = ("possible_neighbor_spillover",)
BACKGROUND_WORDS = ("possible_local_background",)
WEAK_WORDS = ("genuine_weak_expression",)
SEGMENTATION_WORDS = ("possible_segmentation_error",)


def _rule(name):
    return schemas.SPATIAL_RULES[name]


def contrast_trigger(direction, rows, explanations=(), summary=None) -> str | None:
    """The side (`above` / `below`) a like-for-like look should compare, or
    None: a first look's words AND the image's numbers must both point at
    spill-over or background before a direction is held for one. In order:

        direction  words                                   numbers                    side
        up         near/above/near_bright neighbour_only   spill share >= trigger     above
                   or mixed, or a spill-over explanation   or risk high
        up         a background explanation, or near       SNR pf ratio >= rule       above
                   unstained
        down       below own_stain, or weak expression     risk not low, or spill     below
                                                           share >= low
        up         near/above carried by neighbour_only/   spill share >= low         above
                   mixed alone (neither unstained), no
                   threshold_error

    The last row is "neighbour_only alone never raises the gate": a raise
    that only neighbours' stain argues for is compared first."""
    rows = rows or {}
    explanations = set(explanations or ())
    summary = summary or {}
    share = summary.get("spill_share_near_gate")
    share = 0.0 if share is None else float(share)
    risk = summary.get("risk")
    ratio = summary.get("snr_pf_ratio")
    lit = ("neighbour_only", "mixed")
    core = {k: rows.get(k) for k in schemas.CORE_ROWS}
    if direction == "up":
        if (any(rows.get(k) in lit for k in ("near", "above", "near_bright"))
                or explanations & set(SPILL_WORDS)) \
                and (share >= _rule("spill_share_trigger") or risk == "high"):
            return "above"
        if (explanations & set(BACKGROUND_WORDS) or core.get("near") == "unstained") \
                and ratio is not None and ratio >= _rule("snr_pf_ratio"):
            return "above"
    if direction == "down" and (core.get("below") == "own_stain"
                                or explanations & set(WEAK_WORDS)) \
            and (risk in ("moderate", "high") or share >= _rule("spill_share_low")):
        return "below"
    carriers = [core.get("near"), core.get("above")]
    if direction == "up" and "unstained" not in carriers and any(v in lit for v in carriers) \
            and "threshold_error" not in explanations and share >= _rule("spill_share_low"):
        return "above"
    return None


def spatial_case(rows, explanations=(), summary=None, side=None) -> tuple:
    """(case, action, why, flags) for a like-for-like look's rows
    (`schemas.SPATIAL_CASES`, `schemas.SPATIAL_ACTIONS`) -- or None when the
    rows hold no environment row (the first look's rule table decides,
    `direction_from_rows`). Rows: B = `bright` (or `near_bright`), C =
    `clear`, DC = `dense_clear`, L = `low_snr`, R = `reference`, BG =
    `background`; `side` is the side of the gate the cells were drawn from.
    First match wins:

        rule  rows                                              case  action
        1     R not own                                         E     provisional_review
        2     B neighbour_only, C own_stain                     B     hold_flag_spill
        3     B neighbour_only/mixed, C mixed/cannot_tell,      B     hold_flag_spill
              spill share >= trigger
        4     L not own, C own_stain                            C     hold_investigate_background
        5     BG own_stain/mixed, and C own_stain or a          C     hold_investigate_background
              background explanation
        6     L not own, C mixed/cannot_tell, SNR ratio high    C     hold_investigate_background
        7     above: C, DC, L not own (or absent), B not own    A     move_up
        8     above: C, DC own (or absent), L own/absent        -     about_right
        9     below: C, DC own (or absent), L own/absent        D     move_down
        10    below: C, DC not own (or absent), B not own       -     about_right
        11    C and DC judged and disagree                      E     provisional_review
        12    a segmentation explanation, C mixed/cannot_tell   E     provisional_review
        13    otherwise                                         E     provisional_review

    An explanation adds evidence to a case the rows or numbers already
    support (rule 5); it never sets a direction."""
    rows = {k: v for k, v in (rows or {}).items() if v in schemas.ROW_VERDICTS}
    if not set(rows) & set(schemas.ENV_ROWS):
        return None
    explanations = set(explanations or ())
    summary = summary or {}
    share = summary.get("spill_share_near_gate")
    share = 0.0 if share is None else float(share)
    ratio = summary.get("snr_pf_ratio")
    B = rows.get("bright", rows.get("near_bright"))
    C = rows.get("clear")
    DC = rows.get("dense_clear")
    L = rows.get("low_snr")
    R = rows.get("reference")
    BG = rows.get("background")
    own = "own_stain"
    unclear = ("mixed", "cannot_tell")

    def absent_or(value, allowed):
        return value is None or value in allowed

    if R in NOT_OWN:
        return ("E", "provisional_review", "the strong reference cells do not look stained: "
                "the pictures cannot be read", ["reference_unreadable"])
    if B == "neighbour_only" and C == own:
        return ("B", "hold_flag_spill", "cells beside a bright neighbour carry its stain, "
                "clean cells of the same intensity carry their own", ["spill_concentrated"])
    if B in ("neighbour_only", "mixed") and C in unclear \
            and share >= _rule("spill_share_trigger"):
        return ("B", "hold_flag_spill", "cells beside a bright neighbour carry its stain and "
                "the clean cells do not settle it", ["spill_concentrated"])
    if L in NOT_OWN and C == own:
        return ("C", "hold_investigate_background", "cells in high background are not "
                "stained, clean cells of the same intensity are", ["background_concentrated"])
    if BG in (own, "mixed") and (C == own or explanations & set(BACKGROUND_WORDS)):
        return ("C", "hold_investigate_background", "cells well under the gate look stained "
                "where the background is high", ["background_concentrated"])
    if L in NOT_OWN and C in unclear and ratio is not None and ratio >= _rule("snr_pf_ratio"):
        return ("C", "hold_investigate_background", "positives concentrate where the "
                "background is high, and the clean cells do not settle it",
                ["background_concentrated"])
    if side == "above" and C in NOT_OWN and absent_or(DC, NOT_OWN) \
            and absent_or(L, NOT_OWN) and B != own:
        return ("A", "move_up", "cells just above the gate are not stained in any "
                "environment: the threshold is too low", [])
    if side == "above" and C == own and absent_or(DC, (own,)) \
            and absent_or(L, (own, "cannot_tell")):
        return (None, "about_right", "cells just above the gate carry their own stain in "
                "every environment", [])
    if side == "below" and C == own and absent_or(DC, (own,)) \
            and absent_or(L, (own, "cannot_tell")):
        return ("D", "move_down", "cells just below the gate carry their own stain in every "
                "environment: real dim positives", [])
    if side == "below" and C in NOT_OWN and absent_or(DC, NOT_OWN) and B != own:
        return (None, "about_right", "cells just below the gate are not stained in any "
                "environment", [])
    if C is not None and DC is not None and C not in unclear and DC not in unclear \
            and (C == own) != (DC == own):
        return ("E", "provisional_review", "clean cells in sparse and dense tissue disagree",
                ["density_disagreement"])
    if explanations & set(SEGMENTATION_WORDS) and C in unclear + (None,):
        return ("E", "provisional_review", "the outlines may take in other cells; the rows "
                "cannot be read cell by cell", ["segmentation_suspected"])
    return ("E", "provisional_review", "the like-for-like rows do not settle the gate", [])
