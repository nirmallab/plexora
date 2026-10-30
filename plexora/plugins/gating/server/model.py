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
from plexora.agent.expression import values_logged  # noqa: F401  (gating's callers)
from plexora.plugins.gating.server.database import LEGACY_STATE_TABLE
from plexora.server.utils import safe_pickle

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
    """The cells the gates call positive -- never a cell QC says cannot be
    called for one of the gated markers (`ProjectData.calls`)."""
    ds = api.project_data(datasource_name)
    table = ds.table
    if not gates:
        return []
    id_key = start_keys[0]
    values = table.geometry()[id_key].to_numpy()[ds.calls(gates) == 1].tolist()
    return [{id_key: v} for v in values]


def _thresholded(channel, gate_start, gate_end, description):
    """Whether this marker was ever narrowed from its own full data range.

    The honest answer to "was this thresholded", and the reason the exported
    CSV no longer carries `gate_active`: that flag only ever reflects whichever
    single marker is on screen right now -- `selections` is reset on every
    marker switch, by design, for the live single-marker slider -- so a CSV
    listing twenty markers the user had gated came out with one True in it.

    The stored range compared against the column's own min/max is the same test
    the marker dropdown's green dot applies client-side (gatingSidebarController's
    hasCustomGate) and the same one save_gates_to_anndata applies, so all three
    agree on which markers count as gated.

    A channel the description does not know reads as thresholded: it has a
    stored range and nothing to say that range is the default one, and
    under-reporting a real gate is the worse of the two failures.
    """
    if gate_start is None or gate_end is None:
        return False
    desc = description.get(channel) or {}
    return gate_start != desc.get('min') or gate_end != desc.get('max')


#: The download's `gated_by`: who set the gate the row carries.
GATED_BY_AI = "Plexora AI"
GATED_BY_USER = "User"
#: Provenance methods a person sets (`provenance.METHODS`); a written gate by
#: any other method was an agent's.
_USER_METHODS = ("manual", "rolled_back", "imported")


def _gated_by(row, low, high):
    """`GATED_BY_AI` when an agent wrote this very gate (its provenance row
    holds the value written, and the gate still equals it), else
    `GATED_BY_USER`: set by hand, or an agent's gate moved since."""
    from plexora.plugins.gating.server import provenance

    if not row or row.get("written_low") is None or row.get("method") in _USER_METHODS:
        return GATED_BY_USER
    if provenance.edited_since(row, {"low": low, "high": high}):
        return GATED_BY_USER
    return GATED_BY_AI


def download_gates(datasource_name, gates, channels):
    """The gates file: one row per marker -- `channel`, `gate_start`,
    `gate_end`; `thresholded` TRUE for a marker narrowed from its full range
    and empty otherwise; `gated_by` (`GATED_BY_AI` or `GATED_BY_USER`) for a
    thresholded one, empty otherwise. Read back by the panel's upload, where
    an empty `thresholded` is a marker left ungated."""
    from plexora.plugins.gating.server import provenance

    rows = []
    for key, value in channels.items():
        rows.append([key, value[0], value[1]])
    csv = pl.DataFrame(rows, schema=['channel', 'gate_start', 'gate_end'], orient='row')

    schema = csv.schema
    for channel in gates:
        is_channel = pl.col('channel') == channel
        csv = csv.with_columns([
            pl.when(is_channel).then(pl.lit(gates[channel][0]).cast(schema['gate_start']))
              .otherwise(pl.col('gate_start')).alias('gate_start'),
            pl.when(is_channel).then(pl.lit(gates[channel][1]).cast(schema['gate_end']))
              .otherwise(pl.col('gate_end')).alias('gate_end'),
        ])

    # Per-column min/max, cached per datasource -- this is a handful of markers
    # against a summary that the panel already asked for, not a table read.
    description = api.project_data(datasource_name).table.describe()
    origins = provenance.read(datasource_name)
    thresholded, gated_by = [], []
    for row in csv.iter_rows(named=True):
        gated = _thresholded(row['channel'], row['gate_start'], row['gate_end'], description)
        # TRUE or nothing: a spreadsheet reads a column of FALSEs as data.
        thresholded.append("TRUE" if gated else None)
        gated_by.append(_gated_by(origins.get(row['channel']), row['gate_start'],
                                  row['gate_end']) if gated else None)
    return csv.with_columns(pl.Series('thresholded', thresholded, dtype=pl.Utf8),
                            pl.Series('gated_by', gated_by, dtype=pl.Utf8))


def save_gating_list(datasource_name, gates, channels, *, base=None, defaults=None):
    """The sidebar's save: its whole list, as it holds it. Returns the new
    revision.

    `base` is the revision the page last read or wrote (`GatingApi.revision`).
    When the stored list has moved on since -- an agent's session wrote gates
    while the page held the old list -- only what the page changed since
    `base` is taken (`merge_unchanged`); the rest stays as stored. `defaults`
    ({channel: (low, high)}, or a callable giving it) is each column's full
    range: a row the page only seeded counts as unchanged."""
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
    with _lock_for(datasource_name):
        current = _store(datasource_name).get_state()
        if base is not None and current and _blob_revision(current) != str(base):
            seen = rows_at(datasource_name, base)
            if seen is not None:
                if callable(defaults):
                    defaults = defaults()
                temp = merge_unchanged(seen, temp, safe_pickle.loads(current) or [],
                                       defaults or {})
        f = pickle.dumps(temp, protocol=4)
        _store(datasource_name).put_state(f)
        return remember(datasource_name, f)


