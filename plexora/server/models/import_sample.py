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
from plexora.server.utils import boundary_mask

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

    if str(reference.src or "").startswith("node://"):
        from plexora import nodes as node_api
        from plexora.server.models.project import ImageSpec
        from plexora.server.routes.import_routes import _node_locator

        node, resource_id = _node_locator(reference.src)
        # An empty record first, then the node fills it in. `attach_image`
        # points an EXISTING project's image at a node -- the geometry, the
        # channel names and the pyramid depth all come back from the machine
        # that can open the file -- so there has to be a project for it to
        # point at.
        Project(name=name, image=ImageSpec()).save()
        node_api.attach_image(name, node=node, resource_id=resource_id)
        return Project.load(name).to_entry()

    from plexora.server.providers.base import is_remote_locator

    if is_remote_locator(reference.src):
        # A web address is registered as the string it is: `Path` would fold
        # `https://` into `https:/`, and only OME-Zarr is read from the web.
        return register_image_datasource(name=name, image=str(reference.src))

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


#: What a registered greyscale image layer is drawn in until somebody says
#: otherwise. A neutral blue-grey, chosen the way a channel's first colour is:
#: legible on the dark ground a fluorescence composite is drawn on, and not one
#: of the saturated primaries a user is likely to have given a channel.
#:
#: It matters because a registered layer composites with `lighter`, the same
#: blend a fluorescence channel uses. Served grey it adds equally to all three
#: components and washes whatever is under it toward white; served in a colour
#: it reads as a second signal, which is what it is.
DEFAULT_LAYER_COLOR = "#8ea2b8"

#: Keys the proposal puts in `render` for the import screen's benefit and the
#: record has no use for.
_PROPOSAL_ONLY = ("detail", "frameScale")


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

    # `detail` is the import screen's sentence about this row, not something
    # the viewer draws. It rode along in `render` because that is the bag the
    # proposal had; storing it would put "40k molecules" in every /config on
    # every viewer boot for nothing.
    render = {key: value for key, value in (proposal.render or {}).items()
              if key not in _PROPOSAL_ONLY}
    # Only a greyscale layer. A brightfield one's tiles ARE the picture --
    # three real colour samples, drawn `source-over` -- and tinting those would
    # be colouring an H&E.
    if proposal.kind == "image" and not render.get("rgb") and "color" not in render:
        render["color"] = DEFAULT_LAYER_COLOR
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
        render=render,
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


#: Bundle formats whose tables arrive in a frame Plexora cannot read as it
#: stands. Everything in one of these runs is in MICRONS and its cell ids are
#: vendor strings; see `server/utils/xenium_cells.py` for what that breaks.
_SPATIAL_FORMATS = ("xenium", "visium_hd")


def _spatial_context(table, reference):
    """`{"pixel_size", "root"}` for a table that needs correcting, else None.

    Keyed on the BUNDLE's format rather than on the table's shape: a CSV that
    happens to have `cell_id` and `x_centroid` columns is somebody's own
    quantification in whatever frame they chose, and rescaling it by a pixel
    size would move every cell for no reason anybody could find.
    """
    bundle = dict(table.bundle or {})
    if bundle.get("format") not in _SPATIAL_FORMATS:
        return None
    if bundle.get("format") == "visium_hd":
        # The matrix is converted on the way in (import_routes._tenx_context);
        # what it needs from here is what the reference is of the run's
        # full-res frame, which is where Space Ranger states every position.
        return {"format": "visium_hd", "root": bundle.get("root"),
                "frame_scale": ((reference.render or {}).get("frameScale")
                                if reference is not None else None) or 1.0}
    return {
        "pixel_size": reference.pixel_size if reference is not None else None,
        "root": bundle.get("root"),
    }


def _preferred_mask(layers):
    """Which of the proposal's mask candidates becomes the segmentation.

    A raster mask the user brought wins over a run's own boundary polygons,
    always. Somebody who hands Plexora a mask has segmented this slide
    themselves -- with their model, their parameters, their corrections --
    and quietly drawing the vendor's outlines over it because they happened
    to import the run folder too would replace their answer with one they did
    not ask for.

    The loser is not discarded: it goes back to being an ordinary registered
    layer, so the run still records what it shipped.
    """
    candidates = [l for l in layers if l.role == "mask"]
    if not candidates:
        return None
    chosen = next(
        (l for l in candidates
         if not boundary_mask.is_boundary_source(l.src or "")),
        candidates[0])
    for layer in candidates:
        if layer is not chosen:
            layer.role = "layer"
    return chosen


def register_sample(proposal, *, name=None, dataset=None, answers=None,
                    replace=None, data_dir=None, token=None):
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
    from plexora.server.routes.import_routes import _dataset_request

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
    # A name somebody TYPED and a name detection derived are different things.
    # A typed one is an instruction: filing their import under `melanoma_2`
    # because a folder of that name already exists is how two copies of one
    # slide happen, so that collides loudly with a free name offered. A derived
    # one nobody chose, so it deduplicates in silence -- which is what makes
    # importing two slides from one folder work without a dialog per slide.
    wanted = (name or proposal.name or "sample").strip()
    if name and wanted in existing and replace != wanted:
        raise NameTaken(wanted, _dedupe_dataset_name(wanted, existing))
    final = wanted if replace == wanted else _dedupe_dataset_name(wanted, existing)

    layers = list(proposal.layers)
    reference = next((l for l in layers if l.reference), None)
    mask = _preferred_mask(layers)
    table = next((l for l in layers if l.role == "table"), None)
    # After `_preferred_mask`, which demotes a mask candidate that lost.
    registered = [l for l in layers if l.role == "layer"]

    created = replace != final
    with layer_jobs.registering(
            token, [l.id for l in layers if l.role != "note"]):
        return _register(final, created, proposal, answers, layers, reference,
                         mask, table, registered, dataset_id, new_dataset)


