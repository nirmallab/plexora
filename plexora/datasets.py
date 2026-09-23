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
from pathlib import Path, PureWindowsPath
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
    # Which machine the files are on, when it is not this one.
    "node",
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
                   node=None, exist_ok=False) -> Dataset:
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

    `node` puts the whole batch on a data node -- every path that does not say
    otherwise is a path on THAT machine, and the projects address it rather
    than copying anything here. An entry may name its own `node`, and a single
    field may opt out with `{"path": …, "node": None}`, so the commonest split
    of all is sayable: the slides on the cluster, the quantification on the
    laptop.

    An entry naming a project that already exists is adopted rather than
    re-registered, so a call can be re-run after fixing one bad path without
    re-converting everything before it.

    **Everything is validated before anything is registered**: the dataset
    name, every spec key, every path. A cohort that fails on the last slide
    because of a typo in its filename should fail before the first one has
    spent four minutes building a pyramid. That promise holds across machines
    too: a file on a node is checked by ASKING the node about it, which leaves
    the node's own registry untouched.

    On a failure PART WAY THROUGH conversion, what succeeded is kept, assigned,
    and named in the `DatasetCreateError` -- see that class for why there is no
    rollback.
    """
    from plexora.server.models import datasets as registry

    if images is not None and projects is not None:
        raise ValueError("Pass `images` or `projects`, not both -- "
                         "`images` is the short form of `projects`.")
    specs = [_as_spec(entry, node) for entry in (projects if projects is not None
                                                 else images or [])]

    existing = registry.find_by_name(name)
    if existing is not None and not exist_ok:
        raise ValueError(f"there is already a dataset called {existing.name!r} "
                         "(pass exist_ok=True to add to it)")

    # Everything that can be checked without touching a file, first. What the
    # nodes said is carried into the loop rather than asked again: detection
    # reads pixels, and over a mount that is the expensive part of a cohort.
    checked = _validate_batch(specs)

    record = existing or registry.create(name, description=description)
    created = []
    for spec in specs:
        try:
            created.append(create_project(**spec, dataset=record.id,
                                          _checked=checked))
        except Exception as exc:
            raise DatasetCreateError(
                f"{_describe(_locate(spec['image'], 'image', spec.get('node')))}: {exc}",
                dataset=Dataset._from_record(registry.get(record.id)),
                created=created, failed=spec, cause=exc) from exc
    return Dataset._from_record(registry.get(record.id))


def project_from_spec(spec, node=None) -> str:
    """Register one project from a spec dict. `create_project(**spec)`.

    Exists so the CLI's `--from file.json` and `create_dataset(projects=[…])`
    read the same document through the same code, rather than each growing its
    own idea of what a spec is.
    """
    return create_project(**_as_spec(spec, node))


def import_sample(*paths, name=None, dataset=None, answers=None, node=None,
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
    @param node - the data node these paths are on. Detection then runs over
        there, on that machine's own disk, and the sample it registers
        addresses the files rather than reading them from here.
    @param wait - block until every derived artefact (transcript tiles, the
        mask pyramid) has been built. False returns as soon as the record
        exists, which is what the viewer wants; True is for a script whose next
        line reads the result.
    """
    from plexora.server.models import import_sample as importer

    result = importer.import_sample(
        [str(p) for p in paths], answers=answers, name=name, dataset=dataset,
        node=node, replace=replace)
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
                   dataset=None, node=None, _checked=None) -> str:
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

    `node` is which machine the files are on when it is not this one, and each
    of `image`, `segmentation` and `data` may answer that for itself -- as a
    `node://<node>/<path>` string, or as `{"path": …, "node": …}` where
    `"node": None` keeps that one file here. A file on a node is ADDRESSED:
    the project records the node and the resource, and the bytes never move.
    """
    from plexora import get_config
    from plexora.datasource import (
        _dedupe_dataset_name,
        _derive_dataset_name_from_path,
        _find_existing_datasource_for_image,
    )
    from plexora.server.routes.import_routes import _FLAT_IMAGE_SUFFIXES

    checked = {} if _checked is None else _checked
    image_at = _locate(image, "image", node)
    mask_at = _locate(segmentation, "segmentation", node) if segmentation else None
    data_at = _locate(data, "data", node) if data else None
    for at in (image_at, mask_at, data_at):
        if at is not None:
            _check_located(at, checked)
    if copy and not image_at.local:
        raise ValueError("copy= has nothing to copy for an image on a node -- "
                         "a node serves the file where it lies, which is the "
                         "whole reason for addressing it.")

    config = get_config()
    image_path = _existing_path(image_at.path, "image") if image_at.local else None

    # An image already registered is the same project, not a second copy of
    # it. The rule quick view uses, and what makes re-running a batch cheap.
    # A node-backed image has no path here to compare, so the comparison is
    # the binding -- the same one `import_proposal._find_existing` makes.
    if image_at.local:
        if name is None and exist_ok:
            name = _find_existing_datasource_for_image(image_path, config)
        base = _derive_dataset_name_from_path(image_path)
    else:
        if name is None and exist_ok:
            name = _find_project_on_node(image_at.node, _resource_id(image_at),
                                         config)
        base = _derive_dataset_name_from_path(_basename(image_at.path))
    if name is None:
        name = _dedupe_dataset_name(base, config.keys())
    name = str(name)

    # Roles the registration below does not itself apply, handed to
    # `configure_project` afterwards. Empty for the all-local case, which is
    # what keeps that path exactly what it has always been.
    extra = {}
    ours_alone = False
    if name in config:
        if not exist_ok:
            raise ValueError(f"there is already a project called {name!r} "
                             "(pass exist_ok=True to configure it instead)")
        # Only the mask, as adoption has always applied: re-attaching the
        # table would re-read a file this project already read, which is the
        # cost re-running a batch with `exist_ok` exists to avoid.
        if mask_at is not None:
            extra["segmentation"] = _serve(mask_at)
    elif image_at.local:
        if image_path.suffix.lower() in _FLAT_IMAGE_SUFFIXES \
                and (mask_at is not None or data_at is not None):
            # `_register` makes this refusal for a local mask or table. A file
            # on a node is no different -- there is still no layer to draw it
            # into -- and those never reach `_register`.
            _refuse_flat(image_path)
        mask = Path(_serve(mask_at)) if mask_at is not None and mask_at.local else None
        source = Path(_serve(data_at)) if data_at is not None and data_at.local else None
        _register(name, image_path, mask, source, table=table, subset=subset,
                  channel_names=channel_names, image_type=image_type, copy=copy)
        # Attached separately for a non-flat image, exactly as the import route
        # does: the conversion is a background job and the project is valid
        # while it runs.
        if mask and image_path.suffix.lower() not in _FLAT_IMAGE_SUFFIXES \
                and not source:
            from plexora.server.routes.import_routes import attach_segmentation

            attach_segmentation(name, str(mask))
        extra = _roles_to_apply(mask_at, data_at, table, subset, remote_only=True)
    else:
        _register_on_node(name, image_at, channel_names=channel_names,
                          image_type=image_type)
        ours_alone = True
        extra = _roles_to_apply(mask_at, data_at, table, subset)

    answers = {**_answers(locals()), **extra}
    try:
        if answers:
            configure_project(name, **answers)
    except Exception:
        # Only a project this call CREATED is this call's to remove. One that
        # was adopted, or one whose image registered locally, is a working
        # project that a failed mask would otherwise turn into data loss --
        # the distinction `register_sample` draws with `created`.
        if ours_alone:
            from plexora.server.models.project import Project

            found = Project.find(name)
            if found is not None:
                found.delete()
        raise

    if dataset is not None:
        _assign_to(name, dataset)
    return name


def configure_project(name, **answers) -> dict:
    """Answer questions about a project that is already registered.

        plexora.configure_project("s1", data="cells.csv", cell_id="CellID")

    Takes the same keys as `create_project` minus the ones that describe the
    image itself, and records every one of them as an answer. Returns the
    project's manifest, so the caller can see what is still open.

    `node` says which machine `segmentation` and `data` are on, unless one of
    them says otherwise for itself -- the same three spellings `create_project`
    takes, so moving a mask onto a node is a spec edit rather than a different
    call.
    """
    from plexora.server.models.project import Project
    from plexora.server.routes.import_routes import (
        attach_segmentation,
        replace_project_data,
    )

    project = Project.find(name)
    if project is None:
        raise KeyError(f"no project named {name!r}")

    # Popped before the check below, because it is not an answer about the
    # project -- it is where the answers' files are.
    default_node = answers.pop("node", None)
    unknown = [key for key in answers
               if key not in PROJECT_SPEC_KEYS or key in ("image", "name", "copy",
                                                          "channel_names", "exist_ok")]
    if unknown:
        raise ValueError(f"unknown project option(s): {', '.join(sorted(unknown))}")

    checked = {}
    if answers.get("segmentation"):
        at = _locate(answers["segmentation"], "segmentation", default_node)
        _check_located(at, checked)
        attach_segmentation(name, _serve(at))
    if answers.get("data"):
        at = _locate(answers["data"], "data", default_node)
        _check_located(at, checked)
        replace_project_data(name, _serve(at), _data_payload(answers))
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
        if mask or source:
            _refuse_flat(image_path)
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


def _refuse_flat(image_path):
    """A flat picture has no layer to draw a mask into, and says so.

    Said in one place because it is reached two ways: `_register` sees the
    local files, and `create_project` sees the ones on a node, which never get
    that far.
    """
    raise ValueError(
        f"{image_path.name} is a flat picture, which Plexora opens for "
        "viewing only -- it has no layer to draw a segmentation mask "
        "into and no coordinate space to place cells in. Use a tiled "
        "format (OME-TIFF, OME-Zarr, or a whole-slide file).")


def _register_on_node(name, at, *, channel_names, image_type):
    """Make a project whose image is a resource on another machine.

    The share comes FIRST, before the record exists. It is the step that can
    still refuse -- a node already serving something else under this id -- and
    a refusal at that point leaves nothing half-made behind it.

    The record starts as an empty `ImageSpec`, exactly as
    `import_sample._register_reference` starts one, because the geometry is
    something only the node can answer and `attach_image` is what asks.
    """
    from plexora import nodes as node_api
    from plexora.server.models.project import ImageSpec, Project

    resource_id = _resource_id(at)
    _serve(at)
    Project(name=name, image=ImageSpec()).save()
    try:
        node_api.attach_image(name, node=at.node, resource_id=resource_id,
                              channel_names=channel_names, image_type=image_type)
    except Exception:
        # A half-registered project is worse than none: it appears in the
        # picker, opens onto an error, and the name is taken so the call
        # cannot simply be run again.
        found = Project.find(name)
        if found is not None:
            found.delete()
        raise


def _roles_to_apply(mask_at, data_at, table, subset, *, remote_only=False) -> dict:
    """The role values `configure_project` still has to apply.

    `remote_only` is the local-image case: registration has already read the
    mask and the table that were on this machine, and handing them back would
    re-read the files it just read.
    """
    extra = {}
    if mask_at is not None and not (remote_only and mask_at.local):
        extra["segmentation"] = _serve(mask_at)
    if data_at is not None and not (remote_only and data_at.local):
        extra["data"] = _serve(data_at)
        # `_answers` drops `table` and `subset` because the local registration
        # path consumes them. This table did not go that way, so
        # `replace_project_data` still needs to be told which part to read.
        if table is not None:
            extra["table"] = table
        if subset is not None:
            extra["subset"] = subset
    return extra


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


def _complete_columns(project, payload):
    """One side of the marker/metadata split implies the other.

    `metadata=[...]` on its own used to record an EMPTY marker list, which
    reads as "nobody has classified these columns" (`ColumnGroups.classified`)
    and puts the question back on screen -- the opposite of what naming the
    morphology columns was for. Registration has already written down every
    column the table has, so the side left unsaid is that list minus the side
    given.

    Naming BOTH is left exactly as it was: a caller who listed both has said
    that a column in neither belongs in neither.
    """
    columns = payload.get("columns")
    if not columns:
        return payload
    markers = list(columns.get("markers") or ())
    metadata = list(columns.get("metadata") or ())
    if bool(markers) == bool(metadata):
        return payload
    # The table's own vocabulary, as registration recorded it. Empty means
    # there is nothing to take the complement of -- a project with no table, or
    # one whose columns were never classified -- and a one-sided answer then
    # stands as given rather than being completed out of nothing.
    known = list(project.columns.all) if project is not None else []
    if not known:
        return payload
    named = set(markers) | set(metadata)
    rest = [column for column in known if column not in named]
    if markers:
        metadata = rest
    else:
        markers = rest
    return {**payload, "columns": {"markers": markers, "metadata": metadata}}


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
    payload = _complete_columns(previous, payload)
    if payload.get("features_layer") or "features_log" in payload:
        apply_feature_choice(previous, payload)
    Project.mutate(name, lambda p: _apply(p, payload))

    current = Project.find(name)
    # Only the structural formats: `plan()` is where AnnData and SpatialData
    # resolve a read spec and say whether it can be read, and a CSV has no such
    # step -- its adapter reads the header and there is nothing to refuse.
    if current is None or not current.has_table or not current.columns_are_structural:
        return
    binding = current.resource("table")
    if binding is not None and binding.is_node:
        # `plan()` opens the file, and this file is on another machine: the
        # adapter would hand `node://…` to h5py and raise an OSError the
        # `except ValueError` below does not catch. The node ran the same
        # resolution when the table was attached, which is what makes skipping
        # it a skip rather than a gap.
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


def _as_spec(entry, default_node=None) -> dict:
    """A spec dict from a path, a Path, or a dict."""
    if isinstance(entry, (str, Path)):
        spec = {"image": str(entry)}
    elif not isinstance(entry, Mapping):
        raise ValueError(f"a project is a path or a dict of options, not {entry!r}")
    else:
        spec = dict(entry)
        if not spec.get("image"):
            raise ValueError("every project needs an `image`")
        unknown = [key for key in spec if key not in PROJECT_SPEC_KEYS]
        if unknown:
            raise ValueError(
                f"unknown project option(s): {', '.join(sorted(unknown))} "
                f"(known: {', '.join(PROJECT_SPEC_KEYS)})")
    if default_node is not None:
        # `setdefault` and not a truth test, because an entry that wrote
        # `"node": null` has opted this whole project out of the batch default
        # and that is an answer, not an omission.
        spec.setdefault("node", default_node)
    return spec


def _validate_batch(specs) -> dict:
    """Everything checkable before a single pyramid is built.

    A cohort that fails on the last slide because of a typo in its filename
    should fail before the first one has spent four minutes converting -- and
    that holds across machines, because every remote check here is a question
    put to the node rather than a change made to it.

    Returns what the nodes said, for the registration loop to reuse: detection
    reads pixels, and asking twice about fifty slides is the difference
    between seconds and minutes.
    """
    from plexora import get_config

    checked = {}
    taken = set(get_config())
    for spec in specs:
        default_node = spec.get("node")
        _check_located(_locate(spec["image"], "image", default_node), checked)
        for key in ("segmentation", "data"):
            if spec.get(key):
                _check_located(_locate(spec[key], key, default_node), checked)
        name = spec.get("name")
        if not name or spec.get("exist_ok"):
            continue
        if name in taken:
            raise ValueError(f"there is already a project called {name!r}")
        taken.add(str(name))
    return checked


def _existing_path(value, what) -> Path:
    """A local path that is really there.

    Every value reaching here is one `_locate` has already decided is on this
    machine, so there is no node address to tell apart from a path.
    """
    path = Path(str(value)).expanduser()
    if not path.exists():
        raise ValueError(f"no such {what}: {path}")
    return path


# -- where a file is ---------------------------------------------------------


#: What a node serves each role as. The ROLE decides, not the node's own
#: reading of the file: somebody who wrote `segmentation=` has said what the
#: file is for, exactly as picking it into that field on the import form does.
_KIND_FOR_ROLE = {"image": "image", "segmentation": "segmentation", "data": "table"}


@dataclass(frozen=True)
class _Located:
    """One role's file, and which machine it is on."""

    role: str
    path: str
    node: str | None = None
    #: A resource the node already serves, rather than a path on its disk.
    #: The two are told apart by `import_proposal._looks_like_a_path`, so a
    #: locator copied out of the UI means here what it means there.
    is_id: bool = False

    @property
    def local(self) -> bool:
        return self.node is None