#: Gate lists a page was handed or saved, by revision, so a later save can say
#: which it started from (`save_gating_list`'s `base`). Process memory, the
#: last `_SEEN_MAX` across projects: a page that outlives the server saves as
#: it always did.
_SEEN: dict = {}
_SEEN_MAX = 64


def _blob_revision(blob) -> str:
    return hashlib.sha1(blob).hexdigest()[:16] if blob else "0"


def remember(datasource_name, blob) -> str:
    """Keep the list in `blob` by its revision (`revision`); returns it."""
    rev = _blob_revision(blob)
    if blob:
        key = (str(datasource_name), rev)
        _SEEN.pop(key, None)
        _SEEN[key] = blob
        while len(_SEEN) > _SEEN_MAX:
            _SEEN.pop(next(iter(_SEEN)))
    return rev


def saved_gating_list_at(datasource_name) -> tuple:
    """(rows or None, revision) of the stored list, read once and kept
    (`remember`) for a later save to name as its base."""
    blob = _store(datasource_name).get_state()
    rev = remember(datasource_name, blob)
    return (None if blob is None else safe_pickle.loads(blob)), rev


def rows_at(datasource_name, rev):
    """The rows of a list `remember` kept, or None."""
    blob = _SEEN.get((str(datasource_name), str(rev)))
    if blob is None:
        return None
    return [dict(row) for row in safe_pickle.loads(blob) or [] if isinstance(row, dict)]


def merge_unchanged(base_rows, saved_rows, current_rows, defaults) -> list:
    """`saved_rows` with every gate the page did not change since `base_rows`
    taken from `current_rows` instead.

    A row is the page's change when its range differs from the base's (or,
    for a channel the base did not have, from `defaults`, its full range --
    the sidebar seeds a marker it is shown). Anything else the page sent is
    the list it was handed, and the stored one, which may have moved on since,
    stands. Which marker is active is always the page's. A channel stored but
    not sent is kept."""
    base = {row.get("channel"): row for row in base_rows}
    current = {row.get("channel"): row for row in current_rows if isinstance(row, dict)}
    out, sent = [], set()
    for row in saved_rows:
        channel = row.get("channel")
        sent.add(channel)
        was = base.get(channel)
        if was is None and channel in defaults:
            low, high = defaults[channel]
            was = {"gate_start": low, "gate_end": high}
        now = current.get(channel)
        if was is not None and now is not None and _same_range(row, was):
            row = {**row, "gate_start": now.get("gate_start"), "gate_end": now.get("gate_end")}
        out.append(row)
    out.extend(dict(row) for channel, row in current.items() if channel not in sent)
    return out


def _same_range(a, b):
    def same(x, y):
        if x is None or y is None:
            return x is y
        return abs(float(x) - float(y)) <= 1e-9 * max(1.0, abs(float(x)), abs(float(y)))
    return same(a.get("gate_start"), b.get("gate_start")) \
        and same(a.get("gate_end"), b.get("gate_end"))


def default_ranges(ds) -> dict:
    """{channel: (min, max)}: each marker's full range (`default_rows`)."""
    return {row["channel"]: (row["gate_start"], row["gate_end"]) for row in default_rows(ds)}


def get_saved_gating_list(datasource_name):
    cells = _store(datasource_name).get_state()
    if cells is None:
        return None
    return safe_pickle.loads(cells)


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


def _sorted_quantile(sorted_values, q):
    """np.quantile's linear rule on an array that is already sorted: O(k)."""
    n = sorted_values.shape[0]
    position = np.clip(np.asarray(q, dtype=np.float64), 0.0, 1.0) * (n - 1)
    below = np.floor(position).astype(np.int64)
    above = np.minimum(below + 1, n - 1)
    weight = position - below
    return sorted_values[below] * (1 - weight) + sorted_values[above] * weight


FLOOR_MIN_FRACTION = 0.001   # [cal] of the finite cells ...
FLOOR_MIN_CELLS = 50         # [cal] ... or this many, whichever is fewer
FLOOR_MAX_FRACTION = 0.2
FLOOR_GAP_IQR = 2.0          # [cal] gap to the body, in the body's IQRs


