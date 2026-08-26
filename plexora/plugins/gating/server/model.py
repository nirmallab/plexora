"""Gating's query and persistence logic.

Reaches the host only through `plexora.api`. Everything it needs -- the
feature table, the role->column map, per-plugin storage -- arrives as handles,
so this module names no core internals and holds no core state.
"""

import pickle

import numpy as np
import polars as pl
from scipy.stats import norm
from sklearn.mixture import GaussianMixture

from plexora import api
from plexora.plugins.gating.server.database import LEGACY_STATE_TABLE

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


def get_gated_cells(datasource_name, gates, start_keys):
    table = api.dataset(datasource_name).table
    if not gates:
        return []
    id_key = start_keys[0]
    values = table.geometry()[id_key].to_numpy()[table.range_mask(gates)].tolist()
    return [{id_key: v} for v in values]


def gated_frame(dataset, gates, channels, selection_ids, encoding):
    """The export table: every row, with each gated channel rewritten.

    Takes a dataset rather than a name because it runs where the table's file
    is -- on this server for an ordinary project, on the node otherwise -- and
    a name would mean a config lookup that only makes sense on the primary.
    """
    df = dataset.table.frame()

    csv = df
    # The role, not a literal column name -- schema.cell_id is whatever the
    # project recorded. No fallback: this plugin declares cell_id in its
    # Requires, so core collects it before the tool can open at all, and a
    # literal default here would only mask a project that slipped through.
    idField = dataset.schema.cell_id

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


def stream_csv(df, chunksize=100_000):
    """Yield a large DataFrame as CSV in row chunks instead of materializing
    the full serialized string (and holding it alongside the DataFrame) in
    memory at once, as df.write_csv() would for a multi-million-row gating
    export. Polars has no built-in chunked-string-generator, so this slices
    and writes each chunk by hand.

    Lives here rather than in `routes` because it is the tail of the export
    itself: when the table is on a node, the chunking happens there and the
    route only forwards what arrives.
    """
    header = True
    for start in range(0, df.height, chunksize):
        yield df.slice(start, chunksize).write_csv(include_header=header)
        header = False


def download_gates(datasource_name, gates, channels):
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

    return csv


def save_gating_list(datasource_name, gates, channels):
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

    temp = csv.to_dicts()
    f = pickle.dumps(temp, protocol=4)
    _store(datasource_name).put_state(f)


def get_saved_gating_list(datasource_name):
    cells = _store(datasource_name).get_state()
    if cells is None:
        return None
    return pickle.loads(cells)


#: Components in the auto-gate fit.
#:
#: Three, not two. A marker's negative population is not a point -- it is a
#: broad distribution of background and autofluorescence, and on a log scale it
#: is close to symmetric. A two-component fit given that has an easier job
#: splitting the *background* down the middle than separating background from
#: positives, and the resulting gate sits inside the negative population: every
#: cell above the middle of the background reads as positive.
#:
#: With a third component the background can occupy two and the bright tail
#: gets its own, so the boundary between the top two is a real one. Same count,
#: and the same reasoning, as core's `get_channel_gmm` for image channels.
_GATE_COMPONENTS = 3

#: EM cost scales with N per iteration, and a 1-D mixture's fitted parameters
#: barely move between 100k rows and millions. Fixed seed, so a given column
#: gates the same way twice.
_GMM_FIT_SAMPLE_CAP = 100_000


def _fit_mixture(values, components):
    """(means, sds, weights), ascending by mean, or None if it cannot fit.

    A column with fewer distinct values than components -- a flag, a constant,
    a nearly empty channel -- has no mixture to find, and sklearn either raises
    or returns degenerate components. Saying so is better than a gate derived
    from noise.
    """
    values = values[np.isfinite(values)]
    if values.size < components or np.unique(values).size < components:
        return None

    sample = values
    if sample.shape[0] > _GMM_FIT_SAMPLE_CAP:
        rng = np.random.default_rng(0)
        sample = sample[rng.choice(sample.shape[0], size=_GMM_FIT_SAMPLE_CAP,
                                   replace=False)]

    gmm = GaussianMixture(components, max_iter=1000, tol=1e-6, random_state=0)
    gmm.fit(sample.reshape(-1, 1))
    order = np.argsort(gmm.means_[:, 0])
    return (gmm.means_[order, 0],
            np.sqrt(gmm.covariances_[order, 0, 0]),
            gmm.weights_[order])


