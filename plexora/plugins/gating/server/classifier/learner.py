"""The model: small, regularised, and fitted in well under a second.

A handful of clicked cells is very little to learn from, so the default is a
logistic regression with a firm L2 penalty over standardised features, its
classes weighted so ten positives and three negatives pull equally. Once there
are enough examples to tell, a gradient-boosted tree model is tried as well
and kept only when cross-validation says it is clearly better.

The score is a logistic output in [0, 1]. It is called a probability only
when it was calibrated, which needs enough examples to hold some out
(`CALIBRATE_AT`); below that it is a score, and the exports say so.

**Which cells are uncertain** is not a fixed band around 0.5. The examples
are resampled (`RESAMPLES` bootstrap draws, each class drawn on its own so
both are always there) and a model is fitted to each draw; a cell is
uncertain when at least `UNCERTAIN_DISAGREEMENT` of those models call it the
other way from the final model. So the band is as wide as the examples leave
it: with six examples it is wide and says "label more cells like these", with
sixty it narrows to the cells that really sit on the boundary, and it is
wider where the examples are sparse than where they are dense -- which a
fixed distance from 0.5 cannot say. Each cell's `agreement` (the share of
resampled models that agree) is kept with its score.

Scoring is vectorised: the logistic models share one imputation and scaling,
so all of them score a chunk of cells in one matrix product.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from plexora.plugins.gating.server.classifier import MIN_EXAMPLES, UNCERTAIN_DISAGREEMENT

#: Examples (both classes together) before gradient boosting is tried.
BOOST_AT = 40
#: How much better (balanced accuracy) boosting must cross-validate to be kept.
BOOST_MARGIN = 0.03
#: Examples before the score is calibrated and called a probability.
CALIBRATE_AT = 50
#: Rows scored at once.
CHUNK = 262_144
#: Bootstrap draws behind the uncertainty: logistic models are cheap enough
#: to score together in one product; trees are scored one by one.
RESAMPLES = 25
RESAMPLES_TREES = 8
#: Logistic regression's L2 strength (sklearn's C: smaller is firmer).
C = 0.5


def _prep():
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    return make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True),
                         StandardScaler())


def _logistic():
    from sklearn.linear_model import LogisticRegression

    return LogisticRegression(C=C, class_weight="balanced", max_iter=1000)


def _boosted():
    from sklearn.ensemble import HistGradientBoostingClassifier

    return HistGradientBoostingClassifier(max_depth=3, learning_rate=0.1, max_iter=150,
                                          l2_regularization=1.0, class_weight="balanced")


def _logistic_pipeline():
    from sklearn.pipeline import make_pipeline

    return make_pipeline(_prep(), _logistic())


def _cv_score(make, matrix, labels, folds):
    from sklearn.model_selection import StratifiedKFold, cross_val_score

    splitter = StratifiedKFold(n_splits=folds, shuffle=True, random_state=0)
    return float(np.mean(cross_val_score(make(), matrix, labels, cv=splitter,
                                         scoring="balanced_accuracy")))


def _draws(labels, count, seed=0):
    """`count` bootstrap index sets, each class resampled on its own."""
    rng = np.random.default_rng(seed)
    pos = np.flatnonzero(labels == 1)
    neg = np.flatnonzero(labels == 0)
    return [np.concatenate([rng.choice(pos, pos.size), rng.choice(neg, neg.size)])
            for _ in range(count)]


@dataclass
class Fitted:
    kind: str
    calibrated: bool
    cv_balanced_accuracy: float | None
    n_positive: int
    n_negative: int
    prep: object = None
    estimator: object = None
    #: Logistic ensemble: (k, B) weights and (B,) intercepts on `prep`'s scale.
    weights: np.ndarray | None = None
    intercepts: np.ndarray | None = None
    #: Tree ensemble: fitted models, scored one by one.
    trees: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def scores(self, matrix, cancelled=None):
        """(score float32, agreement float32) per row: the final model's
        score, and the share of resampled models whose call matches its."""
        n = matrix.shape[0]
        score = np.empty(n, dtype=np.float32)
        agreement = np.empty(n, dtype=np.float32)
        for start in range(0, n, CHUNK):
            if cancelled is not None and cancelled():
                raise InterruptedError("cancelled")
            block = np.asarray(matrix[start:start + CHUNK], dtype=np.float32)
            if self.kind == "gradient_boosting":
                p = self.estimator.predict_proba(block)[:, 1]
                votes = np.mean([tree.predict_proba(block)[:, 1] >= 0.5
                                 for tree in self.trees], axis=0)
            else:
                scaled = self.prep.transform(block)
                p = self.estimator.predict_proba(scaled)[:, 1]
                votes = ((scaled @ self.weights + self.intercepts) > 0).mean(axis=1)
            call = p >= 0.5
            score[start:start + CHUNK] = p
            agreement[start:start + CHUNK] = np.where(call, votes, 1.0 - votes)
        return score, agreement


def uncertain(agreement):
    """Where at least UNCERTAIN_DISAGREEMENT of the resampled models disagree."""
    return np.asarray(agreement) <= (1.0 - UNCERTAIN_DISAGREEMENT)


def fit(matrix, labels) -> Fitted:
    """Fit on the examples' rows (`matrix`) and their labels (1/0)."""
    labels = np.asarray(labels, dtype=np.int64)
    n_pos, n_neg = int((labels == 1).sum()), int((labels == 0).sum())
    if n_pos < MIN_EXAMPLES or n_neg < MIN_EXAMPLES:
        from plexora.plugins.gating.server.classifier import ClassifierRefused

        raise ClassifierRefused(
            f"Needs {MIN_EXAMPLES} positive and {MIN_EXAMPLES} negative examples "
            f"(has {n_pos} positive, {n_neg} negative)")
    matrix = np.asarray(matrix, dtype=np.float32)
    folds = min(5, n_pos, n_neg)
    notes = []
    cv = _cv_score(_logistic_pipeline, matrix, labels, folds) if folds >= 2 else None
    kind = "logistic_regression"
    if n_pos + n_neg >= BOOST_AT and folds >= 3:
        boosted = _cv_score(_boosted, matrix, labels, folds)
        if cv is None or boosted >= cv + BOOST_MARGIN:
            notes.append(f"gradient boosting cross-validated {boosted:.3f} against "
                         f"{cv:.3f} for logistic regression")
            kind, cv = "gradient_boosting", boosted
    calibrated = n_pos + n_neg >= CALIBRATE_AT
    fitted = Fitted(kind, calibrated, cv, n_pos, n_neg, notes=notes)
    if kind == "gradient_boosting":
        base = _boosted
        fitted.trees = [_boosted().fit(matrix[idx], labels[idx])
                        for idx in _draws(labels, RESAMPLES_TREES)]
        if calibrated:
            from sklearn.calibration import CalibratedClassifierCV

            fitted.estimator = CalibratedClassifierCV(base(), method="sigmoid", cv=3)
        else:
            fitted.estimator = base()
        fitted.estimator.fit(matrix, labels)
        return fitted
    fitted.prep = _prep().fit(matrix)
    scaled = fitted.prep.transform(matrix)
    if calibrated:
        from sklearn.calibration import CalibratedClassifierCV

        fitted.estimator = CalibratedClassifierCV(_logistic(), method="sigmoid", cv=3)
    else:
        fitted.estimator = _logistic()
    fitted.estimator.fit(scaled, labels)
    weights, intercepts = [], []
    for idx in _draws(labels, RESAMPLES):
        model = _logistic().fit(scaled[idx], labels[idx])
        weights.append(model.coef_[0])
        intercepts.append(model.intercept_[0])
    fitted.weights = np.asarray(weights, dtype=np.float64).T
    fitted.intercepts = np.asarray(intercepts, dtype=np.float64)
    return fitted