def floor_spike(fit) -> int:
    """How many cells of the ascending `fit` sit in a spike at its minimum,
    set well apart from everything else -- 0 when there is none.

    Quantification writes 0 (log1p 0) for cells it could not measure: off the
    tissue, under a dropped tile, clipped by the mask. A few hundred of them
    are a component of their own to a mixture, which then gates between them
    and the rest and calls 99% of the cells positive, with a background sd of
    0.001 that no refinement step can climb out of. They are negatives; the
    fits are made on the body above them. A spike too large to be a nuisance
    (FLOOR_MAX_FRACTION) is left to the fit as a real background.

    The smallest spike counted is FLOOR_MIN_FRACTION of the cells or
    FLOOR_MIN_CELLS, whichever is fewer: on a large image a fraction alone is
    hundreds of cells, and 83 to 155 unmeasured cells of 820,618 were still a
    component of their own (lsp35766_p193: GZMB, Rb, KLF2, CD66b gated at
    ~0.03, calling 99.98% of cells positive).
    """
    n = fit.shape[0]
    if n < 50:
        return 0
    low = fit[0]
    k = int(np.searchsorted(fit, low + 1e-6 * max(1.0, abs(low)), side="right"))
    least = max(10, min(FLOOR_MIN_FRACTION * n, FLOOR_MIN_CELLS))
    if k < least or k > FLOOR_MAX_FRACTION * n or k >= n - 50:
        return 0
    body = fit[k:]
    # To the body's 1st percentile, not its first value: a few stragglers
    # between the spike and the body (partly clipped cells) do not make it one.
    q01, q25, q75 = _sorted_quantile(body, np.array([0.01, 0.25, 0.75]))
    iqr = max(float(q75 - q25), 1e-9)
    return k if float(q01 - low) > FLOOR_GAP_IQR * iqr else 0


def _fit_space(values, log_transformed):
    """(finite values in the fit's space, in their own order, to_log): log1p
    for raw non-negative intensities, as they stand otherwise (see
    `auto_gate`)."""
    values = np.asarray(values, dtype=np.float64)
    values = values[np.isfinite(values)]
    to_log = not log_transformed and values.size > 0 and values.min() >= 0
    return (np.log1p(values) if to_log else values), bool(to_log)


def _above_floor(fit):
    """(the values above a floor spike, in their own order, floor_n).

    The order is kept: the mixture's initialisation depends on it, and a
    column with no spike must fit exactly as it always has."""
    ordered = np.sort(fit)
    floor_n = floor_spike(ordered) if fit.size else 0
    if not floor_n:
        return fit, 0
    return fit[fit > ordered[floor_n - 1]], floor_n


def _fit_body(values, log_transformed):
    """(fitted or None, to_log, floor_n): the mixture fitted above a floor
    spike (`floor_spike`) -- the one fit the Auto button and automatic gating
    both use."""
    fit, to_log = _fit_space(values, log_transformed)
    body, floor_n = _above_floor(fit)
    return _fit_mixture(body, _GATE_COMPONENTS), to_log, floor_n


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

    That is also why the caller says which (`values_logged`: the project's
    switch, or a matrix the expression rule reads as already logged) rather
    than this guessing per column. Logging twice is its own failure -- it
    compresses the separation until a marker with 3% positives gates at 28% --
    so this cannot be "always log1p", and one column's values alone do not tell
    the two apart.

    A spike of unmeasured cells at the column's minimum is left out of the fit
    (`floor_spike`): it would otherwise take a component of its own and the
    gate would land between it and everything else.

    `at` asks for the fitted curves as well, evaluated at those points and in
    the values' own units. Returns (gate, background, positive), any of which
    is None when the column has no mixture to find -- a constant, a flag, a
    nearly empty channel. The caller ships nothing rather than a number derived
    from noise.
    """
    # log1p rather than log: it is the transform Plexora itself applies, expm1
    # inverts it exactly, and it is defined at zero -- which is where a large
    # part of a quantification column sits. Negative values (arcsinh, z-scored)
    # have no log to take, so those are fitted as they stand.
    fit, to_log = _fit_space(values, log_transformed)
    body, floor_n = _above_floor(fit)
    fitted = _fit_mixture(body, _GATE_COMPONENTS)
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
    if floor_n:
        # The fit describes the body; the histogram holds the spike as well.
        body_share = body.shape[0] / fit.shape[0]
        background, positive = background * body_share, positive * body_share
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
    dataset = api.project_data(datasource_name)
    return gmm_for(dataset, channel_name, selection_ids)


# -- the same state, for a caller holding a ProjectData ----------------------
#
# Everything above takes a datasource NAME and builds its own handles through
# `api.project_data`, which is right for a route: the browser is looking at that
# project, and the handles read through the loaded one. An agent is not the
# browser. It holds provider-backed handles of its own (plexora/agent/session.py)
# precisely so that asking about a project never swaps the one on screen -- so
# the functions below take the handle set they are given and never build one.
#
# The stored shape is untouched: the same pickled list of
# {channel, gate_start, gate_end, gate_active} rows the browser writes, so a
# gate set here is the gate the sidebar shows on its next load, and one set in
# the sidebar is the gate read here.

import hashlib
import threading

_ROW_LOCKS: dict[str, threading.Lock] = {}
_ROW_LOCKS_GUARD = threading.Lock()


def _lock_for(datasource_name):
    """One lock per datasource around the read-modify-write in `set_gate`.

    The same arrangement as ROI's repository, for the same reason: two writers
    in one process (an agent tool call and a route) must not both read the list,
    both change one row, and have one of them vanish.
    """
    with _ROW_LOCKS_GUARD:
        lock = _ROW_LOCKS.get(datasource_name)
        if lock is None:
            lock = _ROW_LOCKS[datasource_name] = threading.Lock()
        return lock


class GateConflict(Exception):
    """The stored gates changed since the caller last read them."""

    def __init__(self, current_revision):
        super().__init__("the saved gates changed since they were read")
        self.current_revision = current_revision


class GateLocked(Exception):
    """The marker's gate is locked or approved (autogate/provenance.py)."""

    def __init__(self, marker, status):
        super().__init__(f"{marker!r} is {status}; only the user changes it"
                         + (" (unlock it in the marker menu)" if status == "locked" else ""))
        self.marker = marker
        self.status = status


