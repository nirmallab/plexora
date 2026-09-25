import datetime
import json
import re
import shutil
from dataclasses import replace
from pathlib import Path

import polars as pl

from plexora.server.models.adapters.classify import classify_columns
from plexora.server.models.project import (
    ColumnGroups,
    ColumnRoles,
    DataSpec,
    DEFAULT_PIXEL_SIZE_UNIT,
    IMAGE_KIND_BLANK,
    ImageSpec,
    Project,
    SegmentationSpec,
    normalize_transform,
    write_config,
)


def _now():
    return datetime.datetime.now().isoformat()


def _image_channel_entries(name, channel_info, channel_names, segmentation_path):
    """The `imageData` list: one entry per servable layer, in the order the
    viewer expects -- the 'Area' segmentation placeholder first when there is
    a mask, then one per image channel."""
    entries = []
    if segmentation_path:
        label_name = _segmentation_channel_name(segmentation_path)
        entries.append({
            "name": "Area",
            "fullname": "Area",
            "src": f"/generated/data/{name}/{label_name}/",
        })
    # Driven by the generated tile keys rather than by `num_channels`, because
    # the two are the same number for every channel stack and deliberately are
    # not for a brightfield image: three planes, one servable layer.
    for idx, key in enumerate(channel_info["channel_names"]):
        display_name = str(channel_names[idx])
        entries.append({
            "name": display_name,
            "fullname": display_name,
            "src": f"/generated/data/{name}/{key}/",
        })
    return entries


def _layer_count(channel_info):
    """How many layers the viewer will draw for this image.

    `num_channels` counts the planes the pyramid holds, which is what a node's
    geometry check compares against; this counts the tile keys, which is what
    needs a display name. They differ only for brightfield, where three samples
    are one picture."""
    return len(channel_info["channel_names"])


def _brightfield_channel_names(channel_info):
    """Display names for a brightfield image's single layer, or None.

    None for every other kind, so a caller can write
    `names = _brightfield_channel_names(info) or <derive them>` and not have to
    know why the brightfield case is different."""
    from plexora.server.models.project import IMAGE_TYPE_BRIGHTFIELD

    if channel_info.get("image_kind") != IMAGE_TYPE_BRIGHTFIELD:
        return None
    return ["Image"]


def _with_area_channel(name, channels, segmentation_path):
    """`channels` with the 'Area' placeholder present exactly when there is a mask.

    The two have to move together. `viewerManager.load_label_image` gates on
    the project recording a segmentation and then loads `imageData[0]` as the
    label layer -- so a project that names a mask without the placeholder in
    front draws its first real channel as a label mask, silently and with no
    error to fall back from.

    `segmentation_path` is a path or a `node://` locator; either way it only
    supplies the channel key the tile URL is built from, and the key is what
    tells the tile route this is a label layer rather than channel N (see
    data_model._parse_channel).
    """
    entries = [c for c in channels if c.get("fullname") != "Area"]
    if segmentation_path:
        label_name = _segmentation_channel_name(segmentation_path)
        entries.insert(0, {"name": "Area", "fullname": "Area",
                           "src": f"/generated/data/{name}/{label_name}/"})
    return entries


def _image_spec(name, image_path, channel_info, channel_names, segmentation_path,
                image_type=None):
    return ImageSpec(
        src=str(image_path),
        # The conversion knows which format it read; nothing here re-derives it
        # from the path, so the two can never disagree.
        kind=channel_info.get("image_kind") or "ome_tiff",
        channels=tuple(
            _image_channel_entries(name, channel_info, channel_names, segmentation_path)
        ),
        width=channel_info["width"],
        height=channel_info["height"],
        max_level=channel_info["maxLevel"],
        tile_width=channel_info["tileWidth"],
        tile_height=channel_info["tileHeight"],
        num_channels=channel_info["num_channels"],
        pyramid=channel_info.get("imagePyramid"),
        pyramid_key=channel_info.get("imagePyramidKey"),
        image_type_choice=image_type or None,
        image_type_detected=channel_info.get("imageTypeDetected"),
        image_type_reason=channel_info.get("imageTypeReason"),
    )


def _segmentation_spec(fields):
    """SegmentationSpec from what _segmentation_config_fields() produced."""
    return SegmentationSpec(
        derived=fields.get("segmentation"),
        source=fields.get("segmentationSource"),
        source_key=fields.get("segmentationSourceKey"),
        mode=fields.get("segmentationMode"),
        status=fields.get("segmentation_status", "ready"),
        transform=normalize_transform(fields.get("segmentationTransform")),
    )


def _copy_if_requested(path, target_dir, copy):
    from plexora.server.providers.base import is_remote_locator

    if is_remote_locator(path):
        # A web address is read where it is, through the chunk cache. Copying
        # a store off the web is what "Make available offline" is for, and it
        # keeps the bytes somewhere the cache can manage.
        if copy:
            raise ValueError(
                "An image at a web address cannot be copied into the project. "
                "Open it in place, then use Make available offline instead.")
        from plexora.server.utils import remote_store

        return remote_store.canonical_url(path)
    path = Path(path).expanduser().resolve()
    if not copy:
        return path
    target = target_dir / path.name
    if path != target:
        # A SpatialData store is a .zarr *directory*, not a file -- copy2
        # raises IsADirectoryError on it.
        if path.is_dir():
            shutil.copytree(path, target, dirs_exist_ok=True)
        else:
            shutil.copy2(path, target)
    return target


def _resolve_image(path):
    """The image a registration should actually read.

    Identity for a file. For a zarr *store* -- a SpatialData store, a
    bioformats2raw output -- it is the multiscale group inside, and finding it
    is `ome_zarr.resolve_image_path`'s job.

    Identity for a DICOM slide too, whether a `.dcm` file or the folder holding
    one: there is nothing inside to resolve *to*, because the slide is the
    whole collection rather than one member of it, and which instances belong
    to it is a question `dicom_wsi.assemble_slide` answers from the metadata
    every time the slide is opened. Recording the folder is what makes that
    re-answerable; recording one instance would freeze a 252-file slide to
    whichever file happened to be picked.

    Always called AFTER `_copy_if_requested`, which is the whole reason it is a
    separate step: `copy=True` on a store that is also the feature table has to
    copy the store once, coherently, and then be resolved -- not resolve first
    and copy an image element out of a store whose tables stayed behind.
    """
    from plexora.server.utils import dicom_wsi, ome_zarr

    if dicom_wsi.is_dicom_path(path):
        return Path(path)
    return ome_zarr.resolve_image_path(path)


def _segmentation_channel_name(segmentation_path):
    name = Path(segmentation_path).name
    lowered = name.lower()
    for suffix in (".ome.tiff", ".ome.tif", ".tiff", ".tif", ".png", ".zarr"):
        if lowered.endswith(suffix):
            channel_name = name[: -len(suffix)]
            break
    else:
        channel_name = Path(name).stem
    if re.match(r".*_(\d*)$", channel_name):
        channel_name = f"{channel_name}_segmentation"
    return channel_name


