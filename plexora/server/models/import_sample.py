"""Turning a proposal into a registered sample.

`import_proposal` says what these files are; this writes them down. The split is
deliberate: "what would this be" is answerable without a data root, which is
what makes detection testable and what lets the import screen show a proposal
that is then thrown away for nothing.

**Everything here composes functions that already existed.** The reference image
goes through `register_image_datasource`, the mask through
`attach_segmentation`, the table through `replace_project_data`, a node-backed
resource through `nodes.attach_*` -- the same four the edit page and the
requirements modal call. That is the whole answer to "does this introduce a
second import workflow": there is one set of per-resource writers, and this is a
caller of them rather than a rival to them. A mask attached here and a mask
attached from the Cells control leave an identical record and start an identical
job, which `tests/test_import_entry_points.py` asserts rather than assumes.

Order matters and is fixed:

1. the dataset is validated BEFORE anything is written, because a project is
   filed after it exists and a dataset that vanished in between would leave the
   user with a registered sample in the wrong place and an error page;
2. the reference frame, because every other piece is expressed against it;
3. the mask, then the table, then the registered layers;
4. the builds;
5. the filing, which never fails.

A failure anywhere in 2-4 deletes the half-written project, unless this was a
re-import of one that already existed -- in which case the old one is still
better than nothing.
"""

from __future__ import annotations

from dataclasses import replace as _replace
from pathlib import Path

from plexora.server.models import import_proposal, layer_jobs
from plexora.server.models.project import LayerSpec, Project

#: What a flat picture is converted to when it has to be the reference of a
#: multi-layer sample. See `_tiled_picture`.
TILED_PICTURE_SUFFIX = "_tiled.ome.tif"


class ImportError_(Exception):
    """An import that cannot proceed, with something worth showing the user."""


class NameTaken(ImportError_):
    """The name is already registered and the caller did not ask to replace it."""

    def __init__(self, name, suggestion):
        super().__init__(f"A sample called {name!r} already exists.")
        self.name = name
        self.suggestion = suggestion


def _channel_src(project_name, layer_id, channel_name):
    """Where a registered layer's tiles come from.

    `/generated/layer/<sample>/<layer>/<channel>/`, ending in a slash because
    the client's tile source appends `level/x_y.png` to it -- the identical
    shape `imageData[].src` has for the reference image, so one tile source
    serves both.
    """
    return f"/generated/layer/{project_name}/{layer_id}/{channel_name}/"


def _tiled_picture(name, source):
    """A flat PNG/JPEG rewritten as a tiled RGB OME-TIFF in the project.

    Only for a picture that has to be the REFERENCE of a sample with other
    layers -- a Visium hires image with spots over it, a photograph with a mask
    beside it. `image_kind == "rgb"` boots `RgbImageViewer`, which has no layer
    stack, no plugin surface and no way to draw anything on top; a sample
    registered that way could not show its own spots. So the picture is
    converted once, at import, into the format the ordinary viewer reads.

    A lone picture is NOT converted: quick-view of a screenshot is a real use
    and it should stay instant.
    """
    import numpy as np
    import tifffile
    from PIL import Image

    from plexora import paths

    source = Path(source)
    target = paths.derived_root(name) / (source.stem + TILED_PICTURE_SUFFIX)
    target.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as handle:
        array = np.asarray(handle.convert("RGB"))

    tilesize = 512
    levels = _halvings(array.shape[1], array.shape[0], tilesize)
    with tifffile.TiffWriter(target) as writer:
        # `photometric="rgb"` is what `brightfield.is_rgb_layout` reads to know
        # these three samples are one colour picture rather than a 3-plex
        # panel, which is the difference between an H&E and three grey
        # channels the user has to colour by hand.
        writer.write(array, photometric="rgb", tile=(tilesize, tilesize),
                     compression="zlib", subifds=levels or None)
        # The levels the viewer zooms out through. Derived here rather than
        # later because the source has none at all and a picture this size --
        # a Visium hires image is about 2,000 px -- costs milliseconds to
        # halve. `subfiletype=1` is what marks them as reduced-resolution
        # pages of the one above rather than separate images.
        current = array
        for _ in range(levels):
            current = np.ascontiguousarray(current[::2, ::2])
            writer.write(current, photometric="rgb", tile=(tilesize, tilesize),
                         compression="zlib", subfiletype=1)
    return target


