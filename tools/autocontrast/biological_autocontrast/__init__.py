"""Biological auto-contrast: learned per-channel display windows for multiplex images.

A standalone research pipeline (see tools/autocontrast/README.md and
docs/internal/AUTOCONTRAST.md). Nothing here is imported by Plexora; Plexora is
imported from here, as a library, for its readers and heuristics.

This module stays import-light on purpose: the training environment has torch
but neither tifffile nor plexora, so every heavy import lives inside the
function that needs it.
"""

from biological_autocontrast.config import VERSION

__all__ = ["VERSION", "predict_contrast"]


def predict_contrast(*args, **kwargs):
    """See `biological_autocontrast.predict.predict_contrast`."""
    from biological_autocontrast.predict import predict_contrast as run

    return run(*args, **kwargs)