def gate_decimals(low_bound, high_bound):
    """The sidebar's precision for a marker: enough decimals for ~200 steps
    across its observed range (`dataLayer.gateDecimals`)."""
    import math

    try:
        span = abs(float(high_bound) - float(low_bound))
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(span) or span <= 0:
        return 0
    return max(0, min(6, math.ceil(math.log10(200 / span))))


def snap_to_grid(low, high, description):
    """(low, high) on the grid the sidebar rounds a gate onto.

    `gatingSidebarController.normalizeGateRange`, in Python: the low bound
    floored and the high bound ceiled at the marker's own precision, so
    rounding never excludes a boundary cell -- and so a gate an agent writes
    is one the slider can express, and stays exactly what was written until
    the user moves it.
    """
    import math

    desc = description or {}
    decimals = gate_decimals(desc.get("min"), desc.get("max"))
    factor = 10 ** decimals
    return (math.floor(float(low) * factor + 1e-9) / factor,
            math.ceil(float(high) * factor - 1e-9) / factor)


def _stored_blob(ds):
    return _store(ds.name).get_state()


def revision(ds) -> str:
    """A digest of the stored gate list, "0" when nothing is stored.

    The list carries no revision of its own and the browser writes it whole, so
    the content is the only thing that can say "this changed under you".
    """
    blob = _stored_blob(ds)
    if not blob:
        return "0"
    return hashlib.sha1(blob).hexdigest()[:16]


def gate_rows(ds) -> list:
    """The stored rows, or [] when this project has never been gated."""
    blob = _stored_blob(ds)
    if not blob:
        return []
    rows = safe_pickle.loads(blob)
    return [dict(row) for row in rows or [] if isinstance(row, dict)]


def _description(ds) -> dict:
    return ds.table.describe() or {}


def default_rows(ds) -> list:
    """One row per marker at its own full data range -- what the sidebar seeds a
    marker with the first time it is browsed to."""
    description = _description(ds)
    rows = []
    for marker in ds.table.markers:
        desc = description.get(marker) or {}
        if desc.get("min") is None or desc.get("max") is None:
            continue
        rows.append({"channel": marker, "gate_start": desc["min"],
                     "gate_end": desc["max"], "gate_active": False})
    return rows


def _gate_record(marker, row, desc):
    default_low, default_high = desc.get("min"), desc.get("max")
    low = row.get("gate_start") if row else default_low
    high = row.get("gate_end") if row else default_high
    return {
        "marker": marker,
        "low": None if low is None else float(low),
        "high": None if high is None else float(high),
        "thresholded": bool(row) and _thresholded(marker, low, high, {marker: desc}),
        "default_low": None if default_low is None else float(default_low),
        "default_high": None if default_high is None else float(default_high),
    }


def get_gate(ds, marker):
    """One marker's gate, or None when it is not a marker of this project."""
    if marker not in ds.table.markers:
        return None
    desc = _description(ds).get(marker) or {}
    row = next((r for r in gate_rows(ds) if r.get("channel") == marker), None)
    return _gate_record(marker, row, desc)


def all_gates(ds) -> list:
    """Every marker's gate, thresholded or not, in marker order."""
    description = _description(ds)
    rows = {r.get("channel"): r for r in gate_rows(ds)}
    return [_gate_record(marker, rows.get(marker), description.get(marker) or {})
            for marker in ds.table.markers]


def active_gates(ds) -> dict:
    """{marker: (low, high)} for every marker narrowed from its full range.

    The rule `save_gates_to_anndata` applies and the sidebar's green dot shows:
    a stored range equal to the column's own min/max was never customized.
    Rows for channels that are not markers of the table (a mask's "Area"
    pseudo-channel, a marker since removed) are not gates.
    """
    description = _description(ds)
    markers = set(ds.table.markers)
    active = {}
    for row in gate_rows(ds):
        channel = row.get("channel")
        if not channel or channel not in markers:
            continue
        low, high = row.get("gate_start"), row.get("gate_end")
        if low is None or high is None:
            continue
        desc = description.get(channel) or {}
        if low == desc.get("min") and high == desc.get("max"):
            continue
        active[channel] = (low, high)
    return active


# -- versions: no gate set is lost ------------------------------------------------
#
# The gate list holds one gate per marker, and every run and every slider move
# writes over it. A version keeps a set as it stood: when an automatic run
# finishes, just before one starts writing over gates no version holds yet
# (the user's hand edits), and when the user saves one. The panel shows the
# latest -- the stored list -- and the gates file and the source file carry
# them all (`versions_table`, `anndata_gates.save_gate_versions`).

#: The plugin store table of versions: a row per (version, gated marker).
VERSIONS_TABLE = "versions"
VERSION_COLUMNS = {"version": pl.Int64, "created": pl.Utf8, "by": pl.Utf8,
                   "session_id": pl.Utf8, "marker": pl.Utf8, "low": pl.Float64,
                   "high": pl.Float64}