def _segmentation_config_fields(segmentation_path, dataset_dir, segmentation_async,
                                segmentation_mode=None):
    """Build the segmentation-related config keys for a registration.

    Returns (fields, source_needing_generation). A returned source means the
    caller should start a background job for it *after* writing config.json,
    since the job patches that same file when it completes.

    `segmentationSource`/`segmentationSourceKey` are recorded either way: they
    let a later load confirm the derived mask still matches its source with a
    stat, instead of re-deriving it or re-sampling its pixels.

    `segmentation_mode` picks what gets stored: "filled" (the default) stores a
    filled label pyramid -- served untouched when the user's mask already is
    one -- and leaves boundary-finding to renderLabelTile. "outlines" bakes the
    boundaries into the file instead; nothing in the UI asks for it any more,
    but it stays supported for callers that pass it explicitly.
    """
    from plexora.server.models import data_model
    from plexora.server.utils import segmentation_pyramid

    if not segmentation_path:
        return {"segmentation": None, "segmentation_status": "ready"}, None

    mode = (
        segmentation_pyramid.MODE_OUTLINES
        if segmentation_mode == segmentation_pyramid.MODE_OUTLINES
        else segmentation_pyramid.DEFAULT_MODE
    )
    fields = {
        "segmentationSource": str(segmentation_path),
        "segmentationSourceKey": segmentation_pyramid.source_fingerprint(segmentation_path),
        "segmentationMode": mode,
    }
    if segmentation_async:
        # Converting a large mask takes tens of seconds, far too long to hold a
        # form submission open -- the import page waits on the job's reported
        # progress instead and opens the viewer when it finishes.
        fields["segmentation"] = None
        fields["segmentation_status"] = "pending"
        return fields, segmentation_path

    label_info = data_model.convertOmeTiff(
        segmentation_path,
        dataDirectory=str(dataset_dir),
        isLabelImg=True,
        segmentation_mode_=mode,
    )
    fields["segmentation"] = label_info["segmentation"]
    fields["segmentation_status"] = "ready"
    return fields, None


def _derive_dataset_name_from_path(path):
    """Server-side mirror of importFormValidation.js's deriveDatasetName() --
    kept in sync deliberately (same suffix vocabulary) so a quick-viewed file
    and a full-wizard import of the same file suggest the same base name."""
    from plexora.server.providers.base import is_remote_locator

    if is_remote_locator(path):
        from plexora.server.utils import remote_store

        stem = remote_store.url_name(path)
    else:
        stem = Path(path).name
    return re.sub(
        r"\.(ome\.tiff|ome\.tif|ome\.zarr|tiff|tif|svs|zarr|png|jpg|jpeg|qptiff"
        r"|ndpi|mrxs|scn|bif|svslide|dcm|dicom)$",
        "",
        stem,
        flags=re.IGNORECASE,
    )


def _dedupe_dataset_name(base_name, existing_names):
    """Suffix base_name with _2, _3, ... until it doesn't collide with any of
    existing_names -- callers pass get_config_names() in."""
    existing = set(existing_names)
    if base_name not in existing:
        return base_name
    i = 2
    while f"{base_name}_{i}" in existing:
        i += 1
    return f"{base_name}_{i}"


def _find_existing_datasource_for_image(image_path, config):
    """Return the name of an already-registered datasource pointing at the
    same on-disk image file, or None. Every registration path (quick view,
    the full import wizard, anndata) stamps config[name]['channelFile'] with
    the image path it was given, so resolving both sides (expanduser,
    symlinks, '..', relative vs. absolute) catches the same file being
    quick-viewed twice, or quick-viewed after already being imported."""
    from plexora.server.providers.base import is_remote_locator

    if is_remote_locator(image_path):
        from plexora.server.utils import remote_store

        target_url = remote_store.canonical_url(image_path)
        for name, entry in (config or {}).items():
            channel_file = (entry or {}).get("channelFile")
            if channel_file and is_remote_locator(channel_file) \
                    and remote_store.canonical_url(channel_file) == target_url:
                return name
        return None
    try:
        target = Path(image_path).expanduser().resolve()
    except OSError:
        return None
    for name, entry in (config or {}).items():
        channel_file = (entry or {}).get("channelFile")
        if not channel_file:
            continue
        if is_remote_locator(channel_file):
            continue
        try:
            if Path(channel_file).expanduser().resolve() == target:
                return name
        except OSError:
            continue
    return None


def _sniff_quick_view_kind(path):
    """Classify a dropped/browsed file as 'ome_tiff' or 'ome_zarr' (both go
    through the full multi-channel tile pipeline) or 'rgb' (flat single-image
    display, no channels) purely by extension, with a PIL-based content sniff
    on the RGB branch as a guard against a mislabeled file. Raises ValueError
    for anything else -- quick view has no format-detection fallback.

    A directory is answered first, because an OME-Zarr store IS one and every
    suffix test below would otherwise read it as a file with a strange name.
    """
    from plexora.server.providers.base import is_remote_locator
    from plexora.server.utils import brightfield, dicom_wsi, ome_zarr

    if is_remote_locator(path):
        if ome_zarr.is_zarr_image_path(path):
            return "ome_zarr"
        raise ValueError(
            "Plexora opens an image from a web address only when it is an "
            "OME-Zarr store (https://, s3://, gs:// or az://).")
    if Path(path).is_dir():
        if ome_zarr.is_zarr_image_path(path):
            return "ome_zarr"
        if dicom_wsi.is_dicom_path(path):
            # A folder is the normal way to arrive at a DICOM slide, because a
            # slide IS a folder of instances. Which kind it ends up as --
            # 'dicom' or 'brightfield' -- is the conversion's call, made by
            # reading the headers rather than the folder's name. Assembled here
            # so a folder holding two slides is refused while the user is still
            # choosing, rather than half way through an import.
            dicom_wsi.assemble_slide(path)
            return "ome_tiff"
        raise ValueError(
            f"{Path(path).name} is a folder, not an image. Plexora opens a "
            "folder only when it is an OME-Zarr (.zarr) store or a folder of "
            "DICOM whole-slide images.")
    suffix = Path(path).suffix.lower()
    if dicom_wsi.is_dicom_path(path):
        # One instance of a slide selects that slide; its siblings are gathered
        # from the metadata. Probed here, like the OpenSlide formats below, so
        # a missing install is reported while the user is still choosing.
        dicom_wsi.assemble_slide(path)
        return "ome_tiff"
    if suffix in (".tif", ".tiff", ".qptiff") or brightfield.is_wsi_path(path):
        # All one answer: these go through the full tile pipeline, and which
        # kind they end up as -- 'ome_tiff' or 'brightfield' -- is the
        # conversion's call, made by reading the file rather than its name (see
        # data_model.convertOmeTiff). A whole-slide container that needs
        # OpenSlide is probed here so a missing install is reported while the
        # user is still choosing a file.
        if brightfield.is_openslide_format(path):
            brightfield.open_rgb(path)
        return "ome_tiff"
    if suffix in (".png", ".jpg", ".jpeg"):
        from PIL import Image
        with Image.open(path) as img:
            img.verify()
        return "rgb"
    raise ValueError(f"Unsupported file type for quick view: {suffix or path}")