def _register(final, created, proposal, answers, layers, reference, mask,
              table, registered, dataset_id, new_dataset):
    """`register_sample`'s writes, reporting each step as it starts and ends."""
    from plexora.server.routes.import_routes import (_file_under,
                                                     attach_segmentation,
                                                     replace_project_data)

    step = layer_jobs.registration_step
    try:
        if reference is not None:
            step(reference.id, "Preparing the image")
        _register_reference(final, reference, proposal.frame, layers)
        if reference is not None:
            step(reference.id, status="ready")

        if table is not None:
            step(table.id, "Reading the table")
            replace_project_data(final, table.src, {
                "table": table.table,
                "subset_column": answers.get("subset_column"),
                "subset_value": answers.get("subset_value"),
            }, spatial=_spatial_context(table, reference))
            step(table.id, status="ready")

        def _apply(project):
            if reference is not None and reference.modality:
                project = project.patch(
                    image=_replace(project.image, modality=reference.modality))
            if reference is not None and reference.pixel_size \
                    and not project.image.pixel_size:
                # The run states it and the registration already uses it --
                # every layer's transform is built from this number -- so a
                # project whose ImageSpec did not carry it was one where the
                # scale bar said "px" while every transform in the file was in
                # microns. Only when nothing else has said: a value the user
                # typed outranks one read off a manifest.
                project = project.patch(image=_replace(
                    project.image,
                    pixel_size={"value": float(reference.pixel_size),
                                "unit": "µm", "source": "metadata"}))
            for bundle in proposal.bundles:
                if (reference is not None and bundle.get("format") == "visium_hd"
                        and (reference.render or {}).get("frameScale")):
                    # Recorded for "+ Add Layer": what the reference is of
                    # this run's full-res frame. See `_scoped_sample`.
                    bundle = {**bundle,
                              "frameScale": reference.render["frameScale"]}
                project = project.with_bundle(bundle)
            for layer in registered:
                project = project.with_layer(
                    _layer_spec(final, layer, _outstanding(layer, answers)))
            return project

        Project.mutate(final, _apply)
        for layer in registered:
            if _needs_build(layer):
                # Its build starts once the record exists; the rail hands
                # over to `/import/status?sample=` for that part.
                step(layer.id, status="waiting",
                     message="builds once the sample opens")
            else:
                step(layer.id, "Recorded", status="ready")

        # LAST, and not where it reads most naturally. A mask stated as
        # boundary polygons is drawn into the reference frame by a job that
        # reads the pixel size off the project -- and the patch above is
        # where the pixel size arrives. Attaching before it ran drew every
        # Xenium cell at one pixel per micron: a fifth-scale mask in the
        # corner of its own slide.
        if mask is not None:
            attach_segmentation(
                final, mask.src,
                transform=(mask.transform
                           if boundary_mask.is_boundary_geojson(mask.src or "")
                           else None))
            step(mask.id, "Queued", status="ready")
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
    mask = _preferred_mask(proposal.layers)
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
                  replace=None, index=0, key=None, token=None):
    """Inspect these paths and register the sample they make.

    The one function every entry point reaches -- the modal's route, the Python
    API, the CLI. Inspection is re-run here rather than trusting a proposal
    handed back by a client, so the record is always written from what the
    files actually say.

    @param index - which of the samples these paths make. A bulk import posts
        once per sample, and `index` is how it says which.
    @param key - that sample's `SampleProposal.key`, when the caller has one.
        `index` alone is a position in a list this call has just rebuilt, and
        the clamp below will happily register SOMETHING for an index that no
        longer exists -- so a caller that knows what it was looking at says
        so, and a mismatch is refused rather than quietly importing its
        neighbour. Optional: the Python API and the CLI pass paths and nothing
        else, and there is nothing for them to disagree with.
    @param token - names this request for `/import/status?token=`, so the
        dialog can draw progress before the sample exists.
    """
    proposal = import_proposal.inspect_paths(paths, node=node, answers=answers)
    if not proposal.samples:
        reasons = [entry["reason"] for entry in proposal.unrecognised]
        raise ImportError_(
            reasons[0] if reasons else "Nothing Plexora can read here.")
    sample = proposal.samples[min(index, len(proposal.samples) - 1)]
    if key and sample.key != key:
        raise ImportError_(
            "These files have changed since they were looked at. Look again, "
            "then import.")
    if sample.existing and not name and not replace:
        # This data is already registered. Reopening rather than making a
        # second copy of it -- the rule quick view has always followed, and the
        # reason is that two projects over one slide diverge: each collects its
        # own ROIs, gates and figures, and nothing afterwards can tell you that
        # the other one exists. A caller who genuinely wants a second copy says
        # so by naming it.
        return {"name": sample.existing, "layers": [], "pending": False,
                "existing": True}
    return register_sample(sample, name=name, dataset=dataset,
                           answers=answers, replace=replace, token=token)


def add_layers(project_name, paths, *, answers=None, node=None):
    """Inspect these paths and add them to an existing sample."""
    proposal = import_proposal.inspect_paths(
        paths, node=node, answers=answers, sample=project_name)
    if not proposal.samples:
        reasons = [entry["reason"] for entry in proposal.unrecognised]
        raise ImportError_(
            reasons[0] if reasons else "Nothing Plexora can read here.")
    return register_layers(project_name, proposal.samples[0], answers=answers)
