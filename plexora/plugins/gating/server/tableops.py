"""Gating work that has to happen where the cell table's file is.

Three things in this plugin cannot be answered with a buffer of values:

- **Writing thresholds into `uns`.** The whole reason the plugin has an AnnData
  path at all -- and it opens the user's file in place, past a consolidated
  zarr index, and rewrites exactly one subtree.
- **Reading them back.** Same file, same codec, same reason.
- **The per-channel mixture fit.** It needs the raw column in its own dtype,
  the whole column for the histogram edges, and a filtered copy for the fit.
  Sending all of that so the primary can do arithmetic on it would be sending
  the table.

There was a fourth -- exporting the whole cell table with each gated marker
rewritten to 1/0 or to a kept-or-zeroed intensity, which streamed because it
was the table by definition. That download is gone, and with it this plugin's
only use of `table_stream`.

Registered on import; `routes.py` imports this module, so they exist wherever
the plugin's server half does.
"""

from __future__ import annotations

from plexora.api import table_operation

#: Refusals these return, in the same shape the ROI plugin's operations use.
INVALID = "invalid"


def _refused(reason, **detail):
    return {"ok": False, "reason": reason, **detail}


def _done(result):
    return {"ok": True, **result}


@table_operation("gating.save_gates")
def save_gates(dataset, payload):
    """Write the active gates into the source file's `uns[<table_name>]`."""
    from plexora.plugins.gating.server import anndata_gates

    try:
        result = anndata_gates.save_gates_to_anndata(
            dataset.table.source,
            payload.get("image_id") or dataset.name,
            payload.get("gates") or {},
            table_name=payload.get("table_name") or "gates",
            imageid_column=payload.get("imageid_column"),
        )
    except ValueError as exc:
        return _refused(INVALID, message=str(exc))
    return _done(result)


@table_operation("gating.load_gates")
def load_gates(dataset, payload):
    """Read gates already present in the source file."""
    from plexora.plugins.gating.server import anndata_gates

    try:
        result = anndata_gates.load_gates_from_anndata(
            dataset.table.source,
            payload.get("image_id") or dataset.name,
            table_name=payload.get("table_name") or "gates",
            imageid_column=payload.get("imageid_column"),
        )
    except ValueError as exc:
        return _refused(INVALID, message=str(exc))
    return _done(result)


@table_operation("gating.gmm")
def gmm(dataset, payload):
    """One channel's fitted density and the gate it implies.

    Body unchanged from where it used to live in `model.get_gating_gmm` -- the
    caching around it stayed on the primary, because a fit is worth caching
    wherever it happened and the cache key does not depend on which machine
    did the arithmetic.
    """
    import numpy as np

    from plexora.plugins.gating.server.model import _curve, auto_gate

    channel_name = payload.get("channel")
    selection_ids = payload.get("selection_ids") or []

    packet_gmm = {}

    # Through `columns`, not `frame()[channel]`: a wide table (a whole
    # transcriptome) does not hold its genes in the frame, and `columns`
    # is the one read that knows where they are.
    column_data = np.asarray(dataset.table.columns([channel_name])[channel_name])
    # The histogram the curves below are laid over -- binned on the whole
    # column, in its own units, and deliberately not subsampled.
    bin_edges = np.histogram_bin_edges(column_data[~np.isnan(column_data)], bins=50)
    midpoints = (bin_edges[1:] + bin_edges[:-1]) / 2

    if selection_ids:
        ids = dataset.table.frame()[dataset.schema.cell_id].to_numpy()
        column_data_filtered = column_data[np.isin(ids, selection_ids)]
    else:
        # No selection to filter by (the only case current callers use, since
        # lasso/spatial-selection was removed).
        column_data_filtered = column_data

    # One fit answers both: where to put the gate, and the two curves that show
    # why it went there. They used to be able to disagree -- the curves were
    # the fit, the gate was a summary of it that ignored their widths -- so the
    # auto button landed somewhere the picture did not explain.
    gate, background, positive = auto_gate(
        column_data_filtered, dataset.table.log_transformed, at=midpoints)
    if gate is not None:
        packet_gmm['gate'] = gate
    packet_gmm['gmm_1'] = _curve(midpoints, background)
    packet_gmm['gmm_2'] = _curve(midpoints, positive)
    return packet_gmm