def _populations(fitted, x):
    """(background, positive) weighted densities at `x`.

    Two populations out of three components: the brightest one is the positive
    population, and everything below it pooled is the background. Pooling
    rather than taking the second component alone is what makes the pair add up
    to the whole distribution, so the two curves drawn over the histogram cover
    it instead of leaving the largest peak unexplained.
    """
    means, sds, weights = fitted
    positive = norm(means[-1], sds[-1]).pdf(x) * weights[-1]
    background = sum(norm(means[i], sds[i]).pdf(x) * weights[i]
                     for i in range(len(means) - 1))
    return background, positive


def _crossover(fitted):
    """Where the positive population overtakes the background.

    The threshold, rather than `mean(means)` -- which is what this used to take
    and which ignores both how wide each component is and how much of the data
    it holds. A narrow background beside a broad positive tail has its midpoint
    far below the point where the two actually change places, and that
    difference is the gate being wrong by tens of percent of the cells.
    """
    means = fitted[0]
    x = np.linspace(means[0], means[-1], 2000)
    background, positive = _populations(fitted, x)
    above = np.flatnonzero(positive > background)
    # Never overtakes: nothing here is separable, so the top component's own
    # centre is as honest an answer as there is.
    return float(x[above[0]]) if above.size else float(means[-1])


def auto_gate(values, log_transformed, at=None):
    """Where the positive population starts, in the values' own units.

    Fitted on a log scale whichever scale the table is stored on: marker
    intensities are log-normal, and a mixture of *normals* fitted to raw counts
    is fitting the wrong shape -- the components chase the skew instead of the
    populations. So raw values are log1p'd for the fit and the answer mapped
    back with expm1, while values the project already log1p'd are fitted as
    they stand. The same underlying data then gives the same gate either way,
    which it did not before: turning the log switch on used to move the gate
    from roughly the right place to the middle of the background.

    That is also why the project's own flag decides this rather than a guess at
    the numbers. Logging twice is its own failure -- it compresses the
    separation until a marker with 3% positives gates at 28% -- so this cannot
    be "always log1p", and nothing in the values themselves tells the two
    apart.

    `at` asks for the fitted curves as well, evaluated at those points and in
    the values' own units. Returns (gate, background, positive), any of which
    is None when the column has no mixture to find -- a constant, a flag, a
    nearly empty channel. The caller ships nothing rather than a number derived
    from noise.
    """
    values = values[np.isfinite(values)]
    # log1p rather than log: it is the transform Plexora itself applies, expm1
    # inverts it exactly, and it is defined at zero -- which is where a large
    # part of a quantification column sits. Negative values (arcsinh, z-scored)
    # have no log to take, so those are fitted as they stand.
    to_log = not log_transformed and values.size > 0 and values.min() >= 0
    fitted = _fit_mixture(np.log1p(values) if to_log else values, _GATE_COMPONENTS)
    if fitted is None:
        return None, None, None

    gate = _crossover(fitted)
    gate = float(np.expm1(gate)) if to_log else gate
    if at is None:
        return gate, None, None

    # Back into the values' own units, which is what the histogram underneath
    # these curves is binned in. Densities do not survive a change of variable
    # unchanged -- dividing by (1 + x) is the log1p Jacobian, and without it
    # the curves would be the right shape at the wrong height.
    background, positive = _populations(fitted, np.log1p(at) if to_log else at)
    if to_log:
        background, positive = background / (1 + at), positive / (1 + at)
    return gate, background, positive


def _curve(x, y):
    """A fitted density as the client plots it. Empty when there was no fit."""
    if y is None:
        return []
    return [{'x': float(x[i]), 'y': float(y[i])} for i in range(len(x))]


def get_gating_gmm(channel_name, datasource_name, selection_ids):
    """The fit for one channel, cached per (channel, selection).

    The fit itself is a table operation -- it needs the raw column in its own
    dtype and a filtered copy of it, which is the table rather than a summary
    of it -- so it runs where the file is. The cache stays here: a fit is worth
    keeping whichever machine performed it, and it is dropped by the same
    datasource reload that drops every other derived result.
    """
    dataset = api.dataset(datasource_name)
    selection_key = tuple(sorted(selection_ids)) if selection_ids else None
    cache_key = (channel_name, selection_key)

    return dataset.cached(cache_key, lambda: dataset.table.run("gating.gmm", {
        "channel": channel_name,
        "selection_ids": list(selection_ids or []),
    }))
