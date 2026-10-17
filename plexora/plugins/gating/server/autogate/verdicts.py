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

`about_right` is the weakest of these readings (wrong 22 of 24 times on
lsp11385_2 when the human gate lay elsewhere; behind most PCA misses, with
`near: mixed`). A look at both sides of the gate was tried for it and did
not help: the rows beside the gate read mixed too (2026-10-11,
docs/internal/GATING_REPLAN_PLAN.md).
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


def direction_from_rows(rows, pattern=None, *, mixed_down=False) -> tuple:
    """(direction, why) for a first look's rows (see the module docstring).
    `direction` is one of `DIRECTIONS`; `why` names the rule that fired.
    `mixed_down` (`SessionOptions.vision_first`): cells below the gate seen
    mixed, with none at the gate seen unstained, hold positives the gate
    misses, so it goes down -- the way cells above seen mixed send it up."""
    rows = {k: v for k, v in (rows or {}).items() if v in schemas.ROW_VERDICTS}
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
    if mixed_down and below == "mixed" and near in ("mixed", "cannot_tell", None):
        return "down", "the cells below the gate include positives"
    return "about_right", "positive above, not below"


def legacy_direction(direction) -> str | None:
    """The engine's older words for a direction (`too_low` raises the gate)."""
    return {"up": "too_low", "down": "too_high"}.get(direction, direction)