def rename_channels(name, channel_names, data_dir=None):
    """Rename an already-registered datasource's image channels in place --
    used by the viewer's channel-names CSV upload to fix gating/channel
    auto-matching after the fact, without re-registering (and re-running
    image pyramid generation for) the whole datasource. Caller is
    responsible for reloading the runtime datasource afterward (see
    data_model.load_datasource(name, reload=True)) so the cached description
    and in-memory config pick up the change.
    """
    from plexora import paths

    data_root = Path(data_dir).expanduser().resolve() if data_dir else paths.data_root()
    try:
        project = Project.load(name, data_root)
    except KeyError:
        raise ValueError(f"No datasource named {name!r}.") from None

    renamable = [c for c in project.image.channels if c.get("name") != "Area"]
    if len(channel_names) != len(renamable):
        raise ValueError(
            f"channel_names has {len(channel_names)} entries but {name!r} has {len(renamable)} channels."
        )

    # Rebuilt rather than mutated in place: ImageSpec is frozen, and editing
    # the channel dicts it holds would also edit whatever the caller passed in.
    new_names = list(str(n) for n in channel_names)
    channels = []
    for channel in project.image.channels:
        channel = dict(channel)
        if channel.get("name") != "Area":
            renamed = new_names.pop(0)
            channel["name"] = renamed
            channel["fullname"] = renamed
        channels.append(channel)

    updated = project.patch(image=replace(project.image, channels=tuple(channels)))
    return updated.save(data_root)


def rename_layer_channels(name, layer_id, channel_names, data_dir=None):
    """Rename a registered LAYER's channels in place.

    What `rename_channels` is for the reference image, for a second image
    added afterwards -- a multiplex registered beside an H&E arrives as
    Channel_0 … Channel_n just as often, and its panel is the same panel.

    `src` IS DELIBERATELY LEFT ALONE, and that is the whole reason a rename is
    safe here. A layer channel's tiles come from
    `/generated/layer/<sample>/<layer>/<key>/`, and `key` is what
    `data_model._parse_channel` reads the plane number out of; the name is
    only what the panel calls it. So renaming moves no address and no index --
    which is what lets the saved channel list survive one untouched, since
    `render.channels` stores an index beside every name and the panel resolves
    by index first (see layerChannelPanel.savedRowsFor).

    Raises ValueError for an unknown project or layer, and for a list that is
    not the length of the layer's channels -- half a panel renamed and half
    left on Channel_12 is worse than the original, and impossible to see.
    """
    from dataclasses import replace as _replace

    from plexora import paths
    from plexora.server.models.project import Project

    data_root = Path(data_dir).expanduser().resolve() if data_dir else paths.data_root()
    project = Project.find(name, data_root)
    if project is None:
        raise ValueError(f"No datasource named {name!r}.")
    layer = project.layer(layer_id)
    if layer is None:
        raise ValueError(f"{name!r} has no layer {layer_id!r}.")
    if len(channel_names) != len(layer.channels):
        raise ValueError(
            f"channel_names has {len(channel_names)} entries but layer "
            f"{layer_id!r} has {len(layer.channels)} channels."
        )

    new_names = [str(n) for n in channel_names]

    def renamed(current):
        # Re-read inside the lock rather than closing over `layer`: the panel
        # that opens this dialog can be changing the same record's `render`
        # from the other end of the same session.
        target = current.layer(layer_id)
        if target is None or len(target.channels) != len(new_names):
            return current
        channels = tuple(
            {**dict(channel), "name": renamed_to, "fullname": renamed_to}
            for channel, renamed_to in zip(target.channels, new_names))
        return current.with_layer(_replace(target, channels=channels))

    return Project.mutate(name, renamed, data_root)


def set_pixel_size(name, value, unit=None, data_dir=None):
    """Record what one pixel is worth for an already-registered datasource.

    The viewer's calibration control, and the only way a project that arrived
    with no physical scale gets one. `value` is microns per pixel by default;
    `None` (or anything not positive) CLEARS the calibration, which puts the
    scale bar back to counting pixels rather than leaving a number nobody
    stands behind.

    Only ever stores `source="manual"`. A calibration the file states is read
    off the file on every load and is deliberately not copied into the project
    -- see `ImageSpec.pixel_size` -- so this is always a statement by a person,
    and the viewer can tell the two apart well enough to know whether to offer
    an edit.

    Caller reloads the runtime datasource afterwards, the same contract
    `rename_channels` has.
    """
    from plexora import paths
    from plexora.server.models.project import (DEFAULT_PIXEL_SIZE_UNIT,
                                               normalize_pixel_size)

    data_root = Path(data_dir).expanduser().resolve() if data_dir else paths.data_root()
    try:
        project = Project.load(name, data_root)
    except KeyError:
        raise ValueError(f"No datasource named {name!r}.") from None

    pixel_size = normalize_pixel_size({
        "value": value,
        "unit": unit or DEFAULT_PIXEL_SIZE_UNIT,
        "source": "manual",
    })
    # A value that cannot be a length is a clear, not an error: the control
    # that sends it has an X on it, and "0" typed into the box means the same
    # thing as pressing that.
    updated = project.patch(image=replace(project.image, pixel_size=pixel_size))
    updated.save(data_root)
    return pixel_size


def _channel_names_from_ome_xml(image_path, n_channels):
    """Channel names embedded in the image's own OME-XML metadata, if
    present and if the count matches -- returns None otherwise. Shared by
    derive_anndata_channel_names (tier 2 there) and derive_image_channel_names
    (tier 2 here); a pure extraction, same tifffile/ome_types read as before."""
    import tifffile as tf
    from ome_types import from_xml

    try:
        with tf.TiffFile(str(image_path), is_ome=False) as tiff:
            xml = tiff.pages[0].tags['ImageDescription'].value
        ome_channels = from_xml(xml).images[0].pixels.channels
        ome_names = [c.name for c in ome_channels]
        if len(ome_names) == n_channels and all(ome_names):
            return [str(n) for n in ome_names]
    except Exception:
        pass
    return None


def _channel_names_from_zarr_attrs(image_path, n_channels):
    """The OME-Zarr counterpart of _channel_names_from_ome_xml: names out of the
    store's `omero.channels[].label`, and only when they account for every
    channel."""
    from plexora.server.utils import ome_zarr

    return ome_zarr.channel_labels(image_path, n_channels)


#: A channel list written *beside* the image rather than inside it. An Akoya /
#: CODEX export names it exactly this and leaves it at the root of the region
#: folder, one line per plane; the underscored spelling is what a few
#: conversion scripts write instead. Not a general "any CSV in the directory"
#: search -- a file has to be named one of these to be read as the panel.
SIDECAR_CHANNEL_FILES = ("channelNames.txt", "channel_names.txt")