def gate_versions(datasource_name) -> list:
    """[{version, created (UTC, ISO), by, session_id, gates: {marker: (low,
    high)}}], oldest first; [] when none was kept."""
    frame = _store(datasource_name).get_table(VERSIONS_TABLE)
    if frame is None or frame.height == 0:
        return []
    out = {}
    for row in frame.sort("version", maintain_order=True).iter_rows(named=True):
        version = out.setdefault(int(row["version"]), {
            "version": int(row["version"]), "created": row["created"], "by": row["by"],
            "session_id": row["session_id"] or None, "gates": {}})
        if row["marker"] is not None:
            version["gates"][row["marker"]] = (row["low"], row["high"])
    return [out[k] for k in sorted(out)]


def save_version(ds, by=None, *, session_id=None):
    """Keep the gates as they stand as a new version and return it -- or the
    latest version when it holds exactly these gates already, or None when
    nothing is gated. `by` (`GATED_BY_AI` or `GATED_BY_USER`) defaults to who
    set them: Plexora AI only when an agent set every one (`_gated_by`)."""
    from datetime import datetime, timezone

    from plexora.plugins.gating.server import provenance

    gates = active_gates(ds)
    if not gates:
        return None
    if by is None:
        origins = provenance.read(ds.name)
        by = GATED_BY_AI if all(_gated_by(origins.get(m), low, high) == GATED_BY_AI
                                for m, (low, high) in gates.items()) else GATED_BY_USER
    with _lock_for(ds.name):
        kept = gate_versions(ds.name)
        if kept and _same_gates(kept[-1]["gates"], gates):
            return kept[-1]
        number = kept[-1]["version"] + 1 if kept else 1
        created = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows = [{"version": number, "created": created, "by": by,
                 "session_id": session_id, "marker": marker, "low": float(low),
                 "high": float(high)} for marker, (low, high) in gates.items()]
        frame = _store(ds.name).get_table(VERSIONS_TABLE)
        added = pl.DataFrame(rows, schema=VERSION_COLUMNS, orient="row")
        frame = added if frame is None or frame.height == 0 else pl.concat(
            [frame.select(list(VERSION_COLUMNS)).cast(VERSION_COLUMNS), added])
        _store(ds.name).put_table(VERSIONS_TABLE, frame)
    return {"version": number, "created": created, "by": by, "session_id": session_id,
            "gates": dict(gates)}


def _same_gates(a, b):
    return set(a) == set(b) and all(
        _same_range({"gate_start": a[m][0], "gate_end": a[m][1]},
                    {"gate_start": b[m][0], "gate_end": b[m][1]}) for m in a)


def version_label(version, taken=()) -> str:
    """A version's column name: who and when, local time ("Plexora AI
    2026-10-30 11:28"), with a number added when `taken` has it already."""
    from datetime import datetime

    try:
        when = datetime.fromisoformat(str(version["created"])).astimezone()
        stamp = when.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        stamp = str(version["created"])
    label = f"{version['by']} {stamp}"
    n = 2
    while label in taken:
        label = f"{version['by']} {stamp} ({n})"
        n += 1
    return label


def versions_table(ds) -> pl.DataFrame | None:
    """Markers as rows (every one any version gated, in the table's order),
    a column per version, oldest first (`version_label`), each holding the
    lower gate it kept; empty where a version had none. None with fewer than
    two versions -- one is the gates file itself."""
    kept = gate_versions(ds.name)
    if len(kept) < 2:
        return None
    gated = {m for v in kept for m in v["gates"]}
    order = [m for m in ds.table.markers if m in gated]
    order += sorted(gated - set(order))
    columns, labels = {"marker": order}, []
    for version in kept:
        label = version_label(version, labels)
        labels.append(label)
        columns[label] = [version["gates"][m][0] if m in version["gates"] else None
                          for m in order]
    return pl.DataFrame(columns, schema={"marker": pl.Utf8,
                                         **{label: pl.Float64 for label in labels}})


def versions_payload(datasource_name) -> list:
    """[{label, gates: {marker: low}}], oldest first, for the source file's
    `uns[<table>_versions]` (`anndata_gates.save_gate_versions`); [] with
    fewer than two versions, as `versions_table`."""
    kept = gate_versions(datasource_name)
    if len(kept) < 2:
        return []
    out, labels = [], []
    for version in kept:
        label = version_label(version, labels)
        labels.append(label)
        out.append({"label": label,
                    "gates": {m: float(low) for m, (low, _high) in version["gates"].items()}})
    return out


def source_extras(ds) -> dict:
    """What a save to the source file writes beside the latest gates:
    `provenance` -- a row per gated marker, who set it, how and when
    (`uns[<table>_provenance]`) -- and `versions` (`versions_payload`)."""
    from plexora.agent import cell_exclusions
    from plexora.plugins.gating.server.autogate import report

    try:
        qc_fp = cell_exclusions.fingerprint(cell_exclusions.call_record(ds))
    except Exception:  # no QC to read: the provenance goes without it
        qc_fp = ""
    rows = [{**row, "call_mode": cell_exclusions.current_call_mode(), "qc_fingerprint": qc_fp}
            for row in report.provenance_long_of(ds)]
    return {"provenance": rows, "versions": versions_payload(ds.name)}


