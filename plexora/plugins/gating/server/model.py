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
    table = api.project_data(datasource_name).table
    if not gates:
        return []
    id_key = start_keys[0]
    values = table.geometry()[id_key].to_numpy()[table.range_mask(gates)].tolist()
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


def download_gates(datasource_name, gates, channels):
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
    return csv.with_columns(pl.Series('thresholded', [
        _thresholded(row['channel'], row['gate_start'], row['gate_end'], description)
        for row in csv.iter_rows(named=True)
    ], dtype=pl.Boolean))


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
    dataset = api.project_data(datasource_name)
    selection_key = tuple(sorted(selection_ids)) if selection_ids else None
    cache_key = (channel_name, selection_key)

    return dataset.cached(cache_key, lambda: dataset.table.run("gating.gmm", {
        "channel": channel_name,
        "selection_ids": list(selection_ids or []),
    }))


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
    rows = pickle.loads(blob)
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
    """
    description = _description(ds)
    active = {}
    for row in gate_rows(ds):
        channel = row.get("channel")
        if not channel:
            continue
        low, high = row.get("gate_start"), row.get("gate_end")
        if low is None or high is None:
            continue
        desc = description.get(channel) or {}
        if low == desc.get("min") and high == desc.get("max"):
            continue
        active[channel] = (low, high)
    return active


def set_gate(ds, marker, low, high, *, expected_revision=None):
    """Store one marker's range; returns (before, after, new_revision).

    Only the gating state in Plexora's own database changes. The source file is
    never touched here -- that stays an explicit, separate act
    (`gating.save_gates`), exactly as it is in the sidebar.
    """
    if marker not in ds.table.markers:
        raise KeyError(f"{marker!r} is not a marker of {ds.name!r}")
    low, high = float(low), float(high)
    if not low < high:
        raise ValueError(f"a gate needs low < high (got {low} and {high})")

    with _lock_for(ds.name):
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


def gated_summary(ds, marker, low=None, high=None) -> dict:
    """How many cells a range calls positive: the stored gate, or the one given."""
    gate = get_gate(ds, marker)
    if gate is None:
        raise KeyError(f"{marker!r} is not a marker of {ds.name!r}")
    low = gate["low"] if low is None else float(low)
    high = gate["high"] if high is None else float(high)
    values = np.asarray(ds.table.columns([marker])[marker], dtype=np.float64)
    finite = np.isfinite(values)
    # The strict inequality `range_mask` uses, so this count is the count the
    # viewer colours.
    positive = finite & (values > low) & (values < high)
    n_finite = int(finite.sum())
    n_positive = int(positive.sum())
    return {
        "marker": marker,
        "low": low,
        "high": high,
        "n_cells": int(values.size),
        "n_finite": n_finite,
        "n_positive": n_positive,
        "fraction": (n_positive / n_finite) if n_finite else None,
    }


def gmm_for(ds, channel, selection_ids=()) -> dict:
    """`get_gating_gmm` for a handle set: the curves and the gate they imply."""
    selection_key = tuple(sorted(selection_ids)) if selection_ids else None
    return ds.cached((channel, selection_key), lambda: ds.table.run("gating.gmm", {
        "channel": channel,
        "selection_ids": list(selection_ids or []),
    }))


def fit_for(ds, channel):
    """The fitted components behind the auto gate, in the space they were fitted.

    {means, sds, weights, gate, fitted_in_log} or None when the column has no
    mixture to find. `gate` is in the values' own units; the components are in
    log1p space when `fitted_in_log`.
    """
    def compute():
        values = np.asarray(ds.table.columns([channel])[channel], dtype=np.float64)
        values = values[np.isfinite(values)]
        to_log = (not ds.table.log_transformed and values.size > 0
                  and values.min() >= 0)
        fitted = _fit_mixture(np.log1p(values) if to_log else values, _GATE_COMPONENTS)
        if fitted is None:
            return None
        gate = _crossover(fitted)
        return {
            "means": [float(v) for v in fitted[0]],
            "sds": [float(v) for v in fitted[1]],
            "weights": [float(v) for v in fitted[2]],
            "gate": float(np.expm1(gate)) if to_log else float(gate),
            "fitted_in_log": bool(to_log),
        }

    return ds.cached(("fit", channel), compute)


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
        if direction == "up":
            moved = g + f * (positive - g)
            moved = min(moved, positive - 1e-9 * max(1.0, abs(positive)))
        else:
            moved = g - f * (g - background)
            moved = max(moved, background + 1e-9 * max(1.0, abs(background)))
        new_low = float(from_space(moved))
        reason = (f"moved {direction} {magnitude} ({f:.0%} of the distance to the "
                  f"{'positive' if direction == 'up' else 'background'} population's "
                  f"centre{', in log1p space' if fit['fitted_in_log'] else ''})")
    else:
        values = np.asarray(ds.table.columns([marker])[marker], dtype=np.float64)
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