def _locate(value, role, default_node=None) -> _Located:
    """Where one role's file is: on this machine, or on a named data node.

    Three spellings, because three things write specs. `{"path": …, "node": …}`
    is the one to write in a file, and `"node": None` is how a single field
    opts out of a default that the entry or the batch set -- without it, "the
    slides on the cluster, the table on my laptop" is unsayable.
    `node://<node>/<resource>` is what the import form already posts, kept so a
    locator can be pasted straight across. A bare string is a path, on the
    default node if there is one.
    """
    from plexora.server.routes.import_routes import _node_locator

    if isinstance(value, Mapping):
        unknown = [key for key in value if key not in ("path", "node")]
        if unknown:
            raise ValueError(
                f"{role}: unknown key(s) {', '.join(sorted(unknown))} -- a file "
                'is written {"path": …, "node": …}')
        path = value.get("path")
        if not path:
            raise ValueError(f"{role}: a file written as an object needs a `path`")
        node = value["node"] if "node" in value else default_node
        return _Located(role=role, path=str(path),
                        node=str(node) if node else None)

    text = str(value)
    located = _node_locator(text)
    if located:
        from plexora.server.models.import_proposal import _looks_like_a_path

        return _Located(role=role, path=located[1], node=located[0],
                        is_id=not _looks_like_a_path(located[1]))
    return _Located(role=role, path=text,
                    node=str(default_node) if default_node else None)