def _channel_names_from_sidecar(image_path, n_channels):
    """Channel names from a list written next to the image.

    This is most of what "QuPath opens my CODEX stack with the panel already
    named and Plexora does not" actually was: the names are not in the TIFF at
    all. An Akoya export writes `channelNames.txt` at the root of the region
    folder while the stacks sit in a subdirectory (`bestFocus/`, or whatever
    the processing step wrote), so the image's own directory and its parent are
    both looked at, nearest first.

    Read through `server/utils/channel_file.py` -- the same parser the upload
    modal and the typed-path box use -- so a file that works there works here
    and the two cannot disagree about what a list says. Accepted only when
    `channel_file.autodetect` says it is a single column that accounts for
    every channel; a file that needs a column picked, or whose length does not
    match, is a question for the user, and answering it by guessing would
    rename the panel to the wrong markers with nothing on screen to say so.
    """
    from plexora.server.utils import channel_file

    # An image on a data node is a `node://` locator, not a path on this
    # machine, and there is no directory here to look beside. Nothing below
    # would find a file for one anyway; this just keeps it from being a
    # TypeError on the way to the same answer.
    if not image_path:
        return None
    from plexora.server.providers.base import is_remote_locator

    # Nor for a web address: there is no folder beside it to look in.
    if is_remote_locator(image_path):
        return None
    try:
        image = Path(image_path)
    except TypeError:
        return None
    directories = [image.parent]
    if image.parent.parent != image.parent:
        directories.append(image.parent.parent)
    for directory in directories:
        for filename in SIDECAR_CHANNEL_FILES:
            candidate = directory / filename
            if not candidate.is_file():
                continue
            try:
                grid = channel_file.read_grid(path=str(candidate),
                                              filename=candidate.name)
                has_header = channel_file.autodetect(grid, n_channels)
                if has_header is None:
                    continue
                return [str(name)
                        for name in channel_file.names(grid, 0, has_header)]
            except Exception:
                # An unreadable sidecar is not a failed import. The image is
                # fine; it just goes on to generic names, exactly as before
                # this tier existed.
                continue
    return None


def _channel_names_from_image_metadata(image_path, n_channels):
    """Channel names the image file carries about itself, whatever format it is.

    One dispatcher rather than two call sites choosing, so the tier order below
    stays a statement about authority (var_names beats the file's own metadata)
    and not about format.

    The sidecar list is consulted **last**, and only where the answer was
    previously None: metadata inside the file outranks a text file next to it,
    so no project that already resolved names can have them change."""
    from plexora.server.utils import dicom_wsi, ome_zarr, xenium_focus

    if xenium_focus.is_focus_dir(image_path):
        # The panel, one stain per file, out of each file's own OME-XML.
        names = xenium_focus.channel_names(image_path)
        names = names if len(names) == n_channels else None
    elif ome_zarr.is_zarr_image_path(image_path):
        names = _channel_names_from_zarr_attrs(image_path, n_channels)
    elif dicom_wsi.is_dicom_path(image_path):
        # Optical Path Description, which is where a multiplex exporter writes
        # the marker -- so a t-CyCIF slide arrives with its panel already named
        # and nobody has to upload a channel-names CSV to find CD45 again.
        names = dicom_wsi.channel_names(image_path)
        names = names if names and len(names) == n_channels else None
    else:
        names = _channel_names_from_ome_xml(image_path, n_channels)
    if names is not None:
        return names
    return _channel_names_from_sidecar(image_path, n_channels)


def derive_image_channel_names(image_path, n_channels):
    """Resolve display names for a quick-view (no feature table) image:
    the image's own channel names if present and complete, else generic
    "Channel N". Same tier-2/tier-4 logic as derive_anndata_channel_names,
    minus the var_names/all_markers tiers that only make sense with an AnnData
    table.

    "The image's own channel names" is whatever the format has to offer plus,
    last, a `channelNames.txt` written beside it -- see
    `_channel_names_from_image_metadata`. The `"image metadata"` label covers
    all of them; both callers of this function discard it.
    """
    ome_names = _channel_names_from_image_metadata(image_path, n_channels)
    if ome_names is not None:
        return ome_names, "image metadata"
    return [f"Channel {i + 1}" for i in range(n_channels)], "generic"


def derive_anndata_channel_names(image_path, features_path, n_channels):
    """Resolve a display name for every image channel, trying progressively
    less-authoritative sources in order and only accepting one that accounts
    for every channel -- a partial or wrong-length source is more likely to
    silently mislabel a channel than a generic name is, so it's skipped:

    1. adata.var_names (deduplicated) -- the marker panel actually used for
       analysis/gating. Checked first (ahead of embedded image metadata)
       because gating always matches channels to var_names by name; a
       length match here is treated as proof the two are already in the
       same per-channel order, so it's linked by index rather than by
       comparing text -- var_names' own text is what's used, even if the
       image's embedded metadata disagrees or uses different wording for
       the same channels.
    2. Channel names embedded in the image's own metadata (OME-XML for a
       TIFF, `omero.channels[].label` for an OME-Zarr store) -- falls back
       to this only when var_names' length doesn't fit (e.g. QC trimmed the
       panel), since matching gating out of the box beats a more
       "authoritative" name that gating can't use.
    3. adata.uns['all_markers'] -- some pipelines (e.g. scimap) keep the full
       acquisition panel here separately from var_names, which may have been
       trimmed by QC.
    4. Generic "Channel N" names.

    Whichever tier resolves, the result is returned alongside a short label
    identifying the source, so callers can surface it to the user -- names
    from tiers 2-4 are not guaranteed to match the vocabulary gating markers
    use (e.g. a marker panel recorded as gene symbols in var_names vs.
    antibody/clinical names in all_markers), so auto-matching between gating
    and image channels may still require renaming channels (e.g. via the
    channel-list CSV upload in the viewer) or manual matching there.
    """
    import anndata as ad

    adata = ad.read_h5ad(features_path, backed='r')
    try:
        return _derive_channel_names_from_adata(image_path, adata, n_channels)
    finally:
        if adata.isbacked:
            adata.file.close()


def derive_spatialdata_channel_names(image_path, store, table, n_channels):
    """derive_anndata_channel_names() for one table inside a SpatialData
    store -- identical tier order and semantics, since the resolved table is
    an AnnData with its own var_names and uns['all_markers']."""
    from plexora.server.models.adapters.spatialdata_adapter import read_spatialdata_table

    adata = read_spatialdata_table(store, table)
    return _derive_channel_names_from_adata(image_path, adata, n_channels)


def _derive_channel_names_from_adata(image_path, adata, n_channels):
    """Tier logic of derive_anndata_channel_names(), against an already-open
    AnnData so the .h5ad and SpatialData entry points share one
    implementation. Caller owns opening and closing `adata`."""
    from plexora.server.models.adapters.anndata_adapter import _deduplicate_names

    var_names = _deduplicate_names([str(v) for v in adata.var_names])
    if len(var_names) == n_channels:
        return var_names, "adata.var_names"

    ome_names = _channel_names_from_image_metadata(image_path, n_channels)
    if ome_names is not None:
        return ome_names, "image metadata"

    all_markers = adata.uns.get('all_markers')
    if all_markers is not None:
        all_markers = [str(m) for m in all_markers]
        if len(all_markers) == n_channels:
            return all_markers, "adata.uns['all_markers']"

    return [f"Channel {i + 1}" for i in range(n_channels)], "generic"


