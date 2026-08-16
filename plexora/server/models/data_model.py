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
from plexora import config_json_path, data_path, cwd_path, get_config
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
# Name of the datasource whose load_datasource() body has run to completion.
# This is the ONLY correct "is it loaded?" signal: `datasource` and `seg` are
# legitimately None for image-only projects (has_feature_data=False, no
# segmentation), so guards that infer loadedness from them can never
# short-circuit -- they re-run the whole load (reopening the OME-TIFF, wiping
# the derived caches, bumping load_generation) on every single tile request.
_loaded_source = None

# Cache of derived, expensive-to-recompute results, keyed off the currently
# loaded datasource. Cleared whenever load_datasource actually (re)loads data,
# since these caches were only ever valid for the previously loaded content.
_gmm_cache = {}
# Per-key locks making an uncached get_channel_gmm() single-flight: the compute
# is ~0.7 s and is triggered from the tile path, so concurrent requests for the
# same channel must wait for one computation rather than each doing their own.
_gmm_compute_locks = {}
_gmm_compute_locks_guard = threading.Lock()


def _gmm_compute_lock(cache_key):
    with _gmm_compute_locks_guard:
        lock = _gmm_compute_locks.get(cache_key)
        if lock is None:
            lock = threading.Lock()
            _gmm_compute_locks[cache_key] = lock
        return lock


_image_stats_cache = {}
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
# Plugins (plexora/plugins/*, reaching core only via plexora.api) should go
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
    global _loaded_source
    with load_lock:
        if _loaded_source == datasource_name and reload is False:
            return
        load_config(datasource_name)
        if reload:
            load_ball_tree(datasource_name, reload=reload)
        has_feature_data = config[datasource_name].get('has_feature_data', True)
        if has_feature_data:
            data_type = config[datasource_name].get('data_type', 'csv')
            adapter = get_adapter(data_type)(config[datasource_name]['featureData'][0])
            print("Loading datasource data.. (this can take some time)")
            loaded_datasource = adapter.load_table().table
        else:
            loaded_datasource = None
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
        _image_stats_cache.clear()
        _gate_filter_cache.clear()
        # Bumped so downstream tile-byte caches (keyed on this) know to
        # treat previously cached tiles as stale without needing a direct
        # reference back into this module's caches.
        load_generation += 1
        # Set last, after every global above is in place, so a concurrent
        # reader never sees _loaded_source set against a half-built state.
        _loaded_source = datasource_name
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
        # Update Feature SRC -- skip entirely for a no-feature-data datasource
        # (has_feature_data=False, featureData=[]), which has no src to fix up.
        if config[datasource_name].get('featureData'):
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

    if not config[datasource_name_name].get('has_feature_data', True):
        # No feature file exists for this datasource (quick-view, image-only)
        # -- nothing to build a ball tree from. Every direct consumer of
        # ball_tree/datasource branches on this same flag rather than
        # dereferencing a tree that was never built.
        ball_tree = None
        return

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


def _saved_channels_first(datasource_name, fullnames):
    """Reorder `fullnames` so the channels the project last had switched on come
    first.

    Warming is not free even in pass 1: the quantization window needs one
    full-resolution read per channel, and all tile I/O is globally serialized
    (zarr funnels through a single io thread, tifffile takes a per-file read
    lock). Warming in config order therefore puts up to 18 channels the user has
    not asked for ahead of the ones the page is about to request, and the page
    waits behind them. Best-effort: any failure here just leaves the original
    order, which is what this did before.
    """
    try:
        saved = get_saved_channel_list(datasource_name)
        if not saved:
            return fullnames
        # save_channel_list writes whatever map_channels supplied, which the
        # client builds from imageChannelsIdx -- i.e. fullnames. Also match the
        # config's short `name` so this keeps working for a datasource where
        # the two differ.
        active = {row['channel'] for row in saved if row.get('channel_active')}
        if not active:
            return fullnames
        short_of = {c['fullname']: c.get('name') for c in config[datasource_name]['imageData']}
        is_active = lambda n: n in active or short_of.get(n) in active
        return ([n for n in fullnames if is_active(n)] +
                [n for n in fullnames if not is_active(n)])
    except Exception:
        return fullnames