def _describe(at) -> str:
    """One role's file, for a message: the path, and where it is."""
    return f"{at.path} on {at.node}" if at.node else at.path


def _basename(path) -> str:
    """The last component of a path written for EITHER kind of machine.

    A node is as likely to be a Windows workstation as a cluster, and this
    process is whichever the node is not -- so `Path` is the wrong tool here
    and `PureWindowsPath`, which treats both separators as separators, is the
    right one.
    """
    return PureWindowsPath(str(path)).name or str(path)


def _resource_id(at) -> str:
    """The id a node serves this file under."""
    from plexora import nodes as node_api

    return at.path if at.is_id else node_api.resource_id_for(at.path)


def _check_located(at, checked):
    """Refuse a file that is not there -- WITHOUT writing anything anywhere.

    Every remote question here is a read: `detect_on_node` asks a node what it
    makes of a path on its own disk and leaves its registry alone. That is what
    lets `create_dataset` keep its promise across machines, and it is why the
    check and the share (`_serve`) are two functions rather than one.

    `checked` carries the answers, because detection opens the image to decide
    whether it is a label field and a whole slide over a mount is not a
    question worth asking twice.
    """
    if at.local:
        _existing_path(at.path, at.role)
        return

    from plexora import nodes as node_api
    from plexora.server.models import nodes as node_registry
    # The base of every failure this seam has: unreachable, refused, too old.
    from plexora.server.providers.base import ResourceError

    try:
        node_registry.get(at.node)
    except KeyError as exc:
        # KeyError's str() carries its own quotes; a sentence read by a user
        # should not.
        sentence = str(exc).strip("'")
        raise ValueError(f"{at.role}: {sentence}") from None

    try:
        if at.is_id:
            key = ("serving", at.node, "")
            if key not in checked:
                checked[key] = {str(described.get("id"))
                                for described in node_api.node_resources(at.node)}
            if at.path not in checked[key]:
                raise ValueError(
                    f"{at.role}: {at.node!r} is not serving {at.path!r}")
            return

        key = ("detected", at.node, at.path)
        if key not in checked:
            checked[key] = node_api.detect_on_node(at.node, at.path)
        detected = checked[key]
    except ResourceError as exc:
        # The provider's sentences already name the machine and what went
        # wrong with it. Re-raised as ValueError so the CLI prints them the way
        # it prints every other refusal, rather than as a traceback.
        raise ValueError(f"{at.role} on {at.node!r}: {exc}") from None

    kind = detected.get("kind")
    if not kind:
        raise ValueError(f"{at.role} on {at.node!r}: "
                         f"{detected.get('reason') or 'nothing Plexora can read'}")
    # A table is a table and a picture is a picture, and confusing those is
    # worth refusing. Image versus segmentation is NOT: whether a label field
    # is a mask is a reading the node offers and the role is the user's
    # statement, which is the precedence the import form gives the same field.
    if (at.role == "data") != (kind == "table"):
        raise ValueError(f"{at.role} on {at.node!r}: {_basename(at.path)} is a "
                         f"{kind}, not a {at.role}")