def register_datasource(
    name,
    image,
    features,
    x=None,
    y=None,
    segmentation=None,
    id_column=None,
    celltype_column=None,
    channel_names=None,
    copy=False,
    data_dir=None,
    segmentation_async=False,
    segmentation_mode=None,
    image_type=None,
):
    """Register a dataset in Plexora's config without using the upload UI.

    `segmentation_async` defers mask conversion to a background job and leaves
    `segmentation_status` as "pending"; callers then poll
    /get_segmentation_status. It defaults to off so programmatic callers get a
    fully-registered datasource back from a single call.

    `image_type` overrides the brightfield/fluorescence detector -- see
    `data_model.convertOmeTiff`. None means "decide from the file".
    """
    from plexora import paths
    from plexora.server.models import data_model

    data_root = Path(data_dir).expanduser().resolve() if data_dir else paths.data_root()
    dataset_dir = data_root / name
    dataset_dir.mkdir(parents=True, exist_ok=True)
    config_path = data_root / "config.json"
    if not config_path.exists():
        write_config(config_path, {})

    image_path = _resolve_image(_copy_if_requested(image, dataset_dir, copy))
    segmentation_path = _copy_if_requested(segmentation, dataset_dir, copy) if segmentation else None
    features_path = _copy_if_requested(features, dataset_dir, copy)

    from plexora.server.models.adapters import detect_data_type, read_flat_table

    # Which flat encoding, off the suffix, because the schema read and the
    # recorded `DataSpec.type` both need it. This is the flat-table entry
    # point -- a container comes through `register_anndata_datasource` -- and
    # an unreadable suffix raises here naming the accepted formats, before a
    # project directory has anything in it.
    data_type = detect_data_type(features_path)
    feature_table = read_flat_table(features_path, data_type, n_rows=1)

    # One predictor for the marker/metadata split and the column roles, shared
    # with the import UI (adapters/classify.py). Explicit arguments win over
    # its guesses -- a caller who named the coordinate columns has answered
    # already -- and anything left unset is a role nobody has established yet,
    # which is a legitimate state rather than an error. Whatever first needs
    # one asks for it (plexora/api/plugin.py's Requires).
    spec = flat_table_spec(
        features_path,
        [{"name": c, "dtype": str(dt)} for c, dt in feature_table.schema.items()],
        x=x, y=y, id_column=id_column, celltype_column=celltype_column,
        data_type=data_type,
    )
    roles = spec.roles
    markers = list(spec.columns.markers)

    channel_info = data_model.convertOmeTiff(
        image_path, dataDirectory=str(dataset_dir), isLabelImg=False,
        image_type=image_type)
    segmentation_fields, pending_segmentation_source = _segmentation_config_fields(
        segmentation_path, dataset_dir, segmentation_async, segmentation_mode
    )

    n_channels = _layer_count(channel_info)
    brightfield_names = _brightfield_channel_names(channel_info)
    if brightfield_names is not None:
        channel_names = brightfield_names
    else:
        if channel_names is None:
            channel_names = markers[:n_channels]
        if len(channel_names) < n_channels:
            stem = image_path.name
            channel_names = list(channel_names) + [f"{stem}_{i}" for i in range(len(channel_names), n_channels)]

    project = Project(
        name=name,
        image=_image_spec(name, image_path, channel_info, channel_names,
                          segmentation_path, image_type),
        segmentation=_segmentation_spec(segmentation_fields),
        dataset=spec,
        created_at=_now(),
    )
    entry = project.save(data_root)

    if pending_segmentation_source:
        data_model.start_segmentation_job(
            name, pending_segmentation_source, dataset_dir,
            segmentation_fields["segmentationMode"],
        )

    return entry


def anndata_spec(src, *, table=None, coordinate_source=None, obsm_key=None,
                 x=None, y=None, feature_source="X", layer=None,
                 feature_obs_columns=None, subset_by=None, subset_value=None,
                 apply_log_transform=False, obs_id_field=None,
                 celltype_column=None) -> DataSpec:
    """How to read an AnnData, as the project will record it.

    Every argument here is an answer somebody gave -- on the import form, in a
    `register_anndata_datasource(...)` call, or in a notebook's
    `plexora.view(adata=..., obsm_key=...)`. Kept as one function because there
    is exactly one right translation of those answers into a `DataSpec`, and
    the notebook path arriving at a different one is the sort of divergence
    nobody notices until a project imported one way opens differently from the
    same data imported the other.

    `src` is where the table is. A path for a file, `memory://<id>` for an
    object a kernel is holding -- neither is interpreted here; the adapter that
    reads it is chosen by the caller (see providers/memory.py).
    """
    coordinates_config = {}
    if coordinate_source == "obsm":
        coordinates_config = {"source": "obsm", "obsm_key": obsm_key or "spatial"}
    elif coordinate_source == "obs":
        if not x or not y:
            raise ValueError("x and y are required when coordinate_source='obs'")
        coordinates_config = {"source": "obs", "x_column": x, "y_column": y}
    elif coordinate_source is not None:
        raise ValueError(f"Unknown coordinate_source: {coordinate_source!r}")
    # coordinate_source left as None: coordinates_config stays empty and
    # AnnDataAdapter auto-detects adata.obsm['spatial'] if unambiguous.

    if feature_source == "X":
        features_config = {"source": "X"}
    elif feature_source == "layer":
        if not layer:
            raise ValueError("layer is required when feature_source='layer'")
        features_config = {"source": "layer", "layer": layer}
    elif feature_source == "obs":
        if not feature_obs_columns:
            raise ValueError("feature_obs_columns is required when feature_source='obs'")
        features_config = {"source": "obs", "obs_columns": list(feature_obs_columns)}
    else:
        raise ValueError(f"Unknown feature_source: {feature_source!r}")

    subset_config = {}
    if subset_by:
        subset_config = {"column": subset_by, "value": subset_value}

    return DataSpec(
        type="spatialdata" if table else "anndata",
        # In SpatialData mode this is the *store root*, with the chosen table
        # named alongside it, so a plugin needing the store's other elements
        # (images/labels/shapes) can open it from here.
        src=str(src),
        table=str(table) if table else None,
        coordinates=coordinates_config,
        features=features_config,
        subset=subset_config,
        # True only when apply_log_transform is explicitly requested --
        # no heuristic guessing at whether the chosen feature source "looks"
        # already transformed. This also gates whether the gate slider/
        # auto-gate keep float precision or round to whole numbers, so an
        # incorrect guess here would silently destroy narrow-range gates
        # (e.g. rounding a real [1.85, 2.23] gate to [1, 3] matches nearly
        # every cell) -- the user's call, every time.
        is_transformed=bool(apply_log_transform),
        obs_id_field=obs_id_field,
        roles=ColumnRoles(
            # The adapter synthesizes X/Y columns with these literal names.
            x="X",
            y="Y",
            # Defaults to the adapter's own positional "id" column (0..n-1,
            # always int -- matches NormalizedDatasource.id_column), not
            # DEFAULT_ID_COLUMN ("obs_id"). The cell_id role has to be
            # uint32-castable: get_all_cells() packs [cell_id, X, Y] into one
            # flat array and casts the whole thing to uint32 for the fast
            # binary cell-loading path (numericData.js), which crashes if it
            # holds adata.obs_names strings -- the common case, since those
            # are rarely small integers. An explicit obs_id_field is still
            # honored as-is; a non-numeric choice there is the caller's
            # informed tradeoff, not a silent default.
            #
            # This is a description of the emitted table, NOT an answer to the
            # cell-id question -- `obs_id_field` is where that lives, and it
            # stays None here until somebody says otherwise. Reading the role
            # as the answer is what let every import arrive pre-answered with
            # a row number nobody chose (see plugin.py's `_answered`).
            cell_id=obs_id_field or "id",
            celltype=celltype_column,
            image_id=subset_by or None,
        ),
    )