def _warm_datasource_caches(datasource_name):
    """Pre-populate description/GMM caches in the background so the first
    real request after a datasource load doesn't pay for them synchronously.
    Best-effort only: if a concurrent switch to a different datasource races
    this, the _ensure_loaded() calls inside will just reload as needed.

    Two passes, not one pass per channel. The client blocks on
    get_image_channel_stats (histogram, quantization window, provisional
    auto-level) before it can display anything, and that is ~0.13 s per
    channel; get_channel_gmm only refines contrast afterwards and costs
    0.2-1.9 s per channel (17 s for all 19 here). Interleaving them meant a
    channel the user activated early queued behind GMM fits for channels they
    had not asked for -- measured 2.44 s for a single on-demand GMM against
    ~0.95 s in isolation. Same total work either way; this just finishes
    everything the UI actually waits on ~7x sooner.
    """
    lock = _warmup_lock_for(datasource_name)
    if not lock.acquire(blocking=False):
        return
    try:
        get_datasource_description(datasource_name)
        to_warm = [c['fullname'] for c in config[datasource_name]['imageData']
                   if c['name'] != 'Area']
        to_warm = _saved_channels_first(datasource_name, to_warm)
        # Pass 1 -- everything the first paint blocks on.
        for fullname in to_warm:
            get_image_channel_stats(fullname, datasource_name)
        # Pass 2 -- the expensive refinement nothing blocks on.
        for fullname in to_warm:
            get_channel_gmm(fullname, datasource_name)
    except Exception as exc:
        print(f"Background cache warmup failed for {datasource_name}: {exc}")
    finally:
        lock.release()


