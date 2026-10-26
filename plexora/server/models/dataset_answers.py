"""The samples of one dataset share what was said about them once.

A cohort's tables almost always come out of one pipeline: the same columns,
the same cell id, the same X and Y, the same markers. And its images come off
one instrument with one panel. Asking about each sample's table, and naming
each image's channels, once per sample is asking the same question forty
times. So a sample that has not been answered yet borrows the answers of a
sample in the same dataset that has -- when, and only when, they fit:

- A **table** borrows a sibling's confirmed answers (column roles, the
  marker/metadata split, coordinates, the matrix, the one-image and row-id
  flags) when it is the same file, or has exactly the same columns as the
  sibling's table: the same header for a CSV, the same obs columns and obsm
  arrays for an AnnData. Anything else is asked as before, so a sample whose
  table differs is the one that gets the question. A sample that has answered
  anything about its own table is never touched, and the borrowed answers are
  only kept if the table then reads (`tool_routes._reload_or_restore`).
- An **image** whose channels still carry names nothing gave them
  (`Channel 1`, `S1.ome.tiff_1`, the tile key a data node served the file
  under) takes the names of a sibling with the same number of channels whose
  names were all given.

Pulled, not pushed: a sample borrows when it is about to ask (a tool opening,
the requirements route) or to be shown (the viewer), so samples added to the
dataset later are covered the same way, and nothing writes into forty records
because one was answered.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace

log = logging.getLogger(__name__)

#: Names Plexora gives channels when nothing named them: `Channel 3`, or the
#: image file's name and an index (`S1.ome.tiff_3`). A data node's tile key
#: (`nlu138-ome-443cf33d_0`) is caught by comparing with the key itself.
_UNNAMED = re.compile(
    r"channel[ _-]?\d+|.+\.(ome\.)?(tiff?|btf|qptiff|svs|ndpi|ims|zarr)_\d+",
    re.IGNORECASE)


def siblings(name) -> list:
    """The other samples in `name`'s dataset, in the dataset's order."""
    from plexora.server.models import datasets as registry

    dataset = registry.membership().get(name)
    if dataset is None:
        return []
    return [other for other in dataset.projects if other != name]


# -- tables --------------------------------------------------------------------


def _table_scoped(key) -> bool:
    from plexora.server.models.project import _is_table_scoped

    return _is_table_scoped(key)


def _answered(project) -> bool:
    return any(_table_scoped(key) for key in project.confirmed)


def _header(spec) -> frozenset:
    return frozenset(spec.columns.markers) | frozenset(spec.columns.metadata)


def same_table(mine, theirs) -> bool:
    """Whether two samples' tables are read the same way."""
    from plexora.server.models.adapters import is_flat_table

    if mine is None or theirs is None or mine.type != theirs.type:
        return False
    if mine.table != theirs.table:
        return False
    if mine.src == theirs.src:
        return True
    if is_flat_table(mine.type):
        header = _header(mine)
        return bool(header) and header == _header(theirs)
    obsm = lambda spec: frozenset(str(entry.get("key") or entry.get("name") or entry)  # noqa: E731
                                  for entry in spec.obsm)
    return (bool(mine.obs_columns) and frozenset(mine.obs_columns) == frozenset(theirs.obs_columns)
            and obsm(mine) == obsm(theirs))


def table_donor(project):
    """The sibling whose table answers `project` can borrow, or None."""
    from plexora.server.models.project import Project

    if project is None or project.dataset is None or _answered(project):
        return None
    if project.dataset.unresolved:
        return None
    for other in siblings(project.name):
        donor = Project.find(other)
        if donor is not None and donor.dataset is not None and _answered(donor) \
                and same_table(project.dataset, donor.dataset):
            return donor
    return None


def adopt_table_answers(name):
    """Borrow a sibling's table answers for `name`; the sibling's name, or None.

    Kept only if the table reads with them. A sibling whose answers do not
    read here is not tried a second time in the same call: the next sibling
    with the same columns would carry the same answers.
    """
    from plexora.server.models.project import Project
    from plexora.server.routes.tool_routes import _reload_or_restore

    previous = Project.find(name)
    donor = table_donor(previous)
    if donor is None:
        return None
    theirs = donor.dataset
    keys = [key for key in donor.confirmed if _table_scoped(key)]

    def borrow(project):
        spec = replace(project.dataset, roles=theirs.roles, columns=theirs.columns,
                       single_image=theirs.single_image,
                       row_number_ids=theirs.row_number_ids,
                       flat_cells=theirs.flat_cells,
                       coordinates=dict(theirs.coordinates),
                       features=dict(theirs.features),
                       obs_id_field=theirs.obs_id_field)
        return project.patch(dataset=spec).with_confirmed(keys)

    Project.mutate(name, borrow)
    try:
        refused = _reload_or_restore(name, previous)
    except Exception:  # noqa: BLE001 -- restored; the sample asks as it would have
        log.exception("Borrowing %s's table answers for %s", donor.name, name)
        return None
    if refused:
        log.info("%s's table answers do not read for %s: %s", donor.name, name, refused)
        return None
    log.info("%s took its table answers from %s (same dataset, same columns)",
             name, donor.name)
    return donor.name


# -- channel names -------------------------------------------------------------


def _channels(project):
    return [channel for channel in project.image.channels if channel.get("name") != "Area"]


def _unnamed(channel) -> bool:
    name = str(channel.get("name") or "")
    key = str(channel.get("src") or "").rstrip("/").rsplit("/", 1)[-1]
    return not name or name == key or bool(_UNNAMED.fullmatch(name))


def channel_donor(project):
    """The sibling whose channel names `project` can take, or None."""
    from plexora.server.models.project import Project

    # Any default name left means nobody named this panel: a list is named
    # whole, and a half-named one (a first channel guessed from a table's
    # markers, the rest `S1.ome.tiff_N`) is no one's choice either.
    mine = _channels(project) if project is not None else []
    if not mine or not any(_unnamed(channel) for channel in mine):
        return None
    for other in siblings(project.name):
        donor = Project.find(other)
        if donor is None:
            continue
        theirs = _channels(donor)
        if len(theirs) == len(mine) and not any(_unnamed(channel) for channel in theirs):
            return donor
    return None


def adopt_channel_names(name):
    """Name `name`'s unnamed channels after a sibling's; the sibling, or None."""
    from plexora.datasource import rename_channels
    from plexora.server.models.project import Project

    donor = channel_donor(Project.find(name))
    if donor is None:
        return None
    names = [channel["name"] for channel in _channels(donor)]
    try:
        rename_channels(name, names)
    except (ValueError, KeyError):
        return None
    try:
        from plexora.server.models import data_model

        data_model.load_datasource(name, reload=True)
    except Exception:  # noqa: BLE001 -- the record is right; the page reloads it anyway
        pass
    log.info("%s took its channel names from %s (same dataset)", name, donor.name)
    return donor.name