def deferred_spec(src, data_type, *, table=None, subset_by=None) -> "DataSpec | None":
    """The spec for a source that cannot be read yet, or None if it can.

    This is the whole of "register first, configure progressively" on the data
    side. Two questions genuinely cannot be answered from the file -- which of
    a store's tables to load, and which image inside a table that spans several
    -- and until now both were answered by refusing the import outright. That
    put the user in the worst place: they have the files, they know they belong
    together, and Plexora will not record that until they make a decision it
    has given them no way to explore.

    So: the path is a fact and is written down; the question is written down
    beside it as `unresolved`, the project opens as an image, and the first
    tool that needs a table asks -- through the same modal that asks for a
    mask, a cell id or a marker split.

    Returns None when the source is ordinary (a CSV, an .h5ad of one image, a
    store with exactly one table), in which case the caller goes on and reads
    it as it always did. Falls back to None on any inspection failure too: a
    file that cannot even be inspected should fail in the importer with the
    importer's error message, not be quietly recorded as "unresolved".
    """
    from plexora.server.models.adapters import inspection as data_inspection
    from plexora.server.models.adapters.spatialdata_adapter import list_spatialdata_tables

    if data_type not in ("anndata", "spatialdata"):
        return None

    def _spec(*missing, table=None):
        return DataSpec(type=data_type, src=str(src), table=table,
                        unresolved=tuple(missing))

    if data_type == "spatialdata" and not table:
        try:
            tables = list_spatialdata_tables(src)
        except Exception:
            return None
        if len(tables) != 1:
            # Zero tables is deferred too, and deliberately: a store whose
            # tables arrive later is a real workflow, and refusing the import
            # loses the pairing with the image for no gain.
            return _spec("table")
        table = tables[0]["name"]

    try:
        proposal = data_inspection.propose_read_spec(
            data_inspection.inspect_spatialdata_table(src, table)
            if data_type == "spatialdata"
            else data_inspection.inspect_anndata(src))
    except Exception:
        return None

    if proposal.get("ambiguous") and not subset_by:
        return _spec("subset", table=table)
    return None


def described_spec(spec, planned) -> DataSpec:
    """`spec` with what the adapter's `plan()` just discovered written into it.

    The marker/metadata split and the file's own obs/layer/obsm vocabularies.
    None of it changes how the table is READ -- it is what a user later picks
    from when changing the read spec -- which is why it is recorded once, here,
    from the one pass that already knows.
    """
    markers = list(planned.feature_columns)
    metadata = [c for c in planned.table_columns if c not in set(markers)]
    return replace(
        spec,
        columns=ColumnGroups(markers=tuple(markers), metadata=tuple(metadata)),
        # Kept alongside the split, and not the same thing: `metadata` is what
        # the loaded table holds, while these are the file's own annotations --
        # the list a user picks from when saying which column holds the cell id
        # or the coordinates (see Project.role_columns).
        obs_columns=tuple(planned.obs_columns),
        # Likewise: the other matrices the file carries, so the choice of which
        # one to threshold on stays changeable after import.
        layers=tuple(planned.layers),
        # And the obsm arrays, so the coordinate source stays changeable too.
        # Without these recorded the coordinate question has nothing to offer,
        # and the importer's name-based pick is the only one there will ever be.
        obsm=tuple(planned.obsm),
    )


def flat_table_spec(src, schema, *, x=None, y=None, id_column=None,
                    celltype_column=None, data_type="csv") -> DataSpec:
    """How to read a flat table, as the project will record it.

    The flat-file counterpart of `anndata_spec`, and the same reasoning: one
    translation of the answers, whether they came from a form, from
    `register_datasource(...)`, or from a notebook handing over a DataFrame.

    `schema` is `[{"name", "dtype"}, ...]`. A flat table's header does not draw
    the marker/metadata line itself -- that is what the classification screen
    exists for -- so `classify_columns` guesses it and every explicit argument
    beats the guess.

    `data_type` is the encoding -- "csv" or "parquet" -- and is recorded rather
    than assumed, because it is what `get_adapter` later reads the file with.
    A notebook frame that was never a file keeps the default.
    """
    classified = classify_columns(list(schema))
    guessed = classified["roles"]
    roles = ColumnRoles(
        cell_id=id_column or guessed.get("cell_id"),
        x=x or guessed.get("x"),
        y=y or guessed.get("y"),
        celltype=celltype_column or guessed.get("celltype"),
        image_id=guessed.get("image_id"),
    )
    names = [entry["name"] for entry in schema]
    named = set(roles.to_dict().values())
    missing = [column for column in named if column not in names]
    if missing:
        raise ValueError("Missing feature column(s): " + ", ".join(sorted(missing)))
    markers = [c for c in classified["markers"] if c not in named]
    metadata = [c for c in names if c not in markers]
    return DataSpec(
        type=data_type,
        src=str(src),
        roles=roles,
        columns=ColumnGroups(markers=tuple(markers), metadata=tuple(metadata)),
    )


