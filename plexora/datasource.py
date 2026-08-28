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
    ImageSpec,
    Project,
    SegmentationSpec,
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
    generated = channel_info["channel_names"]
    for idx in range(channel_info["num_channels"]):
        display_name = str(channel_names[idx])
        entries.append({
            "name": display_name,
            "fullname": display_name,
            "src": f"/generated/data/{name}/{generated[idx]}/",
        })
    return entries


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


def _image_spec(name, image_path, channel_info, channel_names, segmentation_path):
    return ImageSpec(
        src=str(image_path),
        kind="ome_tiff",
        channels=tuple(
            _image_channel_entries(name, channel_info, channel_names, segmentation_path)
        ),
        width=channel_info["width"],
        height=channel_info["height"],
        max_level=channel_info["maxLevel"],
        tile_width=channel_info["tileWidth"],
        tile_height=channel_info["tileHeight"],
        num_channels=channel_info["num_channels"],
    )


def _segmentation_spec(fields):
    """SegmentationSpec from what _segmentation_config_fields() produced."""
    return SegmentationSpec(
        derived=fields.get("segmentation"),
        source=fields.get("segmentationSource"),
        source_key=fields.get("segmentationSourceKey"),
        mode=fields.get("segmentationMode"),
        status=fields.get("segmentation_status", "ready"),
    )