def _halvings(width, height, target):
    levels, longest = 0, max(int(width), int(height))
    while longest > target:
        longest = -(-longest // 2)
        levels += 1
    return levels


def _register_reference(name, reference, frame, layers):
    """Write the sample's coordinate system. Returns the project entry."""
    from plexora.datasource import (register_blank_datasource,
                                    register_image_datasource)

    if reference is None:
        frame = dict(frame or {})
        return register_blank_datasource(
            name, width=frame.get("width") or 1024,
            height=frame.get("height") or 1024,
            pixel_size=frame.get("pixel_size"),
            modality="blank")

    if reference.binding:
        from plexora import nodes as node_api
        from plexora.server.models.project import ImageSpec

        # An empty record first, then the node fills it in. `attach_image`
        # points an EXISTING project's image at a node -- the geometry, the
        # channel names and the pyramid depth all come back from the machine
        # that can open the file -- so there has to be a project for it to
        # point. The same two steps `_register_node_image` already makes.
        Project(name=name, image=ImageSpec()).save()
        node_api.attach_image(name, node=reference.binding["node"],
                              resource_id=reference.binding["resource_id"])
        return Project.load(name).to_entry()

    source = Path(reference.src)
    flat = source.suffix.lower() in (".png", ".jpg", ".jpeg")
    if flat and len(layers) > 1:
        source = _tiled_picture(name, source)
        return register_image_datasource(name=name, image=source,
                                         image_type="brightfield")
    if flat:
        from plexora.datasource import register_rgb_datasource

        return register_rgb_datasource(name=name, image=source)
    image_type = "brightfield" if (reference.render or {}).get("rgb") else None
    return register_image_datasource(name=name, image=source,
                                     image_type=image_type)


def _layer_spec(project_name, proposal, unresolved):
    """One `LayerProposal` as the `LayerSpec` that gets stored.

    The channel addresses are filled in HERE and not by detection, because they
    name the sample the layer ended up in -- which detection does not know and
    must not have to.
    """
    geometry = proposal.geometry or {}
    channels = tuple(
        {**dict(channel),
         "src": _channel_src(project_name, proposal.id, channel["name"])}
        for channel in (proposal.channels or ()))
    return LayerSpec(
        id=proposal.id,
        kind=proposal.kind,
        label=proposal.label or proposal.id,
        src=proposal.src,
        modality=proposal.modality,
        channels=channels,
        width=geometry.get("width"),
        height=geometry.get("height"),
        max_level=geometry.get("maxLevel"),
        tile_width=geometry.get("tileWidth"),
        tile_height=geometry.get("tileHeight"),
        transform=proposal.transform,
        transform_source=proposal.transform_source,
        pixel_size=({"value": proposal.pixel_size, "unit": "µm",
                     "source": "metadata"} if proposal.pixel_size else None),
        render=dict(proposal.render or {}),
        source=dict(proposal.bundle) if proposal.bundle else None,
        # A layer whose drawable form has to be built is `pending` from the
        # moment it is written, so the viewer never asks for tiles that do not
        # exist yet and the card says what it is waiting for.
        status="pending" if _needs_build(proposal) else "ready",
        unresolved=tuple(unresolved),
    )


def _needs_build(proposal) -> bool:
    """Whether this layer has to be prepared before anything can be drawn.

    A points layer does: what is drawn is a tile cache derived from a vendor
    file, and the file itself is not servable. An image layer does not -- its
    pixels are already in a format the tile route reads.
    """
    return proposal.kind == "points"


def register_sample(proposal, *, name=None, dataset=None, answers=None,
                    replace=None, data_dir=None):
    """Write one `SampleProposal` down as a project.

    @param name - overrides the proposed name. A collision raises `NameTaken`
        with a free suggestion rather than silently renaming: somebody who
        typed a name meant it, and quietly filing their import under
        `melanoma_2` is how two copies of one slide happen.
    @param replace - the name of an existing sample this is a re-import of.
        Its layers are replaced in place (`with_layer` keeps a layer's
        position), so correcting a pixel size does not restack somebody's
        scene.
    @returns `{"name", "layers": [{"id", "status"}], "pending", "reload"}`
    """
    from plexora import get_config_names
    from plexora.datasource import _dedupe_dataset_name
    from plexora.server.routes.import_routes import (_dataset_request,
                                                     _file_under,
                                                     attach_segmentation,
                                                     replace_project_data)

    answers = dict(answers or {})
    # FIRST, before a byte is written. Filing happens after the project exists,
    # so a dataset that has since been deleted would otherwise be reported
    # against a sample that is already registered -- the user ends up with the
    # thing they asked for in a place they did not ask for and an error page.
    #
    # `_dataset_request` is the import form's own validator, reused verbatim:
    # it resolves a new name that turns out to be an existing folder, and it
    # refuses a deleted id. Its input is a form mapping, so a caller's
    # `{"id": ...}` / `{"new": ...}` (or a bare id string) is translated here
    # rather than the validator growing a second calling convention.
    dataset_id, new_dataset = _dataset_request(_dataset_form(dataset))

    existing = set(get_config_names())
    wanted = (name or proposal.name or "sample").strip()
    if wanted in existing and replace != wanted:
        raise NameTaken(wanted, _dedupe_dataset_name(wanted, existing))
    final = wanted if replace == wanted else _dedupe_dataset_name(wanted, existing)

    layers = list(proposal.layers)
    reference = next((l for l in layers if l.reference), None)
    mask = next((l for l in layers if l.role == "mask"), None)
    table = next((l for l in layers if l.role == "table"), None)
    registered = [l for l in layers if l.role == "layer"]

    created = replace != final
    try:
        _register_reference(final, reference, proposal.frame, layers)

        if mask is not None:
            attach_segmentation(final, mask.src)

        if table is not None:
            replace_project_data(final, table.src, {
                "table": table.table,
                "subset_column": answers.get("subset_column"),
                "subset_value": answers.get("subset_value"),
            })

        def _apply(project):
            if reference is not None and reference.modality:
                project = project.patch(
                    image=_replace(project.image, modality=reference.modality))
            for bundle in proposal.bundles:
                project = project.with_bundle(bundle)
            for layer in registered:
                project = project.with_layer(
                    _layer_spec(final, layer, _outstanding(layer, answers)))
            return project

        Project.mutate(final, _apply)
    except Exception:
        # A half-written sample is worse than none: it appears in the library,
        # opens onto an error and gives the user nothing to act on. A
        # RE-import is different -- what was there before this attempt is still
        # a working sample, and deleting it would turn a failed correction into
        # data loss.
        if created:
            found = Project.find(final)
            if found is not None:
                found.delete()
        raise

    project = Project.load(final)
    started = []
    for layer in project.spatial_layers:
        if layer.pending:
            layer_jobs.start_builder(project, layer)
        started.append({"id": layer.id, "status": layer.status})

    # Never raises: every caller is past the point of no return, so a dataset
    # that vanished costs the filing and not the import.
    _file_under(final, dataset_id, new_dataset)

    return {
        "name": final,
        "layers": started,
        "pending": any(entry["status"] == "pending" for entry in started),
    }


def _dataset_form(dataset):
    """A caller's dataset argument as the form mapping `_dataset_request` takes."""
    if not dataset:
        return {}
    if isinstance(dataset, str):
        return {"dataset": dataset}
    return {"dataset": str(dataset.get("id") or ""),
            "dataset_new": str(dataset.get("new") or dataset.get("name") or "")}


def _outstanding(layer, answers):
    """Which of this layer's questions the user did not answer.

    Recorded on the layer rather than blocking the import -- the same move
    `DataSpec.unresolved` makes. Whatever needs the answer asks for it later,
    with the file already registered and the sample already open.
    """
    return tuple(key for key in (layer.needs or ()) if not answers.get(key))


def register_layers(project_name, proposal, *, answers=None):
    """Add a proposal's layers to a sample that already exists.

    "+ Add Layer", the requirements modal's layer row, and the edit page's
    Layers section all end here. It goes through the SAME per-resource writers
    as a fresh import -- a mask added here is `attach_segmentation`, a table is
    `replace_project_data` -- which is what makes "add a mask from the Layers
    panel" and "add a mask from the Cells control" one code path rather than
    two that drift.

    @returns `{"layers", "reload"}`. `reload` is true only when a MASK was
        attached: that inserts the "Area" placeholder into `imageData`, and
        every channel index on the open page is wired into the GL pass, so the
        page has to be rebuilt. Everything else is adopted in place.
    """
    from plexora.server.routes.import_routes import (attach_segmentation,
                                                     replace_project_data)

    answers = dict(answers or {})
    mask = next((l for l in proposal.layers if l.role == "mask"), None)
    table = next((l for l in proposal.layers if l.role == "table"), None)
    registered = [l for l in proposal.layers if l.role == "layer"]

    if mask is not None:
        attach_segmentation(project_name, mask.src)
    if table is not None:
        replace_project_data(project_name, table.src, {
            "table": table.table,
            "subset_column": answers.get("subset_column"),
            "subset_value": answers.get("subset_value"),
        })

    def _apply(project):
        for bundle in proposal.bundles:
            project = project.with_bundle(bundle)
        for layer in registered:
            project = project.with_layer(
                _layer_spec(project_name, layer, _outstanding(layer, answers)))
        return project

    Project.mutate(project_name, _apply)

    project = Project.load(project_name)
    added = {layer.id for layer in registered}
    started = []
    for layer in project.spatial_layers:
        if layer.id not in added:
            continue
        if layer.pending:
            layer_jobs.start_builder(project, layer)
        started.append({"id": layer.id, "status": layer.status})

    return {"layers": started, "reload": mask is not None,
            "pending": any(entry["status"] == "pending" for entry in started)}


def import_sample(paths, *, answers=None, name=None, dataset=None, node=None,
                  replace=None, index=0):
    """Inspect these paths and register the sample they make.

    The one function every entry point reaches -- the modal's route, the Python
    API, the CLI. Inspection is re-run here rather than trusting a proposal
    handed back by a client, so the record is always written from what the
    files actually say.
    """
    proposal = import_proposal.inspect_paths(paths, node=node, answers=answers)
    if not proposal.samples:
        reasons = [entry["reason"] for entry in proposal.unrecognised]
        raise ImportError_(
            reasons[0] if reasons else "Nothing Plexora can read here.")
    sample = proposal.samples[min(index, len(proposal.samples) - 1)]
    return register_sample(sample, name=name, dataset=dataset,
                           answers=answers, replace=replace)


def add_layers(project_name, paths, *, answers=None, node=None):
    """Inspect these paths and add them to an existing sample."""
    proposal = import_proposal.inspect_paths(
        paths, node=node, answers=answers, sample=project_name)
    if not proposal.samples:
        reasons = [entry["reason"] for entry in proposal.unrecognised]
        raise ImportError_(
            reasons[0] if reasons else "Nothing Plexora can read here.")
    return register_layers(project_name, proposal.samples[0], answers=answers)
