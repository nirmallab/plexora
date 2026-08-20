"""Where the auto button puts the gate.

The threshold used to be `mean(gmm.means_)` over a two-component fit: the
midpoint between the two component centres, ignoring how wide each one is and
how much of the data it holds. That is survivable on raw intensities, which are
so right-skewed that a two-component fit puts one narrow component on the bulk
and one wide one on the tail.

It falls apart the moment the values are log1p'd, which is what a user does to
make a threshold readable. log1p makes the background near-symmetric, and a
two-component fit given a symmetric background has an easier job splitting
*that* down the middle than separating it from the positives -- so the gate
landed inside the negative population and a marker with 3% positive cells gated
at 43%.

The fix is two changes that only work together: three components rather than
two, so the background can occupy two of them; and the crossover of the two
resulting populations rather than the midpoint of their centres. Fitted on a
log scale whichever scale the table is stored on, so the answer no longer
depends on whether the user ticked the log1p switch.

These tests are written against populations whose true membership is known, so
"the gate is right" means a number rather than a shape.
"""

import numpy as np
import pytest

from plexora.plugins.gating.server.model import auto_gate

N = 30000


def two_populations(fraction_positive, *, background=150, bright=1400,
                    spread=0.5, seed=11):
    """A marker column: a large log-normal background and a brighter
    population, which is what a quantification column actually looks like.

    The defaults are an ordinary well-stained marker -- roughly a ninefold
    separation. `bright=900, spread=0.6` gives a deliberately hard one, where
    the two populations overlap so heavily that no threshold can recover the
    split; that case has its own test rather than a loosened tolerance here.
    """
    rng = np.random.default_rng(seed)
    n_pos = int(N * fraction_positive)
    values = np.concatenate([
        rng.lognormal(np.log(background), spread, N - n_pos),
        rng.lognormal(np.log(bright), spread, n_pos),
    ])
    rng.shuffle(values)
    return values


def midpoint_of_means(values):
    """The estimator this replaced, for tests that measure the improvement
    rather than restate it."""
    from sklearn.mixture import GaussianMixture

    gmm = GaussianMixture(n_components=2, random_state=0)
    gmm.fit(values.reshape(-1, 1))
    return float(np.mean(gmm.means_))


def positive_fraction(values, gate):
    return float((values >= gate).mean())


# --------------------------------------------------------------------------
# The reported bug
# --------------------------------------------------------------------------

@pytest.mark.parametrize("truth", [0.03, 0.12, 0.25])
def test_a_log1p_column_gates_near_its_real_positive_fraction(truth):
    """A well separated marker, gated on the values the user is looking at."""
    log_values = np.log1p(two_populations(truth))

    gate, _, _ = auto_gate(log_values, log_transformed=True)

    assert positive_fraction(log_values, gate) == pytest.approx(truth, abs=0.04)


@pytest.mark.parametrize("truth", [0.12, 0.25])
def test_the_gate_no_longer_lands_in_the_middle_of_the_background(truth):
    """The reported regression, with the estimator it replaced beside it.

    Populations that overlap the way real ones do -- a sixfold separation, not
    a textbook gap. This is where the midpoint of two component means comes
    apart on log1p'd values: the background is near-symmetric on a log scale,
    a two-component fit splits *it* rather than separating it from the
    positives, and the midpoint of the resulting centres sits inside the
    negative population. Every cell brighter than average background then reads
    as positive, which is the "way more positive cells than there should be"
    this was reported as.
    """
    log_values = np.log1p(two_populations(truth, bright=900, spread=0.6))

    gate, _, _ = auto_gate(log_values, log_transformed=True)
    was = positive_fraction(log_values, midpoint_of_means(log_values))

    assert positive_fraction(log_values, gate) == pytest.approx(truth, abs=0.04)
    assert was > truth + 0.05, f"the old estimator called {was:.1%} positive"


def test_the_log_switch_does_not_move_the_gate():
    """The property the fix turns on. Log1p is a display choice about the same
    measurements, so the same cells have to come out positive either way. They
    did not: ticking the switch moved a 12% marker to 38%."""
    raw = two_populations(0.12)

    raw_gate, _, _ = auto_gate(raw, log_transformed=False)
    log_gate, _, _ = auto_gate(np.log1p(raw), log_transformed=True)

    assert np.expm1(log_gate) == pytest.approx(raw_gate, rel=1e-6)
    assert (positive_fraction(raw, raw_gate)
            == pytest.approx(positive_fraction(np.log1p(raw), log_gate), abs=1e-6))


def test_a_raw_column_is_not_made_worse():
    """The fit moved to log space for every project, not just transformed ones,
    so the untransformed path has to be at least as good as what it replaced."""
    raw = two_populations(0.03)

    gate, _, _ = auto_gate(raw, log_transformed=False)

    assert positive_fraction(raw, gate) == pytest.approx(0.03, abs=0.02)
    assert (abs(positive_fraction(raw, gate) - 0.03)
            <= abs(positive_fraction(raw, midpoint_of_means(raw)) - 0.03))


