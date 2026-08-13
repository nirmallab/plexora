from sklearn.neighbors import BallTree
from sklearn.preprocessing import MinMaxScaler
import numpy as np
import polars as pl
import polars.selectors as cs
import json
import os
import io
from pathlib import Path
from pathlib import PurePath
from ome_types import from_xml
from plexora import config_json_path, data_path, cwd_path
from plexora.server.utils import pyramid_assemble, pyramid_upgrade
from plexora.server.utils import fast_png
from plexora.server.models.adapters import get_adapter
from plexora.server.models import database_model, centroid_tiles
from plexora.server.utils import smallestenclosingcircle
from PIL import Image
import matplotlib.path as mpltPath
from itertools import chain
import dateutil.parser
import time
import pickle
import tifffile as tf
import re
import threading
import zarr
import cv2
from sklearn.mixture import GaussianMixture
from scipy.stats import norm
from skimage.measure import block_reduce
from skimage.transform import resize

ball_tree = None
database = None
source = None
config = None
seg = None
zarray = None
channels = None
metadata = None
load_lock = threading.RLock()

# Cache of derived, expensive-to-recompute results, keyed off the currently
# loaded datasource. Cleared whenever load_datasource actually (re)loads data,
# since these caches were only ever valid for the previously loaded content.
_gmm_cache = {}
_description_cache = {}
_gate_filter_cache = {}
# Incremented each time load_datasource actually (re)loads data, so other
# modules can key a cache off "which load is this" without importing this
# module's internal cache dicts directly.
load_generation = 0


def _zarr_level(group, level):
    if isinstance(group, zarr.Array):
        return group
    return group[str(level)]


def _zarr_levels(group):
    if isinstance(group, zarr.Array):
        return [group]
    return [group[str(i)] for i in range(len(group))]


def _sample_segmentation_array(level):
    shape = level.shape[-2:]
    max_sample_dim = 1536
    step = max(1, int(np.ceil(max(shape) / max_sample_dim)))
    sample = level[::step, ::step]
    return np.asarray(sample)


def _looks_like_outline_mask(segmentation_path):
    if str(segmentation_path).endswith('.zarr'):
        group = zarr.open(segmentation_path)
        sample = _sample_segmentation_array(_zarr_levels(group)[0])
    else:
        with tf.TiffFile(str(segmentation_path), is_ome=False) as seg_io:
            group = zarr.open(seg_io.series[0].aszarr())
            sample = _sample_segmentation_array(_zarr_levels(group)[0])
    if sample.ndim != 2 or sample.size == 0:
        return False

    nonzero = sample != 0
    nonzero_count = int(np.count_nonzero(nonzero))
    if nonzero_count == 0:
        return False

    density = nonzero_count / sample.size
    center = nonzero[1:-1, 1:-1]
    if center.size == 0:
        return density < 0.25

    same_id_interior = (
        center
        & (sample[1:-1, 1:-1] == sample[:-2, 1:-1])
        & (sample[1:-1, 1:-1] == sample[2:, 1:-1])
        & (sample[1:-1, 1:-1] == sample[1:-1, :-2])
        & (sample[1:-1, 1:-1] == sample[1:-1, 2:])
    )
    interior_fraction = int(np.count_nonzero(same_id_interior)) / max(1, int(np.count_nonzero(center)))
    return density <= 0.20 and interior_fraction <= 0.05


def _outline_level(labels):
    labels = np.asarray(labels)
    outline = np.zeros(labels.shape, dtype=labels.dtype)
    nonzero = labels != 0
    edge = np.zeros(labels.shape, dtype=bool)
    edge[0, :] = nonzero[0, :]
    edge[-1, :] = nonzero[-1, :]
    edge[:, 0] = edge[:, 0] | nonzero[:, 0]
    edge[:, -1] = edge[:, -1] | nonzero[:, -1]
    edge[1:, :] = edge[1:, :] | (nonzero[1:, :] & (labels[1:, :] != labels[:-1, :]))
    edge[:-1, :] = edge[:-1, :] | (nonzero[:-1, :] & (labels[:-1, :] != labels[1:, :]))
    edge[:, 1:] = edge[:, 1:] | (nonzero[:, 1:] & (labels[:, 1:] != labels[:, :-1]))
    edge[:, :-1] = edge[:, :-1] | (nonzero[:, :-1] & (labels[:, :-1] != labels[:, 1:]))
    outline[edge] = labels[edge]
    return outline


def _downsample_labels_nearest(labels):
    output_shape = tuple(max(1, int(np.ceil(dim / 2))) for dim in labels.shape)
    return resize(
        labels,
        output_shape,
        order=0,
        preserve_range=True,
        anti_aliasing=False,
    ).astype(labels.dtype, copy=False)


