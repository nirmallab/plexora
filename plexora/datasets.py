"""Datasets and projects, from Python.

    import plexora
    plexora.create_dataset("Melanoma Cohort",
                           images=["s1.ome.tif", "s2.ome.tif", "s3.ome.tif"])

Everything the Open Project page can do is here, and the reverse: a dataset
made from a notebook is the same record the page draws, because there is one
registry underneath both. That is the whole point of the module -- a cohort of
forty slides is registered in a loop, not by dropping forty files on a form.

**An image is the only thing a project must have.** `create_project("x.tif")`
is a complete, valid project. Everything else -- a mask, a table, which table
inside a store, which column holds the cell id -- is optional here exactly as
it is in the UI, and what is left unsaid is asked for later by whatever first
needs it. A registration that refuses because the user has not yet decided
which of six tables to read is a registration that makes them decide before
they can look.

**An answer given here is an ANSWER; a value worked out is a guess.** Naming
`cell_id="CellID"` records it as confirmed and nothing asks again. Letting the
column predictor find it leaves it as a prefill the first tool that depends on
it will show back once. The two are different states in the project record
(`Project.confirmed`) and this module is careful about which it writes, because
a bulk registration that marked every guess as a decision would silently
propagate one bad heuristic across a whole cohort.

Registration is **synchronous**, and a slide's pyramid takes real time to
build -- a cohort of ten is minutes. That is accepted rather than hidden: the
call blocks, and batching it into the background is dataset-level work for
later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

#: Every key a project may be defined with, in one place, so the Python API,
#: the CLI and any `--from spec.json` all accept exactly the same vocabulary.
#: A caller that knows this tuple knows the whole schema.
PROJECT_SPEC_KEYS = (
    # The one required key.
    "image",
    # Files.
    "name", "segmentation", "data",
    # Which part of the data file.
    "table", "subset", "layer", "log1p",
    # What the columns mean.
    "cell_id", "x", "y", "sample", "celltype", "markers", "metadata",
    "coordinates", "single_image", "row_number_ids",
    # The image itself.
    "channel_names", "image_type", "copy",
    # Where it goes, and what to do about a name already taken.
    "dataset", "exist_ok",
)


class DatasetCreateError(RuntimeError):
    """A batch registration that stopped part-way.

    Carries what DID land (`created`) as well as what failed, because the
    alternative -- rolling back -- means throwing away however many slides were
    already converted, which on a cohort is an hour of work undone to tidy up
    after a typo in the last filename.
    """

    def __init__(self, message, *, dataset=None, created=(), failed=None, cause=None):
        super().__init__(message)
        #: The dataset the successful ones were assigned to, or None if the
        #: failure came before it existed.
        self.dataset = dataset
        #: Project names that were registered before the failure.
        self.created = list(created)
        #: The spec that failed.
        self.failed = failed
        #: What it raised.
        self.cause = cause


@dataclass(frozen=True)
class Dataset:
    """One dataset, as a handle you can go on using.

    Every method returns a FRESH handle rather than mutating this one: the
    registry is a file that other processes write too, so a handle is a
    snapshot and saying so in the type is cheaper than explaining it.
    """

    id: str
    name: str
    description: str = ""
    created_at: str = ""
    projects: tuple[str, ...] = ()
    meta: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def _from_record(cls, record):
        return cls(id=record.id, name=record.name, description=record.description,
                   created_at=record.created_at, projects=tuple(record.projects),
                   meta=dict(record.meta))

    def __len__(self):
        return len(self.projects)

    def __iter__(self):
        return iter(self.projects)

    def __contains__(self, name):
        return name in self.projects

    def refresh(self) -> "Dataset":
        """Read this dataset again. What another process has done since."""
        return dataset(self.id)

    def add(self, *names) -> "Dataset":
        """Put projects in this dataset, taking them out of any other."""
        from plexora.server.models import datasets as registry

        registry.assign(_flatten(names), self.id)
        return self.refresh()

    def remove(self, *names) -> "Dataset":
        """Take projects out of this dataset. **They are not deleted.**"""
        from plexora.server.models import datasets as registry

        wanted = [name for name in _flatten(names) if name in self.projects]
        if wanted:
            registry.assign(wanted, None)
        return self.refresh()

    def rename(self, name) -> "Dataset":
        from plexora.server.models import datasets as registry

        return Dataset._from_record(registry.rename(self.id, name))

    def describe(self, text=None, **meta) -> "Dataset":
        """Set the description, merge into `meta`, or both."""
        from plexora.server.models import datasets as registry

        return Dataset._from_record(registry.describe(
            self.id, description=text, meta=meta or None))

    def delete(self) -> None:
        """Delete this dataset. **The projects in it stay**, at the top
        level -- exactly as deleting a folder does in the UI."""
        from plexora.server.models import datasets as registry

        registry.remove(self.id)

    def manifest(self) -> dict:
        """What every project in this dataset has, keyed by name.

        The dataset-level view of "is this cohort ready?" -- one read per
        project of the same rules a plugin applies before opening.
        """
        return {name: project_manifest(name) for name in self.projects}


def _flatten(names) -> list:
    """Accept `add("a", "b")` and `add(["a", "b"])` alike."""
    out = []
    for item in names:
        if isinstance(item, str):
            out.append(item)
        else:
            out.extend(str(name) for name in item)
    return out


# -- reading ---------------------------------------------------------------


def list_datasets() -> list:
    """Every dataset, by name.

    `list_datasets` and not `datasets`, which would collide with this module's
    own name the moment anybody wrote `from plexora import datasets`.
    """
    from plexora.server.models import datasets as registry
    from plexora.server.models.project import Project

    known = set(Project.load_all())
    return [Dataset._from_record(record)
            for record in sorted(registry.load_all(known=known).values(),
                                 key=lambda d: d.name.casefold())]


def dataset(name_or_id) -> Dataset:
    """One dataset, by name or by id. Raises KeyError naming what does exist."""
    from plexora.server.models import datasets as registry
    from plexora.server.models.project import Project

    known = set(Project.load_all())
    try:
        return Dataset._from_record(registry.resolve(name_or_id, known=known))
    except registry.DatasetError as exc:
        # KeyError, because that is what "no such thing" is in Python and what
        # a notebook user will have written `except KeyError` for. The registry
        # raises ValueError because its other callers are routes turning it
        # into a 400.
        raise KeyError(str(exc)) from None


def project_manifest(name) -> dict:
    """What one project has, what it was told, and what is still open.

    `{"name": …, "manifest": {key: {status, value, confirmed}}, "summary": {…}}`
    -- the same read every other surface makes, so "is this set up?" has one
    answer whoever asks.
    """
    from plexora.server.models import manifest as manifest_model
    from plexora.server.models.project import Project

    project = Project.find(name)
    if project is None:
        raise KeyError(f"no project named {name!r}")
    return {"name": project.name,
            "manifest": manifest_model.manifest(project),
            "summary": manifest_model.summary(project)}


# -- registering -----------------------------------------------------------


def create_dataset(name, images=None, projects=None, *, description="",
                   exist_ok=False) -> Dataset:
    """Make a dataset, registering its projects if they are not there yet.

        plexora.create_dataset("Melanoma Cohort",
                               images=["s1.ome.tif", "s2.ome.tif"])

        plexora.create_dataset("Melanoma Cohort", projects=[
            {"image": "s1.ome.tif", "segmentation": "s1_mask.tif",
             "data": "s1.csv", "cell_id": "CellID"},
            {"image": "s2.ome.tif"},
        ])

    `images` is the short form: a list of image paths, each becoming a project
    named after its file. `projects` is the long form: a list of spec dicts (or
    bare paths, which mean the same as in `images`) taking any of
    `PROJECT_SPEC_KEYS`. Neither makes an empty dataset, which is a perfectly
    reasonable thing to want -- somewhere to drag things into.

    An entry naming a project that already exists is adopted rather than
    re-registered, so a call can be re-run after fixing one bad path without
    re-converting everything before it.

    **Everything is validated before anything is registered**: the dataset
    name, every spec key, every path. A cohort that fails on the last slide
    because of a typo in its filename should fail before the first one has
    spent four minutes building a pyramid.

    On a failure PART WAY THROUGH conversion, what succeeded is kept, assigned,
    and named in the `DatasetCreateError` -- see that class for why there is no
    rollback.
    """
    from plexora.server.models import datasets as registry

    if images is not None and projects is not None:
        raise ValueError("Pass `images` or `projects`, not both -- "
                         "`images` is the short form of `projects`.")
    specs = [_as_spec(entry) for entry in (projects if projects is not None
                                           else images or [])]

    existing = registry.find_by_name(name)
    if existing is not None and not exist_ok:
        raise ValueError(f"there is already a dataset called {existing.name!r} "
                         "(pass exist_ok=True to add to it)")

    # Everything that can be checked without touching a file, first.
    _validate_batch(specs)

    record = existing or registry.create(name, description=description)
    created = []
    for spec in specs:
        try:
            created.append(create_project(**spec, dataset=record.id))
        except Exception as exc:
            raise DatasetCreateError(
                f"{spec['image']}: {exc}",
                dataset=Dataset._from_record(registry.get(record.id)),
                created=created, failed=spec, cause=exc) from exc
    return Dataset._from_record(registry.get(record.id))


def project_from_spec(spec) -> str:
    """Register one project from a spec dict. `create_project(**spec)`.

    Exists so the CLI's `--from file.json` and `create_dataset(projects=[…])`
    read the same document through the same code, rather than each growing its
    own idea of what a spec is.
    """
    return create_project(**_as_spec(spec))


def import_sample(*paths, name=None, dataset=None, answers=None,
                  replace=None, wait=False) -> str:
    """Register one sample from whatever these paths are, and return its name.

    The programmatic form of **Import Sample**: point it at a Xenium run, a
    SpatialData store, a folder, or any mix of files, and it detects what they
    are, groups them, picks the reference and registers the lot.

        plexora.import_sample("/data/xenium/run_0042")
        plexora.import_sample("slide.ome.tif", "slide_mask.tif", "cells.csv")

    `create_project` is the same registration reached the other way -- by
    NAMING each role (`image=`, `segmentation=`, `data=`) rather than letting
    detection work them out. Both end at the same per-resource writers
    (`register_image_datasource`, `attach_segmentation`,
    `replace_project_data`), which is what makes them produce the same record;
    `tests/test_import_entry_points.py` asserts that rather than assuming it.
    Use `create_project` when you already know which file is which -- in a
    script over a directory of runs, it is the clearer thing to read.

    @param answers - `{question_id: value}` for anything detection could not
        work out. Unanswered questions take their default and are recorded on
        the layer as `unresolved`, so nothing here ever refuses an import for
        want of an answer.
    @param wait - block until every derived artefact (transcript tiles, the
        mask pyramid) has been built. False returns as soon as the record
        exists, which is what the viewer wants; True is for a script whose next
        line reads the result.
    """
    from plexora.server.models import import_sample as importer

    result = importer.import_sample(
        [str(p) for p in paths], answers=answers, name=name, dataset=dataset,
        replace=replace)
    if wait:
        _wait_for_layers(result["name"])
    return result["name"]


def add_layers(name, *paths, answers=None, wait=False) -> list:
    """Add layers to a sample that already exists. Returns what was added."""
    from plexora.server.models import import_sample as importer

    result = importer.add_layers(name, [str(p) for p in paths], answers=answers)
    if wait:
        _wait_for_layers(name)
    return result["layers"]


def _wait_for_layers(name, timeout=3600):
    """Block until nothing about this sample is still being prepared.

    Polls the same document the browser polls, for the same reason a script
    needs one: the builds are daemon threads, and a script that read the tiles
    immediately would read a half-written cache.
    """
    import time

    from plexora.server.models import layer_jobs

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not layer_jobs.status(name).get("pending"):
            return True
        time.sleep(0.25)
    return False


def create_project(image, *, name=None, segmentation=None, data=None,
                   table=None, subset=None, cell_id=None, x=None, y=None,
                   sample=None, celltype=None, markers=None, metadata=None,
                   coordinates=None, layer=None, log1p=None,
                   single_image=None, row_number_ids=None, channel_names=None,
                   image_type=None, copy=False, exist_ok=False,
                   dataset=None) -> str:
    """Register one project and return its name.

    `image` is the only required argument, and a project with nothing else is
    complete: it opens, it can be looked at, and a mask or a table can be
    attached to it at any point afterwards.

    Everything named here is recorded as an ANSWER -- `Project.confirmed` --
    so nothing asks about it again. Everything left out is either absent or a
    guess, and is asked for by whatever first needs it. That distinction is the
    whole contract: see the module docstring.

    `name` defaults to the image's filename, deduplicated against what is
    already registered. With `exist_ok`, a project already pointing at this
    image is adopted instead of a second one being made beside it.
    """
    from plexora import get_config
    from plexora.datasource import (
        _dedupe_dataset_name,
        _derive_dataset_name_from_path,
        _find_existing_datasource_for_image,
    )
    from plexora.server.routes.import_routes import (
        _FLAT_IMAGE_SUFFIXES,
        attach_segmentation,
    )

    image_path = _existing_path(image, "image")
    config = get_config()

    # An image already registered is the same project, not a second copy of
    # it. The rule quick view uses, and what makes re-running a batch cheap.
    if name is None and exist_ok:
        name = _find_existing_datasource_for_image(image_path, config)
    if name is None:
        name = _dedupe_dataset_name(
            _derive_dataset_name_from_path(image_path), config.keys())
    name = str(name)

    if name in config:
        if not exist_ok:
            raise ValueError(f"there is already a project called {name!r} "
                             "(pass exist_ok=True to configure it instead)")
    else:
        mask = _existing_path(segmentation, "segmentation") if segmentation else None
        source = _existing_path(data, "data") if data else None
        _register(name, image_path, mask, source, table=table, subset=subset,
                  channel_names=channel_names, image_type=image_type, copy=copy)
        # Attached separately for a non-flat image, exactly as the import route
        # does: the conversion is a background job and the project is valid
        # while it runs.
        if mask and image_path.suffix.lower() not in _FLAT_IMAGE_SUFFIXES \
                and not source:
            attach_segmentation(name, str(mask))
        segmentation = None  # applied

    answers = _answers(locals())
    if answers or segmentation:
        configure_project(name, segmentation=segmentation, **answers)

    if dataset is not None:
        _assign_to(name, dataset)
    return name


def configure_project(name, **answers) -> dict:
    """Answer questions about a project that is already registered.

        plexora.configure_project("s1", data="cells.csv", cell_id="CellID")

    Takes the same keys as `create_project` minus the ones that describe the
    image itself, and records every one of them as an answer. Returns the
    project's manifest, so the caller can see what is still open.
    """
    from plexora.server.models.project import Project
    from plexora.server.routes.import_routes import (
        attach_segmentation,
        replace_project_data,
    )

    project = Project.find(name)
    if project is None:
        raise KeyError(f"no project named {name!r}")

    unknown = [key for key in answers
               if key not in PROJECT_SPEC_KEYS or key in ("image", "name", "copy",
                                                          "channel_names", "exist_ok")]
    if unknown:
        raise ValueError(f"unknown project option(s): {', '.join(sorted(unknown))}")

    if answers.get("segmentation"):
        attach_segmentation(name, str(_existing_path(answers["segmentation"],
                                                     "segmentation")))
    if answers.get("data"):
        replace_project_data(name, str(_existing_path(answers["data"], "data")),
                             _data_payload(answers))
    elif answers.get("table") or answers.get("subset"):
        # Answering the table question for a source already recorded. The path
        # is the one the project has -- the user gave it at registration and
        # asking for it again would be asking something they answered.
        current = Project.find(name)
        if current.dataset is None:
            raise ValueError(f"{name!r} has no data file for `table` to name a "
                             "table inside -- pass `data` as well.")
        replace_project_data(name, current.dataset.src, _data_payload(answers))

    if answers.get("dataset") is not None:
        _assign_to(name, answers["dataset"])

    column_answers = _column_payload(answers)
    if column_answers:
        _apply_columns(name, column_answers)
    return project_manifest(name)


# -- the parts ---------------------------------------------------------------


def _register(name, image_path, mask, source, *, table, subset, channel_names,
              image_type, copy):
    """Route one project to the registration function its data type needs."""
    from plexora.datasource import (
        register_anndata_datasource,
        register_datasource,
        register_image_datasource,
        register_rgb_datasource,
    )
    from plexora.server.models.adapters import detect_data_type, is_flat_table
    from plexora.server.routes.import_routes import _FLAT_IMAGE_SUFFIXES

    flat = image_path.suffix.lower() in _FLAT_IMAGE_SUFFIXES
    if flat:
        # A flat picture has no tile pyramid and no label layer, so a mask or a
        # table recorded against one would be a project claiming something
        # nothing can show. Said rather than silently dropped.
        if mask or source:
            raise ValueError(
                f"{image_path.name} is a flat picture, which Plexora opens for "
                "viewing only -- it has no layer to draw a segmentation mask "
                "into and no coordinate space to place cells in. Use a tiled "
                "format (OME-TIFF, OME-Zarr, or a whole-slide file).")
        return register_rgb_datasource(name=name, image=image_path, copy=copy)

    if source is None:
        return register_image_datasource(
            name=name, image=image_path, channel_names=channel_names,
            copy=copy, image_type=image_type)

    data_type = detect_data_type(source)
    if is_flat_table(data_type):
        return register_datasource(
            name=name, image=image_path, features=source, segmentation=mask,
            segmentation_async=bool(mask), channel_names=channel_names,
            copy=copy, image_type=image_type)
    # AnnData or SpatialData. A `table`/`subset` this call cannot answer is not
    # an error -- the source is recorded unresolved and asked about later. See
    # datasource.deferred_spec.
    column, value = _subset_pair(subset)
    return register_anndata_datasource(
        name=name, image=image_path, features=source, table=table,
        segmentation=mask, segmentation_async=bool(mask),
        subset_by=column, subset_value=value, channel_names=channel_names,
        copy=copy, image_type=image_type)


def _answers(scope) -> dict:
    """The answer keys a `create_project` call actually supplied.

    Read off the frame rather than listed again, so a new argument on
    `create_project` is one edit rather than two that can disagree.
    """
    keys = ("data", "table", "subset", "cell_id", "x", "y", "sample", "celltype",
            "markers", "metadata", "coordinates", "layer", "log1p",
            "single_image", "row_number_ids")
    # `data`, `table` and `subset` were consumed by the registration above --
    # re-applying them would re-read the file that was just read.
    given = {key: scope[key] for key in keys if scope.get(key) is not None}
    for key in ("data", "table", "subset"):
        given.pop(key, None)
    return given


def _data_payload(answers) -> dict:
    """What `replace_project_data` reads out of a spec."""
    column, value = _subset_pair(answers.get("subset"))
    payload = {"table": answers.get("table")}
    if column:
        payload["subset_column"] = column
        payload["subset_value"] = value or ""
    if answers.get("layer"):
        payload["features_layer"] = f"layer:{answers['layer']}"
    return payload


def _subset_pair(subset):
    """`{"column": …, "value": …}`, `("col", "val")` or `"col=val"`."""
    if subset is None:
        return (None, None)
    if isinstance(subset, Mapping):
        return (subset.get("column"), subset.get("value"))
    if isinstance(subset, str):
        column, _, value = subset.partition("=")
        return (column.strip() or None, value.strip() or None)
    column, value = subset
    return (column, value)


def _column_payload(answers) -> dict:
    """The column answers, in the shape `tool_routes._apply` already takes.

    Reusing that payload rather than writing to the record directly is what
    keeps this in step with the modal and the edit page: all three mean the
    same thing by `roles`, `coordinates`, `single_image` and `row_number_ids`,
    because all three go through `apply_column_answers`.
    """
    roles = {}
    for key, role in (("cell_id", "cell_id"), ("x", "x"), ("y", "y"),
                      ("sample", "image_id"), ("celltype", "celltype")):
        if answers.get(key):
            roles[role] = answers[key]

    payload = {}
    if roles:
        payload["roles"] = roles
    if answers.get("coordinates"):
        payload["coordinates"] = dict(answers["coordinates"])
    if answers.get("single_image"):
        payload["single_image"] = True
    if answers.get("row_number_ids"):
        payload["row_number_ids"] = True
    if answers.get("markers") is not None or answers.get("metadata") is not None:
        payload["columns"] = {"markers": list(answers.get("markers") or ()),
                              "metadata": list(answers.get("metadata") or ())}
    if answers.get("layer"):
        payload["features_layer"] = f"layer:{answers['layer']}"
    if answers.get("log1p") is not None:
        payload["features_log"] = bool(answers["log1p"])
    return payload


def _apply_columns(name, payload):
    """Record column answers, refusing any the adapter cannot read the file by.

    The `_reload_or_restore` discipline the requirements route already applies:
    an answer is only accepted once the file can actually be read with it, or a
    caller ends up with a project that no longer opens at all -- a far worse
    outcome than the question they were trying to answer.
    """
    from plexora.server.models.project import Project
    from plexora.server.routes.project_routes import apply_feature_choice
    from plexora.server.routes.tool_routes import _apply

    previous = Project.find(name)
    if payload.get("features_layer") or "features_log" in payload:
        apply_feature_choice(previous, payload)
    Project.mutate(name, lambda p: _apply(p, payload))

    current = Project.find(name)
    # Only the structural formats: `plan()` is where AnnData and SpatialData
    # resolve a read spec and say whether it can be read, and a CSV has no such
    # step -- its adapter reads the header and there is nothing to refuse.
    if current is None or not current.has_table or not current.columns_are_structural:
        return
    from plexora.server.models.adapters import get_adapter

    try:
        get_adapter(current.dataset.type)(current.dataset).plan()
    except ValueError as exc:
        previous.save()
        raise ValueError(str(exc)) from None


def _assign_to(name, dataset_ref):
    """Put a project in a dataset named by id, by name, or by handle."""
    from plexora.server.models import datasets as registry

    if isinstance(dataset_ref, Dataset):
        registry.assign([name], dataset_ref.id)
        return
    try:
        record = registry.resolve(dataset_ref)
    except registry.DatasetError:
        # A name nobody has used yet is a dataset to make, which is what
        # `create_project(..., dataset="Cohort")` plainly means.
        record = registry.create(str(dataset_ref))
    registry.assign([name], record.id)


# -- validation --------------------------------------------------------------


def _as_spec(entry) -> dict:
    """A spec dict from a path, a Path, or a dict."""
    if isinstance(entry, (str, Path)):
        return {"image": str(entry)}
    if not isinstance(entry, Mapping):
        raise ValueError(f"a project is a path or a dict of options, not {entry!r}")
    spec = dict(entry)
    if not spec.get("image"):
        raise ValueError("every project needs an `image`")
    unknown = [key for key in spec if key not in PROJECT_SPEC_KEYS]
    if unknown:
        raise ValueError(
            f"unknown project option(s): {', '.join(sorted(unknown))} "
            f"(known: {', '.join(PROJECT_SPEC_KEYS)})")
    return spec


def _validate_batch(specs):
    """Everything checkable before a single pyramid is built.

    A cohort that fails on the last slide because of a typo in its filename
    should fail before the first one has spent four minutes converting.
    """
    from plexora import get_config

    taken = set(get_config())
    for spec in specs:
        _existing_path(spec["image"], "image")
        for key in ("segmentation", "data"):
            if spec.get(key):
                _existing_path(spec[key], key)
        name = spec.get("name")
        if not name or spec.get("exist_ok"):
            continue
        if name in taken:
            raise ValueError(f"there is already a project called {name!r}")
        taken.add(str(name))


def _existing_path(value, what) -> Path:
    """A local path that is really there.

    `node://` is refused with the name of the thing that does handle it rather
    than a FileNotFoundError about a path that was never meant to be one.
    """
    text = str(value)
    if text.startswith("node://"):
        raise ValueError(
            f"{what}: a file on a data node is attached with plexora.attach_"
            f"{'table' if what == 'data' else what}(), not registered here.")
    path = Path(text).expanduser()
    if not path.exists():
        raise ValueError(f"no such {what}: {path}")
    return path