def test_heavily_overlapping_populations_are_improved_but_not_solved():
    """The honest limit. When the two populations overlap this much even a
    threshold that knows the true mixture only recovers ~2%, so this is not a
    case any gate gets right -- it is a case where the histogram is one hump
    and the user has to look.

    Asserted as an improvement rather than a target, because that is all that
    is on offer: the midpoint estimator called 46% of the cells positive here,
    which is not a hard case handled imperfectly but an answer with no
    relationship to the data.
    """
    hard = np.log1p(two_populations(0.03, bright=900, spread=0.6))

    gate, _, _ = auto_gate(hard, log_transformed=True)

    assert positive_fraction(hard, gate) < 0.15
    assert positive_fraction(hard, midpoint_of_means(hard)) > 0.40


def test_counts_with_zeros_are_gateable():
    """Integer counts, a large zero-inflated background, no transform. log1p
    rather than log is what keeps this case in: a plain log would drop every
    zero-valued cell out of the fit, and they are most of the column."""
    rng = np.random.default_rng(3)
    values = np.concatenate([rng.poisson(0.8, int(N * 0.9)).astype(float),
                             rng.poisson(40, int(N * 0.1)).astype(float)])
    rng.shuffle(values)

    gate, _, _ = auto_gate(values, log_transformed=False)

    assert positive_fraction(values, gate) == pytest.approx(0.10, abs=0.02)


# --------------------------------------------------------------------------
# Why the project's flag decides the scale, rather than a look at the numbers
# --------------------------------------------------------------------------

def test_logging_already_logged_values_is_the_failure_the_flag_prevents():
    """Why the scale is asked for rather than guessed at.

    A second log1p compresses what separation there is, so the damage lands
    exactly on the markers whose gate already had to be careful -- a well
    separated one survives it almost unchanged. That is the shape of the
    argument against "just always transform": it looks harmless on the cases
    where nothing was at stake.

    Neither number here is right -- these two populations overlap too heavily
    for any threshold (see the test above) -- so what is asserted is the size
    of the mistake, which is what the flag buys.
    """
    log_values = np.log1p(two_populations(0.03, bright=900, spread=0.6))

    told_the_truth, _, _ = auto_gate(log_values, log_transformed=True)
    logged_twice, _, _ = auto_gate(log_values, log_transformed=False)

    honest = positive_fraction(log_values, told_the_truth)
    doubled = positive_fraction(log_values, logged_twice)
    assert doubled > 2 * honest, (
        f"a wrong log flag cost {doubled:.1%} against {honest:.1%}")


def test_values_that_have_no_log_are_fitted_as_they_stand():
    """arcsinh and z-scored tables carry negatives. There is no log to take, so
    the fit happens in the values' own space rather than dropping half the
    column or producing NaNs."""
    values = np.log1p(two_populations(0.12)) - 5.0
    assert values.min() < 0

    gate, _, _ = auto_gate(values, log_transformed=False)

    assert np.isfinite(gate)
    assert positive_fraction(values, gate) == pytest.approx(0.12, abs=0.05)


# --------------------------------------------------------------------------
# What the panel draws
# --------------------------------------------------------------------------

def test_the_curves_explain_the_gate():
    """One fit answers both, so the picture cannot disagree with the button.
    The curves used to be a separate two-component fit while the gate was a
    summary of it that ignored their widths, which left the auto button landing
    somewhere the drawing did not account for."""
    log_values = np.log1p(two_populations(0.12))
    at = np.linspace(log_values.min(), log_values.max(), 50)

    gate, background, positive = auto_gate(log_values, log_transformed=True, at=at)

    below = at < gate
    above = at > gate
    assert (background[below] >= positive[below]).all()
    assert (positive[above] >= background[above]).any()


def test_the_curves_come_back_in_the_columns_own_units():
    """They are laid over a histogram of the raw column, so a density fitted in
    log space has to be mapped back -- Jacobian included, or the shape is right
    and the height is not. Integrating them has to give back roughly 1."""
    raw = two_populations(0.12)
    at = np.linspace(raw.min(), raw.max(), 4000)

    _, background, positive = auto_gate(raw, log_transformed=False, at=at)

    area = np.trapezoid(background + positive, at)
    assert area == pytest.approx(1.0, abs=0.05)


# --------------------------------------------------------------------------
# Columns there is nothing to fit
# --------------------------------------------------------------------------

@pytest.mark.parametrize("values, why", [
    (np.zeros(500), "a constant column"),
    (np.array([0.0, 1.0]), "fewer rows than components"),
    (np.array([]), "an empty column"),
    (np.full(500, np.nan), "nothing finite"),
])
def test_a_column_with_no_mixture_reports_no_gate(values, why):
    """No number rather than one derived from noise -- the client leaves the
    slider where the user put it. Returning something plausible-looking here is
    worse than saying nothing, because a gate carries no visible uncertainty."""
    assert auto_gate(values, log_transformed=False) == (None, None, None), why