def _outline_output_path(segmentation_path, dataDirectory=None):
    source_path = Path(segmentation_path)
    target_dir = Path(dataDirectory) if dataDirectory else source_path.parent
    suffix = ".fast-outlines.pyramid.ome.tiff"
    stem = re.sub(r'\.ome\.tiff|\.ome\.tif|\.tiff|\.tif|\.png|\.zarr', '', source_path.name)
    return target_dir / f"{stem}{suffix}"


def ensure_outline_segmentation(segmentation_path, dataDirectory=None):
    output_path = _outline_output_path(segmentation_path, dataDirectory)
    if _looks_like_outline_mask(segmentation_path):
        return str(segmentation_path)
    if output_path.exists() and _looks_like_outline_mask(output_path):
        return str(output_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    arrays = []
    if str(segmentation_path).endswith('.zarr'):
        group = zarr.open(segmentation_path)
        levels = _zarr_levels(group)
        if len(levels) > 1:
            arrays = [_outline_level(np.asarray(level)) for level in levels]
        else:
            labels = np.asarray(levels[0])
            while True:
                arrays.append(_outline_level(labels))
                if min(labels.shape) <= 256:
                    break
                labels = _downsample_labels_nearest(labels)
    else:
        with tf.TiffFile(str(segmentation_path), is_ome=False) as seg_io:
            group = zarr.open(seg_io.series[0].aszarr())
            levels = _zarr_levels(group)
            if len(levels) > 1:
                arrays = [_outline_level(np.asarray(level)) for level in levels]
            else:
                labels = np.asarray(levels[0])
                while True:
                    arrays.append(_outline_level(labels))
                    if min(labels.shape) <= 256:
                        break
                    labels = _downsample_labels_nearest(labels)

    with tf.TiffWriter(str(output_path), bigtiff=True) as writer:
        writer.write(
            arrays[0],
            subifds=max(0, len(arrays) - 1),
            photometric='minisblack',
            metadata={'axes': 'YX'},
            compression='zlib',
        )
        for array in arrays[1:]:
            writer.write(
                array,
                subfiletype=1,
                photometric='minisblack',
                compression='zlib',
            )

    return str(output_path)


def init(datasource_name):
    load_ball_tree(datasource_name)


# Read-only accessors onto this module's currently-loaded-datasource state.
# Feature modules (server/modules/gating, and any future module) should go
# through these rather than reading data_model.datasource/.config/etc.
# directly -- same values today, but it keeps module code from depending on
# this module's private global names surviving a future rewrite (see
# SKILL.md's "Known Performance Hot Spots" for why that rewrite is a
# deliberately deferred, separate task, not something to block on here).
def get_current_datasource_name():
    return source


def get_datasource_df():
    return datasource


def get_current_config():
    return config


def get_current_ball_tree():
    return ball_tree


def get_current_zarray():
    return zarray


def get_current_channels():
    return channels


def gmm_cache_get_or_set(key, compute_fn):
    """Shared entry point into this module's _gmm_cache for feature modules
    (e.g. gating's per-selection GMM) that want the same warm/invalidate-on-
    reload behavior as this module's own GMM caching, without reaching into
    _gmm_cache directly -- this module keeps sole invalidation authority
    (load_datasource() clears it on every reload)."""
    if key in _gmm_cache:
        return _gmm_cache[key]
    value = compute_fn()
    _gmm_cache[key] = value
    return value


def load_datasource(datasource_name, reload=False):
    global datasource
    global source
    global config
    global seg
    global zarray
    global channels
    global metadata
    global load_generation
    with load_lock:
        if source == datasource_name and datasource is not None and channels is not None and reload is False:
            return
        load_config(datasource_name)
        if reload:
            load_ball_tree(datasource_name, reload=reload)
        data_type = config[datasource_name].get('data_type', 'csv')
        adapter = get_adapter(data_type)(config[datasource_name]['featureData'][0])
        print("Loading datasource data.. (this can take some time)")
        loaded_datasource = adapter.load_table().table
        print("Loading segmentation.")
        segmentation_path = config[datasource_name].get('segmentation')
        if not segmentation_path:
            loaded_seg = None
        elif str(segmentation_path).endswith('.zarr'):
            loaded_seg = zarr.open(segmentation_path)
        else:
            seg_io = tf.TiffFile(segmentation_path, is_ome=False)
            loaded_seg = zarr.open(seg_io.series[0].aszarr())
        channel_io = tf.TiffFile(config[datasource_name]['channelFile'], is_ome=False)
        print("Loading image descriptions.")
        try:
            xml = channel_io.pages[0].tags['ImageDescription'].value
            loaded_metadata = from_xml(xml).images[0].pixels
        except:
            loaded_metadata = {}
        loaded_channels = zarr.open(channel_io.series[0].aszarr())

        level_series = next(
            level for level in reversed(channel_io.series[0].levels)
            if all(d >= 200 for d in level.shape[1:])
        )
        loaded_zarray = zarr.open(level_series.aszarr())
        if loaded_zarray.shape[1] > 400 or loaded_zarray.shape[2] > 400:
            x_reduce = loaded_zarray.shape[1] // 200
            y_reduce = loaded_zarray.shape[2] // 200
            reduce = np.min([x_reduce, y_reduce])
            # block_reduce needs a real strided numpy array -- loaded_zarray
            # here is a lazy zarr.Array, which has no .strides. This is
            # already the smallest pyramid level with both dims >= 200, so
            # materializing it is bounded regardless of the source image's
            # full resolution.
            loaded_zarray = block_reduce(np.asarray(loaded_zarray), (1, reduce, reduce), np.mean)

        datasource = loaded_datasource
        seg = loaded_seg
        channels = loaded_channels
        zarray = loaded_zarray
        metadata = loaded_metadata
        source = datasource_name
        # Data on disk just changed underneath us (first load or explicit
        # reload) -- any cached GMM/description results are now stale.
        _gmm_cache.clear()
        _description_cache.clear()
        _gate_filter_cache.clear()
        # Bumped so downstream tile-byte caches (keyed on this) know to
        # treat previously cached tiles as stale without needing a direct
        # reference back into this module's caches.
        load_generation += 1
        print("Data loading done.")

    # Warm the description/GMM caches in the background so the first real
    # request after this load doesn't pay for them synchronously.
    threading.Thread(
        target=_warm_datasource_caches, args=(datasource_name,), daemon=True
    ).start()


def load_config(datasource_name):
    global config

    with open(config_json_path, "r+") as configJson:
        config = json.load(configJson)
        updated = False
        # Update Feature SRC
        original = config[datasource_name]['featureData'][0]['src']
        config[datasource_name]['featureData'][0]['src'] = original.replace('static/data', 'plexora/data')
        csvPath = config[datasource_name]['featureData'][0]['src']
        if Path(csvPath).exists() is False:
            if Path('.' + csvPath).exists():
                csvPath = '.' + csvPath
        config[datasource_name]['featureData'][0]['src'] = str(Path(csvPath))
        if original != config[datasource_name]['featureData'][0]['src']:
            updated = True

        segmentation_path = config[datasource_name].get('segmentation')
        if segmentation_path:
            updated_path = segmentation_path.replace('static/data', 'plexora/data')
            updated_path = ensure_outline_segmentation(updated_path, data_path / datasource_name)
            if updated_path != segmentation_path:
                config[datasource_name]['segmentation'] = updated_path
                updated = True

        if updated:
            configJson.seek(0)  # <--- should reset file position to the beginning.
            json.dump(config, configJson, indent=4)
            configJson.truncate()


def _ball_tree_source_signature(csv_path):
    stat = csv_path.stat()
    return {
        "csv_size": stat.st_size,
        "csv_mtime_ns": stat.st_mtime_ns,
    }


def load_ball_tree(datasource_name_name, reload=False):
    global ball_tree
    global datasource
    global config
    if datasource_name_name != source:
        load_datasource(datasource_name_name)

    pickled_kd_tree_path = str(
        PurePath(cwd_path, data_path, datasource_name_name, "ball_tree.pickle"))

    csvPath = Path(config[datasource_name_name]['featureData'][0]['src'])
    signature = _ball_tree_source_signature(csvPath)

    if Path(pickled_kd_tree_path).is_file() and reload is False:
        print("Pickled KD Tree Exists, Loading")
        try:
            with open(pickled_kd_tree_path, "rb") as tree_file:
                cached = pickle.load(tree_file)
            if isinstance(cached, dict) and cached.get('signature') == signature:
                ball_tree = cached['tree']
                print("Pickled KD Tree Loaded.")
                return
            print("Pickled KD Tree is stale (source CSV changed), rebuilding.")
        except Exception as exc:
            print(f"Could not load pickled KD Tree, rebuilding: {exc}")

    print("Creating KD Tree.")
    xCoordinate = config[datasource_name_name]['featureData'][0]['xCoordinate']
    yCoordinate = config[datasource_name_name]['featureData'][0]['yCoordinate']
    # Reuse the feature table load_datasource already parsed instead of
    # re-reading the (potentially multi-million-row) CSV from disk again.
    points = datasource.select([xCoordinate, yCoordinate]).to_numpy()
    ball_tree = BallTree(points, metric='euclidean')
    with open(pickled_kd_tree_path, 'wb') as tree_file:
        pickle.dump({'signature': signature, 'tree': ball_tree}, tree_file)
    print('Creating KD Tree done.')


def _ensure_loaded(datasource_name):
    """Ensure the CSV/BallTree for datasource_name is the currently loaded one."""
    if datasource_name != source:
        load_ball_tree(datasource_name)


_warmup_locks = {}
_warmup_locks_guard = threading.Lock()


def _warmup_lock_for(datasource_name):
    with _warmup_locks_guard:
        if datasource_name not in _warmup_locks:
            _warmup_locks[datasource_name] = threading.Lock()
        return _warmup_locks[datasource_name]


def _warm_datasource_caches(datasource_name):
    """Pre-populate description/GMM caches in the background so the first
    real request after a datasource load doesn't pay for them synchronously.
    Best-effort only: if a concurrent switch to a different datasource races
    this, the _ensure_loaded() calls inside will just reload as needed.
    """
    lock = _warmup_lock_for(datasource_name)
    if not lock.acquire(blocking=False):
        return
    try:
        get_datasource_description(datasource_name)
        for channel in config[datasource_name]['imageData']:
            if channel['name'] != 'Area':
                get_channel_gmm(channel['fullname'], datasource_name)
    except Exception as exc:
        print(f"Background cache warmup failed for {datasource_name}: {exc}")
    finally:
        lock.release()


def query_for_closest_cell(x, y, datasource_name):
    global datasource
    global source
    global ball_tree
    _ensure_loaded(datasource_name)
    distance, index = ball_tree.query([[x, y]], k=1)
    if distance == np.inf:
        return {}
    #         Nothing found
    else:
        try:
            row = datasource[index[0].tolist()]
            obj = row.to_dicts()[0]
            if 'celltype' not in obj:
                obj['celltype'] = ''
            return obj
        except:
            return {}


def get_row(row, datasource_name):
    global database
    global source
    global ball_tree
    _ensure_loaded(datasource_name)
    obj = database.loc[[row]].to_dict(orient='records')[0]
    obj['id'] = row
    return obj


def get_channel_names(datasource_name, shortnames=True):
    global datasource
    global source
    _ensure_loaded(datasource_name)
    # imageData[0] is only the "Area" placeholder when segmentation was
    # registered -- without it, index 0 is a real channel. Filter by name
    # instead of slicing [1:], which silently dropped the first real
    # channel for segmentation-less datasources.
    real_channels = [channel for channel in config[datasource_name]['imageData'] if channel['fullname'] != 'Area']
    key = 'name' if shortnames else 'fullname'
    return [channel[key] for channel in real_channels]


def get_filter_columns(datasource_name, columns):
    """Numeric numpy views of the requested columns pulled from the
    already-loaded datasource, cached (one entry at a time, like
    centroid_tiles._load_filter_table) so repeated range-filter queries on
    the same columns reuse the same arrays instead of re-deriving them per
    request. Shared core primitive -- used directly by get_channel_cells
    below, and by the gating module's own queries (server/modules/gating/
    model.py) via this same function, not a private copy.
    """
    key = (datasource_name, tuple(sorted(set(columns))))
    cached = _gate_filter_cache.get(key)
    if cached is not None:
        return cached
    cols = {
        c: datasource[c].cast(pl.Float32, strict=False).fill_null(float('nan')).to_numpy()
        for c in columns
    }
    _gate_filter_cache.clear()
    _gate_filter_cache[key] = cols
    return cols


def apply_range_mask(columns, gates, mode='and'):
    n = len(next(iter(columns.values()))) if columns else 0
    keep = np.ones(n, dtype=bool) if mode == 'and' else np.zeros(n, dtype=bool)
    for key, value in gates.items():
        if key not in columns:
            continue
        low, high = float(value[0]), float(value[1])
        match = (columns[key] > low) & (columns[key] < high)
        if mode == 'and':
            keep &= match
        else:
            keep |= match
    return keep


def get_channel_cells(datasource_name, channels):
    global datasource

    _ensure_loaded(datasource_name)

    if not channels:
        return []

    gate_range = (0, 65536)
    columns = get_filter_columns(datasource_name, channels)
    keep = apply_range_mask(columns, {c: gate_range for c in channels}, mode='and')
    ids = datasource['id'].to_numpy()[keep].tolist()
    return [{'id': v} for v in ids]


def get_phenotype_description(datasource):
    try:
        data = ''
        csvPath = config[datasource]['featureData'][0]['celltypeData']
        if Path(csvPath).is_file():
        #old os.path usage: if os.path.isfile(csvPath):
            data = pl.read_csv(csvPath)
            data = data.to_numpy().tolist()
            # data = data.to_json(orient='records', lines=True)
        return data;
    except KeyError:
        return ''
    except TypeError:
        return ''


def get_phenotype_column_name(datasource):
    try:
        return config[datasource]['featureData'][0]['celltype']
    except KeyError:
        return ''
    except TypeError:
        return ''


def get_cells_phenotype(datasource_name):
    global datasource
    global source
    global ball_tree

    range = [0, 65536]

    # Load if not loaded
    _ensure_loaded(datasource_name)

    try:
        phenotype_field = config[datasource_name]['featureData'][0]['celltype']
    except KeyError:
        phenotype_field = 'celltype'
    except TypeError:
        phenotype_field = 'celltype'

    query = datasource.select(['id', phenotype_field]).to_dicts()
    return query


def get_all_cells(datasource_name, start_keys, data_type=float):
    global datasource
    global source

    # Load if not loaded
    _ensure_loaded(datasource_name)

    query = datasource.select(start_keys).to_numpy().flatten()
    if np.issubdtype(data_type, int):
        return query.astype(np.uint32)
    return query.astype(np.float32)


def get_centroid_manifest(datasource_name):
    global config
    if config is None or datasource_name not in config:
        load_config(datasource_name)
    return centroid_tiles.get_manifest(config, datasource_name, build=True)


def get_centroid_tiles(datasource_name, level, tiles, gates=None, max_points=None):
    global config
    if config is None or datasource_name not in config:
        load_config(datasource_name)
    return centroid_tiles.get_tiles(config, datasource_name, level, tiles, gates or {}, max_points)


def download_channels(datasource_name, map_channels, active_channels, list_colors, list_ranges, list_channels):
    global datasource
    global source
    global ball_tree

    # Load if not loaded
    _ensure_loaded(datasource_name)
    rows = []
    for channel in map_channels:
        channel_name = map_channels[channel]
        rows.append([channel_name, list_channels[channel_name][0], list_channels[channel_name][1], 255, 255, 255, 1, False])
    csv = pl.DataFrame(rows, schema=['channel', 'start', 'end', 'r', 'g', 'b', 'opacity', 'channel_active'], orient='row')

    schema = csv.schema
    for channel in list_colors:
        is_channel = pl.col('channel') == map_channels[channel]
        color = list_colors[channel]['color']
        csv = csv.with_columns([
            pl.when(is_channel).then(pl.lit(color['r']).cast(schema['r'])).otherwise(pl.col('r')).alias('r'),
            pl.when(is_channel).then(pl.lit(color['g']).cast(schema['g'])).otherwise(pl.col('g')).alias('g'),
            pl.when(is_channel).then(pl.lit(color['b']).cast(schema['b'])).otherwise(pl.col('b')).alias('b'),
            pl.when(is_channel).then(pl.lit(color['opacity']).cast(schema['opacity'])).otherwise(pl.col('opacity')).alias('opacity'),
        ])
    for channel in active_channels:
        is_channel = pl.col('channel') == map_channels[channel]
        csv = csv.with_columns(
            pl.when(is_channel).then(pl.lit(True)).otherwise(pl.col('channel_active')).alias('channel_active')
        )

    return csv


def save_channel_list(datasource_name, map_channels, active_channels, list_colors, list_ranges, list_channels):
    global datasource
    global source
    global ball_tree

    # Load if not loaded
    _ensure_loaded(datasource_name)
    rows = []
    for channel in map_channels:
        channel_name = map_channels[channel]
        rows.append([channel_name, list_channels[channel_name][0], list_channels[channel_name][1], 255, 255, 255, 1, False])
    csv = pl.DataFrame(rows, schema=['channel', 'start', 'end', 'r', 'g', 'b', 'opacity', 'channel_active'], orient='row')

    schema = csv.schema
    for channel in list_colors:
        is_channel = pl.col('channel') == map_channels[channel]
        color = list_colors[channel]['color']
        csv = csv.with_columns([
            pl.when(is_channel).then(pl.lit(color['r']).cast(schema['r'])).otherwise(pl.col('r')).alias('r'),
            pl.when(is_channel).then(pl.lit(color['g']).cast(schema['g'])).otherwise(pl.col('g')).alias('g'),
            pl.when(is_channel).then(pl.lit(color['b']).cast(schema['b'])).otherwise(pl.col('b')).alias('b'),
            pl.when(is_channel).then(pl.lit(color['opacity']).cast(schema['opacity'])).otherwise(pl.col('opacity')).alias('opacity'),
        ])
    for channel in active_channels:
        is_channel = pl.col('channel') == map_channels[channel]
        csv = csv.with_columns(
            pl.when(is_channel).then(pl.lit(True)).otherwise(pl.col('channel_active')).alias('channel_active')
        )

    temp = csv.to_dicts()
    f = pickle.dumps(temp, protocol=4)
    database_model.save_list(database_model.ChannelList, datasource=datasource_name, cells=f)




def get_saved_channel_list(datasource_name):
    channel_list = database_model.get(database_model.ChannelList, datasource=datasource_name)
    if channel_list is None:
        return None
    return pickle.loads(channel_list.cells)


def _describe_numeric(df):
    """Vectorized equivalent of df.describe().to_dict() for numeric columns.
    Avoids pandas' per-column describe() loop, which is slow at millions of
    rows across dozens of columns.
    """
    numeric_df = df.select(cs.numeric())
    values = numeric_df.cast(pl.Float64).to_numpy()
    count = np.sum(~np.isnan(values), axis=0)
    mean = np.nanmean(values, axis=0)
    std = np.nanstd(values, axis=0, ddof=1)
    minimum = np.nanmin(values, axis=0)
    maximum = np.nanmax(values, axis=0)
    q25, q50, q75 = np.nanpercentile(values, [25, 50, 75], axis=0)
    description = {}
    for i, column in enumerate(numeric_df.columns):
        description[column] = {
            'count': count[i],
            'mean': mean[i],
            'std': std[i],
            'min': minimum[i],
            '25%': q25[i],
            '50%': q50[i],
            '75%': q75[i],
            'max': maximum[i],
        }
    return description



def get_datasource_description(datasource_name):
    global datasource
    global source
    global ball_tree
    global config

    # Load if not loaded
    _ensure_loaded(datasource_name)

    if datasource_name in _description_cache:
        return _description_cache[datasource_name]

    description = _describe_numeric(datasource)
    for column in description:
        column_data = datasource[column].to_numpy()
        [hist, bin_edges] = np.histogram(column_data[~np.isnan(column_data)], bins=50, density=True)
        midpoints = (bin_edges[1:] + bin_edges[:-1]) / 2
        description[column]['histogram'] = {}
        dat = []
        for i in range(len(hist)):
            obj = {}
            obj['x'] = midpoints[i]
            obj['y'] = hist[i]
            dat.append(obj)
        description[column]['histogram'] = dat

    list_channels = config[datasource_name]['imageData']
    image_layer = 0
    for channel in list_channels:
        if channel['name'] != 'Area':
            fullName = channel['fullname']
            if fullName not in description:
                # No feature-table column matches this image channel's
                # display name -- happens whenever the image has more
                # channels than the feature table has markers (a synthetic
                # "<file>_<i>" fallback name gets used for the extra
                # channels in register_datasource()/register_anndata_
                # datasource() alike). There's no marker-expression data to
                # describe, but image_min/image_max/image_histogram below are
                # pure pixel statistics computed straight from the image
                # array -- they don't need a feature column, and the channel
                # list UI (channelList.js) reads them for every image
                # channel unconditionally, so this entry must still exist.
                description[fullName] = {}

            image_data = zarray[image_layer]
            img_log = np.log(image_data[image_data > 0])
            [hist, bin_edges] = np.histogram(img_log.flatten(), bins=50, density=True)
            midpoints = (bin_edges[1:] + bin_edges[:-1]) / 2
            description[fullName]['image_histogram'] = {}

            dat = []
            for i in range(len(hist)):
                obj = {}
                obj['x'] = midpoints[i]
                obj['y'] = hist[i]
                dat.append(obj)

            description[fullName]['image_histogram'] = dat
            description[fullName]['image_min'] = np.ceil(np.exp(np.min(img_log)))
            description[fullName]['image_max'] = np.ceil(np.exp(np.max(img_log)))

            image_layer += 1
        else:
            continue

    _description_cache[datasource_name] = description
    return description


def get_channel_gmm(channel_name, datasource_name):
    global datasource
    global source
    global ball_tree
    global config

    # Load if not loaded
    _ensure_loaded(datasource_name)

    cache_key = (datasource_name, channel_name)
    if cache_key in _gmm_cache:
        return _gmm_cache[cache_key]

    packet_gmm = {}

    # zarray only ever holds the real image channels (Area is a Plexora-side
    # UI placeholder, never part of the physical image) -- so the correct
    # zarray index is the channel's position among imageData entries with
    # fullname != 'Area', not its raw imageData index minus a hardcoded 1
    # (which was only correct when segmentation put Area at position 0).
    real_channels = [d for d in config[datasource_name]['imageData'] if d['fullname'] != 'Area']
    image_channelIdx = next(index for (index, d) in enumerate(real_channels) if d["fullname"] == channel_name)
    image_data = zarray[image_channelIdx]
    nonzero = image_data[image_data > 0]
    img_log = np.log(nonzero)
    gmm = GaussianMixture(3, max_iter=1000, tol=1e-6)
    gmm.fit(img_log.reshape((-1, 1)))

    means = gmm.means_[:, 0]
    i0, i1, i2 = np.argsort(means)
    mean1, mean2 = means[[i1, i2]]
    std1, std2 = gmm.covariances_[[i1, i2], 0, 0] ** 0.5

    x = np.linspace(mean1, mean2, 50)
    y1 = norm(mean1, std1).pdf(x) * gmm.weights_[i1]
    y2 = norm(mean2, std2).pdf(x) * gmm.weights_[i2]

    lmax = mean2 + 2 * std2
    lmin = x[np.argmin(np.abs(y1 - y2))]
    if lmin >= mean2:
        lmin = mean2 - 2 * std2
    vmin = max(np.exp(lmin), image_data.min(), 0)
    vmax = min(np.exp(lmax), image_data.max())
    packet_gmm['vmin'] = np.rint(vmin)
    packet_gmm['vmax'] = np.rint(vmax)

    # Quantization window for the default (non-HD) WebP tile path -- deliberately
    # separate from vmin/vmax above (the display/contrast-slider default).
    # Straight linear (data/max)*255: no clipping anywhere, ever, at the cost
    # of coarser uint8 steps through the bulk of the image whenever a channel
    # has a single much-brighter-than-typical peak (verified against real
    # slide data -- see webp_compare_report.pdf for the tradeoff vs a
    # percentile window).
    #
    # image_data (the zarray sample) is NOT a valid source for this ceiling,
    # even though it's already loaded: it's mean-pooled down from full
    # resolution (a pyramid level plus an additional block_reduce, ~1000x
    # area averaging in a typical whole-slide image here) purely so the GMM
    # fit above stays fast. Mean-pooling dilutes real single/few-pixel peaks
    # far below what the actual full-resolution tiles served by encode_tile()
    # contain -- using it as a max-based ceiling under-clips real data.
    # Verified live: caused whole channels to saturate to a single solid
    # color, since most full-res pixels legitimately exceeded that
    # artificially low ceiling. The true max requires reading full-resolution
    # data at least once; do that here, cached afterwards same as everything
    # else in this function.
    if isinstance(channels, zarr.Array):
        full_res_channel = channels[image_channelIdx]
    else:
        full_res_channel = _zarr_level(channels, 0)[image_channelIdx]
    qmin = 0.0
    qmax = max(float(np.asarray(full_res_channel).max()), 1.0)
    packet_gmm['qmin'] = qmin
    packet_gmm['qmax'] = qmax

    [hist, bin_edges] = np.histogram(img_log.flatten(), bins=50, density=True)
    midpoints = (bin_edges[1:] + bin_edges[:-1]) / 2

    covars = gmm.covariances_[:, 0, 0]
    weights = gmm.weights_
    pdf_gmm1 = weights[i0] * norm.pdf(midpoints, means[i0], np.sqrt(covars[i0]))
    pdf_gmm2 = weights[i1] * norm.pdf(midpoints, means[i1], np.sqrt(covars[i1]))
    pdf_gmm3 = weights[i2] * norm.pdf(midpoints, means[i2], np.sqrt(covars[i2]))

    dat_gmm1 = []
    dat_gmm2 = []
    dat_gmm3 = []
    for i in range(len(hist)):
        obj1 = {}
        obj1['x'] = midpoints[i]
        obj1['y'] = pdf_gmm1[i]
        dat_gmm1.append(obj1)

        obj2 = {}
        obj2['x'] = midpoints[i]
        obj2['y'] = pdf_gmm2[i]
        dat_gmm2.append(obj2)

        obj3 = {}
        obj3['x'] = midpoints[i]
        obj3['y'] = pdf_gmm3[i]
        dat_gmm3.append(obj3)

    packet_gmm['image_gmm_1'] = dat_gmm1
    packet_gmm['image_gmm_2'] = dat_gmm2
    packet_gmm['image_gmm_3'] = dat_gmm3

    _gmm_cache[cache_key] = packet_gmm
    return packet_gmm


def generate_zarr_png(datasource_name, channel, level, tile):
    global channels
    global seg
    if source != datasource_name or config is None or channels is None or seg is None:
        load_datasource(datasource_name)
    [tx, ty] = tile.replace('.png', '').split('_')
    tx = int(tx)
    ty = int(ty)
    level = int(level)
    tile_width = config[datasource_name]['tileWidth']
    tile_height = config[datasource_name]['tileHeight']
    ix = tx * tile_width
    iy = ty * tile_height
    channel_num, segmentation = _parse_channel(channel)
    if segmentation:
        tile = _zarr_level(seg, level)[iy:iy + tile_height, ix:ix + tile_width]
        if tile.dtype.itemsize != 4:
            tile = tile.astype(np.uint32)
        tile = tile.view('uint8').reshape(tile.shape + (-1,))[..., [0, 1, 2]]
        tile = np.append(tile, np.zeros((tile.shape[0], tile.shape[1], 1), dtype='uint8'), axis=2)
    else:
        if isinstance(channels, zarr.Array):
            tile = channels[channel_num, iy:iy + tile_height, ix:ix + tile_width]
        else:
            tile = _zarr_level(channels, level)[channel_num, iy:iy + tile_height, ix:ix + tile_width]
            tile = tile.astype('uint16')

    # tile = np.ascontiguousarray(tile, dtype='uint32')
    # png = tile.view('uint8').reshape(tile.shape + (-1,))[..., [2, 1, 0]]
    return tile


def _parse_channel(channel):
    """Returns (channel_num, is_segmentation) for a channel identifier like
    "<file>_<N>" (channel_num=N), or a segmentation/label channel name with
    no trailing "_<N>" (is_segmentation=True)."""
    try:
        return int(re.match(r".*_(\d*)$", channel).groups()[0]), False
    except AttributeError:
        return None, True


def _channel_num_to_name(datasource_name, channel_num):
    real_channels = [d for d in config[datasource_name]['imageData'] if d['fullname'] != 'Area']
    return real_channels[channel_num]['fullname']


def encode_tile(datasource_name, channel, level, tile, quality):
    """Returns (encoded_bytes, mimetype) for one tile request. `quality` is
    'hd' for full-precision 16-bit, 'legacy' for the original uncompressed
    PNG behavior, anything else selects the fast/default 8-bit-quantized
    WebP path for channel tiles. Segmentation tiles ignore `quality`
    entirely -- they always need byte-exact label IDs, and always use the
    fast libdeflate PNG encoder (never WebP: verified that WebP lossless,
    even with exact=True at encode time, gets its RGB corrupted by the
    browser's own decoder wherever alpha=0 -- which is every pixel here --
    regardless of decode API used; PNG has no such decode-side risk since
    the frontend parses PNG bytes directly via UPNG.js, not a canvas)."""
    array = generate_zarr_png(datasource_name, channel, level, tile)
    channel_num, is_segmentation = _parse_channel(channel)

    if is_segmentation:
        return fast_png.encode_rgba8_png(array), 'image/png'

    if quality == 'hd':
        return fast_png.encode_gray16_png(array), 'image/png'

    if quality == 'legacy':
        file_object = io.BytesIO()
        Image.fromarray(array).save(file_object, 'PNG', compress_level=0)
        return file_object.getvalue(), 'image/png'

    # Default: quantize linearly into [0, channel_max] (see get_channel_gmm's
    # qmin/qmax) -- deliberately NOT vmin/vmax, which is the narrower GMM
    # display/contrast-slider window applied separately client-side. This
    # window never clips, at the cost of a coarser uint8 step size across the
    # image whenever one pixel is much brighter than the rest (see
    # webp_compare_report.pdf for the measured tradeoff). Encode WebP lossy q90.
    channel_name = _channel_num_to_name(datasource_name, channel_num)
    gmm = get_channel_gmm(channel_name, datasource_name)
    qmin, qmax = gmm['qmin'], gmm['qmax']
    span = qmax - qmin  # already guarded >= 1 in get_channel_gmm
    quantized = np.clip(np.rint((array.astype(np.float64) - qmin) / span * 255), 0, 255).astype(np.uint8)
    file_object = io.BytesIO()
    Image.fromarray(quantized, mode='L').save(file_object, 'WEBP', quality=90, method=6)
    return file_object.getvalue(), 'image/webp'


def get_ome_metadata(datasource_name):
    global metadata
    if source != datasource_name or config is None or metadata is None:
        load_datasource(datasource_name)
    return metadata


def convertOmeTiff(filePath, channelFilePath=None, dataDirectory=None, isLabelImg=False):
    channel_info = {}
    channelNames = []

    # image is a normal channel?
    if isLabelImg == False:
        channel_io = tf.TiffFile(str(filePath), is_ome=False)
        channels = zarr.open(channel_io.series[0].aszarr())
        if isinstance(channels, zarr.Array):
            channel_info['maxLevel'] = 1
            chunks = channels.chunks
            shape = channels.shape
        else:
            channel_info['maxLevel'] = len(channels)
            shape = _zarr_level(channels, 0).shape
            chunks = (1, 1024, 1024)
        chunks = (chunks[-2], chunks[-1])
        channel_info['tileHeight'] = chunks[0]
        channel_info['tileWidth'] = chunks[1]
        channel_info['height'] = shape[1]
        channel_info['width'] = shape[2]
        channel_info['num_channels'] = shape[0]
        for i in range(shape[0]):
            channelName = re.sub(r'\.ome\.tiff|\.ome\.tif|\.tiff|\.tif|\.png', '', filePath.name) + "_" + str(i)
            channelNames.append(channelName)
        channel_info['channel_names'] = channelNames
        return channel_info

    # segmentation mask
    else:
        channel_io = tf.TiffFile(str(channelFilePath), is_ome=False)
        channels = zarr.open(channel_io.series[0].aszarr())
        write_path = None
        directory = Path(dataDirectory + "/" + filePath.name)
        segmentation_mask = tf.TiffFile(str(filePath), is_ome=False)
        if segmentation_mask.series[0].aszarr().is_multiscales is False:
            args = {}
            args['in_paths'] = [Path(filePath)]
            args['out_path'] = directory
            args['is_mask'] = True
            pyramid_assemble.main(py_args=args)
            pyramid_upgrade.main(py_args=args)
            write_path = str(directory)
        else:
            write_path = str(filePath)
        write_path = ensure_outline_segmentation(write_path, dataDirectory)
        return {'segmentation': write_path}


def logTransform(csvPath, skip_columns=[]):
    df = pl.read_csv(csvPath)
    transform_cols = [c for c in df.columns if c not in skip_columns]
    df = df.with_columns([pl.col(c).log1p().alias(c) for c in transform_cols])
    df.write_csv(csvPath)