def query_for_closest_cell(x, y, datasource_name):
    global datasource
    global source
    global ball_tree
    _ensure_loaded(datasource_name)
    if ball_tree is None:
        return {}
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
    below, and by the gating plugin's own queries (plexora/plugins/gating/
    server/model.py) via this same function, not a private copy.
    """
    if datasource is None:
        # Defensive backstop -- callers into this shared primitive
        # (get_channel_cells above, the gating module) should already
        # short-circuit on has_feature_data before reaching here.
        return {}
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
    if not config[datasource_name].get('has_feature_data', True):
        return []

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
    except (KeyError, IndexError):
        return ''
    except TypeError:
        return ''


def get_phenotype_column_name(datasource):
    try:
        return config[datasource]['featureData'][0]['celltype']
    except (KeyError, IndexError):
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
    if not config[datasource_name].get('has_feature_data', True):
        return []

    try:
        phenotype_field = config[datasource_name]['featureData'][0]['celltype']
    except (KeyError, IndexError):
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
    if not config[datasource_name].get('has_feature_data', True):
        return np.array([], dtype=np.uint32 if np.issubdtype(data_type, int) else np.float32)

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

    if not config[datasource_name].get('has_feature_data', True):
        _description_cache[datasource_name] = {}
        return {}

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

    _description_cache[datasource_name] = description
    return description


def get_channel_gmm(channel_name, datasource_name):
    _ensure_loaded(datasource_name)
    cache_key = (datasource_name, channel_name)
    cached = _gmm_cache.get(cache_key)
    if cached is not None:
        return cached
    # Single-flight. encode_tile() calls this for every default-quality tile,
    # and a cold entry costs ~0.7 s: a GaussianMixture(3) fit plus a .max()
    # over the entire full-resolution channel plane (~218 MB, see the comment
    # on qmax below for why a downsampled source is not valid here). Without
    # this lock, the burst of concurrent tile requests that opening a project
    # produces would each redo that same work for the same channel.
    with _gmm_compute_lock(cache_key):
        cached = _gmm_cache.get(cache_key)
        if cached is not None:
            return cached
        return _compute_channel_gmm(channel_name, datasource_name, cache_key)


def get_channel_quantization_window(channel_name, datasource_name):
    """(qmin, qmax) for the default (non-HD) WebP tile path.

    Deliberately separate from the GMM's vmin/vmax (the display/contrast-slider
    default) AND from get_channel_gmm() itself: encode_tile() needs only this
    window, while the surrounding GaussianMixture fit costs ~1 s per channel.
    Folding the two together made every first tile of a channel block on that
    fit -- with 7+ channels active that was several seconds of stall on the
    first pan after opening a project. The max below is ~8 ms by comparison.

    Straight linear (data/max)*255: no clipping anywhere, ever, at the cost of
    coarser uint8 steps through the bulk of the image whenever a channel has a
    single much-brighter-than-typical peak (verified against real slide data --
    see webp_compare_report.pdf for the tradeoff vs a percentile window).

    The `zarray` sample is NOT a valid source for this ceiling, even though
    it's already loaded: it's mean-pooled down from full resolution (a pyramid
    level plus an additional block_reduce, ~1000x area averaging in a typical
    whole-slide image here). Mean-pooling dilutes real single/few-pixel peaks
    far below what the full-resolution tiles served by encode_tile() contain --
    using it as a max-based ceiling under-clips real data. Verified live: it
    caused whole channels to saturate to a single solid color, since most
    full-res pixels legitimately exceeded that artificially low ceiling. The
    true max requires reading full-resolution data at least once.
    """
    _ensure_loaded(datasource_name)
    cache_key = ('qwindow', datasource_name, channel_name)
    cached = _gmm_cache.get(cache_key)
    if cached is not None:
        return cached
    with _gmm_compute_lock(cache_key):
        cached = _gmm_cache.get(cache_key)
        if cached is not None:
            return cached
        real_channels = [d for d in config[datasource_name]['imageData'] if d['fullname'] != 'Area']
        idx = next(i for (i, d) in enumerate(real_channels) if d['fullname'] == channel_name)
        if isinstance(channels, zarr.Array):
            full_res_channel = channels[idx]
        else:
            full_res_channel = _zarr_level(channels, 0)[idx]
        window = (0.0, max(float(np.asarray(full_res_channel).max()), 1.0))
        _gmm_cache[cache_key] = window
        return window


def _compute_channel_gmm(channel_name, datasource_name, cache_key):
    global datasource
    global source
    global ball_tree
    global config

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

    # Quantization window for the default (non-HD) WebP tile path. Computed by
    # get_channel_quantization_window() so the tile path can reach it without
    # paying for the GaussianMixture fit above; see that function for why the
    # ceiling has to come from full-resolution data.
    qmin, qmax = get_channel_quantization_window(channel_name, datasource_name)
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



# Percentiles of the log-intensity distribution used for vmin_hint/vmax_hint
# below. Chosen by measurement, not taste: swept 7 vmin x 5 vmax candidates
# against the real GMM auto-level for all 19 channels of the reference slide,
# scoring the error in the BYTE domain (what the shader actually applies, and
# where a big raw-unit error high up the range can be worth only a byte or
# two). p50/p99.5 won on worst case -- 15 byte-levels of combined error, mean
# 6.6. For comparison the [0, 255] default this replaces is ~234 off for a
# typical channel, which is why a newly enabled channel looks black today.
_HINT_PERCENTILES = (50.0, 99.5)


def get_image_channel_stats(channel_name, datasource_name):
    """Per-channel image_min/image_max/image_histogram, split out of
    get_datasource_description so a page load only pays for the channels the
    user actually activates instead of every channel up front.

    Also carries two things the client needs *before* it can show a channel,
    both far cheaper than the GaussianMixture fit in get_channel_gmm():

    - qmin/qmax, the server's quantization window. The client stores channel
      ranges in raw 16-bit units but its slider and shader work in the [0, 255]
      byte domain, so it cannot even display a *saved* range without these.
      They used to be reachable only as two extra fields on the GMM packet,
      which meant restoring saved channels ran a ~1 s fit per channel purely to
      read them -- serialized, so 3 saved channels cost 5.8 s on a cold server.
    - vmin_hint/vmax_hint, a provisional auto-level so a newly enabled channel
      is visible immediately instead of near-black until the fit lands.

    The GMM stays authoritative: the client applies the hint on arrival and
    replaces it with the real vmin/vmax once the fit completes.
    """
    global zarray
    global config

    _ensure_loaded(datasource_name)

    cache_key = (datasource_name, channel_name)
    if cache_key in _image_stats_cache:
        return _image_stats_cache[cache_key]

    real_channels = [d for d in config[datasource_name]['imageData'] if d['fullname'] != 'Area']
    image_channelIdx = next(index for (index, d) in enumerate(real_channels) if d["fullname"] == channel_name)
    image_data = zarray[image_channelIdx]
    img_log = np.log(image_data[image_data > 0])
    [hist, bin_edges] = np.histogram(img_log.flatten(), bins=50, density=True)
    midpoints = (bin_edges[1:] + bin_edges[:-1]) / 2

    dat = []
    for i in range(len(hist)):
        obj = {}
        obj['x'] = midpoints[i]
        obj['y'] = hist[i]
        dat.append(obj)

    pmin, pmax = _HINT_PERCENTILES
    qmin, qmax = get_channel_quantization_window(channel_name, datasource_name)

    stats = {
        'image_histogram': dat,
        'image_min': np.ceil(np.exp(np.min(img_log))),
        'image_max': np.ceil(np.exp(np.max(img_log))),
        'qmin': qmin,
        'qmax': qmax,
        'vmin_hint': float(np.rint(np.exp(np.percentile(img_log, pmin)))),
        'vmax_hint': float(np.rint(np.exp(np.percentile(img_log, pmax)))),
    }
    _image_stats_cache[cache_key] = stats
    return stats


def ensure_loaded(datasource_name):
    """Load `datasource_name` if it isn't already the loaded one, and return the
    resulting load_generation.

    Callers that key a cache on load_generation must call this BEFORE reading
    the generation: loading is what bumps it, so a generation sampled first
    would key the entry under the pre-load value and be missed by every
    subsequent request.
    """
    if _loaded_source != datasource_name:
        load_datasource(datasource_name)
    return load_generation


def generate_zarr_png(datasource_name, channel, level, tile):
    global channels
    global seg
    ensure_loaded(datasource_name)
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

# Cache of 65536-entry uint16 -> uint8 quantization tables, keyed by the
# (qmin, span) window they encode. Applying one is a single gather over the
# tile; the arithmetic form it replaces promoted each 1024x1024 tile to float64
# (an 8 MB temporary) and made four passes over it for the rint/clip/astype.
_quantize_lut_cache = {}
_quantize_lut_lock = threading.Lock()


def _quantize_to_uint8(array, qmin, span):
    """Linearly quantize `array` from [qmin, qmin + span] into [0, 255]."""
    if array.dtype != np.uint16:
        # Non-uint16 sources (a non-pyramidal zarr keeps its native dtype) can't
        # index a 65536-entry table -- fall back to the arithmetic form.
        return np.clip(np.rint((array.astype(np.float32) - qmin) / span * 255), 0, 255).astype(np.uint8)
    key = (float(qmin), float(span))
    with _quantize_lut_lock:
        lut = _quantize_lut_cache.get(key)
    if lut is None:
        levels = np.arange(65536, dtype=np.float32)
        lut = np.clip(np.rint((levels - qmin) / span * 255), 0, 255).astype(np.uint8)
        with _quantize_lut_lock:
            _quantize_lut_cache[key] = lut
    return lut[array]



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
        # Stored (uncompressed) deflate, not level 6. The client decodes 16-bit
        # PNG with UPNG.js/pako in JavaScript, and a CPU profile of a 7-channel
        # HD pan put 81% of ALL time in pako's inflate (inflate_fast alone was
        # 71%). Storing the data uncompressed leaves nothing to inflate: the
        # stream is copied rather than Huffman-decoded. Costs ~15% more bytes
        # over what is normally a loopback connection, and also drops the
        # server-side encode from ~37 ms to a few ms.
        return fast_png.encode_gray16_png(array, compress_level=0), 'image/png'

    if quality == 'legacy':
        file_object = io.BytesIO()
        Image.fromarray(array).save(file_object, 'PNG', compress_level=0)
        return file_object.getvalue(), 'image/png'

    # Default: quantize linearly into [0, channel_max] (see
    # get_channel_quantization_window) -- deliberately NOT vmin/vmax, which is
    # the narrower GMM display/contrast-slider window applied separately
    # client-side. This window never clips, at the cost of a coarser uint8 step
    # size across the image whenever one pixel is much brighter than the rest
    # (see webp_compare_report.pdf for the measured tradeoff).
    #
    # Note this asks for the window only, NOT the full get_channel_gmm packet:
    # the GaussianMixture fit in there costs ~1 s per channel and nothing on
    # the tile path needs its output.
    channel_name = _channel_num_to_name(datasource_name, channel_num)
    qmin, qmax = get_channel_quantization_window(channel_name, datasource_name)
    span = qmax - qmin  # qmax is guarded >= 1 and qmin is 0, so span >= 1
    quantized = _quantize_to_uint8(array, qmin, span)
    file_object = io.BytesIO()
    # method=0, not libwebp's default-ish method=6. Measured on a real
    # 1024x1024 tile from this dataset (encode time / output bytes):
    #   method=0  21.2 ms  64410      method=4  58.3 ms  59876
    #   method=1  26.0 ms  64288      method=6  97.0 ms  58884
    #   method=2  28.7 ms  61698
    # Encoding is ~70% of the remaining per-tile cost (zarr read is 7.9 ms,
    # LUT quantization 1.1 ms), and these tiles are generated on demand while
    # the user pans. 9% more bytes to localhost is far cheaper than 76 ms of
    # extra latency per tile per channel.
    Image.fromarray(quantized, mode='L').save(file_object, 'WEBP', quality=90, method=0)
    return file_object.getvalue(), 'image/webp'


def generate_thumbnail(datasource_name, max_size=320):
    """Cheap preview image for the Open Project grid. Deliberately does NOT
    go through load_datasource/encode_tile -- those pull in the full feature
    table, ball tree and segmentation (see load_datasource above), which is
    far more than a thumbnail needs and would make browsing a page of many
    projects trigger a full data load per card. This opens only the channel
    image file and reads the smallest pyramid level with both dims >= 200
    (same level-selection heuristic load_datasource uses for its own
    overview array), so it never touches the shared source/channels/seg
    globals other requests depend on.

    Returns (encoded_bytes, mimetype), or None if the project has no
    channel image yet or it can't be opened.
    """
    with open(config_json_path, "r") as config_file:
        cfg = json.load(config_file)
    entry = cfg.get(datasource_name)
    channel_file = entry.get('channelFile') if entry else None
    if not channel_file or not Path(channel_file).exists():
        return None

    try:
        channel_io = tf.TiffFile(channel_file, is_ome=False)
        level_series = next(
            level for level in reversed(channel_io.series[0].levels)
            if all(d >= 200 for d in level.shape[1:])
        )
        array = np.asarray(zarr.open(level_series.aszarr()))
    except Exception:
        return None

    if array.ndim == 3:
        array = array[0]
    array = array.astype(np.float32)
    low, high = np.percentile(array, [1, 99])
    span = max(high - low, 1)
    quantized = np.clip((array - low) / span * 255, 0, 255).astype(np.uint8)

    image = Image.fromarray(quantized, mode='L')
    image.thumbnail((max_size, max_size))
    file_object = io.BytesIO()
    image.save(file_object, 'WEBP', quality=85, method=6)
    return file_object.getvalue(), 'image/webp'


def get_ome_metadata(datasource_name):
    global metadata
    # `metadata` is {} (not None) when the OME-XML fails to parse, so it was
    # never a reliable loadedness signal either -- use _loaded_source.
    if _loaded_source != datasource_name:
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


_segmentation_jobs = {}
_segmentation_job_locks = {}
_segmentation_job_locks_guard = threading.Lock()
_config_write_lock = threading.Lock()


def _segmentation_job_lock_for(datasource_name):
    with _segmentation_job_locks_guard:
        if datasource_name not in _segmentation_job_locks:
            _segmentation_job_locks[datasource_name] = threading.Lock()
        return _segmentation_job_locks[datasource_name]


def _patch_config_segmentation(datasource_name, segmentation_path, status):
    with _config_write_lock:
        with open(config_json_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if datasource_name not in cfg:
            return  # datasource was deleted while the job was running
        cfg[datasource_name]['segmentation'] = segmentation_path
        cfg[datasource_name]['segmentation_status'] = status
        with open(config_json_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4)


def start_segmentation_job(datasource_name, label_file, channel_file, data_directory):
    """Kick off segmentation-mask processing (pyramid_assemble/pyramid_upgrade
    when needed, plus the always-run ensure_outline_segmentation inside
    convertOmeTiff) in a background thread, so the /upload request that
    triggers this doesn't block on it -- the viewer can open as soon as the
    (cheap, metadata-only) main image conversion and config write are done,
    with the segmentation layer appearing once this job finishes.
    """
    lock = _segmentation_job_lock_for(datasource_name)
    if not lock.acquire(blocking=False):
        return  # already running for this datasource
    _segmentation_jobs[datasource_name] = {"status": "pending", "error": None}

    def _run():
        try:
            result = convertOmeTiff(
                label_file,
                channelFilePath=channel_file,
                dataDirectory=data_directory,
                isLabelImg=True,
            )
            _segmentation_jobs[datasource_name] = {
                "status": "ready",
                "error": None,
                "segmentation": result["segmentation"],
            }
            _patch_config_segmentation(datasource_name, result["segmentation"], "ready")
            if source == datasource_name:
                load_datasource(datasource_name, reload=True)
        except Exception as exc:
            _segmentation_jobs[datasource_name] = {"status": "error", "error": str(exc)}
            _patch_config_segmentation(datasource_name, None, "error")
        finally:
            lock.release()

    threading.Thread(target=_run, daemon=True).start()


def get_segmentation_job_status(datasource_name):
    if datasource_name in _segmentation_jobs:
        return _segmentation_jobs[datasource_name]
    # Fall back to config.json's persisted status (server restarted mid-job,
    # or this process never ran the job -- e.g. multi-worker deployment).
    entry = get_config().get(datasource_name, {})
    return {"status": entry.get("segmentation_status", "ready"), "error": None}


def logTransform(csvPath, skip_columns=[]):
    df = pl.read_csv(csvPath)
    transform_cols = [c for c in df.columns if c not in skip_columns]
    df = df.with_columns([pl.col(c).log1p().alias(c) for c in transform_cols])
    df.write_csv(csvPath)