def set_gate(ds, marker, low, high, *, expected_revision=None, allow_protected=False,
             empty=False):
    """Store one marker's range; returns (before, after, new_revision).

    `empty=True` allows `low == high`: a gate no cell passes (`low < v <= high`),
    which is how a failed or all-negative marker is recorded -- at the column's
    maximum, so every cell reads negative and the marker still counts as gated.

    Only the gating state in Plexora's own database changes. The source file is
    never touched here -- that stays an explicit, separate act
    (`gating.save_gates`), exactly as it is in the sidebar.

    A marker the user locked or approved is refused (`GateLocked`) unless
    `allow_protected` -- which only an explicit override may pass, and which
    never unlocks `locked`.
    """
    if marker not in ds.table.markers:
        raise KeyError(f"{marker!r} is not a marker of {ds.name!r}")
    low, high = float(low), float(high)
    if low > high or (low == high and not empty):
        raise ValueError(f"a gate needs low < high (got {low} and {high})")

    with _lock_for(ds.name):
        from plexora.plugins.gating.server import provenance

        status = provenance.status_of(ds.name, marker)
        if status == "locked" or (status == "approved" and not allow_protected):
            raise GateLocked(marker, status)
        current = revision(ds)
        if expected_revision is not None and str(expected_revision) != current:
            raise GateConflict(current)
        before = get_gate(ds, marker)
        rows = gate_rows(ds)
        present = {row.get("channel") for row in rows}
        rows.extend(row for row in default_rows(ds) if row["channel"] not in present)
        for row in rows:
            if row.get("channel") == marker:
                row["gate_start"] = low
                row["gate_end"] = high
                row.setdefault("gate_active", False)
        _store(ds.name).put_state(pickle.dumps(rows, protocol=4))
        after = get_gate(ds, marker)
        return before, after, revision(ds)


def reset_gates(ds, markers=None, *, expected_revision=None, include_approved=False):
    """Put markers back at their full range (never a locked one; an approved
    one only with `include_approved`). Returns (reset, skipped, new_revision):
    the markers reset and {marker: status} of those left alone."""
    from plexora.plugins.gating.server import provenance

    wanted = list(ds.table.markers) if markers is None else list(markers)
    unknown = [m for m in wanted if m not in ds.table.markers]
    if unknown:
        raise KeyError(f"not markers of {ds.name!r}: {unknown}")
    with _lock_for(ds.name):
        current = revision(ds)
        if expected_revision is not None and str(expected_revision) != current:
            raise GateConflict(current)
        protected = provenance.protected(ds.name)
        skipped = {m: protected[m] for m in wanted if protected.get(m) == "locked"
                   or (protected.get(m) == "approved" and not include_approved)}
        reset = [m for m in wanted if m not in skipped]
        defaults = {row["channel"]: row for row in default_rows(ds)}
        rows = gate_rows(ds)
        present = {row.get("channel") for row in rows}
        rows.extend(row for row in default_rows(ds) if row["channel"] not in present)
        for row in rows:
            if row.get("channel") in reset and row["channel"] in defaults:
                row["gate_start"] = defaults[row["channel"]]["gate_start"]
                row["gate_end"] = defaults[row["channel"]]["gate_end"]
        _store(ds.name).put_state(pickle.dumps(rows, protocol=4))
        return reset, skipped, revision(ds)


def clear_gates(ds, markers=None):
    """The panel's Clear: markers back to never gated -- their full range and
    no provenance row, so nothing says a person or an agent ever set them.
    `gate_active` is left as it is: it is not part of a gate but which marker
    the panel had on screen (`save_gating_list`), and a reload goes back to it.
    An approved gate is cleared too (the user is the one clearing it); a
    locked one is kept, as `/save_gating_list` keeps it: the lock is the
    user's word against every write, their own included, until they unlock
    it. Returns (cleared, kept): the markers cleared, and {marker: status} of
    those left alone."""
    from plexora.plugins.gating.server import provenance

    wanted = list(ds.table.markers) if markers is None else list(markers)
    unknown = [m for m in wanted if m not in ds.table.markers]
    if unknown:
        raise KeyError(f"not markers of {ds.name!r}: {unknown}")
    with _lock_for(ds.name), provenance._lock(ds.name):
        protected = provenance.protected(ds.name)
        kept = {m: protected[m] for m in wanted if protected.get(m) == "locked"}
        cleared = [m for m in wanted if m not in kept]
        defaults = {row["channel"]: row for row in default_rows(ds)}
        rows = gate_rows(ds)
        present = {row.get("channel") for row in rows}
        # Every marker keeps a row: an empty list is read as "never saved",
        # and the panel then seeds it from the source file's gates again.
        rows.extend(dict(row) for row in default_rows(ds) if row["channel"] not in present)
        for row in rows:
            if row.get("channel") in cleared and row["channel"] in defaults:
                row["gate_start"] = defaults[row["channel"]]["gate_start"]
                row["gate_end"] = defaults[row["channel"]]["gate_end"]
        _store(ds.name).put_state(pickle.dumps(rows, protocol=4))
        prov = provenance.read(ds.name)
        left = {m: r for m, r in prov.items() if m not in cleared}
        if len(left) != len(prov):
            provenance._write(ds.name, left)
    return cleared, kept


