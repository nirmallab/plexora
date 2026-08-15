import json
import re
import shutil
from pathlib import Path

import polars as pl


def _default_marker_columns(columns, x, y, id_column, celltype_column):
    excluded = {
        x,
        y,
        id_column,
        celltype_column,
        "id",
        "Area",
        "CellID",
        "ID",
        "X Position",
        "Y Position",
        "X_centroid",
        "Y_centroid",
        "column_centroid",
        "row_centroid",
        "phenotype",
    }
    return [column for column in columns if column and column not in excluded]


def _copy_if_requested(path, target_dir, copy):
    path = Path(path).expanduser().resolve()
    if not copy:
        return path
    target = target_dir / path.name
    if path != target:
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
    from plexora import data_path

    data_root = Path(data_dir).expanduser().resolve() if data_dir else data_path
    config_path = data_root / "config.json"
    with config_path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    if name not in config:
        raise ValueError(f"No datasource named {name!r}.")

    renamable = [channel for channel in config[name]["imageData"] if channel["name"] != "Area"]
    if len(channel_names) != len(renamable):
        raise ValueError(
            f"channel_names has {len(channel_names)} entries but {name!r} has {len(renamable)} channels."
        )
    for channel, new_name in zip(renamable, channel_names):
        channel["name"] = str(new_name)
        channel["fullname"] = str(new_name)

    with config_path.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=4)

    return config[name]


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
    from plexora.server.models.adapters.anndata_adapter import _deduplicate_names

    adata = ad.read_h5ad(features_path, backed='r')
    try:
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
    finally:
        if adata.isbacked:
            adata.file.close()

    return [f"Channel {i + 1}" for i in range(n_channels)], "generic"


