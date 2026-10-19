"""The model: small, regularised, and fitted in well under a second.

A handful of clicked cells is very little to learn from, so the default is a
logistic regression with a firm L2 penalty over standardised features, its
classes weighted so ten positives and three negatives pull equally. Once there
are enough examples to tell, a gradient-boosted tree model is tried as well
and kept only when cross-validation says it is clearly better.

The score is a logistic output in [0, 1]. It is called a probability only
when it was calibrated, which needs enough examples to hold some out
(`CALIBRATE_AT`); below that it is a score, and the exports say so.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from plexora.plugins.gating.server.classifier import MIN_EXAMPLES

#: Examples (both classes together) before gradient boosting is tried.
BOOST_AT = 40
#: How much better (balanced accuracy) boosting must cross-validate to be kept.
BOOST_MARGIN = 0.03
#: Examples before the score is calibrated and called a probability.
CALIBRATE_AT = 50
#: Rows scored at once.
CHUNK = 262_144


@dataclass
class Fitted:
    estimator: object
    kind: str
    calibrated: bool
    cv_balanced_accuracy: float | None
    n_positive: int
    n_negative: int
    notes: list = field(default_factory=list)

    def scores(self, matrix, cancelled=None):
        out = np.empty(matrix.shape[0], dtype=np.float32)
        for start in range(0, matrix.shape[0], CHUNK):
            if cancelled is not None and cancelled():
                raise InterruptedError("cancelled")
            block = matrix[start:start + CHUNK]
            out[start:start + CHUNK] = self.estimator.predict_proba(block)[:, 1]
        return out


def _logistic():
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    return make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True),
                         StandardScaler(),
                         LogisticRegression(C=0.5, class_weight="balanced", max_iter=1000))


def _boosted():
    from sklearn.ensemble import HistGradientBoostingClassifier

    return HistGradientBoostingClassifier(max_depth=3, learning_rate=0.1, max_iter=150,
                                          l2_regularization=1.0, class_weight="balanced")


def _cv_score(make, matrix, labels, folds):
    from sklearn.model_selection import StratifiedKFold, cross_val_score

    splitter = StratifiedKFold(n_splits=folds, shuffle=True, random_state=0)
    return float(np.mean(cross_val_score(make(), matrix, labels, cv=splitter,
                                         scoring="balanced_accuracy")))


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
    cv = _cv_score(_logistic, matrix, labels, folds) if folds >= 2 else None
    kind, make = "logistic_regression", _logistic
    if n_pos + n_neg >= BOOST_AT and folds >= 3:
        boosted = _cv_score(_boosted, matrix, labels, folds)
        if cv is None or boosted >= cv + BOOST_MARGIN:
            notes.append(f"gradient boosting cross-validated {boosted:.3f} against "
                         f"{cv:.3f} for logistic regression")
            kind, make, cv = "gradient_boosting", _boosted, boosted
    calibrated = n_pos + n_neg >= CALIBRATE_AT and min(n_pos, n_neg) >= 3
    if calibrated:
        from sklearn.calibration import CalibratedClassifierCV

        estimator = CalibratedClassifierCV(make(), method="sigmoid", cv=3)
    else:
        estimator = make()
    estimator.fit(matrix, labels)
    return Fitted(estimator, kind, calibrated, cv, n_pos, n_neg, notes)