def put_rows(ds, rows, *, expected_revision=None):
    """Store a whole gate list as it was (an undo of `reset_gates`)."""
    with _lock_for(ds.name):
        current = revision(ds)
        if expected_revision is not None and str(expected_revision) != current:
            raise GateConflict(current)
        _store(ds.name).put_state(pickle.dumps([dict(r) for r in rows], protocol=4))
        return revision(ds)


def gated_summary(ds, marker, low=None, high=None) -> dict:
    """How many cells a range calls positive: the stored gate, or the one given.

    For the stored gate (no `low`, no `high`) of a marker the Single Cell
    Classifier decides, the counts are the classifier's calls
    (`classifier.calls.marker_calls`, the seam every consumer reads through),
    `source` says so, and `threshold` keeps what the stored gate alone would
    call -- the two stay distinct, so the gap between them is never lost. A
    range that is given is always counted by the range (`source`
    "threshold"): it is a question about that range."""
    out = _range_summary(ds, marker, low, high)
    if low is not None or high is not None:
        return out
    from plexora.plugins.gating.server.classifier import calls as classifier_calls

    classified = classifier_calls.summary(ds, marker)
    if classified is None:
        return out
    out["threshold"] = {k: out[k] for k in ("n_positive", "n_negative", "fraction")}
    for key in ("source", "n_positive", "n_negative", "n_not_classified", "n_uncertain",
                "fraction", "classifier", "stale"):
        out[key] = classified.get(key)
    return out


def _range_summary(ds, marker, low=None, high=None) -> dict:
    gate = get_gate(ds, marker)
    if gate is None:
        raise KeyError(f"{marker!r} is not a marker of {ds.name!r}")
    low = gate["low"] if low is None else float(low)
    high = gate["high"] if high is None else float(high)
    from plexora.agent import cell_exclusions

    raw = np.asarray(ds.table.columns([marker])[marker], dtype=np.float64)
    # Counted on the QC-passed cells: the fraction is a statistic the agent
    # judges by, and a fold's bright cells are not part of it. The gate itself
    # still applies to every cell (`range_mask`).
    values = cell_exclusions.masked(ds, marker, raw)
    finite = np.isfinite(values)
    # The rule `range_mask` uses -- low < value <= high, compared in float32
    # as the viewer's float32 columns are -- so this count is the count the
    # viewer colours, and the brightest cell passes a gate whose upper bound is
    # the column maximum (a float64 compare would drop it whenever that maximum
    # rounds up as a float32).
    v32 = values.astype(np.float32)
    positive = np.isfinite(v32) & (v32 > np.float32(low)) & (v32 <= np.float32(high))
    # Positives are counted among the cells that are called: a cell QC says
    # cannot be called for this marker is neither positive nor negative, and
    # the viewer draws it grey (`cell_exclusions.calls`).
    called = cell_exclusions.calls(ds, marker, raw, low, high)
    positive &= called != cell_exclusions.NOT_CALLED
    n_finite = int(finite.sum())
    n_positive = int(positive.sum())
    skip = cell_exclusions.not_called_mask(ds, marker)
    n_not_called = int(skip.sum()) if skip is not None else 0
    out = {
        "marker": marker,
        "source": "threshold",
        "low": low,
        "high": high,
        "n_cells": int(values.size),
        "n_finite": n_finite,
        "n_positive": n_positive,
        "n_negative": int((called == cell_exclusions.NEGATIVE).sum()),
        "n_not_called": n_not_called,
        "call_mode": cell_exclusions.current_call_mode(),
        "fraction": (n_positive / n_finite) if n_finite else None,
    }
    qc = cell_exclusions.describe(ds, marker=marker, n_total=int(values.size))
    if qc.get("applied"):
        out["n_qc_excluded"] = int(qc["n_left_out"])
        out["n_evaluated"] = n_finite
    out["qc_exclusion"] = qc
    return out


def gmm_for(ds, channel, selection_ids=()) -> dict:
    """`get_gating_gmm` for a handle set: the curves and the gate they imply,
    fitted on the QC-passed cells (the record rides in the payload, for a
    table on a data node)."""
    from plexora.agent import cell_exclusions

    selection_key = tuple(sorted(selection_ids)) if selection_ids else None
    record = cell_exclusions.current(ds)
    with cell_exclusions.using(record):
        payload = cell_exclusions.attach(ds, {
            "channel": channel, "selection_ids": list(selection_ids or [])})
    return ds.cached((channel, selection_key, cell_exclusions.fingerprint(record)),
                     lambda: _with_qc(ds.table.run("gating.gmm", payload), ds, record,
                                      channel))


def _with_qc(result, ds, record, channel):
    from plexora.agent import cell_exclusions

    if isinstance(result, dict):
        result = dict(result)
        result["qc_exclusion"] = cell_exclusions.describe(ds, record, marker=channel)
    return result