def _serve(at) -> str:
    """The value a writer takes for this file: a local path, or a node address.

    Sharing is what turns a path on the node's disk into a resource it will
    serve, and it is deliberately NOT undone when a later step fails: an
    identical re-add is a no-op, so re-running costs nothing, while unsharing
    could pull a resource out from under another project already reading it.
    """
    if at.local:
        return str(_existing_path(at.path, at.role))
    if at.is_id:
        return f"node://{at.node}/{at.path}"

    from plexora import nodes as node_api

    return node_api.share_path(at.node, _KIND_FOR_ROLE[at.role], at.path)["locator"]


def _find_project_on_node(node, resource_id, config) -> "str | None":
    """A project already reading this exact resource from this node, or None.

    The node-backed half of `_find_existing_datasource_for_image`: a project
    whose image is on another machine records no path here -- by design, since
    that machine's layout is not this one's business -- so the binding is what
    there is to compare. `import_proposal._find_existing` makes the same
    comparison for the import screen.
    """
    from plexora.server.models.project import Project

    for name, entry in (config or {}).items():
        binding = Project.from_entry(name, entry).resource("image")
        if (binding is not None and binding.node == node
                and str(binding.resource_id) == str(resource_id)):
            return name
    return None


def pending_conversions(names) -> list:
    """Node resources these projects read that are still being prepared.

    `[{"node": …, "kind": …, "count": …}]`. A mask on a node converts in the
    background and the project is valid while it runs -- the viewer polls and
    shows the progress, but a command line that said nothing left somebody
    staring at an empty layer wondering what they had done wrong.

    A node that cannot be reached contributes nothing. This is a courtesy line
    under a registration that has already succeeded, and not the place to start
    failing.
    """
    from plexora.server.models.project import Project
    from plexora.server.providers.base import RESOURCE_KINDS

    wanted = {}
    for name in names:
        project = Project.find(name)
        if project is None:
            continue
        for kind in RESOURCE_KINDS:
            binding = project.resource(kind)
            if binding is not None and binding.node:
                wanted.setdefault(binding.node, set()).add(str(binding.resource_id))

    rows = []
    for node, ids in sorted(wanted.items()):
        from plexora import nodes as node_api

        try:
            served = node_api.node_resources(node)
        except Exception:
            continue
        counts = {}
        for described in served:
            if (str(described.get("id")) in ids
                    and str(described.get("state")) == "preparing"):
                kind = str(described.get("kind") or "resource")
                counts[kind] = counts.get(kind, 0) + 1
        rows.extend({"node": node, "kind": kind, "count": count}
                    for kind, count in sorted(counts.items()))
    return rows