def register_anndata_datasource(
    name,
    image,
    features=None,
    adata=None,
    segmentation=None,
    coordinate_source=None,
    obsm_key=None,
    x=None,
    y=None,
    feature_source="X",
    layer=None,
    feature_obs_columns=None,
    obs_id_field=None,
    celltype_column=None,
    subset_by=None,
    subset_value=None,
    apply_log_transform=False,
    channel_names=None,
    copy=False,
    data_dir=None,
    table=None,
    segmentation_async=False,
    segmentation_mode=None,
    image_type=None,
):
    """Register an AnnData (.h5ad)-backed dataset in Plexora's config.

    Exactly one of `features` (a path to an existing .h5ad file) or `adata`
    (an in-memory AnnData object) must be given -- an in-memory object is
    always written to `<dataset_dir>/<name>.h5ad` first, since the runtime
    server process (a separate subprocess for Jupyter) reads datasources
    from config.json/disk, never from a Python object held by the caller.

    `coordinate_source`/`feature_source` etc. mirror AnnDataAdapter's
    dataSource config fields (see adapters/anndata_adapter.py); explicit
    arguments always override auto-detection (requirements §8). Leaving
    `coordinate_source` unset auto-detects adata.obsm['spatial'] if present
    and unambiguous.

    Setting `table` switches this to SpatialData mode: `features` is then a
    .zarr store and `table` names the table inside it to load. Every other
    argument keeps its meaning, because the selected table is itself an
    AnnData -- only the reader differs (see adapters/spatialdata_adapter.py).
    register_spatialdata_datasource() below is the friendlier entry point.
    """
    from plexora import paths
    from plexora.server.models import data_model
    from plexora.server.models.adapters.anndata_adapter import AnnDataAdapter
    from plexora.server.models.adapters.spatialdata_adapter import SpatialDataAdapter

    if (adata is None) == (features is None):
        raise ValueError("Provide exactly one of `adata` (in-memory) or `features` (.h5ad path)")
    if table and adata is not None:
        raise ValueError("`table` selects a table inside a .zarr store, so pass `features`, not `adata`")

    data_root = Path(data_dir).expanduser().resolve() if data_dir else paths.data_root()
    dataset_dir = data_root / name
    dataset_dir.mkdir(parents=True, exist_ok=True)
    config_path = data_root / "config.json"
    if not config_path.exists():
        write_config(config_path, {})

    image_path = _resolve_image(_copy_if_requested(image, dataset_dir, copy))
    segmentation_path = _copy_if_requested(segmentation, dataset_dir, copy) if segmentation else None

    if adata is not None:
        features_path = dataset_dir / f"{name}.h5ad"
        adata.write_h5ad(features_path)
    else:
        features_path = _copy_if_requested(features, dataset_dir, copy)

    # Can this file be read at all yet? A store with six tables and no chosen
    # one, or a table spanning sixty images with no chosen image, is registered
    # as an image project that REMEMBERS the path -- see `deferred_spec`. Only
    # a path on disk can be in that state: an in-memory AnnData is one table by
    # construction, and its caller is a notebook that can answer immediately.
    deferred = None
    if adata is None:
        from plexora.server.models.adapters import detect_data_type

        try:
            data_type = "spatialdata" if table else detect_data_type(features_path)
        except ValueError:
            data_type = None
        if data_type:
            deferred = deferred_spec(features_path, data_type, table=table,
                                     subset_by=subset_by)
        if deferred is not None and deferred.table:
            # The store turned out to hold exactly one table and this is only
            # the image question -- keep the answer so the modal asks one
            # question rather than two.
            table = deferred.table

    spec = None if deferred is not None else anndata_spec(
        features_path, table=table, coordinate_source=coordinate_source,
        obsm_key=obsm_key, x=x, y=y, feature_source=feature_source, layer=layer,
        feature_obs_columns=feature_obs_columns, subset_by=subset_by,
        subset_value=subset_value, apply_log_transform=apply_log_transform,
        obs_id_field=obs_id_field, celltype_column=celltype_column,
    )

    # Validate end-to-end (subset/coordinates/features resolve, coordinates
    # are finite, etc.) before writing anything or doing any expensive image
    # pyramid work. The plan also tells us the marker/metadata split for free,
    # which is why the result is kept rather than discarded: for AnnData the
    # file already draws that line (var = markers, obs = metadata), so unlike
    # CSV the user is never asked to confirm it.
    #
    # `plan()` and NOT `load_table()`: every fact below is metadata -- the
    # column split, the obs/layer/obsm vocabularies -- and every validation is
    # answerable from obs and var. Reading the matrix here is what made a large
    # multi-image file impossible to import at all, rather than merely slow to
    # open: the read happened before the subset was ever consulted.
    if spec is not None:
        adapter_class = SpatialDataAdapter if table else AnnDataAdapter
        spec = described_spec(spec, adapter_class(spec).plan())
    else:
        spec = deferred

    channel_info = data_model.convertOmeTiff(
        image_path, dataDirectory=str(dataset_dir), isLabelImg=False,
        image_type=image_type)
    segmentation_fields, pending_segmentation_source = _segmentation_config_fields(
        segmentation_path, dataset_dir, segmentation_async, segmentation_mode
    )

    n_channels = _layer_count(channel_info)
    brightfield_names = _brightfield_channel_names(channel_info)
    if brightfield_names is not None:
        # A brightfield image has no markers to name, whatever the table says.
        channel_names = brightfield_names
    elif channel_names is None and deferred is not None:
        # Nothing to derive them FROM: the marker names live in the table
        # nobody has chosen. The image's own names (or Channel 1..n) stand in,
        # and are corrected the moment the table question is answered and the
        # source re-registered.
        channel_names, _ = derive_image_channel_names(image_path, n_channels)
    elif channel_names is None:
        if table:
            channel_names, _ = derive_spatialdata_channel_names(
                image_path, features_path, table, n_channels
            )
        else:
            channel_names, _ = derive_anndata_channel_names(image_path, features_path, n_channels)
    elif len(channel_names) != n_channels:
        raise ValueError(
            f"channel_names has {len(channel_names)} entries but the image has {n_channels} channels."
        )

    project = Project(
        name=name,
        image=_image_spec(name, image_path, channel_info, channel_names,
                          segmentation_path, image_type),
        segmentation=_segmentation_spec(segmentation_fields),
        dataset=spec,
        created_at=_now(),
    )
    entry = project.save(data_root)

    if pending_segmentation_source:
        data_model.start_segmentation_job(
            name, pending_segmentation_source, dataset_dir,
            segmentation_fields["segmentationMode"],
        )

    return entry


def register_spatialdata_datasource(
    name,
    image,
    store,
    table,
    **kwargs,
):
    """Register one table of a SpatialData (.zarr) store as a dataset.

    Thin wrapper over register_anndata_datasource() -- a SpatialData table is
    an AnnData, so `coordinate_source`, `feature_source`, `subset_by`,
    `celltype_column`, `channel_names`, `copy`, `data_dir` etc. all behave
    exactly as they do there and are accepted as keyword arguments.

    `store` is the .zarr store root and `table` is the name of the table
    inside it (see spatialdata_adapter.list_spatialdata_tables() to
    enumerate them). Only that one table is read, never the whole store.
    """
    # No `table` is no longer an error. A store with exactly one table resolves
    # itself below; one with several is registered unresolved and asked about
    # by whichever tool first needs the table (see `deferred_spec`).
    return register_anndata_datasource(
        name=name,
        image=image,
        features=store,
        table=table,
        **kwargs,
    )


def register_image_datasource(name, image, channel_names=None, copy=False,
                              data_dir=None, image_type=None):
    """Register a datasource from just an OME-TIFF/TIFF image -- no feature
    table, no segmentation. Used by the quick-view landing page for a fast
    first look, and the floor of the new import flow: an image is the only
    thing a project must have.

    A project with no `dataset` block is the first-class "image only" state --
    load_datasource(), load_ball_tree() and every direct consumer of the
    feature table/ball tree check `project.has_table` rather than requiring a
    real (or synthesized) feature CSV to exist on disk.
    """
    from plexora import paths
    from plexora.server.models import data_model

    data_root = Path(data_dir).expanduser().resolve() if data_dir else paths.data_root()
    dataset_dir = data_root / name
    dataset_dir.mkdir(parents=True, exist_ok=True)
    config_path = data_root / "config.json"
    if not config_path.exists():
        write_config(config_path, {})

    image_path = _resolve_image(_copy_if_requested(image, dataset_dir, copy))

    channel_info = data_model.convertOmeTiff(
        image_path, dataDirectory=str(dataset_dir), isLabelImg=False,
        image_type=image_type)
    n_channels = _layer_count(channel_info)
    brightfield_names = _brightfield_channel_names(channel_info)
    if brightfield_names is not None:
        channel_names = brightfield_names
    elif channel_names is None:
        channel_names, _ = derive_image_channel_names(image_path, n_channels)
    elif len(channel_names) != n_channels:
        raise ValueError(
            f"channel_names has {len(channel_names)} entries but the image has {n_channels} channels."
        )

    # dataset=None is the explicit "no feature table" state. Everything that
    # needs one -- load_datasource(), load_ball_tree(), and the Tools menu via
    # Requires.missing_from() -- reads it as such, and the tool menu turns it
    # into a request for the missing data rather than hiding the tool.
    project = Project(
        name=name,
        image=_image_spec(name, image_path, channel_info, channel_names, None,
                          image_type),
        dataset=None,
        created_at=_now(),
    )
    return project.save(data_root)