def fit_for(ds, channel):
    """The fitted components behind the auto gate, in the space they were fitted.

    {means, sds, weights, gate, fitted_in_log, floor_excluded} or None when the
    column has no mixture to find. `gate` is in the values' own units; the
    components are in log1p space when `fitted_in_log`. `floor_excluded` counts
    the cells of a spike at the minimum the fit was made without
    (`floor_spike`).
    """
    from plexora.agent import cell_exclusions

    record = cell_exclusions.current(ds)

    def compute():
        # The QC-passed cells only: a fold's or a blurred field's cells would
        # otherwise pull a component toward themselves (cell_exclusions.py).
        values = cell_exclusions.masked(ds, channel, ds.table.columns([channel])[channel],
                                        record)
        fitted, to_log, floor_n = _fit_body(values, values_logged(ds))
        if fitted is None:
            return None
        gate = _crossover(fitted)
        return {
            "means": [float(v) for v in fitted[0]],
            "sds": [float(v) for v in fitted[1]],
            "weights": [float(v) for v in fitted[2]],
            "gate": float(np.expm1(gate)) if to_log else float(gate),
            "fitted_in_log": bool(to_log),
            "floor_excluded": int(floor_n),
        }

    return ds.cached(("fit", 3, channel,
                      getattr(ds.table, "expression_fingerprint", None) or "",
                      cell_exclusions.fingerprint(record)), compute)


#: How far one "small / medium / large" step moves a gate, as a fraction of the
#: distance to the neighbouring population's centre.
ADJUST_FRACTIONS = {"small": 0.10, "medium": 0.25, "large": 0.50}

#: The step when there is no fit to measure distances against: a quantile shift.
ADJUST_QUANTILE_STEPS = {"small": 0.02, "medium": 0.05, "large": 0.10}


def adjusted_threshold(ds, marker, direction, magnitude):
    """(new_low, reason) for one qualitative step, without writing anything.

    The agent says "too low, a little" and this decides the number, so the same
    judgement always moves the gate the same distance. In the fit's own space:
    raising moves toward the positive population's centre, lowering toward the
    background's, by `ADJUST_FRACTIONS[magnitude]` of that distance, and never
    past either centre. With no fit, the gate moves by a quantile step instead.
    """
    if direction not in ("up", "down"):
        raise ValueError("direction must be 'up' or 'down'")
    if magnitude not in ADJUST_FRACTIONS:
        raise ValueError("magnitude must be 'small', 'medium' or 'large'")
    gate = get_gate(ds, marker)
    if gate is None:
        raise KeyError(f"{marker!r} is not a marker of {ds.name!r}")
    current = gate["low"]
    fit = fit_for(ds, marker)

    if fit is not None and len(fit["means"]) >= 2:
        to_space = np.log1p if fit["fitted_in_log"] else (lambda v: v)
        from_space = np.expm1 if fit["fitted_in_log"] else (lambda v: v)
        background, positive = fit["means"][-2], fit["means"][-1]
        g = float(to_space(max(current, 0.0) if fit["fitted_in_log"] else current))
        f = ADJUST_FRACTIONS[magnitude]
        # The guard band (server/mixture.py): a step never carries the
        # gate below one background sd above the pooled background's centre,
        # nor past half a positive sd above the positive centre. Fifty per
        # cent of the way to the middle component could otherwise land inside
        # the background. A gate already beyond the band is left where it is
        # rather than moved the wrong way.
        from plexora.plugins.gating.server.mixture import guard_band

        guard = guard_band(fit)
        if direction == "up":
            moved = g + f * (positive - g)
            moved = min(moved, positive - 1e-9 * max(1.0, abs(positive)))
            moved = min(moved, max(guard[1], g))
        else:
            moved = g - f * (g - background)
            moved = max(moved, background + 1e-9 * max(1.0, abs(background)))
            moved = max(moved, min(guard[0], g))
        new_low = float(from_space(moved))
        reason = (f"moved {direction} {magnitude} ({f:.0%} of the distance to the "
                  f"{'positive' if direction == 'up' else 'background'} population's "
                  f"centre{', in log1p space' if fit['fitted_in_log'] else ''})")
    else:
        from plexora.agent import cell_exclusions

        values = cell_exclusions.masked(
            ds, marker, np.asarray(ds.table.columns([marker])[marker], dtype=np.float64))
        values = values[np.isfinite(values)]
        if values.size == 0:
            raise ValueError(f"{marker!r} has no finite values to gate")
        step = ADJUST_QUANTILE_STEPS[magnitude]
        at = float((values <= current).mean())
        target = min(1.0, at + step) if direction == "up" else max(0.0, at - step)
        new_low = float(np.quantile(values, target))
        reason = (f"moved {direction} {magnitude} by a {step:.0%} quantile step "
                  "(no mixture fit for this marker)")

    high = gate["high"]
    if high is not None and new_low >= high:
        new_low = float(np.nextafter(high, -np.inf))
    return new_low, reason


def adjust_gate(ds, marker, direction, magnitude, *, expected_revision=None):
    """One qualitative step, written: (before, after, revision, reason)."""
    new_low, reason = adjusted_threshold(ds, marker, direction, magnitude)
    gate = get_gate(ds, marker)
    before, after, new_revision = set_gate(
        ds, marker, new_low, gate["high"], expected_revision=expected_revision)
    return before, after, new_revision, reason
