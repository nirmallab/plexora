"""The Thresholding panel's second mode: the Single Cell Classifier.

Thresholding calls a marker positive by one number on one table column. The
classifier calls it from examples instead: a person clicks a few cells
positive and a few negative, one run trains a small model on per-cell
features (the table's value, its neighbours', and the marker's own pixels
inside each cell) and calls every cell QC lets be called. A run replaces the
marker's previous run whole; the eraser takes the marker back to how it was
before its first run and keeps the examples.

- `store`    -- examples, run records and per-row arrays, in gating's store
- `features` -- the per-cell feature matrix, cached; pixels read where they are
- `learner`  -- the model and its fit
- `runs`     -- run / revert / reset: the state machine
- `calls`    -- which source decides a marker (threshold or classifier), and
                its calls -- the seam every consumer reads through
- `export`   -- the two wide CSVs, the annotations JSON, and its import
- `source_write` -- AnnData / SpatialData layers and `uns["plexora_classifier"]`

Thresholds are never changed by any of it: the gate list stays exactly what
the slider left, so going back to Thresholding finds it as it was.
"""

#: Fewest examples of EACH class a run accepts.
MIN_EXAMPLES = 3

#: A cell whose score is within this of 0.5 is drawn as uncertain. It is
#: still called (positive at 0.5 and above): the band is a thing to look at,
#: not a third answer.
UNCERTAIN_BAND = 0.15

#: The two sources a marker's calls can come from.
SOURCES = ("threshold", "classifier")

#: One byte per table row in what the viewer draws (`calls.viewer_codes`).
VIEW_NEGATIVE, VIEW_POSITIVE, VIEW_NOT_CLASSIFIED, VIEW_UNCERTAIN = 0, 1, 2, 3


class ClassifierRefused(ValueError):
    """A run, import or revert that cannot go ahead; the message says why in
    the panel's words."""
