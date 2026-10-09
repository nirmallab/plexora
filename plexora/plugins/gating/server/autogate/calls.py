"""Cells whose positive call the spatial evidence does not vouch for.

A gate is one number for the whole image; whether a positive cell's stain
is its own is a question about that cell. Gate confidence (`engine.
confidence_for`) says how sure the threshold is; this layer says, per cell,
which positives sit close to it where their stain may not be their own -- a
bright neighbour touching them, or a local background high enough to lift
them -- unless a look or the targeted pass confirmed them. It is a fourth
state beside `cell_exclusions`' three:

    POSITIVE (1), NEGATIVE (0), NOT_CALLED (-1), UNCERTAIN (2)

and it is written only over POSITIVE cells: an uncertain cell keeps its
positive call in every count, export and `calls()` -- the layer is shown,
never applied. It is derived from the gate, the feature layer and
`UNCERTAIN_VERSION`, so it is never stored in the session record; nothing
reads it to raise a confidence. `None` whenever the session's `spatial`
switch is off.

A cell's surroundings flag it only where the marker's own numbers say they
matter (`spatial.excesses`): touching a strong positive only when the cells
at the gate touch strong positives more often than clear positives do (spill
excess >= `spill_excess_low`), low SNR only when they sit in low SNR more
often (SNR excess >= `snr_excess_trigger`). Where positives simply crowd
together, half the true positives at the gate touch a strong one -- on
lsp11385_2, 1,000-9,000 positives a marker -- and proximity alone says
nothing about any of them.
"""

from __future__ import annotations

import numpy as np

from plexora.agent import cell_exclusions
from plexora.plugins.gating.server.autogate import schemas

UNCERTAIN = 2
UNCERTAIN_VERSION = schemas.UNCERTAIN_VERSION

#: The spill states that leave a positive uncertain: a bright neighbour
#: touches it and no picture has cleared it.
UNCERTAIN_SPILL = ("candidate", "untested", "supported")


def uncertain_mask(ds, marker, low, high=None, *, spatial=None, confirmed_ids=()):
    """Row-aligned bool, or None when `spatial` (`{mode, map, pixel_um}`) is
    None: positive at the gate, within two band halves of it, and either a
    spill state in `UNCERTAIN_SPILL` (where the marker's spill excess says
    spill concentrates at the gate) or in a low-SNR environment (where its
    SNR excess says the same of background) -- less the cells a look or the
    targeted pass confirmed (`confirmed_ids`: a bright row judged
    `own_stain`, or a targeted `unsupported`)."""
    if not spatial:
        return None
    from plexora.plugins.gating.server.autogate import cells as cellmod
    from plexora.plugins.gating.server.autogate import spatial as spatialmod

    bgmap = spatialmod.decode_map(spatial.get("map"))
    feats = spatialmod.per_cell(ds, marker, bgmap=bgmap, pixel_um=spatial.get("pixel_um"))
    gate = spatialmod.at_gate(ds, marker, feats, low, high, mode=spatial.get("mode") or "C",
                              bgmap=bgmap)
    rules = schemas.SPATIAL_RULES
    found = spatialmod.excesses(feats, gate)
    flagged = np.zeros(feats.n, dtype=bool)
    spill = found["spill"][2]
    if spill is not None and spill >= rules["spill_excess_low"]:
        flagged |= np.isin(gate.spill, [spatialmod.SPILL[s] for s in UNCERTAIN_SPILL])
    background = found["snr"][2]
    if background is not None and background >= rules["snr_excess_trigger"]:
        flagged |= gate.snr_class == spatialmod.SNR_LOW
    mask = gate.positive & gate.near & flagged
    if confirmed_ids:
        c = cellmod.cells(ds)
        mask &= ~np.isin(c.ids, np.asarray(list(confirmed_ids), dtype=np.int64))
    return mask


def calls_with_uncertainty(ds, marker, values, low, high, mask) -> np.ndarray:
    """`cell_exclusions.calls` with `UNCERTAIN` written over the positive
    cells `mask` names (never over a negative or a not-called cell)."""
    out = cell_exclusions.calls(ds, marker, values, low, high)
    if mask is not None and mask.shape[0] == out.shape[0]:
        out[mask & (out == cell_exclusions.POSITIVE)] = UNCERTAIN
    return out


def uncertain_for_gate(session, ds, marker):
    """(row mask or None, why): the uncertain positives at the marker's
    CURRENT gate, for a gate a session decided with spatial evidence
    (`detail["spatial"]` of its provenance row, with `decide`) -- the mode,
    and the cells its looks cleared, read from there. None (and why) for any
    other gate: the layer belongs to the spatial evidence, not to the gate."""
    from plexora.plugins.gating.server import model, provenance
    from plexora.plugins.gating.server.autogate import spatial, tableops

    row = provenance.read(ds.name).get(marker) or {}
    found = (row.get("detail") or {}).get("spatial")
    if not isinstance(found, dict) or found.get("option") != "decide":
        return None, "no spatial evidence for this gate"
    gate = model.get_gate(ds, marker)
    if not gate.get("thresholded") or gate.get("low") is None:
        return None, "the marker is not gated"
    payload = spatial.payload_for(session, ds, marker, found.get("mode") or "C")
    result = tableops.local_or_node(ds, "gating.autogate.uncertain", {
        **payload, "marker": marker, "low": float(gate["low"]), "high": float(gate["high"]),
        "confirmed": list(found.get("confirmed") or []), "rows": True})
    n = int(ds.table.geometry().height)
    mask = np.zeros(n, dtype=bool)
    mask[np.asarray(result.get("rows") or [], dtype=np.int64)] = True
    return mask, None