def reregister_image(name, data_dir=None):
    """Re-read an existing project's image under its current `imageTypeChoice`.

    What the edit page's Image type control does after it records a choice.
    Only the image half of the project is rewritten -- the mask, the feature
    table, the roles and every answered requirement are the same facts about
    the same slide whichever way its pixels are read.

    It has to re-run the conversion rather than just flip `image_kind`: the two
    readings disagree about the layer list (one layer or three), and a project
    claiming three channels while the tile route serves one is a viewer with
    two dead channels in it.

    Channel names are re-derived rather than kept. Going to brightfield there
    is nothing to name; coming back from it the old names were `["Image"]`,
    which is not a panel.
    """
    from plexora import paths
    from plexora.server.models import data_model

    data_root = Path(data_dir).expanduser().resolve() if data_dir else paths.data_root()
    project = Project.find(name, data_root)
    if project is None:
        raise ValueError(f"Unknown project: {name}")
    image_path = project.image.src
    if not image_path:
        raise ValueError(
            f"{name!r} has no local image file to re-read -- an image on a "
            "data node is read by the node that holds it.")

    dataset_dir = data_root / name
    channel_info = data_model.convertOmeTiff(
        image_path, dataDirectory=str(dataset_dir), isLabelImg=False,
        image_type=project.image.image_type_choice)

    channel_names = _brightfield_channel_names(channel_info)
    if channel_names is None:
        channel_names, _ = derive_image_channel_names(
            image_path, _layer_count(channel_info))

    def _swap(current):
        return current.patch(image=replace(
            _image_spec(name, image_path, channel_info, channel_names,
                        current.segmentation.derived,
                        current.image.image_type_choice),
            # Preserved across the swap: `_image_spec` builds a fresh spec from
            # the conversion, and these two are facts about the project rather
            # than about this reading of the file.
            image_type_choice=current.image.image_type_choice,
        ))

    return Project.mutate(name, _swap, data_root)


#: The coarsest level a blank frame needs. The same number `ome_zarr` derives
#: coarse levels down to, so a blank frame zooms out exactly as far as a real
#: image of the same size does and the viewer's fit-to-window lands in the same
#: place.
BLANK_LEVEL_TARGET = 1024

#: A blank frame's tile grid. Not read off anything -- nothing is being read --
#: so it is the size every other path already assumes when a source does not
#: say (`resolve_providers`, `_node_thumbnail_plane`, the segmentation pyramid).
BLANK_TILE = 1024


def _blank_levels(width, height, tile=BLANK_TILE, target=BLANK_LEVEL_TARGET):
    """How many halvings it takes to get `width`x`height` down to one tile.

    `maxLevel` is a COUNT of levels, which is what the client turns into
    `extraZoomLevels + maxLevel - 1`. Getting it wrong in either direction is
    visible: too few and the viewer cannot zoom out to the whole sample, too
    many and it asks for levels past the point the frame has collapsed to a
    pixel.
    """
    width, height = max(int(width or 1), 1), max(int(height or 1), 1)
    levels, longest = 1, max(width, height)
    while longest > max(int(target), int(tile)):
        longest = -(-longest // 2)
        levels += 1
    return levels


def register_blank_datasource(name, *, width, height, pixel_size=None,
                              unit=None, modality=None, data_dir=None):
    """Register a sample whose reference frame has no image behind it.

    The answer to "a sample with no conventional image layer". Transcripts on
    their own, a table and a mask, spots from a Visium run with no hires
    picture: all of them still need ONE coordinate system, because every other
    layer's transform is expressed against it and every viewer surface reads
    `width`/`height`/`maxLevel` off it. A blank frame is that coordinate system
    and nothing else -- `src` None, no channels, geometry computed by the
    caller from what the layers actually cover.

    `pixel_size` is stored with `source: "metadata"` when it came out of a
    file's own manifest (a Xenium `experiment.xenium`, a Visium scalefactors
    file), because it did: it is not a number somebody typed for an
    uncalibrated import, and the viewer's calibration control reads that
    difference (see data_model._with_pixel_size).
    """
    from plexora import paths

    data_root = Path(data_dir).expanduser().resolve() if data_dir else paths.data_root()
    dataset_dir = data_root / name
    dataset_dir.mkdir(parents=True, exist_ok=True)
    config_path = data_root / "config.json"
    if not config_path.exists():
        write_config(config_path, {})

    width, height = max(int(width or 1), 1), max(int(height or 1), 1)
    calibration = None
    if pixel_size:
        calibration = {"value": float(pixel_size),
                       "unit": unit or DEFAULT_PIXEL_SIZE_UNIT,
                       "source": "metadata"}

    project = Project(
        name=name,
        image=ImageSpec(
            src=None,
            kind=IMAGE_KIND_BLANK,
            channels=(),
            width=width,
            height=height,
            max_level=_blank_levels(width, height),
            tile_width=BLANK_TILE,
            tile_height=BLANK_TILE,
            num_channels=0,
            pixel_size=calibration,
            modality=modality or "blank",
        ),
        dataset=None,
        created_at=_now(),
    )
    return project.save(data_root)


def register_rgb_datasource(name, image, copy=False, data_dir=None):
    """Register a datasource from a flat RGB image (PNG/JPEG) -- the
    minimal quick-view path: view-only, no channels, no gating. Displayed
    client-side via OpenSeadragon's native single-image tile source (see
    RgbImageViewer), served whole by GET /generated/rgb/<name>, not tiled.
    """
    from PIL import Image

    from plexora import paths

    data_root = Path(data_dir).expanduser().resolve() if data_dir else paths.data_root()
    dataset_dir = data_root / name
    dataset_dir.mkdir(parents=True, exist_ok=True)

    image_path = _copy_if_requested(image, dataset_dir, copy)
    with Image.open(image_path) as img:
        width, height = img.size

    project = Project(
        name=name,
        image=ImageSpec(
            src=str(image_path),
            # 'rgb' is permanently incompatible with marker tools -- a flat
            # image has no channels to threshold. Requires.applies_to() reads
            # this, so those tools are hidden rather than offered and blocked.
            kind="rgb",
            channels=(),
            width=width,
            height=height,
            num_channels=0,
        ),
        dataset=None,
        created_at=_now(),
    )
    return project.save(data_root)
