"""Gating query/persistence logic, physically split out of
server/models/data_model.py -- these functions only ever touch the core
`datasource`/`config` state (never `seg`/`zarray`/tile generation), so they
go through data_model's read accessors (get_datasource_df(),
get_current_config(), gmm_cache_get_or_set(), etc.) rather than reaching
into that module's globals/caches directly.
"""

import pickle

import numpy as np
import polars as pl
from scipy.stats import norm
from sklearn.mixture import GaussianMixture

from plexora import api
from plexora.server.models import data_model
from plexora.server.modules.gating.database import LEGACY_STATE_TABLE

#: Identifies this plugin's storage namespace. Must stay 'gating' -- it is what
#: `plugin_gating_state` is keyed on, and changing it would strand saved gates.
PLUGIN_NAME = "gating"


def _store(datasource_name):
    """Gating's slice of this datasource's database.

    `legacy_state_table` points at the un-namespaced `gatinglist` table earlier
    builds wrote directly. Reads fall back to it when the namespaced table is
    empty, so upgrading the host does not lose a user's saved gates; writes
    always go to the namespaced table, so the old one is left frozen rather
    than kept in sync.
    """
    return api.store(datasource_name, PLUGIN_NAME, legacy_state_table=LEGACY_STATE_TABLE)


def _records_for_keys(keys, keep):
    df = data_model.get_datasource_df()
    arrays = [df[k].to_numpy()[keep].tolist() for k in keys]
    return [dict(zip(keys, row)) for row in zip(*arrays)]


def get_gated_cells(datasource_name, gates, start_keys):
    data_model._ensure_loaded(datasource_name)

    if not gates:
        return []
    columns = data_model.get_filter_columns(datasource_name, list(gates.keys()))
    keep = data_model.apply_range_mask(columns, gates, mode='and')
    id_key = start_keys[0]
    values = data_model.get_datasource_df()[id_key].to_numpy()[keep].tolist()
    return [{id_key: v} for v in values]


def get_gated_cells_custom(datasource_name, gates, start_keys):
    data_model._ensure_loaded(datasource_name)

    if not gates:
        return []
    columns = data_model.get_filter_columns(datasource_name, list(gates.keys()))
    keep = data_model.apply_range_mask(columns, gates, mode='or')
    query_keys = start_keys + list(gates.keys())
    return _records_for_keys(query_keys, keep)


def download_gating_csv(datasource_name, gates, channels, selection_ids, encoding):
    data_model._ensure_loaded(datasource_name)
    config = data_model.get_current_config()
    df = data_model.get_datasource_df()

    csv = df
    if 'idField' in config[datasource_name]['featureData'][0]:
        idField = config[datasource_name]['featureData'][0]['idField']
    else:
        idField = "CellID"

    if selection_ids:
        datasource_filter = df.filter(pl.col(idField).is_in(selection_ids))
    else:
        datasource_filter = df

    expr = None
    for key, value in gates.items():
        cond = (pl.col(key) > value[0]) & (pl.col(key) < value[1])
        expr = cond if expr is None else (expr & cond)
    if expr is not None:
        ids = datasource_filter.filter(expr)['id'].to_numpy()
    else:
        # No gates set: no filter, nothing gated in. (pandas' .query('')
        # used to raise ValueError here -- fixed rather than preserved.)
        ids = np.array([], dtype=np.int64)

    if 'Area' in channels:
        del channels['Area']
    is_in_ids = pl.col('id').is_in(ids)
    for channel in channels:
        if channel in gates:
            # Cast to the original column's dtype for CSV-text parity with
            # the pandas version: csv.loc[mask, channel] = 1 silently
            # upcast an int literal into what's typically a float64 marker
            # column (rendering "1.0"), whereas a bare Polars int literal
            # would render "1" -- a real text diff in the exported CSV.
            dtype = csv.schema[channel]
            if encoding == 'binary':
                value_expr = pl.when(is_in_ids).then(pl.lit(1)).otherwise(pl.lit(0)).cast(dtype)
            else:
                value_expr = pl.when(is_in_ids).then(pl.col(channel)).otherwise(pl.lit(0).cast(dtype))
            csv = csv.with_columns(value_expr.alias(channel))
        else:
            csv = csv.with_columns(pl.lit(0).alias(channel))

    return csv


def download_gates(datasource_name, gates, channels, lassos):
    data_model._ensure_loaded(datasource_name)
    rows = []
    for key, value in channels.items():
        rows.append([key, value[0], value[1]])
    csv = pl.DataFrame(rows, schema=['channel', 'gate_start', 'gate_end'], orient='row')
    csv = csv.with_columns(pl.lit(False).alias('gate_active'))

    schema = csv.schema
    for channel in gates:
        is_channel = pl.col('channel') == channel
        csv = csv.with_columns([
            pl.when(is_channel).then(pl.lit(True)).otherwise(pl.col('gate_active')).alias('gate_active'),
            pl.when(is_channel).then(pl.lit(gates[channel][0]).cast(schema['gate_start']))
              .otherwise(pl.col('gate_start')).alias('gate_start'),
            pl.when(is_channel).then(pl.lit(gates[channel][1]).cast(schema['gate_end']))
              .otherwise(pl.col('gate_end')).alias('gate_end'),
        ])

    if len(lassos) > 0:
        # Confirmed dead in current live usage (imageViewer.js permanently
        # sets list_lassos = {} since lasso drawing was removed), but
        # implemented correctly rather than skipped. lasso_polygon is a
        # nested structure that won't unify with the float gate columns
        # above, so build it as its own frame and concat with relaxed
        # schema-widening instead of forcing one shared schema up front.
        lasso_rows = [
            {'channel': 'Lasso', 'gate_start': v['lasso_polygon'], 'gate_end': None, 'gate_active': v['lasso_toggle']}
            for v in lassos.values()
        ]
        lasso_df = pl.DataFrame(lasso_rows, strict=False)
        csv = pl.concat([csv, lasso_df], how='diagonal_relaxed')

    return csv