def register_datasource(
    name,
    image,
    features,
    x,
    y,
    segmentation=None,
    id_column="CellID",
    celltype_column=None,
    channel_names=None,
    copy=False,
    data_dir=None,
):
    """Register a dataset in Plexora's config without using the upload UI."""
    from plexora import config_json_path, data_path
    from plexora.server.models import data_model

    data_root = Path(data_dir).expanduser().resolve() if data_dir else data_path
    dataset_dir = data_root / name
    dataset_dir.mkdir(parents=True, exist_ok=True)
    config_path = data_root / "config.json"
    if not config_path.exists():
        config_path.write_text("{}", encoding="utf-8")

    image_path = _copy_if_requested(image, dataset_dir, copy)
    segmentation_path = _copy_if_requested(segmentation, dataset_dir, copy) if segmentation else None
    features_path = _copy_if_requested(features, dataset_dir, copy)

    feature_table = pl.read_csv(features_path, n_rows=1)
    missing = [column for column in [x, y, id_column] if column not in feature_table.columns]
    if missing:
        raise ValueError("Missing required feature columns: " + ", ".join(missing))
    if celltype_column and celltype_column not in feature_table.columns:
        raise ValueError(f"Missing celltype column: {celltype_column}")

    channel_info = data_model.convertOmeTiff(image_path, isLabelImg=False)
    label_info = None
    if segmentation_path:
        label_info = data_model.convertOmeTiff(
            segmentation_path,
            channelFilePath=image_path,
            dataDirectory=str(dataset_dir),
            isLabelImg=True,
        )

    n_channels = channel_info["num_channels"]
    if channel_names is None:
        marker_columns = _default_marker_columns(feature_table.columns, x, y, id_column, celltype_column)
        channel_names = marker_columns[:n_channels]
    if len(channel_names) < n_channels:
        stem = image_path.name
        channel_names = list(channel_names) + [f"{stem}_{i}" for i in range(len(channel_names), n_channels)]

    with config_path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)

    feature_data = {
        "src": str(features_path),
        "normalization": "none",
        "isTransformed": False,
        "xCoordinate": x,
        "yCoordinate": y,
        "idField": id_column,
    }
    if celltype_column:
        feature_data["celltype"] = celltype_column

    image_data = []
    if segmentation_path:
        label_name = _segmentation_channel_name(segmentation_path)
        image_data.append(
            {
                "name": "Area",
                "fullname": "Area",
                "src": f"/generated/data/{name}/{label_name}/",
            }
        )
    generated_channel_names = channel_info["channel_names"]
    for idx in range(n_channels):
        display_name = str(channel_names[idx])
        image_data.append(
            {
                "name": display_name,
                "fullname": display_name,
                "src": f"/generated/data/{name}/{generated_channel_names[idx]}/",
            }
        )

    config[name] = {
        "shapes": "",
        "activeChannel": "",
        "featureData": [feature_data],
        "imageData": image_data,
        "height": channel_info["height"],
        "width": channel_info["width"],
        "maxLevel": channel_info["maxLevel"],
        "num_channels": channel_info["num_channels"],
        "tileHeight": channel_info["tileHeight"],
        "tileWidth": channel_info["tileWidth"],
        "segmentation": label_info["segmentation"] if label_info else None,
        "channelFile": str(image_path),
        "has_feature_data": True,
    }

    with config_path.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=4)

    return config[name]


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
    """
    from plexora import data_path
    from plexora.server.models import data_model
    from plexora.server.models.adapters.anndata_adapter import AnnDataAdapter

    if (adata is None) == (features is None):
        raise ValueError("Provide exactly one of `adata` (in-memory) or `features` (.h5ad path)")

    data_root = Path(data_dir).expanduser().resolve() if data_dir else data_path
    dataset_dir = data_root / name
    dataset_dir.mkdir(parents=True, exist_ok=True)
    config_path = data_root / "config.json"
    if not config_path.exists():
        config_path.write_text("{}", encoding="utf-8")

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

    feature_data = {
        "src": str(features_path),
        "normalization": "none",
        # True only when apply_log_transform is explicitly requested below --
        # no heuristic guessing at whether the chosen feature source "looks"
        # already transformed. This also gates whether the gate slider/
        # auto-gate keep float precision or round to whole numbers, so an
        # incorrect guess here would silently destroy narrow-range gates
        # (e.g. rounding a real [1.85, 2.23] gate to [1, 3] matches nearly
        # every cell) -- the user's call, every time.
        "isTransformed": bool(apply_log_transform),
        "xCoordinate": "X",
        "yCoordinate": "Y",
        # Defaults to the adapter's own positional "id" column (0..n-1,
        # always int -- matches NormalizedDatasource.id_column), not
        # DEFAULT_ID_COLUMN ("obs_id"). idField has to be uint32-castable:
        # get_all_cells() packs [idField, X, Y] into one flat array and
        # casts the whole thing to uint32 for the fast binary cell-loading
        # path (numericData.js), which crashes if idField holds adata.obs_names
        # strings -- the common case, since those are rarely small integers.
        # An explicit obs_id_field is still honored as-is; a non-numeric
        # choice there is the caller's informed tradeoff, not a silent default.
        "idField": obs_id_field or "id",
        "dataSource": {
            "format": "anndata",
            "path": str(features_path),
            "coordinates": coordinates_config,
            "features": features_config,
            "obs_id_field": obs_id_field,
            "subset": subset_config,
            "apply_log_transform": bool(apply_log_transform),
        },
    }
    if celltype_column:
        feature_data["celltype"] = celltype_column

    # Validate end-to-end (subset/coordinates/features resolve, coordinates
    # are finite, etc.) before touching config.json or doing any expensive
    # image pyramid work.
    AnnDataAdapter(feature_data).load_table()

    channel_info = data_model.convertOmeTiff(image_path, isLabelImg=False)
    label_info = None
    if segmentation_path:
        label_info = data_model.convertOmeTiff(
            segmentation_path,
            channelFilePath=image_path,
            dataDirectory=str(dataset_dir),
            isLabelImg=True,
        )

    n_channels = channel_info["num_channels"]
    if channel_names is None:
        channel_names, _ = derive_anndata_channel_names(image_path, features_path, n_channels)
    elif len(channel_names) != n_channels:
        raise ValueError(
            f"channel_names has {len(channel_names)} entries but the image has {n_channels} channels."
        )

    with config_path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)

    image_data = []
    if segmentation_path:
        label_name = _segmentation_channel_name(segmentation_path)
        image_data.append(
            {
                "name": "Area",
                "fullname": "Area",
                "src": f"/generated/data/{name}/{label_name}/",
            }
        )
    generated_channel_names = channel_info["channel_names"]
    for idx in range(n_channels):
        display_name = str(channel_names[idx])
        image_data.append(
            {
                "name": display_name,
                "fullname": display_name,
                "src": f"/generated/data/{name}/{generated_channel_names[idx]}/",
            }
        )

    config[name] = {
        "shapes": "",
        "activeChannel": "",
        "data_type": "anndata",
        "featureData": [feature_data],
        "imageData": image_data,
        "height": channel_info["height"],
        "width": channel_info["width"],
        "maxLevel": channel_info["maxLevel"],
        "num_channels": channel_info["num_channels"],
        "tileHeight": channel_info["tileHeight"],
        "tileWidth": channel_info["tileWidth"],
        "segmentation": label_info["segmentation"] if label_info else None,
        "channelFile": str(image_path),
        "has_feature_data": True,
    }

    with config_path.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=4)

    return config[name]


def register_image_datasource(name, image, channel_names=None, copy=False, data_dir=None):
    """Register a datasource from just an OME-TIFF/TIFF image -- no feature
    table, no segmentation. Used by the quick-view landing page for a fast
    first look. has_feature_data=False and an empty featureData list mark
    this as a first-class no-feature-data datasource -- load_datasource(),
    load_ball_tree(), and every direct consumer of the feature table/ball
    tree branch on this flag instead of requiring a real (or synthesized)
    feature CSV to exist on disk.
    """
    from plexora import config_json_path, data_path
    from plexora.server.models import data_model

    data_root = Path(data_dir).expanduser().resolve() if data_dir else data_path
    dataset_dir = data_root / name
    dataset_dir.mkdir(parents=True, exist_ok=True)
    config_path = data_root / "config.json"
    if not config_path.exists():
        config_path.write_text("{}", encoding="utf-8")

    image_path = _copy_if_requested(image, dataset_dir, copy)

    channel_info = data_model.convertOmeTiff(image_path, isLabelImg=False)
    n_channels = channel_info["num_channels"]
    if channel_names is None:
        channel_names, _ = derive_image_channel_names(image_path, n_channels)
    elif len(channel_names) != n_channels:
        raise ValueError(
            f"channel_names has {len(channel_names)} entries but the image has {n_channels} channels."
        )

    with config_path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)

    generated_channel_names = channel_info["channel_names"]
    image_data = []
    for idx in range(n_channels):
        display_name = str(channel_names[idx])
        image_data.append(
            {
                "name": display_name,
                "fullname": display_name,
                "src": f"/generated/data/{name}/{generated_channel_names[idx]}/",
            }
        )

    config[name] = {
        "shapes": "",
        "activeChannel": "",
        "image_kind": "ome_tiff",
        # No feature table was provided for this quick-view datasource --
        # has_feature_data=False and an empty featureData list are the
        # explicit, first-class "no feature data" state that
        # load_datasource()/load_ball_tree() and every direct consumer of
        # the feature table/ball tree in data_model.py branch on. The Tools
        # navbar dropdown (tool_routes.py's open_tool()) also reads this flag
        # to redirect to the "attach data" upload flow instead of opening a
        # tool directly.
        "has_feature_data": False,
        "featureData": [],
        "imageData": image_data,
        "height": channel_info["height"],
        "width": channel_info["width"],
        "maxLevel": channel_info["maxLevel"],
        "num_channels": channel_info["num_channels"],
        "tileHeight": channel_info["tileHeight"],
        "tileWidth": channel_info["tileWidth"],
        "segmentation": None,
        "channelFile": str(image_path),
    }

    with config_path.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=4)

    return config[name]


def register_rgb_datasource(name, image, copy=False, data_dir=None):
    """Register a datasource from a flat RGB image (PNG/JPEG) -- the
    minimal quick-view path: view-only, no channels, no gating. Displayed
    client-side via OpenSeadragon's native single-image tile source (see
    RgbImageViewer), served whole by GET /generated/rgb/<name>, not tiled.
    """
    from PIL import Image

    from plexora import config_json_path, data_path

    data_root = Path(data_dir).expanduser().resolve() if data_dir else data_path
    dataset_dir = data_root / name
    dataset_dir.mkdir(parents=True, exist_ok=True)
    config_path = data_root / "config.json"
    if not config_path.exists():
        config_path.write_text("{}", encoding="utf-8")

    image_path = _copy_if_requested(image, dataset_dir, copy)
    with Image.open(image_path) as img:
        width, height = img.size

    with config_path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)

    config[name] = {
        "shapes": "",
        "activeChannel": "",
        "image_kind": "rgb",
        # See the matching comment in register_image_datasource -- moot today since
        # RGB datasources never show the Tools dropdown at all, kept for consistency.
        "has_feature_data": False,
        "featureData": [],
        "imageData": [],
        "height": height,
        "width": width,
        "num_channels": 0,
        "segmentation": None,
        "channelFile": str(image_path),
    }

    with config_path.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=4)

    return config[name]
