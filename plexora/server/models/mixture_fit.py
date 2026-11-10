"""The mixture fits themselves: Gaussians over one column of values -- a
channel's log intensities (`data_model.channel_gmm_of`, three components) or a
marker's cell values (the gating plugin's auto gate).

On its own, importing numpy and scikit-learn and nothing of Plexora's, because
the website's helper Pythons run this very module (services/fitHelpers.js,
workers/fitWorker.js: the main Python sends its compiled file with each fit),
so a fit made there is the one the main Python would have made, bit for bit --
same code, same builds, same seed. Nothing else belongs here.
"""

import numpy as np

#: The mixture fit's k-means start. Seeded: unseeded, k-means' random start put
#: the same channel's window in one of two places from one fit to the next (a
#: stack's DNA at 267 or 1593 one time in ten), so the website and the desktop
#: -- or one app twice -- opened the same image at different levels.
FIT_SEED = 0


def fit_mixture(values, components=3):
    """The fitted mixture of `values` (1-D) as 3 x `components` numbers: the
    means, the variances, the weights, each in the order scikit-learn found
    them (`mixture_parts` splits them).

    In the values' own floating type, in and out -- scikit-learn fits float32
    in float32 -- so a caller gets exactly the numbers a direct fit gives."""
    from sklearn.mixture import GaussianMixture

    gmm = GaussianMixture(components, max_iter=1000, tol=1e-6, random_state=FIT_SEED)
    gmm.fit(np.asarray(values).reshape((-1, 1)))
    return np.concatenate([gmm.means_[:, 0], gmm.covariances_[:, 0, 0], gmm.weights_])


def mixture_parts(params):
    """(means, variances, weights) from `fit_mixture`'s floats."""
    params = np.asarray(params)
    n = params.size // 3
    return params[0:n], params[n:2 * n], params[2 * n:3 * n]
