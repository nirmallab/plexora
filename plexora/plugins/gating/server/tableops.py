"""Gating work that has to happen where the cell table's file is.

Four things in this plugin cannot be answered with a buffer of values:

- **Writing thresholds into `uns`.** The whole reason the plugin has an AnnData
  path at all -- and it opens the user's file in place, past a consolidated
  zarr index, and rewrites exactly one subtree.
- **Reading them back.** Same file, same codec, same reason.
- **The per-channel mixture fit.** It needs the raw column in its own dtype,
  the whole column for the histogram edges, and a filtered copy for the fit.
  Sending all of that so the primary can do arithmetic on it would be sending
  the table.
- **Exporting the gated CSV.** That is the whole table by definition, so it
  streams rather than returning a value.

Registered on import; `routes.py` imports this module, so they exist wherever
the plugin's server half does.
"""

from __future__ import annotations

from plexora.api import table_operation, table_stream

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
    import polars as pl

    from plexora.plugins.gating.server.model import _curve, auto_gate

    channel_name = payload.get("channel")
    selection_ids = payload.get("selection_ids") or []

    df = dataset.table.frame()
    packet_gmm = {}

    idField = dataset.schema.cell_id
    if selection_ids:
        datasource_filter = df.filter(pl.col(idField).is_in(selection_ids))
    else:
        # No selection to filter by (the only case current callers use, since
        # lasso/spatial-selection was removed) -- avoid a full 2M-row copy
        # that's immediately discarded.
        datasource_filter = df

    column_data = df[channel_name].to_numpy()
    # The histogram the curves below are laid over -- binned on the whole
    # column, in its own units, and deliberately not subsampled.
    bin_edges = np.histogram_bin_edges(column_data[~np.isnan(column_data)], bins=50)
    midpoints = (bin_edges[1:] + bin_edges[:-1]) / 2

    column_data_filtered = datasource_filter[channel_name].to_numpy()

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


@table_stream("gating.export_csv")
def export_csv(dataset, payload):
    """The gated table as CSV, in row chunks.

    A stream rather than a value: this is the whole table by construction, and
    holding the serialized copy alongside the frame it came from is the thing
    the chunking exists to avoid.
    """
    from plexora.plugins.gating.server import model

    frame = model.gated_frame(
        dataset,
        payload.get("gates") or {},
        payload.get("channels") or {},
        payload.get("selection_ids") or [],
        payload.get("encoding"),
    )
    return model.stream_csv(frame)
