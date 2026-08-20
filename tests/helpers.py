"""Builders for the project record, shared by the test suite.

Tests used to hand-write config dicts, so every test file carried its own copy
of the on-disk shape and a schema change meant editing all of them. These
builders keep that knowledge in one place: a test says what it means (this
project has a CSV whose x column is X_centroid) and the shape is somebody
else's problem.
"""

from __future__ import annotations

from plexora.server.models.project import (
    ROLE_NAMES,
    ColumnGroups,
    ColumnRoles,
    DataSpec,
    ImageSpec,
    Project,
    SegmentationSpec,
)

#: Every requirement key core can record an answer for.
#:
#: A test whose subject is not the asking machinery passes this so its project
#: opens a tool straight away. Without it a fully-populated project still stops
#: on first launch, because a value the column predictor guessed is not an
#: answer the user gave -- see plexora/api/plugin.py's unconfirmed_from.
ALL_CONFIRMED = (
    ("table", "segmentation", "markers", "features")
    + tuple(f"role:{role}" for role in ROLE_NAMES)
)


def csv_spec(src, *, cell_id="CellID", x="X_centroid", y="Y_centroid",
             celltype=None, image_id=None, markers=(), metadata=(),
             single_image=True):
    """A DataSpec for a flat CSV feature table.

    `single_image` defaults on for the same reason ALL_CONFIRMED exists: the
    image-id question blocks until it has a real answer, and "this table covers
    one image" is one -- so a fixture whose subject is something else says so
    once here rather than in every test. Pass False to leave that question
    genuinely open.
    """
    return DataSpec(
        type="csv",
        src=str(src),
        roles=ColumnRoles(cell_id=cell_id, x=x, y=y, celltype=celltype, image_id=image_id),
        columns=ColumnGroups(markers=tuple(markers), metadata=tuple(metadata)),
        single_image=single_image and not image_id,
    )


def anndata_spec(src, *, table=None, coordinates=None, features=None, subset=None,
                 obs_id_field=None, cell_id="id", celltype=None, image_id=None,
                 is_transformed=False, markers=(), metadata=(), obs_columns=(),
                 obsm=(), single_image=True, row_number_ids=True):
    """A DataSpec for an .h5ad file, or one table of a .zarr store when
    `table` is given. x/y are always the literal 'X'/'Y' the adapter
    synthesizes; `obs_columns` is the source file's own annotations, which is
    what the role questions are asked about."""
    return DataSpec(
        type="spatialdata" if table else "anndata",
        src=str(src),
        table=table,
        coordinates=coordinates or {},
        features=features or {"source": "X"},
        subset=subset or {},
        obs_id_field=obs_id_field,
        is_transformed=is_transformed,
        roles=ColumnRoles(cell_id=cell_id, x="X", y="Y", celltype=celltype, image_id=image_id),
        columns=ColumnGroups(markers=tuple(markers), metadata=tuple(metadata)),
        obs_columns=tuple(obs_columns),
        obsm=tuple(dict(entry) for entry in obsm),
        # See csv_spec's note -- same reason, same escape hatch.
        single_image=single_image and not image_id,
        # Likewise: a fixture that names no obs id column is a project whose
        # user answered "number the rows", not one that was never asked. Left
        # off, every AnnData fixture would report an unanswered cell id and any
        # test about something else would stall in the requirements modal.
        row_number_ids=row_number_ids and not obs_id_field,
    )


def image_spec(*, channels=("DNA", "CD3"), kind="ome_tiff", width=100, height=80,
               src="/tmp/image.ome.tif", with_area=False):
    """An ImageSpec with one entry per named channel, optionally preceded by
    the 'Area' segmentation placeholder the viewer expects when a project has
    a mask."""
    entries = []
    if with_area:
        entries.append({"name": "Area", "fullname": "Area", "src": "/generated/data/x/seg/"})
    entries.extend(
        {"name": c, "fullname": c, "src": f"/generated/data/x/{c}/"} for c in channels
    )
    return ImageSpec(
        src=src,
        kind=kind,
        channels=tuple(entries),
        width=width,
        height=height,
        max_level=1,
        tile_width=1024,
        tile_height=1024,
        num_channels=len(channels),
    )


def project(name="demo", *, dataset=None, segmentation=None, image=None,
            cell_layer=None, confirmed=(), **image_kwargs):
    """A Project. `segmentation` accepts a path (ready), 'pending', or None."""
    if segmentation == "pending":
        seg = SegmentationSpec(source="/tmp/mask.tif", status="pending")
    elif segmentation:
        seg = SegmentationSpec(derived=str(segmentation), source=str(segmentation))
    else:
        seg = SegmentationSpec()
    return Project(
        name=name,
        image=image if image is not None else image_spec(**image_kwargs),
        segmentation=seg,
        dataset=dataset,
        # The user's override. Leaving it None is the ordinary case -- the
        # project resolves the default itself (mask, else centroids).
        cell_layer_choice=cell_layer,
        confirmed=tuple(confirmed),
    )


def entry(name="demo", **kwargs):
    """The on-disk dict for a project -- for tests that write config.json
    directly or call an API that takes a raw entry."""
    return project(name, **kwargs).to_entry()