def save_gating_list(datasource_name, gates, channels, lassos):
    data_model._ensure_loaded(datasource_name)
    rows = []
    for key, value in channels.items():
        rows.append([key, value[0], value[1]])
    csv = pl.DataFrame(rows, schema=['channel', 'gate_start', 'gate_end'], orient='row')
    csv = csv.with_columns(pl.lit(False).alias('gate_active'))

    schema = csv.schema
    for channel in gates:
        is_channel = pl.col('channel') == channel
        csv = csv.with_columns([
            pl.when(is_channel).then(pl.lit(True)).otherwise(pl.col('gate_active')).alias('gate_active'),
            pl.when(is_channel).then(pl.lit(gates[channel][0]).cast(schema['gate_start']))
              .otherwise(pl.col('gate_start')).alias('gate_start'),
            pl.when(is_channel).then(pl.lit(gates[channel][1]).cast(schema['gate_end']))
              .otherwise(pl.col('gate_end')).alias('gate_end'),
        ])

    if len(lassos) > 0:
        lasso_rows = [
            {'channel': 'Lasso', 'gate_start': v['lasso_polygon'], 'gate_end': None, 'gate_active': v['lasso_toggle']}
            for v in lassos.values()
        ]
        lasso_df = pl.DataFrame(lasso_rows, strict=False)
        csv = pl.concat([csv, lasso_df], how='diagonal_relaxed')

    temp = csv.to_dicts()
    f = pickle.dumps(temp, protocol=4)
    _store(datasource_name).put_state(f)


def get_saved_gating_list(datasource_name):
    cells = _store(datasource_name).get_state()
    if cells is None:
        return None
    return pickle.loads(cells)


def get_gating_gmm(channel_name, datasource_name, selection_ids):
    data_model._ensure_loaded(datasource_name)
    config = data_model.get_current_config()
    df = data_model.get_datasource_df()

    selection_key = tuple(sorted(selection_ids)) if selection_ids else None
    cache_key = (datasource_name, channel_name, selection_key)

    def _compute():
        packet_gmm = {}

        if 'idField' in config[datasource_name]['featureData'][0]:
            idField = config[datasource_name]['featureData'][0]['idField']
        else:
            idField = "CellID"
        if selection_ids:
            datasource_filter = df.filter(pl.col(idField).is_in(selection_ids))
        else:
            # No selection to filter by (the only case current callers use,
            # since lasso/spatial-selection was removed) -- avoid a full
            # 2M-row copy that's immediately discarded.
            datasource_filter = df

        column_data = df[channel_name].to_numpy()
        [hist, bin_edges] = np.histogram(column_data[~np.isnan(column_data)], bins=50, density=True)
        midpoints = (bin_edges[1:] + bin_edges[:-1]) / 2

        column_data_filtered = datasource_filter[channel_name].to_numpy()

        # Cap the GMM fit input at a random subsample when the cell-level
        # column is large -- EM cost scales roughly linearly with N per
        # iteration, and a 2-component 1D mixture's fitted parameters barely
        # move between 100k and millions of samples. Fixed seed keeps the
        # fit deterministic per unique cache key. The histogram above is
        # intentionally left unaffected -- only the .fit() input is capped.
        GMM_FIT_SAMPLE_CAP = 100_000
        fit_data = column_data_filtered
        if fit_data.shape[0] > GMM_FIT_SAMPLE_CAP:
            rng = np.random.default_rng(0)
            fit_data = fit_data[rng.choice(fit_data.shape[0], size=GMM_FIT_SAMPLE_CAP, replace=False)]

        gmm = GaussianMixture(n_components=2)
        gmm.fit(fit_data.reshape((-1, 1)))
        i0, i1 = np.argsort(gmm.means_[:, 0])
        packet_gmm['gate'] = np.mean(gmm.means_)

        pdf_gmm1 = [gmm.weights_[i0] * norm.pdf(midpoints, gmm.means_[i0], np.sqrt(gmm.covariances_[i0]))][0][0]
        pdf_gmm2 = [gmm.weights_[i1] * norm.pdf(midpoints, gmm.means_[i1], np.sqrt(gmm.covariances_[i1]))][0][0]

        dat_gmm1 = []
        dat_gmm2 = []
        for i in range(len(hist)):
            dat_gmm1.append({'x': midpoints[i], 'y': pdf_gmm1[i]})
            dat_gmm2.append({'x': midpoints[i], 'y': pdf_gmm2[i]})

        packet_gmm['gmm_1'] = dat_gmm1
        packet_gmm['gmm_2'] = dat_gmm2
        return packet_gmm

    return data_model.gmm_cache_get_or_set(cache_key, _compute)