def _copy_if_requested(path, target_dir, copy):
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
    stem = Path(path).name
    return re.sub(
        r"\.(ome\.tiff|ome\.tif|ome\.zarr|tiff|tif|svs|zarr|png|jpg|jpeg|qptiff)$",
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
    try:
        target = Path(image_path).expanduser().resolve()
    except OSError:
        return None
    for name, entry in (config or {}).items():
        channel_file = (entry or {}).get("channelFile")
        if not channel_file:
            continue
        try:
            if Path(channel_file).expanduser().resolve() == target:
                return name
        except OSError:
            continue
    return None


def _sniff_quick_view_kind(path):
    """Classify a dropped/browsed file as 'ome_tiff' (goes through the full
    multi-channel zarr/tile pipeline) or 'rgb' (flat single-image display,
    no channels) purely by extension, with a PIL-based content sniff on the
    RGB branch as a guard against a mislabeled file. Raises ValueError for
    anything else -- quick view has no format-detection fallback."""
    suffix = Path(path).suffix.lower()
    if suffix in (".tif", ".tiff"):
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


def derive_image_channel_names(image_path, n_channels):
    """Resolve display names for a quick-view (no feature table) image:
    OME-XML channel names if present and complete, else generic "Channel N".
    Same tier-2/tier-4 logic as derive_anndata_channel_names, minus the
    var_names/all_markers tiers that only make sense with an AnnData table.
    """
    ome_names = _channel_names_from_ome_xml(image_path, n_channels)
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
    2. Channel names embedded in the image's own OME-XML metadata --
       falls back to this only when var_names' length doesn't fit (e.g. QC
       trimmed the panel), since matching gating out of the box beats a
       more "authoritative" name that gating can't use.
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

    ome_names = _channel_names_from_ome_xml(image_path, n_channels)
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
):
    """Register a dataset in Plexora's config without using the upload UI.

    `segmentation_async` defers mask conversion to a background job and leaves
    `segmentation_status` as "pending"; callers then poll
    /get_segmentation_status. It defaults to off so programmatic callers get a
    fully-registered datasource back from a single call.
    """
    from plexora import paths
    from plexora.server.models import data_model

    data_root = Path(data_dir).expanduser().resolve() if data_dir else paths.data_root()
    dataset_dir = data_root / name
    dataset_dir.mkdir(parents=True, exist_ok=True)
    config_path = data_root / "config.json"
    if not config_path.exists():
        write_config(config_path, {})

    image_path = _copy_if_requested(image, dataset_dir, copy)
    segmentation_path = _copy_if_requested(segmentation, dataset_dir, copy) if segmentation else None
    features_path = _copy_if_requested(features, dataset_dir, copy)

    feature_table = pl.read_csv(features_path, n_rows=1)

    # One predictor for the marker/metadata split and the column roles, shared
    # with the import UI (adapters/classify.py). Explicit arguments win over
    # its guesses -- a caller who named the coordinate columns has answered
    # already -- and anything left unset is a role nobody has established yet,
    # which is a legitimate state rather than an error. Whatever first needs
    # one asks for it (plexora/api/plugin.py's Requires).
    classified = classify_columns(
        [{"name": c, "dtype": str(dt)} for c, dt in feature_table.schema.items()]
    )
    guessed = classified["roles"]
    roles = ColumnRoles(
        cell_id=id_column or guessed.get("cell_id"),
        x=x or guessed.get("x"),
        y=y or guessed.get("y"),
        celltype=celltype_column or guessed.get("celltype"),
        image_id=guessed.get("image_id"),
    )

    named = {role: column for role, column in roles.to_dict().items()}
    missing = [c for c in named.values() if c not in feature_table.columns]
    if missing:
        raise ValueError("Missing feature column(s): " + ", ".join(sorted(missing)))
    markers = [c for c in classified["markers"] if c not in named.values()]
    metadata = [c for c in feature_table.columns if c not in markers]

    channel_info = data_model.convertOmeTiff(image_path, isLabelImg=False)
    segmentation_fields, pending_segmentation_source = _segmentation_config_fields(
        segmentation_path, dataset_dir, segmentation_async, segmentation_mode
    )

    n_channels = channel_info["num_channels"]
    if channel_names is None:
        channel_names = markers[:n_channels]
    if len(channel_names) < n_channels:
        stem = image_path.name
        channel_names = list(channel_names) + [f"{stem}_{i}" for i in range(len(channel_names), n_channels)]

    project = Project(
        name=name,
        image=_image_spec(name, image_path, channel_info, channel_names, segmentation_path),
        segmentation=_segmentation_spec(segmentation_fields),
        dataset=DataSpec(
            type="csv",
            src=str(features_path),
            roles=roles,
            columns=ColumnGroups(markers=tuple(markers), metadata=tuple(metadata)),
        ),
        created_at=_now(),
    )
    entry = project.save(data_root)

    if pending_segmentation_source:
        data_model.start_segmentation_job(
            name, pending_segmentation_source, dataset_dir,
            segmentation_fields["segmentationMode"],
        )

    return entry


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

    image_path = _copy_if_requested(image, dataset_dir, copy)
    segmentation_path = _copy_if_requested(segmentation, dataset_dir, copy) if segmentation else None

    if adata is not None:
        features_path = dataset_dir / f"{name}.h5ad"
        adata.write_h5ad(features_path)
    else:
        features_path = _copy_if_requested(features, dataset_dir, copy)

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

    spec = DataSpec(
        type="spatialdata" if table else "anndata",
        # In SpatialData mode this is the *store root*, with the chosen table
        # named alongside it, so a plugin needing the store's other elements
        # (images/labels/shapes) can open it from here.
        src=str(features_path),
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

    # Validate end-to-end (subset/coordinates/features resolve, coordinates
    # are finite, etc.) before writing anything or doing any expensive image
    # pyramid work. The resolved table also tells us the marker/metadata split
    # for free, which is why the result is kept rather than discarded: for
    # AnnData the file already draws that line (var = markers, obs = metadata),
    # so unlike CSV the user is never asked to confirm it.
    adapter_class = SpatialDataAdapter if table else AnnDataAdapter
    normalized = adapter_class(spec).load_table()
    markers = list(normalized.feature_columns)
    metadata = [c for c in normalized.table.columns if c not in set(markers)]
    spec = replace(
        spec,
        columns=ColumnGroups(markers=tuple(markers), metadata=tuple(metadata)),
        # Kept alongside the split, and not the same thing: `metadata` is what
        # the loaded table holds, while these are the file's own annotations --
        # the list a user picks from when saying which column holds the cell id
        # or the coordinates (see Project.role_columns).
        obs_columns=tuple(normalized.obs_columns),
        # Likewise: the other matrices the file carries, so the choice of which
        # one to threshold on stays changeable after import.
        layers=tuple(normalized.layers),
        # And the obsm arrays, so the coordinate source stays changeable too.
        # Without these recorded the coordinate question has nothing to offer,
        # and the importer's name-based pick is the only one there will ever be.
        obsm=tuple(normalized.obsm),
    )

    channel_info = data_model.convertOmeTiff(image_path, isLabelImg=False)
    segmentation_fields, pending_segmentation_source = _segmentation_config_fields(
        segmentation_path, dataset_dir, segmentation_async, segmentation_mode
    )

    n_channels = channel_info["num_channels"]
    if channel_names is None:
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
        image=_image_spec(name, image_path, channel_info, channel_names, segmentation_path),
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
    if not table:
        raise ValueError("`table` is required -- name which table inside the .zarr store to load.")
    return register_anndata_datasource(
        name=name,
        image=image,
        features=store,
        table=table,
        **kwargs,
    )


def register_image_datasource(name, image, channel_names=None, copy=False, data_dir=None):
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

    image_path = _copy_if_requested(image, dataset_dir, copy)

    channel_info = data_model.convertOmeTiff(image_path, isLabelImg=False)
    n_channels = channel_info["num_channels"]
    if channel_names is None:
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
        image=_image_spec(name, image_path, channel_info, channel_names, None),
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
