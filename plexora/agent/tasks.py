"""The biological tasks a request can be about, in the words people use.

`validate_scope` matches a request against capability names and tags first,
then against their purposes. A request phrased in domain language -- "are
these T cells exhausted?" -- names neither: it names a *task* (phenotyping)
that Plexora serves through capabilities whose tags say so. This is the last
tier, and it answers `can_recommend` with what the agent must establish first
(which marker defines the phenotype), or `outside_domain` with a reason when
the task belongs to another tool.

Deliberately a word list, not a model: the agent reading the answer is the one
that understands language. Marker synonyms (CD8 / CD8a / CD8A) are the next
step -- `marker_terms` is where they plug in.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Task:
    name: str
    #: Words that say a request is about this task.
    words: frozenset
    #: Capability tags that serve it. Empty: Plexora does not do this task.
    tags: tuple
    #: What the agent must pin down before any capability can answer.
    establish: tuple = ()
    #: Why, when `tags` is empty.
    reason: str = ""


TASKS = (
    Task(
        "phenotyping",
        frozenset({"phenotype", "phenotypes", "phenotyping", "exhausted", "exhaustion",
                   "activated", "activation", "cytotoxic", "naive", "memory", "effector",
                   "regulatory", "treg", "tregs", "macrophage", "macrophages",
                   "lymphocyte", "lymphocytes", "tumour", "tumor", "immune", "stromal",
                   "epithelial", "proliferating", "proliferative", "subset", "subsets",
                   "population", "populations", "celltype", "type", "types", "identity",
                   "express", "expressing", "expression", "high", "low"}),
        ("marker", "gate", "phenotype"),
        establish=({"key": "marker",
                    "label": "which marker(s) define this phenotype in this panel; "
                             "list_markers shows what the table has"},
                   {"key": "threshold",
                    "label": "where each marker's gate sits; get_gate or "
                             "suggest_auto_gate, then check it visually"}),
    ),
    Task(
        "qc",
        frozenset({"quality", "artefact", "artefacts", "artifact", "artifacts", "blurry",
                   "blur", "saturated", "saturation", "background", "bleed", "bleedthrough",
                   "autofluorescence", "fold", "folds", "debris", "focus", "noisy",
                   "noise", "staining", "stain", "dim", "bright"}),
        ("qc", "distribution", "render"),
        establish=({"key": "marker",
                    "label": "which channel or marker to check; list_channels"},),
    ),
    Task(
        "neighbourhood",
        frozenset({"neighbourhood", "neighborhood", "neighbourhoods", "neighborhoods",
                   "enrichment", "colocalization", "colocalisation", "interaction",
                   "interactions", "proximity", "niche", "niches", "clustering",
                   "communities", "ripley"}),
        (),
        reason="spatial statistics (neighbourhoods, enrichment, interactions) belong to "
               "an analysis server such as SCIMAP; Plexora gates and shows the cells, "
               "and hands them off",
    ),
)


def task_for(words) -> Task | None:
    """The task these request words are most about, or None."""
    best, score = None, 0
    for task in TASKS:
        hits = len(task.words & set(words))
        if hits > score:
            best, score = task, hits
    return best


def marker_terms(words, markers) -> list:
    """The project's markers a request names, matched case-insensitively.

    The seam for a synonym vocabulary: today a word names a marker only when it
    is that marker's own name.
    """
    by_lower = {str(marker).lower(): marker for marker in markers or ()}
    return [by_lower[word] for word in sorted(words) if word in by_lower]
