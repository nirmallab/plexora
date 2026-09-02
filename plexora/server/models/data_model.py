from sklearn.neighbors import BallTree
import numpy as np
import polars as pl
import os
import io
from pathlib import Path
from plexora import paths, get_config
from plexora.server.utils import brightfield
from plexora.server.utils import fast_png
from plexora.server.utils import segmentation_pyramid
from plexora.server.models.adapters import MetadataColumn, get_adapter
from plexora.server.models import database_model, centroid_tiles
from plexora.server.models.project import (
    Project, config_transaction, read_config, write_config,
)
from plexora.server import providers
from plexora.server.utils import smallestenclosingcircle
from PIL import Image
from itertools import chain
import time
import json
import pickle
import tifffile as tf
import re
import threading
import zarr
from sklearn.mixture import GaussianMixture
from scipy.stats import norm

ball_tree = None
source = None
config = None
seg = None
zarray = None
channels = None
metadata = None
load_lock = threading.RLock()
# Name of the datasource whose load_datasource() body has run to completion.
# This is the ONLY correct "is it loaded?" signal: `datasource` and `seg` are
# legitimately None for image-only projects (no feature table, no
# segmentation), so guards that infer loadedness from them can never
# short-circuit -- they re-run the whole load (reopening the OME-TIFF, wiping
# the derived caches, bumping load_generation) on every single tile request.
_loaded_source = None

# What serves each of the loaded project's three scientific resources -- see
# plexora/server/providers. Set under `load_lock` alongside every other global
# here, and never read outside a dispatch guard.
_providers = providers.EMPTY
# True only while a project with at least one node-backed resource is loaded.
#
# Every dispatch guard below tests this global FIRST, and it is False for every
# project that has no `resources` block in its config entry -- which is every
# project that existed before multi-source support. That is what makes this
# whole mechanism free when it is not used: the single-server path pays one
# module-global read and one branch, never a dict lookup, an attribute chain or
# a method call. In particular the warm-tile path (0.005 s, and the hottest
# path in the app) is untouched, because its cache key does not change here.
_remote = False
# kind -> why that resource could not be read, for the resources whose node was
# unreachable at load time. Empty for a project that loaded completely, which
# is every single-server project. Read through `resource_unavailable()` so a
# control can say WHICH node is not answering rather than only that something
# is missing -- "Segmentation lives on node 'hpc-a', which is unreachable" is
# something a user can act on; a greyed-out checkbox is not.
_resource_errors = {}

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
# One entry per (datasource, annotation column) -- see get_metadata_column.
# Bounded rather than unbounded like the caches above: those hold one small
# summary per datasource, while each entry here is a full-length array, and a
# table can carry hundreds of annotation columns. Oldest-first eviction is
# enough -- the access pattern is a user picking columns one at a time, and
# what matters is that going back to the previous one is free.
_metadata_column_cache = {}
_METADATA_COLUMN_CACHE_MAX = 8
# Incremented each time load_datasource actually (re)loads data, so other
# modules can key a cache off "which load is this" without importing this
# module's internal cache dicts directly.
load_generation = 0


def _zarr_level(group, level):
    if isinstance(group, zarr.Array):
        return group
    return group[str(level)]


def _served_directly_as_outlines(segmentation_path):
    """True when a mask can be handed to the viewer as-is.

    That covers both a mask this app generated and a user's own outline export
    -- in either case there is no filled-label interior to strip, so deriving
    anything would be wasted work.
    """
    if segmentation_pyramid.is_generated_outline_mask(segmentation_path):
        return True
    try:
        return segmentation_pyramid.looks_like_outline_mask(segmentation_path)
    except Exception:
        return False


def describe_segmentation_work(segmentation_path, mode):
    """What the conversion is about to do, and why, for the import page's
    progress panel.

    Worth spelling out: a user who supplies a mask that is already filled
    labels (or already pyramidal) reasonably expects it to be served untouched,
    and a panel that just says "preparing" for two minutes gives them no way to
    tell a missing requirement from a hang.
    """
    if mode == segmentation_pyramid.MODE_FILLED:
        gaps = segmentation_pyramid.label_pyramid_gaps(segmentation_path)
        if gaps:
            return f"Building a tiled label pyramid, because {' and '.join(gaps)}"
        return "Preparing label pyramid"
    return "Generating cell outlines"


def _servable_as_is(segmentation_path, mode):
    """True when `segmentation_path` can go straight to the viewer unconverted.

    In outline mode that means it already contains outlines; in filled mode it
    means it is a tiled label pyramid the tile route can serve at every zoom
    level -- which is the "user brought their own pyramidised mask" case, the
    one situation where importing costs no conversion at all.
    """
    if mode == segmentation_pyramid.MODE_FILLED:
        return segmentation_pyramid.is_servable_label_pyramid(segmentation_path)
    return _served_directly_as_outlines(segmentation_path)


def resolve_outline_segmentation(segmentation_path, dataDirectory=None, progress_callback=None,
                                 mode=segmentation_pyramid.DEFAULT_MODE, stage_callback=None):
    """Return the mask to serve for `segmentation_path`, converting it in one
    pass when it is not already servable.

    `mode` selects what gets written: a filled label pyramid (the default,
    where renderLabelTile derives boundaries at tile-load time), or an outline
    pyramid with the boundaries baked in.

    In the default mode a source that is already a tiled label pyramid is
    returned untouched -- the case this is built around. Outline mode has no
    such shortcut: it always reads the source's highest-resolution level and
    derives its own pyramid, so "already tiled" and "flat" input cost the same.

    A pyramid somebody already derived from this mask is adopted rather than
    rebuilt. That is worth doing here, and not only in
    `refresh_segmentation_mapping`, because this is the path a *fresh* import
    takes -- and importing a second project from a mask a first project already
    converted is an ordinary thing to do, which used to cost the full
    conversion a second time.
    """
    # Announced before the check, not after: in outline mode `_servable_as_is`
    # reads sampled pixel windows off the mask, which on a whole-slide file
    # over a network share is real time spent with nothing said.
    if stage_callback is not None:
        stage_callback("inspecting")
    if _servable_as_is(segmentation_path, mode):
        return str(segmentation_path)
    location = segmentation_pyramid.resolve_derived_mask(
        segmentation_path, dataDirectory, mode=mode
    )
    if location.existing is not None:
        return str(location.existing)
    return segmentation_pyramid.pyramidize_segmentation_mask(
        segmentation_path,
        location.target,
        overwrite=True,
        outline=mode != segmentation_pyramid.MODE_FILLED,
        progress_callback=progress_callback,
        stage_callback=stage_callback,
    )


def segmentation_mode(entry):
    """Which kind of mask this datasource serves.

    MODE_FILLED (the default for anything imported now) serves filled labels
    and has renderLabelTile derive boundaries at tile-load time. MODE_OUTLINES
    bakes them into the file instead; imports no longer choose it, but
    datasources built that way keep working.

    An entry written before the mode was recorded gets it read back off its own
    derived file rather than assuming the current default: those were all
    outlines, and calling them filled would make every pre-existing project
    look stale and rebuild its mask on the next load.
    """
    entry = entry or {}
    mode = entry.get('segmentationMode')
    if mode in (segmentation_pyramid.MODE_FILLED, segmentation_pyramid.MODE_OUTLINES):
        return mode
    derived = entry.get('segmentation')
    if derived and segmentation_pyramid.generated_mask_kind(derived) == segmentation_pyramid.MODE_OUTLINES:
        return segmentation_pyramid.MODE_OUTLINES
    return segmentation_pyramid.DEFAULT_MODE


def _segmentation_mapping_is_current(entry):
    """True when `entry`'s recorded derived mask still matches its source.

    Stat-only, so this can gate every datasource load. Before this mapping
    existed each load re-sampled the mask's pixels to guess whether outlines
    were still needed.
    """
    derived = entry.get('segmentation')
    source = entry.get('segmentationSource')
    recorded_key = entry.get('segmentationSourceKey')
    if not derived or not source or not recorded_key:
        return False
    if not Path(derived).exists():
        return False
    # A datasource switched between outline and filled mode still has the other
    # kind's file recorded here; that has to be re-derived rather than served.
    # None means the user's own mask is being served as-is, which no mode owns.
    kind = segmentation_pyramid.generated_mask_kind(derived)
    if kind is not None and kind != segmentation_mode(entry):
        return False
    return segmentation_pyramid.source_fingerprint(source) == recorded_key


def refresh_segmentation_mapping(entry, datasource_name):
    """Bring `entry`'s segmentation mapping up to date in place.

    Returns (changed, source_needing_generation). A returned source means the
    caller should hand it to start_segmentation_job() once it has finished
    writing config -- generation takes tens of seconds on a large mask, far too
    long to do inline on a load that a tile request is waiting behind.
    """
    # Record the mode explicitly on entries that predate the key, so this is the
    # last load that has to infer it by reading the derived file's OME header.
    backfilled = False
    if not entry.get('segmentationMode') and entry.get('segmentation'):
        entry['segmentationMode'] = segmentation_mode(entry)
        backfilled = True

    if _segmentation_mapping_is_current(entry):
        return backfilled, None

    source = entry.get('segmentationSource') or entry.get('segmentation')
    if not source:
        return False, None
    if not Path(source).exists():
        # The user's mask has been moved, renamed or deleted. Leave the entry
        # exactly as it stands and derive nothing.
        #
        # What this replaces is worse than an error. Every branch below assumes
        # the source is readable, so a missing one fell through to "no servable
        # mask yet": the project was rewritten to have no mask at all -- an
        # answer about its own cells, discarded silently -- and a conversion job
        # was queued against a file that is not there.
        #
        # Doing nothing is right in both of the cases that get here. If the
        # derived pyramid is still beside it, that pyramid IS the mask and the
        # project keeps working, which is what somebody who tidied up their
        # inputs would expect. If it is gone too, opening it raises and
        # `load_datasource` reports the mask unavailable, with the Edit page
        # named as the place to point it somewhere new.
        return backfilled, None
    # A legacy entry predates this mapping, so a fingerprint mismatch tells us
    # nothing about whether its derived file is stale -- only a mismatch
    # against a key we actually recorded means the user's mask has changed.
    had_mapping = bool(entry.get('segmentationSource') and entry.get('segmentationSourceKey'))
    source_changed = had_mapping and (
        segmentation_pyramid.source_fingerprint(source) != entry.get('segmentationSourceKey')
    )

    mode = segmentation_mode(entry)
    if mode == segmentation_pyramid.MODE_FILLED and _served_directly_as_outlines(source):
        # Asked for shader outlining, but handed a mask that already *is*
        # outlines -- the shader would then trace the boundary of each outline
        # stroke, hollowing it out. Nothing to derive from this input in either
        # mode, so fall back to serving it as outline mode would.
        mode = segmentation_pyramid.MODE_OUTLINES
        entry['segmentationMode'] = mode

    pending_source = None
    location = segmentation_pyramid.resolve_derived_mask(
        source, paths.derived_root(datasource_name), mode=mode
    )
    if not source_changed and location.existing is not None:
        # Backfilling a legacy entry: its derived file is one of ours, of the
        # kind this datasource wants, and nothing says the source moved on --
        # so adopt it rather than spending a minute reproducing it. Checked
        # before the content sniff below because this reads metadata only,
        # where the sniff has to pull pixels out of the user's own mask -- and
        # this runs on a load a page is waiting on.
        #
        # Both locations are searched, beside the source first. A project
        # imported before masks were written beside their source finds its
        # pyramid in this project's derived root exactly as it always did;
        # nothing rebuilds because the convention changed.
        entry['segmentation'] = str(location.existing)
        entry['segmentation_status'] = 'ready'
    elif _servable_as_is(source, mode):
        entry['segmentation'] = str(source)
        entry['segmentation_status'] = 'ready'
    else:
        entry['segmentation'] = None
        entry['segmentation_status'] = 'pending'
        pending_source = str(source)

    entry['segmentationSource'] = str(source)
    entry['segmentationSourceKey'] = segmentation_pyramid.source_fingerprint(source)
    return True, pending_source


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


def resource_unavailable(datasource_name, kind):
    """Why one of this project's resources could not be read, or None.

    None is the answer for every ordinary project and for every resource that
    loaded, so a caller can use it directly as "is there a problem to report".
    Scoped to the loaded project: asking about one that is not loaded returns
    None rather than loading it, because this is consulted while WORDING a
    control and must not itself be the thing that opens a file.
    """
    if source != datasource_name:
        return None
    return _resource_errors.get(kind)


def get_current_providers():
    """What serves the loaded project's three scientific resources.

    Read-only, like the accessors above. Feature modules that need to know
    whether a resource is here or on a node -- to word an error, or to decide
    whether to offer a control -- ask this rather than the private global.
    """
    return _providers


def held_node_addresses() -> dict:
    """`{node name: endpoint}` the loaded project's providers are addressing.

    What a health check needs in order to be about the same machine the viewer
    is reading from. The registry answers "where is this node now"; this
    answers "where is the open project still sending its requests", and the two
    part company for exactly as long as it takes a reconnect to go unnoticed by
    an already-loaded project.

    Resolves nothing and contacts nothing -- see `ProviderSet.held_addresses`.
    """
    return _providers.held_addresses


# -- dispatch ------------------------------------------------------------
#
# The three helpers below are the entire multi-source mechanism as far as this
# module is concerned. Each public function that reads scientific data starts
# by asking whether its resource is somewhere else; every one of them answers
# "no" in one global read for a single-server project, and the function then
# runs the body it has always run.
#
# `None` therefore means "this is local, carry on" rather than "not found",
# which is why the guards read `if remote is not None:` rather than testing
# truthiness -- a provider object is not something to evaluate for truth.


def _remote_table():
    """The node-backed table provider for the loaded project, or None."""
    if not _remote:
        return None
    table = _providers.table
    return table if table is not None and not table.is_local else None


def _remote_image():
    """The node-backed image provider for the loaded project, or None."""
    if not _remote:
        return None
    image = _providers.image
    return image if image is not None and not image.is_local else None


def _remote_segmentation():
    """The node-backed mask provider for the loaded project, or None."""
    if not _remote:
        return None
    seg_provider = _providers.segmentation
    return seg_provider if seg_provider is not None and not seg_provider.is_local else None


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



def _project(datasource_name):
    """This module's `config` global, as a typed record.

    Reads the in-memory dict rather than re-reading config.json: load_config()
    has already normalized paths in it, and a fresh read would drop those
    fixups. Everything here that asks what a project's columns mean goes
    through this, so the on-disk shape is known in exactly one place
    (server/models/project.py).
    """
    return Project.from_entry(datasource_name, (config or {}).get(datasource_name) or {})


def loaded_scope(datasource_name):
    """What "this datasource is already loaded" has to match on.

    The name alone when there is one root, which is every single-user install.
    With shared roots in play the name is not enough: a project can be shadowed
    -- a user who imports their own `sample1` while the shared `sample1` is the
    one loaded resolves to a different project under the same name, and every
    guard keyed on the name would report it as already loaded and go on serving
    the other one's table.

    Short-circuited on `shared_roots()` because this is consulted from the tile
    path, and resolving the root means reading a config.json per root.
    """
    if not paths.shared_roots():
        return datasource_name
    return (str(Project.root_for(datasource_name) or ""), datasource_name)


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
    global _providers
    global _remote
    with load_lock:
        if _loaded_source == loaded_scope(datasource_name) and reload is False:
            return
        load_config(datasource_name)
        project = _project(datasource_name)
        # Resolved before anything is opened, because it decides WHO opens it.
        # Reads the project record only -- no file, no network -- so a node
        # that is asleep cannot hold this lock, and every tile request behind
        # it, while a probe times out. Unreachability surfaces on the first
        # real call instead, where the caller can degrade.
        resolved = providers.resolve_providers(project)
        # A node that cannot be reached must not stop the project opening. It
        # is the ordinary state of a laptop that closed its lid, and the work
        # the user came for -- their ROIs, their figures, their gates -- is all
        # on this machine and all still there. So each resource is attempted on
        # its own and a failure is recorded rather than raised: the layer that
        # needed it reports itself unusable and names the node, which is
        # something a user can act on, and every other layer carries on.
        #
        # ResourceUnavailable, and -- for the mask and the table only -- a file
        # that is no longer where the project says it is.
        #
        # A node that answers with a REFUSAL is deliberately not caught: that is
        # a different situation with a different fix, and swallowing it would
        # turn a broken project into a quietly empty one. A missing local file
        # used to be in the same category and should not have been: a mask or a
        # table moved on disk is the same shape of problem as a laptop that
        # closed its lid -- one layer is gone, everything else still works, and
        # the fix is to repoint the field on the Edit page. Raising there meant
        # a raw 500 on open, which loses the ROIs and figures too.
        #
        # The IMAGE is exempt and stays loud. A project whose image has moved
        # has nothing to draw, no coordinate space to put anything in, and no
        # useful degraded state to offer -- opening it onto an empty viewer
        # would be a worse answer than saying so.
        failures = {}

        def attempt(kind, read, fallback=None, missing_ok=False):
            catch = ((providers.ResourceUnavailable, FileNotFoundError)
                     if missing_ok else providers.ResourceUnavailable)
            try:
                return read()
            except catch as exc:
                failures[kind] = (
                    str(exc) if isinstance(exc, providers.ResourceUnavailable)
                    else f"{exc}. Point this project at it again on the Edit page.")
                print(f"{datasource_name}: {kind} is unavailable -- {exc}")
                return fallback

        if project.has_table:
            print("Loading datasource data.. (this can take some time)")
            # Somewhere for the load to say where it has got to, polled by the
            # browser while the request doing the work is still outstanding.
            stage, report = table_progress(datasource_name)
            stage("opening")
            table_error = None
            try:
                loaded = attempt(
                    "table",
                    lambda: resolved.table.load(reload=reload, stage=stage, report=report),
                    missing_ok=True)
            except Exception as exc:
                table_error = exc
                raise
            finally:
                finish_table_job(datasource_name, table_error)
            loaded_datasource = loaded.table if loaded is not None else None
        else:
            loaded_datasource = None
        print("Loading segmentation.")
        loaded_seg = attempt("segmentation", resolved.segmentation.open,
                             missing_ok=True)
        print("Loading image descriptions.")
        loaded_channels, loaded_zarray, loaded_metadata = attempt(
            "image", resolved.image.open, (None, None, {}))

        datasource = loaded_datasource
        seg = loaded_seg
        channels = loaded_channels
        zarray = loaded_zarray
        metadata = loaded_metadata
        source = datasource_name
        _providers = resolved
        _resource_errors.clear()
        _resource_errors.update(failures)
        # The one boolean every dispatch guard tests. Set with the rest of the
        # globals rather than at resolve time so a load that raises leaves the
        # previous project's routing intact instead of half-adopting the new
        # one's.
        _remote = resolved.has_remote
        if reload:
            # After the table, not before it. load_ball_tree indexes this
            # module's `datasource` global, and a reload of the project that is
            # already loaded skips its own refresh (`source` matches), so
            # building the tree first indexed the table from before the change.
            # Every path that changes what a project reads is a same-name
            # reload, and that is exactly when the coordinate columns can stop
            # existing -- swapping a CSV for an .h5ad renames them
            # X_centroid -> X, and the build then raised ColumnNotFound against
            # the very table it was replacing.
            load_ball_tree(datasource_name, reload=True)
        # Data on disk just changed underneath us (first load or explicit
        # reload) -- any cached GMM/description results are now stale.
        _gmm_cache.clear()
        # The persisted quantization windows are keyed on the image file's
        # fingerprint, so they survive a reload that did not change the image
        # -- which is the common case (a segmentation regenerated, a column
        # remapped). Only this process's memo of them is dropped, so the next
        # read re-fingerprints and finds out for itself.
        _quantization_store_cache.clear()
        _description_cache.clear()
        _image_stats_cache.clear()
        _gate_filter_cache.clear()
        _metadata_column_cache.clear()
        # Bumped so downstream tile-byte caches (keyed on this) know to
        # treat previously cached tiles as stale without needing a direct
        # reference back into this module's caches.
        load_generation += 1
        # Set last, after every global above is in place, so a concurrent
        # reader never sees _loaded_source set against a half-built state.
        _loaded_source = loaded_scope(datasource_name)
        print("Data loading done.")

    # Warm the description/GMM caches in the background so the first real
    # request after this load doesn't pay for them synchronously.
    threading.Thread(
        target=_warm_datasource_caches, args=(datasource_name,), daemon=True
    ).start()


def load_config(datasource_name):
    global config

    # The migration below only ever rewrites this datasource's own entry, so the
    # read happens outside the lock (refresh_segmentation_mapping can stat and
    # sample files, which is too slow to hold every other writer behind) and
    # only the write re-takes it, against a fresh copy of the file.
    config = Project.load_all()
    entry = config[datasource_name]
    updated = False
    # Update the feature-table path -- skipped entirely for an image-only
    # datasource, which has no source file to fix up.
    spec = (entry or {}).get('dataset')
    # Skipped for a table on a node: everything below is a filesystem fixup,
    # and `Path("node://hpc/cells")` is a valid relative path that exists
    # nowhere -- so the migration would rewrite the locator into a broken one.
    if spec and spec.get('src') and not providers.is_node_locator(spec['src']):
        original = spec['src']
        resolved = original.replace('static/data', 'plexora/data')
        if Path(resolved).exists() is False and Path('.' + resolved).exists():
            resolved = '.' + resolved
        spec['src'] = str(Path(resolved))
        if original != spec['src']:
            updated = True

    pending_segmentation_source = None
    segmentation_path = entry.get('segmentation')
    # Likewise for a mask on a node: there is nothing here to fingerprint, and
    # nothing to convert -- a node serves a pyramid that is already servable.
    if providers.is_node_locator(segmentation_path):
        segmentation_path = None
        # The one part of the refresh below that a node-backed mask still
        # needs. `segmentationMode` missing is not read as "unknown" anywhere
        # downstream: `canDrawFilled` and `renderLabelTile` both test it
        # against "filled", so absent means outlines -- Filled greyed out with
        # "this mask is stored as outlines", and a filled label pyramid painted
        # as solid blobs with Outlines selected. Projects attached against a
        # node that reported no `mask_mode` carry exactly that gap, so it is
        # closed on load rather than only at attach time.
        if not entry.get('segmentationMode'):
            entry['segmentationMode'] = segmentation_mode(entry)
            updated = True
    if segmentation_path:
        migrated_path = segmentation_path.replace('static/data', 'plexora/data')
        if migrated_path != segmentation_path:
            entry['segmentation'] = migrated_path
            updated = True
        mapping_changed, pending_segmentation_source = refresh_segmentation_mapping(
            entry, datasource_name
        )
        updated = updated or mapping_changed

    if updated:
        # Skipped rather than attempted for a project on a read-only shared
        # root. Everything above is a path fixup applied to the in-memory
        # entry, so this load works either way; what would not work is letting
        # write_config spend its two-second Windows retry budget discovering
        # that somebody else's root is not ours to rewrite, on every load.
        config_file = Project.config_path_for(datasource_name)
        if paths.is_writable(config_file.parent):
            with config_transaction():
                stored = read_config(config_file)
                # Gone means deleted while this load was in flight; re-adding
                # it here would resurrect a project the user just removed.
                if datasource_name in stored:
                    stored[datasource_name] = entry
                    write_config(config_file, stored)

    # Started only after the write above has landed, since the job patches the
    # same file when it finishes.
    if pending_segmentation_source:
        start_segmentation_job(
            datasource_name,
            pending_segmentation_source,
            paths.derived_root(datasource_name),
            segmentation_mode(config[datasource_name]),
        )


def _ball_tree_source_signature(csv_path):
    stat = csv_path.stat()
    return {
        "csv_size": stat.st_size,
        "csv_mtime_ns": stat.st_mtime_ns,
    }


def _ball_tree_signature(project):
    """What the cached spatial index was built from.

    For a local table that is the file's size and mtime. For a node-backed one
    there is no file here to stat, so it is the node's own generation and the
    fingerprint it reported -- which is strictly better information, since the
    node bumps the generation on every reload and a stat cannot see a change
    made on another machine at all.
    """
    remote = _remote_table()
    if remote is not None:
        binding = remote.binding
        return {
            "node": binding.node,
            "resource": binding.resource_id,
            "generation": remote.generation,
            "fingerprint": dict(binding.fingerprint or {}),
        }
    return _ball_tree_source_signature(Path(project.dataset.src))


def load_ball_tree(datasource_name_name, reload=False):
    global ball_tree
    global datasource
    global config
    if loaded_scope(datasource_name_name) != _loaded_source:
        load_datasource(datasource_name_name)

    project = _project(datasource_name_name)
    # `datasource is None` is a third way to have nothing to index, alongside
    # the two below: the project HAS a table and its coordinate columns are
    # named, but the node holding it was unreachable when this loaded. Same
    # answer -- no tree -- because the alternative is indexing a table that is
    # not there.
    if datasource is None or not project.has_table or not (project.roles.x and project.roles.y):
        # Nothing to build a tree from: either there is no feature table at all
        # (image-only project), or one was imported whose coordinate columns
        # nobody has identified yet -- a spatial index over columns we cannot
        # name is not something to guess at. Every direct consumer of
        # ball_tree/datasource checks for None rather than dereferencing a tree
        # that was never built.
        ball_tree = None
        return

    pickled_kd_tree_path = str(
        paths.derived_root(datasource_name_name) / "ball_tree.pickle")

    signature = _ball_tree_signature(project)

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
    xCoordinate = project.roles.x
    yCoordinate = project.roles.y
    # Reuse the feature table load_datasource already parsed instead of
    # re-reading the (potentially multi-million-row) CSV from disk again.
    points = datasource.select([xCoordinate, yCoordinate]).to_numpy()
    ball_tree = BallTree(points, metric='euclidean')
    # The tree itself is built and usable at this point; the file is only a
    # cache of it. A project directory that does not exist yet is an ordinary
    # state for a project registered without any data attached, and losing the
    # spatial index over a cache write is a much worse outcome than rebuilding
    # it next time.
    try:
        Path(pickled_kd_tree_path).parent.mkdir(parents=True, exist_ok=True)
        with open(pickled_kd_tree_path, 'wb') as tree_file:
            pickle.dump({'signature': signature, 'tree': ball_tree}, tree_file)
    except OSError as exc:
        print(f"Could not cache KD Tree ({exc}); it will be rebuilt next load.")
    print('Creating KD Tree done.')


def _ensure_loaded(datasource_name):
    """Ensure the CSV/BallTree for datasource_name is the currently loaded one."""
    if loaded_scope(datasource_name) != _loaded_source:
        load_ball_tree(datasource_name)


class UnknownChannelError(LookupError):
    """A name that is not one of this project's image channels.

    Has one real cause worth naming: renaming a project's channels (see
    plexora.datasource.rename_channels) leaves every name a page, a saved
    channel list or an in-flight warm-up pass is already holding pointing at
    nothing. Raised rather than letting the lookup below fall off the end of a
    generator with StopIteration, which carries no message, names neither the
    channel nor the project, and reaches the client as a 500.
    """


def real_channels(datasource_name):
    """This project's actual image channels, in `zarray` order.

    'Area' is a Plexora-side placeholder inserted when a segmentation mask is
    attached -- it is never part of the physical image -- so it is excluded
    here, and a position in this list IS the zarray index. Not the raw
    imageData index minus one, which was only ever correct while segmentation
    happened to put Area at position 0.
    """
    return [d for d in config[datasource_name]['imageData'] if d['fullname'] != 'Area']


def real_channel_index(channel_name, datasource_name):
    """Where `channel_name` sits in `zarray`, or UnknownChannelError."""
    for index, channel in enumerate(real_channels(datasource_name)):
        if channel['fullname'] == channel_name:
            return index
    raise UnknownChannelError(
        f"{channel_name!r} is not a channel of {datasource_name!r}. "
        "Its channels may have been renamed since this page was opened."
    )


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

    Speculative throughout, in the sense `http.speculative()` means: a project
    on a data node warms over the tunnel, and nobody is waiting for any of it.
    So no call retries, and the node going away ends the warm-up quietly
    instead of reporting a failure -- from in here, disconnecting a node and
    losing one are indistinguishable, and one of the two is something the user
    just did on purpose.
    """
    from plexora.server.providers import http as node_http

    lock = _warmup_lock_for(datasource_name)
    if not lock.acquire(blocking=False):
        return
    try:
        # Nobody is waiting for any of this, so none of it retries -- see
        # http.speculative(). A node-backed project warms over a tunnel, and
        # that tunnel going away mid-warm is not a network fault to ride out,
        # it is the user having clicked Disconnect.
        with node_http.speculative():
            get_datasource_description(datasource_name)
            to_warm = [c['fullname'] for c in config[datasource_name]['imageData']
                       if c['name'] != 'Area']
            to_warm = _saved_channels_first(datasource_name, to_warm)
            # A channel that vanished mid-pass is skipped, not fatal. `to_warm`
            # is a snapshot, and a rename landing while this runs
            # (upload_channels reloads the datasource, which starts a second
            # warm-up) would otherwise abandon every channel after the first
            # stale name.
            def warm(step, fullname):
                try:
                    step(fullname, datasource_name)
                except UnknownChannelError:
                    pass
            # Pass 1 -- everything the first paint blocks on.
            for fullname in to_warm:
                warm(get_image_channel_stats, fullname)
            # Pass 2 -- the expensive refinement nothing blocks on.
            for fullname in to_warm:
                warm(get_channel_gmm, fullname)
    except providers.ResourceUnavailable as exc:
        # Not a failure, and it must not read like one. The machine this
        # project reads from is gone -- usually because the user just
        # disconnected it -- so there is nothing here to repair and nothing
        # waiting on the work. Stop at the channel we reached and say so once,
        # plainly, instead of filing a bug report about a deliberate action.
        print(f"Stopped warming {datasource_name}: {exc}")
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
            remote = _remote_table()
            if remote is not None:
                # The tree, and the compact (id, x, y) copy it was built from,
                # are here; the row's other columns are not. One id goes out
                # and one row comes back, which is the whole reason the primary
                # does not need the table itself to answer a hover.
                obj = dict(_first_row(remote.rows(
                    datasource['id'][index[0].tolist()].to_list())))
            else:
                row = datasource[index[0].tolist()]
                obj = row.to_dicts()[0]
            if 'celltype' not in obj:
                obj['celltype'] = ''
            return obj
        except:
            return {}


def _first_row(rows):
    return rows[0] if rows else {}


def _rows_by_id(frame, ids):
    """Whole rows for the given cell ids, in the order asked for.

    Missing ids are dropped rather than reported: the only caller is a hover
    tooltip, and a cell that has gone out of the table between the tree being
    built and the pointer moving is a stale question, not an error.
    """
    wanted = [int(value) for value in ids]
    subset = frame.filter(pl.col('id').is_in(wanted))
    by_id = {int(row['id']): row for row in subset.to_dicts()}
    return [by_id[value] for value in wanted if value in by_id]


def get_channel_names(datasource_name, shortnames=True):
    global datasource
    global source
    _ensure_loaded(datasource_name)
    key = 'name' if shortnames else 'fullname'
    return [channel[key] for channel in real_channels(datasource_name)]


def get_filter_columns(datasource_name, columns):
    """Numeric numpy views of the requested columns pulled from the
    already-loaded datasource, cached (one entry at a time, like
    centroid_tiles._load_filter_table) so repeated range-filter queries on
    the same columns reuse the same arrays instead of re-deriving them per
    request. Shared core primitive -- used directly by get_channel_cells
    below, and by the gating plugin's own queries (plexora/plugins/gating/
    server/model.py) via this same function, not a private copy.
    """
    remote = _remote_table()
    if remote is None and datasource is None:
        # Defensive backstop -- callers into this shared primitive
        # (get_channel_cells above, the gating module) should already
        # short-circuit on project.has_table before reaching here.
        return {}
    key = (datasource_name, tuple(sorted(set(columns))))
    cached = _gate_filter_cache.get(key)
    if cached is not None:
        return cached
    # Cached on THIS server even when the table is on a node, and deliberately.
    # Gating moves a slider and asks for a mask per tick; going back to the
    # node for the same marker columns each time would put a network round trip
    # under an interaction that is currently instant. The columns are pulled
    # once and every subsequent tick is local arithmetic -- see
    # apply_range_mask, which never leaves this process.
    if remote is not None:
        cols = remote.filter_columns(columns)
    else:
        cols = _filter_columns_from_frame(datasource, columns)
    _gate_filter_cache.clear()
    _gate_filter_cache[key] = cols
    return cols


def _filter_columns_from_frame(frame, columns):
    return {
        c: frame[c].cast(pl.Float32, strict=False).fill_null(float('nan')).to_numpy()
        for c in columns
    }


def _frame_metadata_column(name, series):
    """A polars column as a MetadataColumn, keeping a declared level order."""
    categories = None
    dtype = series.dtype
    if dtype in (pl.Categorical, pl.Enum):
        # Polars states the level order for these two dtypes the same way
        # pandas does for a Categorical, and for the same reason -- so honour it
        # here rather than letting the legend fall back to sorting.
        categories = tuple(str(v) for v in series.cat.get_categories().to_list())
        series = series.cast(pl.Utf8)
    return MetadataColumn(name=name, values=series.to_numpy(), categories=categories)


def get_metadata_column(datasource_name, column):
    """One annotation column's values, aligned row-for-row with the table.

    Two sources, one answer. A CSV's loaded frame holds every column of the
    file, so the frame is it. AnnData and SpatialData are the reason this
    function exists: their adapters materialize only id/X/Y/the id field/the
    markers/the celltype column, so an arbitrary `.obs` column is listed by
    `TableHandle.metadata_columns` and is nowhere in `frame()`. Asking the
    adapter to read that one column keeps the alignment (it applies the same
    subset) without re-importing the file.

    Raises KeyError for a column neither place has -- which is the honest answer
    for a stale saved preference naming a column the data no longer carries.
    """
    _ensure_loaded(datasource_name)
    key = (datasource_name, column)
    cached = _metadata_column_cache.get(key)
    if cached is not None:
        return cached

    remote = _remote_table()
    frame = get_datasource_df()
    if remote is not None:
        # The node applies the same two-place lookup and the same length check
        # against ITS loaded frame, which is the only copy that can answer the
        # obs half at all -- the file is not on this machine.
        result = remote.metadata_column(column)
    elif frame is not None and column in frame.columns:
        result = _frame_metadata_column(column, frame[column])
    else:
        result = _read_metadata_column(datasource_name, column)
        if frame is not None and len(result.values) != frame.height:
            # Loud on purpose. A length mismatch means the obs read and the
            # loaded table disagree about which cells they describe, and the
            # values would then be attached to whichever cells happen to sit at
            # those row numbers -- a picture that looks entirely plausible and
            # is wrong. Better no overlay than a convincing one.
            raise ValueError(
                f"metadata column {column!r} has {len(result.values)} values but "
                f"the loaded table has {frame.height} rows"
            )

    if len(_metadata_column_cache) >= _METADATA_COLUMN_CACHE_MAX:
        _metadata_column_cache.pop(next(iter(_metadata_column_cache)))
    _metadata_column_cache[key] = result
    return result


def _read_metadata_column(datasource_name, column):
    project = _project(datasource_name)
    if not project.has_table:
        raise KeyError(column)
    adapter = get_adapter(project.dataset.type)(project.dataset)
    read = getattr(adapter, "read_obs_column", None)
    result = read(column) if read is not None else None
    if result is None:
        # Either the format has no second place to look (CSV), or it is an
        # adapter written before this method existed. Both mean the loaded
        # frame was the whole answer, and it did not have the column.
        raise KeyError(column)
    return result


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
    if not _project(datasource_name).has_table:
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
        csvPath = _project(datasource).dataset.celltype_data
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
    return _project(datasource).roles.celltype or ''


def _all_cells_from_frame(frame, start_keys, data_type):
    """Whole columns as one flat numpy array, in the wire dtype.

    Pure over the frame for the same reason `_describe_frame` is -- a node
    computes this over its own loaded copy, and a node has several.
    """
    query = frame.select(start_keys).to_numpy().flatten()
    if np.issubdtype(data_type, int):
        return query.astype(np.uint32)
    return query.astype(np.float32)


def get_all_cells(datasource_name, start_keys, data_type=float):
    global datasource
    global source

    # Load if not loaded
    _ensure_loaded(datasource_name)
    if not _project(datasource_name).has_table:
        return np.array([], dtype=np.uint32 if np.issubdtype(data_type, int) else np.float32)

    remote = _remote_table()
    if remote is not None:
        return remote.all_cells(start_keys, data_type)
    return _all_cells_from_frame(datasource, start_keys, data_type)


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


def rename_saved_channels(datasource_name, renames):
    """Point the project's saved channel list at the new names.

    The other half of plexora.datasource.rename_channels, and the half that is
    easy to forget: the saved list holds channel NAMES, and it is what the
    sidebar restores its channel slots from on every page load. Renaming the
    image's channels without it left the next load rebuilding a slot for a
    channel that no longer exists -- it reads as an extra marker that matches
    nothing, and the stats request that slot then makes is exactly the one that
    used to come back as a StopIteration out of get_image_channel_stats.

    @param renames every OLD spelling -> the new name. Short name and fullname
                   both, since a project registered before a rename can have
                   the two differ; a row naming a channel that was not renamed
                   is left alone.
    @returns whether anything was written.
    """
    saved = get_saved_channel_list(datasource_name)
    if not saved:
        return False
    changed = False
    for row in saved:
        renamed = renames.get(row.get('channel'))
        if renamed is not None and renamed != row.get('channel'):
            row['channel'] = renamed
            changed = True
    if not changed:
        return False
    database_model.save_list(database_model.ChannelList, datasource=datasource_name,
                             cells=pickle.dumps(saved, protocol=4))
    return True


def _describe_column(values):
    """Stats plus a 50-bin histogram for one column's values.

    One `values` array in, one dict out, and nothing of the column survives the
    return -- which is the whole point. This used to be two passes over the
    WHOLE table: `df.select(cs.numeric()).cast(pl.Float64).to_numpy()` built a
    single dense N x C float64 array (1.9 GB at 6M cells x 40 markers, and
    `np.nanpercentile` partitions a copy of it on top), and then a second
    per-column pass computed the histograms. Peak is now N x 1 instead of
    N x C, which is what lets a table larger than memory be described at all.

    The finite mask is taken once and reused by both halves. `nan`-aware
    reductions would each re-scan for nulls, and the histogram needs the
    filtered array anyway -- so filtering up front is both cheaper and the only
    way the two halves are guaranteed to describe the same rows.
    """
    values = np.asarray(values)
    # One filter, reused by both halves. The `np.nan*` family looks like the
    # obvious tool and is the wrong one: each call re-scans for nulls AND
    # allocates a copy of the column, so six of them cost six copies -- measured
    # at +461 MB for a single 183 MB column. Filtering once costs one.
    #
    # `isfinite` rather than `~isnan` is a deliberate change: the old
    # whole-table `np.nanmin`/`np.nanpercentile` ignored NaN but PROPAGATED
    # +/-inf, so one infinite value made `std`/`max` infinite and collapsed the
    # 50-bin histogram into a single bin. A column carrying an infinity is
    # pathological data, and a usable summary of the finite rest beats a
    # summary that is all infinities.
    finite = values[np.isfinite(values)]
    count = int(finite.size)
    if count == 0:
        # Every reduction below is undefined on an empty array and numpy says
        # so with a warning and a nan. A column that is entirely null is an
        # ordinary thing for a table to carry, so it gets zeros and an empty
        # histogram rather than a log full of RuntimeWarnings.
        return {'count': 0, 'mean': 0.0, 'std': 0.0, 'min': 0.0,
                '25%': 0.0, '50%': 0.0, '75%': 0.0, 'max': 0.0,
                'histogram': []}

    q25, q50, q75 = np.percentile(finite, [25, 50, 75])
    hist, bin_edges = np.histogram(finite, bins=50, density=True)
    midpoints = (bin_edges[1:] + bin_edges[:-1]) / 2
    return {
        'count': count,
        # `dtype=np.float64` is not optional once the stored column is float32:
        # summing six million float32 values in a float32 accumulator loses
        # several significant digits. This is the one thing the old
        # whole-table `cast(pl.Float64)` was buying, bought back one column at
        # a time instead of for the entire table at once.
        'mean': float(np.mean(finite, dtype=np.float64)),
        # ddof=1 on a single finite value is a divide-by-zero; numpy returns
        # nan, which does not survive JSON. Matches what the old whole-table
        # `np.nanstd(..., ddof=1)` produced for such a column, minus the nan.
        'std': float(np.std(finite, dtype=np.float64, ddof=1)) if count > 1 else 0.0,
        'min': float(np.min(finite)),
        '25%': float(q25),
        '50%': float(q50),
        '75%': float(q75),
        'max': float(np.max(finite)),
        'histogram': [{'x': midpoints[i], 'y': hist[i]} for i in range(len(hist))],
    }


def _describe_frame(frame):
    """The `dd` payload for one table: per-column stats plus a 50-bin histogram.

    A pure function of the frame, deliberately. It is called with this module's
    `datasource` global on the primary and with a node's own loaded copy on a
    node -- and a node serves several tables at once, so nothing that computes
    a table's contents may reach for the single-loaded-datasource globals.
    Every frame computation shared with the node side has this shape.

    One column at a time, and never the whole frame at once: `frame` is
    memory-mapped, so pulling a column touches only that column's pages and
    they are droppable again the moment this loop moves on.
    """
    # Read off the schema rather than `frame.select(cs.numeric())`: the names
    # are all this needs, and asking the schema cannot touch a single page.
    numeric = [name for name, dtype in frame.schema.items() if dtype.is_numeric()]
    return {column: _describe_column(frame[column].to_numpy()) for column in numeric}


def get_datasource_description(datasource_name):
    global datasource
    global source
    global ball_tree
    global config

    # Load if not loaded
    _ensure_loaded(datasource_name)

    if datasource_name in _description_cache:
        return _description_cache[datasource_name]

    if not _project(datasource_name).has_table:
        _description_cache[datasource_name] = {}
        return {}

    remote = _remote_table()
    if remote is not None:
        description = remote.describe()
    else:
        description = _describe_frame(datasource)

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
        remote = _remote_image()
        if remote is not None:
            # Fitted on the node, over pixels that never leave it. Cached here
            # anyway: the packet is a few hundred floats and the single-flight
            # lock above is worth just as much against a network call as
            # against a local one -- more, since a burst of tile requests would
            # otherwise open a burst of connections for the same answer.
            packet_gmm = remote.gmm(channel_name)
            _gmm_cache[cache_key] = packet_gmm
            return packet_gmm
        return _compute_channel_gmm(channel_name, datasource_name, cache_key)


# Quantization windows survive a restart, unlike everything else in
# _gmm_cache. Keyed per datasource as (fingerprint, {channel: (qmin, qmax)}),
# memoized here so reading one channel's window is not one sqlite open per
# channel. Cleared alongside _gmm_cache when a datasource actually reloads.
_quantization_store_cache = {}
_quantization_store_lock = threading.Lock()


def _image_fingerprint(datasource_name):
    """Identity of the image file a window was derived from, or None.

    Size and mtime rather than a content hash: the file is routinely gigabytes
    and often on a network filesystem, so hashing it would cost strictly more
    than the full-resolution read this whole cache exists to avoid. Any rewrite
    that could change a channel's maximum changes at least one of the two.

    None means "cannot be established", and every caller treats that as a cache
    miss rather than as a match -- serving a stale ceiling would saturate a
    channel to a solid colour, which is far worse than re-reading it.
    """
    entry = (config or {}).get(datasource_name) or {}
    channel_file = entry.get('channelFile')
    if not channel_file:
        return None
    try:
        stat = os.stat(channel_file)
    except OSError:
        return None
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _quantization_windows(datasource_name):
    """Every persisted window for this datasource, or {} if none are usable.

    Best-effort throughout: a missing table, an unreadable row or a blob written
    by a future version all read as "nothing cached", because the only cost of
    being wrong here is recomputing something.
    """
    fingerprint = _image_fingerprint(datasource_name)
    if fingerprint is None:
        return {}
    with _quantization_store_lock:
        cached = _quantization_store_cache.get(datasource_name)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
    try:
        row = database_model.get(database_model.ChannelQuantization,
                                 datasource=datasource_name)
        stored = json.loads(row.cells) if row is not None else {}
    except Exception:
        stored = {}
    # A fingerprint mismatch is the image having changed under a cache written
    # for the previous one; drop the lot rather than trying to tell which
    # channels are still valid.
    windows = {}
    if isinstance(stored, dict) and stored.get('fingerprint') == fingerprint:
        for name, pair in (stored.get('windows') or {}).items():
            try:
                windows[name] = (float(pair[0]), float(pair[1]))
            except (TypeError, ValueError, IndexError):
                continue
    with _quantization_store_lock:
        _quantization_store_cache[datasource_name] = (fingerprint, windows)
    return windows


def _remember_quantization_window(datasource_name, channel_name, window):
    """Add one channel's window to the persisted set.

    Read-modify-write under a lock, because several channels can finish their
    reads at once and a last-writer-wins race would silently drop every window
    but one -- turning a cache that fills up over one session into one that
    never holds more than a single channel.

    JSON rather than the pickle the channel list uses: this is a handful of
    floats, and a format that cannot execute anything is the better default for
    new data even in a file the user owns.
    """
    fingerprint = _image_fingerprint(datasource_name)
    if fingerprint is None:
        return
    with _quantization_store_lock:
        cached = _quantization_store_cache.get(datasource_name)
        windows = dict(cached[1]) if cached is not None and cached[0] == fingerprint else {}
        windows[channel_name] = (float(window[0]), float(window[1]))
        _quantization_store_cache[datasource_name] = (fingerprint, windows)
        payload = json.dumps({
            'fingerprint': fingerprint,
            'windows': {name: list(pair) for name, pair in windows.items()},
        }).encode('utf-8')
        try:
            database_model.save_list(database_model.ChannelQuantization,
                                     datasource=datasource_name, cells=payload)
        except Exception as exc:
            # Never fatal: the window is already in memory and correct, and a
            # project on a read-only or full filesystem must still open.
            print(f"Could not persist quantization window for "
                  f"{datasource_name}/{channel_name}: {exc}")



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
        # Consulted inside the compute lock, not before it: the point is that
        # exactly one thread does the expensive thing, and a hit here is the
        # cheap path that makes a restart free. Persisted per project and keyed
        # on the image file's fingerprint -- see _quantization_windows.
        stored = _quantization_windows(datasource_name).get(channel_name)
        if stored is not None:
            _gmm_cache[cache_key] = stored
            return stored
        remote = _remote_image()
        if remote is not None:
            # The full-resolution read this needs is the one thing that must
            # not cross a network: the whole point of the window is that it
            # comes from every pixel of the channel plane.
            window = remote.quantization_window(channel_name)
            _gmm_cache[cache_key] = window
            return window
        idx = real_channel_index(channel_name, datasource_name)
        window = quantization_window_of(channels, idx)
        _gmm_cache[cache_key] = window
        _remember_quantization_window(datasource_name, channel_name, window)
        return window


#: How much of a plane one slab of the window scan materializes. The scan
#: below used to be `np.asarray(plane[index]).max()` -- the WHOLE plane in one
#: slice, which for a wide slide is gigabytes in one numpy call. On a data
#: node that was not merely memory: six channels arriving at once (a browser
#: opens that many tile connections) each materialized a plane in parallel,
#: and a process grinding through back-to-back multi-gigabyte reads answers
#: nothing else -- not even /health -- for as long as the pile lasts. Slabs
#: keep the peak memory flat and give every other thread a turn between reads.
_WINDOW_SCAN_SLAB_BYTES = 128 * 2**20

#: At most this many full-resolution window scans at once, process-wide. The
#: single-flight in the node's `_cached` already stops two readers scanning the
#: SAME channel; this is about six readers scanning six DIFFERENT channels of
#: one file on a network filesystem, which turns six sequential-read minutes
#: into one seek-bound quarter of an hour. Two rather than one so a fast local
#: disk still overlaps a little; the win is bounding it at all.
_WINDOW_SCAN_GATE = threading.BoundedSemaphore(2)


def quantization_window_of(channel_pyramid, index):
    """(qmin, qmax) read off a channel's full-resolution plane.

    Pure over the pyramid, so a node computes it for its own image the same way
    -- see `get_channel_quantization_window` above for why the ceiling cannot
    come from the downsampled overview.

    Every pixel is still read -- that is the point of the window -- but in
    bounded slabs, at most two scans at a time. See the two constants above
    for what each bound is protecting.
    """
    plane = (channel_pyramid if isinstance(channel_pyramid, zarr.Array)
             else _zarr_level(channel_pyramid, 0))
    if getattr(plane, "ndim", 0) != 3:
        # The unusual shapes keep the old one-shot behaviour: they are the
        # small cases, and guessing which axis to slab on would be worse.
        return (0.0, max(float(np.asarray(plane[index]).max()), 1.0))
    height, width = plane.shape[-2], plane.shape[-1]
    itemsize = max(1, getattr(getattr(plane, "dtype", None), "itemsize", 2))
    rows = max(1, _WINDOW_SCAN_SLAB_BYTES // max(1, width * itemsize))
    ceiling = 0.0
    with _WINDOW_SCAN_GATE:
        for start in range(0, height, rows):
            slab = np.asarray(plane[index, start:start + rows])
            if slab.size:
                ceiling = max(ceiling, float(slab.max()))
    return (0.0, max(ceiling, 1.0))


def _compute_channel_gmm(channel_name, datasource_name, cache_key):
    global datasource
    global source
    global ball_tree
    global config

    image_channelIdx = real_channel_index(channel_name, datasource_name)
    qmin, qmax = get_channel_quantization_window(channel_name, datasource_name)
    packet_gmm = channel_gmm_of(
        _require_overview(datasource_name)[image_channelIdx], qmin, qmax)
    _gmm_cache[cache_key] = packet_gmm
    return packet_gmm


def channel_gmm_of(image_data, qmin, qmax):
    """The GMM packet for one channel's overview plane.

    Pure over the array and the window, so a node fits its own image with the
    identical code. `image_data` is the mean-pooled overview -- the same domain
    the histogram below is plotted in -- while the window comes from
    full-resolution data; see `get_image_channel_stats` for why mixing those up
    saturates whole channels.
    """
    packet_gmm = {}

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

    NOTE ON DOMAINS: image_min/image_max/image_histogram (and the hints) are
    all computed from `zarray`, the mean-pooled overview -- they are a matched
    set, and image_histogram is only meaningful plotted against image_min/
    image_max. They are NOT full-resolution statistics: pooling dilutes real
    single/few-pixel peaks, so image_max sits well below the brightest pixel
    encode_tile() actually serves (see get_channel_quantization_window for the
    same trap saturating whole channels). Anything that needs the channel's
    true ceiling -- the HD slider's domain, for one -- must use qmax, which is
    computed from full-resolution data. Do not "fix" image_max to be the real
    max: that silently desynchronizes it from image_histogram.
    """
    global zarray
    global config

    _ensure_loaded(datasource_name)

    cache_key = (datasource_name, channel_name)
    if cache_key in _image_stats_cache:
        return _image_stats_cache[cache_key]

    remote = _remote_image()
    if remote is not None:
        stats = remote.channel_stats(channel_name)
        _image_stats_cache[cache_key] = stats
        return stats

    image_channelIdx = real_channel_index(channel_name, datasource_name)
    qmin, qmax = get_channel_quantization_window(channel_name, datasource_name)
    stats = channel_stats_of(
        _require_overview(datasource_name)[image_channelIdx], qmin, qmax)
    _image_stats_cache[cache_key] = stats
    return stats


def _require_overview(datasource_name):
    """The mean-pooled overview array, or a sentence saying why there isn't one.

    `zarray` is None in exactly one situation a caller can do something about:
    the image is on a node that was unreachable when this project loaded. Every
    function that indexes it goes through here, because the alternative is a
    TypeError from inside numpy that names neither the project nor the node.
    """
    if zarray is not None:
        return zarray
    why = _resource_errors.get("image")
    if why:
        raise providers.ResourceUnavailable(
            f"{datasource_name}'s image could not be read: {why}")
    raise providers.ResourceError(
        f"{datasource_name} has no image loaded, so there is nothing to "
        f"measure. Reopen the project.")


def channel_stats_of(image_data, qmin, qmax):
    """One channel's stats packet, pure over the overview plane and the window.

    Shared with the node side unchanged. See `get_image_channel_stats` for the
    note on domains -- the histogram and the min/max are of `image_data`, the
    window is not, and they must not be reconciled.
    """
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

    return {
        'image_histogram': dat,
        'image_min': np.ceil(np.exp(np.min(img_log))),
        'image_max': np.ceil(np.exp(np.max(img_log))),
        'qmin': qmin,
        'qmax': qmax,
        'vmin_hint': float(np.rint(np.exp(np.percentile(img_log, pmin)))),
        'vmax_hint': float(np.rint(np.exp(np.percentile(img_log, pmax)))),
    }


def ensure_loaded(datasource_name):
    """Load `datasource_name` if it isn't already the loaded one, and return the
    resulting load_generation.

    Callers that key a cache on load_generation must call this BEFORE reading
    the generation: loading is what bumps it, so a generation sampled first
    would key the entry under the pre-load value and be missed by every
    subsequent request.
    """
    if _loaded_source != loaded_scope(datasource_name):
        load_datasource(datasource_name)
    return load_generation


def generate_zarr_png(datasource_name, channel, level, tile):
    global channels
    global seg
    ensure_loaded(datasource_name)
    channel_num, segmentation = _parse_channel(channel)
    return read_tile(
        seg if segmentation else channels, channel_num, level, tile,
        config[datasource_name]['tileWidth'],
        config[datasource_name]['tileHeight'],
    )


def read_tile(pyramid, channel_num, level, tile, tile_width, tile_height):
    """One tile's raw pixels, pure over the pyramid it comes from.

    `channel_num` is None for a label mask, which is also what says the array
    is 2-D rather than (channel, y, x) -- the same signal `_parse_channel`
    produces, carried through so a node reading a mask and a node reading an
    image share this one function. It is the `rgb` sentinel for a brightfield
    image, whose one layer is all three samples at once.
    """
    [tx, ty] = str(tile).replace('.png', '').split('_')
    tx = int(tx)
    ty = int(ty)
    level = int(level)
    ix = tx * tile_width
    iy = ty * tile_height
    if channel_num == brightfield.RGB_CHANNEL_KEY:
        # (y, x, 3) uint8, straight from the file's own colour samples. No
        # promotion to uint16 and no quantization window: these bytes ARE the
        # picture, and the browser draws them without a GL pass (see
        # imageViewer.js's tileFormat 24 early-returns).
        return brightfield.rgb_region(
            _zarr_level(pyramid, level),
            slice(iy, iy + tile_height), slice(ix, ix + tile_width))
    if channel_num is None:
        tile = _zarr_level(pyramid, level)[iy:iy + tile_height, ix:ix + tile_width]
        if tile.dtype.itemsize != 4:
            tile = tile.astype(np.uint32)
        tile = tile.view('uint8').reshape(tile.shape + (-1,))[..., [0, 1, 2]]
        tile = np.append(tile, np.zeros((tile.shape[0], tile.shape[1], 1), dtype='uint8'), axis=2)
    else:
        if isinstance(pyramid, zarr.Array):
            tile = pyramid[channel_num, iy:iy + tile_height, ix:ix + tile_width]
        else:
            tile = _zarr_level(pyramid, level)[channel_num, iy:iy + tile_height, ix:ix + tile_width]
            tile = tile.astype('uint16')

    return tile


def _parse_channel(channel):
    """Returns (channel_num, is_segmentation) for a channel identifier like
    "<file>_<N>" (channel_num=N), or a segmentation/label channel name with
    no trailing "_<N>" (is_segmentation=True).

    A brightfield image names no index -- its one layer carries all three
    samples -- so its key is the bare sentinel `rgb`, returned as-is. That
    case has to be tested FIRST: "rgb" does not match the `_<N>` pattern, so
    the fallback below would classify an H&E tile request as a label mask and
    read it through the segmentation pyramid."""
    if channel == brightfield.RGB_CHANNEL_KEY:
        return brightfield.RGB_CHANNEL_KEY, False
    try:
        return int(re.match(r".*_(\d*)$", channel).groups()[0]), False
    except AttributeError:
        return None, True


def _channel_num_to_name(datasource_name, channel_num):
    return real_channels(datasource_name)[channel_num]['fullname']

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
    ensure_loaded(datasource_name)
    channel_num, is_segmentation = _parse_channel(channel)

    if _remote:
        # Encoded on the node and forwarded verbatim, never decoded and
        # re-encoded here. The wire format is identical at both ends -- same
        # quantization window, same encoder settings -- so a re-encode would
        # cost a WebP round trip per tile and degrade the bytes for nothing.
        # The caller's tile LRU caches what comes back either way.
        node = _remote_segmentation() if is_segmentation else _remote_image()
        if node is not None:
            return (node.tile(level, tile) if is_segmentation
                    else node.tile(channel, level, tile, quality))

    array = generate_zarr_png(datasource_name, channel, level, tile)

    if (is_segmentation or quality in ('hd', 'legacy')
            or channel_num == brightfield.RGB_CHANNEL_KEY):
        # A brightfield tile skips both steps below: there is no channel name
        # to look up (the sentinel names no index) and no window to quantize
        # with (the samples are already the 8-bit colour the file recorded).
        return encode_tile_array(array, is_segmentation, quality)

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
    return encode_tile_array(array, is_segmentation, quality, qmin, qmax)


def encode_tile_array(array, is_segmentation, quality, qmin=None, qmax=None):
    """(bytes, mimetype) for one tile's pixels.

    Pure over the array and the window, so a node produces byte-identical tiles
    for the identical inputs -- which is what lets the primary forward a node's
    tile verbatim instead of decoding and re-encoding it.
    """
    if is_segmentation:
        return fast_png.encode_rgba8_png(array), 'image/png'

    if array.ndim == 3 and array.shape[-1] == 3:
        # A brightfield tile: three interleaved samples, already 8-bit, drawn
        # by the browser as-is. Lossy WebP is safe here in a way it is not for
        # a channel tile -- nothing multiplies these bytes afterwards, because
        # there is no contrast window and no colorize pass on top, so a JPEG-
        # scale error stays a JPEG-scale error instead of being amplified.
        # `q=hd` and `q=legacy` still get PNG, so "give me the exact pixels"
        # means the same thing on this path as on every other.
        file_object = io.BytesIO()
        if quality in ('hd', 'legacy'):
            Image.fromarray(array, mode='RGB').save(
                file_object, 'PNG', compress_level=0)
            return file_object.getvalue(), 'image/png'
        Image.fromarray(array, mode='RGB').save(
            file_object, 'WEBP', quality=85, method=0)
        return file_object.getvalue(), 'image/webp'

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


def generate_channel_overview(datasource_name, channel_name):
    """One channel's whole-tissue overview, for the viewer's mini-map.

    Returns WebP bytes, or None if `channel_name` is not one of this
    datasource's image channels.

    Two properties make this the right source, and both are easy to lose:

    - It is `zarray`, the downsampled array load_datasource() already holds
      (~200-400 px per side, every channel, resident for the loaded project).
      So this costs a quantize and an encode, with no zarr read at all. The
      tile route cannot substitute: `tileWidth` is fixed at 1024 while pyramid
      depth is whatever the source file happened to be written with, so "the
      coarsest level" is a 1x1 tile grid for some files and 4x4 for others,
      and there is no level that is reliably one whole-image tile.
    - It is quantized with get_channel_quantization_window() -- the SAME window
      encode_tile() uses -- so the bytes land in the same [0, 255] domain the
      contrast slider works in, and the mini-map needs no colour conversion of
      its own to match what the main viewer draws.

    Note the window comes from full-resolution data even though the pixels
    here do not. That split is deliberate: get_channel_quantization_window()'s
    docstring records that deriving the ceiling FROM `zarray` saturates whole
    channels, because mean-pooling dilutes the real peaks. Pooled pixels
    against a full-res ceiling is correct; a pooled ceiling is not.
    """
    _ensure_loaded(datasource_name)

    remote = _remote_image()
    if remote is not None:
        return remote.overview(channel_name)

    if config[datasource_name].get('image_kind') == brightfield.BRIGHTFIELD:
        # One layer, three samples, no window -- the same asymmetry the tile
        # path has. `channel_name` is not consulted at all: a brightfield
        # project has exactly one image layer to be asking about.
        return encode_rgb_overview(_require_overview(datasource_name))

    try:
        image_channelIdx = real_channel_index(channel_name, datasource_name)
    except UnknownChannelError:
        # No image rather than an error: the mini-map is decoration, and a
        # missing thumbnail is a better answer here than a failed request.
        return None

    qmin, qmax = get_channel_quantization_window(channel_name, datasource_name)
    return encode_overview(
        _require_overview(datasource_name)[image_channelIdx], qmin, qmax)


def encode_overview(image_data, qmin, qmax):
    """One channel's overview as WebP bytes, pure over the plane and window."""
    span = qmax - qmin  # qmax is guarded >= 1 and qmin is 0, so span >= 1
    quantized = _quantize_to_uint8(np.asarray(image_data), qmin, span)

    file_object = io.BytesIO()
    # Fully opaque mode 'L', so the browser-side WebP alpha corruption that
    # rules WebP out for label tiles cannot apply here.
    #
    # Lossless, unlike the tile path. Measured on this array (298x357):
    # lossless is 50408 B / 3.0 ms and byte-exact, quality=90 is 23514 B /
    # 3.4 ms with a max error of 11 grey levels. Tiles can afford that error
    # because they are viewed at their own scale; the mini-map cannot, because
    # the client applies the contrast window ON TOP of these bytes, and a
    # narrow window multiplies a small byte error into a large visible one.
    # 27 KB once per channel is not worth an artefact the slider amplifies.
    Image.fromarray(quantized, mode='L').save(file_object, 'WEBP', lossless=True, method=0)
    return file_object.getvalue()


def encode_rgb_overview(image_data):
    """A brightfield overview as WebP bytes, pure over the (3, y, x) plane.

    Lossless for the same reason `encode_overview` is -- the mini-map is small
    and drawn once -- but with no window to apply, because the samples are
    already the colour the file recorded.
    """
    array = np.asarray(image_data)
    if array.ndim == 3 and array.shape[0] == 3:
        array = np.moveaxis(array, 0, -1)
    if array.dtype != np.uint8:
        array = np.clip(array, 0, 255).astype(np.uint8)
    file_object = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(array), mode='RGB').save(
        file_object, 'WEBP', lossless=True, method=0)
    return file_object.getvalue()


def prime_hot_code(log=None):
    """Every lazy import and first-use initializer on the tile and contrast
    paths, paid once, here, on one thread, before any request exists.

    A cold process otherwise pays these inside its first requests: the first
    `Image.save` runs `PIL.Image.init()`, which imports some twenty format
    plugins; the first `GaussianMixture.fit` has threadpoolctl walk the
    process's shared libraries; the first tile decode pulls in one imagecodecs
    extension per compression it meets. Slow -- but the reason this function
    exists is worse than slow. Importing a C extension runs dlopen() with the
    GIL held (CPython does not release it there) and dlopen takes glibc's
    loader lock; threadpoolctl's walk uses dl_iterate_phdr(), which takes the
    loader lock and then needs the GIL back for its ctypes callback. One
    thread doing each at the same moment deadlocks the whole interpreter --
    every thread frozen mid-line, /health included, forever. Watched live on
    a cluster node: a reconnected viewer's tile burst (first save, so PIL
    plugin imports) met its own contrast warm-up (first fits, so threadpoolctl
    walks), and py-spy read identical stacks twenty seconds apart from a
    process at 2.8% CPU with all 77 threads sleeping. macOS has no
    dl_iterate_phdr, which is why the identical project loaded locally on a
    laptop never once hung.

    Nothing here may raise: a codec or plugin this environment lacks is one no
    request served from this environment can need, so each step reports and
    moves on.
    """
    def step(name, run):
        try:
            run()
        except Exception as exc:
            if log:
                log(f"  could not pre-load {name}: {exc}")

    step("PIL's format plugins", Image.init)

    def encoders():
        tiny = np.arange(16, dtype=np.uint16).reshape(4, 4)
        encode_tile_array(tiny, False, 'webp', qmin=0.0, qmax=15.0)
        encode_tile_array(tiny, False, 'hd')
        encode_tile_array(np.zeros((4, 4), dtype=np.uint8), False, 'legacy')
        encode_tile_array(np.zeros((4, 4, 4), dtype=np.uint8), True, 'webp')
        encode_overview(tiny, 0.0, 15.0)
    step("the tile encoders", encoders)

    def mixture():
        import warnings

        # Three components over three decades, like a real channel; max_iter
        # is tiny because nothing reads the fit -- what matters is that
        # sklearn's k-means init, its OpenMP pool and threadpoolctl's library
        # walk all happen NOW, on this thread, with no import anywhere else.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            GaussianMixture(3, max_iter=5).fit(
                np.linspace(0.0, 6.0, 96).reshape(-1, 1))
        norm(0.0, 1.0).pdf(np.zeros(3))
    step("the mixture fit", mixture)

    def codecs():
        import importlib

        # The compressions OME-TIFFs actually arrive in. Each is its own
        # C extension that tifffile dlopens on first contact; an exotic one
        # missing from this list stays a (tiny) race, an exotic one missing
        # from the environment is skipped because nothing here can need it.
        for name in ("_zlib", "_deflate", "_lzw", "_zstd", "_webp", "_png",
                     "_jpeg8", "_jpeg2k", "_lerc"):
            try:
                importlib.import_module(f"imagecodecs.{name}")
            except Exception:
                continue
    step("the image codecs", codecs)


#: The largest coarse level a thumbnail will pull across a network, in pixels.
#:
#: Only the node path consults it. Locally the chosen level is a memory map of
#: a file already on this disk; over a tunnel it is bytes on a wire, and the
#: rule below can land on a big level -- a pyramid that stops early, or an
#: image written without one, where the coarsest level IS the picture. Four
#: megapixels is ~8 MB of uint16, which is a slow card; past that it is a card
#: that never arrives, and the placeholder icon is the better answer.
_NODE_THUMBNAIL_PIXELS = 4_000_000

#: How long a thumbnail may wait on a node.
#:
#: The Open Project grid asks for every card at once, so this is a hold on a
#: request thread as much as it is a wait: at the default read timeout, one
#: sleeping laptop would pin a Waitress thread per node-backed project on the
#: page. A thumbnail that does not arrive costs a grey rectangle.
_NODE_THUMBNAIL_TIMEOUT = 15.0


def generate_thumbnail(datasource_name, max_size=320):
    """Cheap preview image for the Open Project grid. Deliberately does NOT
    go through load_datasource/encode_tile -- those pull in the full feature
    table, ball tree and segmentation (see load_datasource above), which is
    far more than a thumbnail needs and would make browsing a page of many
    projects trigger a full data load per card. It reads one coarse pyramid
    level of the first channel, so it never touches the shared
    source/channels/seg globals other requests depend on.

    Where that level is read from is the only thing that differs between a
    local project and one whose image lives on a node. The stretch below is
    the same either way, deliberately: two normalizations would mean the same
    tissue looked like two different pictures depending on which machine it
    happened to be stored on.

    Returns (encoded_bytes, mimetype), or None if the project has no
    channel image yet or it can't be read.
    """
    project = Project.find(datasource_name)
    if project is None:
        return None
    binding = project.resource('image')
    if binding is not None and binding.is_node:
        array = _node_thumbnail_plane(project, binding)
    else:
        array = _local_thumbnail_plane(project.image.src, project.image.pyramid)
    if array is None:
        return None

    array = array.astype(np.float32)
    low, high = np.percentile(array, [1, 99])
    span = max(high - low, 1)
    quantized = np.clip((array - low) / span * 255, 0, 255).astype(np.uint8)

    image = Image.fromarray(quantized, mode='L')
    image.thumbnail((max_size, max_size))
    file_object = io.BytesIO()
    image.save(file_object, 'WEBP', quality=85, method=6)
    return file_object.getvalue(), 'image/webp'


def _local_thumbnail_plane(channel_file, pyramid=None):
    """The first channel of a coarse level, off this machine's own disk.

    The smallest pyramid level with both dims >= 200 -- the same
    level-selection heuristic load_datasource uses for its own overview array.

    `pyramid` is the project's derived coarse levels for an OME-Zarr image, so a
    store that arrived without any gets its thumbnail from the derived ones
    rather than by decoding a full-resolution plane for a card in a grid.
    """
    if not channel_file or not Path(channel_file).exists():
        return None
    from plexora.server.utils import ome_zarr

    if ome_zarr.is_zarr_image_path(channel_file):
        try:
            array = ome_zarr.overview_plane(
                ome_zarr.open_image(channel_file, extension=pyramid))
        except Exception:
            return None
        return array[0] if array.ndim == 3 else array
    try:
        channel_io = tf.TiffFile(channel_file, is_ome=False)
        level_series = next(
            level for level in reversed(channel_io.series[0].levels)
            if all(d >= 200 for d in level.shape[1:])
        )
        array = np.asarray(zarr.open(level_series.aszarr()))
    except Exception:
        return None
    return array[0] if array.ndim == 3 else array


def _node_thumbnail_plane(project, binding):
    """The same plane, read off the node that holds the image.

    A node-backed project has no `channelFile` on this machine, so the local
    read finds nothing -- which is why every such card in the Open Project grid
    fell back to the grey placeholder icon while the project itself opened and
    displayed perfectly well.

    Deliberately the region endpoint rather than `overview`, even though
    overview is one round trip instead of two and comes back already encoded.
    Overview bytes are quantized against the channel's own window -- (0, the
    full-resolution maximum) -- because the viewer applies the contrast slider
    ON TOP of them. As a finished picture that window is the wrong one: a
    single hot pixel anywhere in the plane makes the whole thumbnail black.
    Reading numbers and stretching them here is what makes a thumbnail look
    like the tissue, and it is the stretch the local path already used.

    It is also the cheaper ask. Computing that window costs the node a read of
    a full-resolution plane, which is not a thing to trigger from a grid of
    cards somebody is only browsing.
    """
    from plexora.server.providers import http as node_http
    from plexora.server.providers.node import NodeImageProvider

    provider = NodeImageProvider(binding).with_channels(
        project.image.channel_names)
    try:
        # Nobody is waiting on a thumbnail, so it does not retry -- see
        # http.speculative() -- and it does not wait long.
        with node_http.speculative():
            shapes = provider.geometry(
                timeout=_NODE_THUMBNAIL_TIMEOUT).get('level_shapes') or []
            level = _thumbnail_level(shapes)
            if level is None:
                return None
            height, width = shapes[level]
            array, _ = provider.read_region(
                level, (0, 0, width, height), [0],
                max_pixels=_NODE_THUMBNAIL_PIXELS,
                timeout=_NODE_THUMBNAIL_TIMEOUT)
    except Exception:
        # A card, not a page. A node that is down, forgotten, disconnected or
        # simply slow gets the placeholder a project with no image gets, and
        # the rest of the grid draws.
        return None
    return array[0] if array.ndim == 3 else array


def _thumbnail_level(level_shapes):
    """Which pyramid level to make a thumbnail from, or None.

    `level_shapes` is [[height, width], ...] finest first, as the node reports
    it -- every level's own dimensions rather than width >> level, because real
    pyramids are not all exact halvings.

    Coarsest first, so the answer is the smallest level that is still worth
    looking at: both dims >= 200, the same band the local path picks from. The
    budget is the part the local path does not need, and it is checked before
    the >= 200 rule rather than after -- a level too big to carry is not a
    candidate at all, however good it would look.
    """
    coarsest_first = range(len(level_shapes) - 1, -1, -1)
    affordable = [index for index in coarsest_first
                  if level_shapes[index][0] * level_shapes[index][1]
                  <= _NODE_THUMBNAIL_PIXELS]
    for index in affordable:
        if all(d >= 200 for d in level_shapes[index]):
            return index
    # Nothing in that band fits, so the coarsest level that does. Mushy beats
    # absent; only an image whose smallest level is itself over the budget --
    # a big one written with no pyramid at all -- gets no thumbnail.
    return affordable[0] if affordable else None


def get_ome_metadata(datasource_name):
    global metadata
    # `metadata` is {} (not None) when the OME-XML fails to parse, so it was
    # never a reliable loadedness signal either -- use _loaded_source.
    if _loaded_source != loaded_scope(datasource_name):
        load_datasource(datasource_name)
    return metadata


def _image_channel_stem(filePath):
    """The base a generated channel key is built from, for any image format.

    `_parse_channel` reads the trailing "_<N>" off a channel key to get the
    channel index, so every format has to arrive at one of these -- the suffix
    vocabulary is the only part that differs.
    """
    return re.sub(
        r'\.ome\.tiff|\.ome\.tif|\.ome\.zarr|\.tiff|\.tif|\.zarr|\.png'
        r'|\.qptiff|\.svs|\.ndpi|\.scn|\.mrxs|\.bif|\.svslide|\.jpeg|\.jpg'
        r'|\.dcm|\.dicom',
        '', Path(filePath).name, flags=re.IGNORECASE)


def _convert_zarr_image(filePath, dataDirectory=None, progress_callback=None):
    """`convertOmeTiff`'s image branch for an OME-Zarr store.

    The same facts, read from NGFF metadata instead of TIFF tags -- plus the one
    thing a TIFF import never has to decide. A store may legally carry a single
    resolution level, and serving one means decoding the full-resolution plane
    for every zoomed-out tile; so when the levels that exist do not reach far
    enough out, the missing coarse ones are derived into the project directory
    once, here, and recorded for `LocalImageProvider` to open alongside the
    source. Only the missing ones: level 0 is never copied.

    `dataDirectory` is where they go. Without one (a caller that only wants the
    geometry) nothing is derived and the store is served as it stands.
    """
    from plexora.server.utils import ome_zarr

    pyramid = ome_zarr.open_image(filePath)
    extension = None
    if dataDirectory and ome_zarr.needs_extension(pyramid):
        extension = ome_zarr.build_extension(
            pyramid, ome_zarr.extension_path(dataDirectory),
            progress_callback=progress_callback)
        if extension:
            pyramid = ome_zarr.open_image(filePath, extension=extension)

    stem = _image_channel_stem(filePath)
    num_channels = int(pyramid[0].shape[0])
    channel_info = {
        'maxLevel': len(pyramid),
        # The virtual 1024 grid, exactly as the TIFF group branch below uses:
        # a tile request is a slice, and it does not have to land on a chunk.
        'tileHeight': 1024,
        'tileWidth': 1024,
        'height': int(pyramid[0].shape[-2]),
        'width': int(pyramid[0].shape[-1]),
        'num_channels': num_channels,
        'channel_names': [f"{stem}_{i}" for i in range(num_channels)],
        'image_kind': 'ome_zarr',
    }
    if extension:
        channel_info['imagePyramid'] = str(extension)
        channel_info['imagePyramidKey'] = \
            segmentation_pyramid.source_fingerprint(filePath)
    return channel_info


def _convert_brightfield_image(filePath, dataDirectory=None, progress_callback=None,
                               detection=None, as_fluorescence=False):
    """`convertOmeTiff`'s image branch for a file whose pixels are RGB.

    Two outcomes, one reader. Brightfield -- the normal one -- records
    `image_kind='brightfield'` and a single servable layer keyed `rgb`, which
    is what makes the viewer draw true colour instead of three additive
    channels on black. Fluorescence, reached only when the user has overridden
    the detector, records an ordinary `ome_tiff` project of three channels:
    the same pyramid, read through the same CYX views, by code that never
    learns the samples were interleaved. That is the honest reading of "this
    file is three markers that happen to be stored as RGB", and it is why the
    override needs no separate pipeline.

    `num_channels` is 3 in both cases -- it counts the planes the pyramid has,
    which is what a node's geometry check compares against. How many layers
    the *viewer* draws is `channel_names`, and for brightfield that is one.
    """
    pyramid = brightfield.open_rgb(filePath)
    extension = None
    if dataDirectory and brightfield.needs_extension(pyramid):
        # Only a slide written with no usable pyramid gets here: `open_rgb`
        # serves a virtual halving chain off whatever levels the file has, and
        # a scanner's own pyramid covers the whole zoom range. So importing a
        # 300 MB SVS writes nothing, and importing a flat 40000px RGB TIFF
        # derives the coarse levels once.
        extension = brightfield.build_extension(
            pyramid, brightfield.extension_path(dataDirectory),
            progress_callback=progress_callback)
        if extension:
            pyramid = brightfield.open_rgb(filePath, extension=extension)

    stem = _image_channel_stem(filePath)
    channel_info = {
        'maxLevel': len(pyramid),
        'tileHeight': brightfield.TILE_SIZE,
        'tileWidth': brightfield.TILE_SIZE,
        'height': int(pyramid[0].shape[-2]),
        'width': int(pyramid[0].shape[-1]),
        'num_channels': int(pyramid[0].shape[0]),
    }
    if as_fluorescence:
        channel_info['channel_names'] = [f"{stem}_{i}" for i in range(3)]
        channel_info['image_kind'] = 'ome_tiff'
    else:
        channel_info['channel_names'] = [brightfield.RGB_CHANNEL_KEY]
        channel_info['image_kind'] = brightfield.BRIGHTFIELD
    if detection is not None:
        channel_info['imageTypeDetected'] = detection.verdict
        channel_info['imageTypeReason'] = detection.reason
        channel_info['imageTypeConfidence'] = detection.confidence
    if extension:
        channel_info['imagePyramid'] = str(extension)
        channel_info['imagePyramidKey'] = \
            segmentation_pyramid.source_fingerprint(filePath)
    return channel_info


def _convert_dicom_image(filePath, dataDirectory=None, progress_callback=None,
                         detection=None, as_brightfield=False):
    """`convertOmeTiff`'s image branch for a DICOM whole-slide image.

    Two outcomes and one reader, the same shape as `_convert_brightfield_image`
    -- but the choice is made further up, because a DICOM slide states how many
    samples its pixels have and that is not negotiable. A monochrome multiplex
    slide read "as brightfield" would have to borrow three of its markers and
    call them red, green and blue, so that combination is refused rather than
    served wrong; the override is still meaningful in the other direction and
    for a colour slide, which reads as three planes exactly as an RGB TIFF
    does.

    `num_channels` counts the planes the pyramid has -- 36 for a t-CyCIF slide,
    3 for H&E -- which is what a node's geometry check compares against. How
    many layers the *viewer* draws is `channel_names`, and for brightfield that
    is one.
    """
    from plexora.server.utils import dicom_wsi

    pyramid = dicom_wsi.open_image(filePath, rgb=as_brightfield)
    if as_brightfield and not pyramid.is_color:
        paths = int(pyramid[0].shape[0])
        raise ValueError(
            f"This DICOM slide is monochrome with {paths} optical "
            f"path{'s' if paths != 1 else ''}, so it cannot be read as one "
            "colour image -- an optical path is a marker, not the red, green "
            "or blue of a camera. Leave the image type on Auto or "
            "Fluorescence.")

    extension = None
    if dataDirectory and dicom_wsi.needs_extension(pyramid):
        # Rare for DICOM: the format exists to be tiled, and an exporter that
        # wrote a pyramid at all wrote the whole chain. This covers the slide
        # saved at a single resolution, where every zoomed-out tile would
        # otherwise resample the entire slide.
        extension = dicom_wsi.build_extension(
            pyramid, dicom_wsi.extension_path(dataDirectory),
            progress_callback=progress_callback)
        if extension:
            pyramid = dicom_wsi.open_image(filePath, extension=extension,
                                           rgb=as_brightfield)

    stem = _image_channel_stem(filePath)
    num_channels = int(pyramid[0].shape[0])
    channel_info = {
        'maxLevel': len(pyramid),
        'tileHeight': dicom_wsi.TILE_SIZE,
        'tileWidth': dicom_wsi.TILE_SIZE,
        'height': int(pyramid[0].shape[-2]),
        'width': int(pyramid[0].shape[-1]),
        'num_channels': num_channels,
    }
    if pyramid.is_color and as_brightfield:
        channel_info['channel_names'] = [brightfield.RGB_CHANNEL_KEY]
        channel_info['image_kind'] = brightfield.BRIGHTFIELD
    else:
        # Either a fluorescence slide (N optical paths, N layers) or a colour
        # one the user overrode to fluorescence -- the same three planes the
        # `.rgb` seam reads, presented through the same CYX views by code that
        # never learns the samples were interleaved.
        channel_info['channel_names'] = [f"{stem}_{i}" for i in range(num_channels)]
        channel_info['image_kind'] = dicom_wsi.IMAGE_KIND
    # Reported even when it is 1, alongside `imageTypeConfidence` and on the
    # same terms: what the conversion learned, for whoever asked it. `ImageSpec`
    # stores neither, so the fact a person needs -- that this slide has more
    # than one focal plane and they are seeing the middle one -- travels in the
    # detection reason, which the edit page shows. Z is pinned there (see
    # dicom_wsi.open_image) and never becomes channels.
    channel_info['focalPlanes'] = int(pyramid.focal_plane_count)
    if detection is not None:
        channel_info['imageTypeDetected'] = detection.verdict
        channel_info['imageTypeReason'] = detection.reason
        channel_info['imageTypeConfidence'] = detection.confidence
    if extension:
        channel_info['imagePyramid'] = str(extension)
        channel_info['imagePyramidKey'] = \
            segmentation_pyramid.source_fingerprint(filePath)
    return channel_info


def convertOmeTiff(filePath, channelFilePath=None, dataDirectory=None, isLabelImg=False,
                   progress_callback=None, segmentation_mode_=segmentation_pyramid.DEFAULT_MODE,
                   image_type=None, stage_callback=None):
    """What registering an image records about it.

    `image_type` is the user's override -- 'brightfield', 'fluorescence', or
    None for "decide". It changes how the file is READ, never what is in it:
    nothing here writes to `filePath`, and re-registering under the other
    answer produces the other reading of the same bytes.
    """
    channel_info = {}
    channelNames = []

    # image is a normal channel?
    if isLabelImg == False:
        from plexora.server.utils import dicom_wsi, ome_zarr

        if ome_zarr.is_zarr_image_path(filePath):
            return _convert_zarr_image(filePath, dataDirectory, progress_callback)

        # Before the TIFF detector, which would try to open a .dcm as a TIFF.
        # DICOM states its own acquisition mode in every instance header, so
        # detection here is a metadata read rather than the ladder of guesses
        # a TIFF needs -- but it produces the same Detection, flows through the
        # same override, and records the same three fields.
        if dicom_wsi.is_dicom_path(filePath):
            detection = dicom_wsi.detect_image_type(filePath)
            effective = image_type or detection.verdict
            return _convert_dicom_image(
                filePath, dataDirectory, progress_callback, detection=detection,
                as_brightfield=(effective == brightfield.BRIGHTFIELD))

        # Detected even when the user has already chosen, so the edit page can
        # show what the file looks like next to what was asked for -- and so a
        # project imported under an override still records why the override was
        # or was not the obvious answer.
        detection = brightfield.detect_image_type(filePath)
        effective = image_type or detection.verdict
        if brightfield.is_rgb_layout(filePath):
            # Interleaved samples cannot be read as (channel, y, x) by the
            # branch below -- it would record the image's height as its channel
            # count -- so this path is taken for the fluorescence override too.
            return _convert_brightfield_image(
                filePath, dataDirectory, progress_callback, detection=detection,
                as_fluorescence=(effective != brightfield.BRIGHTFIELD))
        channel_io = tf.TiffFile(str(filePath), is_ome=False)
        if (effective == brightfield.BRIGHTFIELD
                and int(channel_io.series[0].shape[0]) >= 3):
            # A planar file the user (or the OME metadata) calls brightfield:
            # three separate planes that mean red, green and blue. Same reader,
            # which handles planar sources as well as interleaved ones. Guarded
            # on the plane count because a brightfield reading of a two-plane
            # image has nothing to put in the third sample.
            channel_io.close()
            return _convert_brightfield_image(
                filePath, dataDirectory, progress_callback, detection=detection)

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
        stem = _image_channel_stem(filePath)
        for i in range(shape[0]):
            channelNames.append(f"{stem}_{i}")
        channel_info['channel_names'] = channelNames
        channel_info['image_kind'] = 'ome_tiff'
        return channel_info

    # segmentation mask. `channelFilePath` is accepted for call-site
    # compatibility but no longer read: the mask's own geometry is all the
    # conversion needs, and opening the (much larger) channel image here just
    # to discard it cost a file handle per import.
    else:
        write_path = resolve_outline_segmentation(
            filePath, dataDirectory, progress_callback=progress_callback,
            mode=segmentation_mode_, stage_callback=stage_callback,
        )
        return {'segmentation': write_path}


_segmentation_jobs = {}
_segmentation_job_locks = {}
_segmentation_job_locks_guard = threading.Lock()


def _segmentation_job_lock_for(datasource_name):
    with _segmentation_job_locks_guard:
        if datasource_name not in _segmentation_job_locks:
            _segmentation_job_locks[datasource_name] = threading.Lock()
        return _segmentation_job_locks[datasource_name]


def _patch_config_segmentation(datasource_name, segmentation_path, status,
                               segmentation_source=None, config_file=None):
    # The same lock every other writer takes -- this runs on the segmentation
    # job's thread, so it can land on top of a request saving an edit.
    #
    # `config_file` is captured when the job STARTS and passed in, never
    # re-resolved here. This runs minutes later on a background thread, and
    # `Project.config_path_for` answers "which registry holds this name right
    # now" -- which is a different question by then if the data directory has
    # moved, and in a test suite is a different directory entirely. A job
    # patches the registry it was started from or it patches nothing.
    config_file = config_file or Project.config_path_for(datasource_name)
    if not paths.is_writable(config_file.parent):
        # A shared project's registry entry is not ours to patch. The derived
        # mask still went to a root this user can write (see
        # paths.derived_root); what cannot be recorded is the pointer to it,
        # so the conversion is redone next time rather than silently half-done.
        return
    with config_transaction():
        cfg = read_config(config_file)
        if datasource_name not in cfg:
            return  # datasource was deleted while the job was running
        entry = cfg[datasource_name]
        entry['segmentation'] = segmentation_path
        entry['segmentation_status'] = status
        if segmentation_source is not None:
            # Record which source this derived file came from, so a later load
            # can confirm it is still current with a stat rather than by
            # re-deriving or re-sampling pixels (see refresh_segmentation_mapping).
            entry['segmentationSource'] = str(segmentation_source)
            entry['segmentationSourceKey'] = segmentation_pyramid.source_fingerprint(
                segmentation_source
            )
        write_config(config_file, cfg)



# --------------------------------------------------------------------------
# Staged progress
#
# A percentage on its own cannot describe this job honestly. The tile loop is
# the only part with a countable total, and everything before it -- deciding
# whether the supplied mask is servable as-is (which in outline mode reads
# sampled pixel windows), adopting a pyramid somebody already derived, probing
# shape and dtype, and above all the full-plane read, measured at 60 s for the
# Orion mask and 179 s streaming -- ran with the bar reading zero. Users read
# that as a hang, and said so.
#
# So each stage owns a BAND of the bar. A stage with a countable total maps its
# own fraction into its band; a stage without one still moves the bar to the
# foot of its band and names itself. The bar therefore leaves zero on the first
# tick and never sits at one number for minutes without saying what it is
# doing. Bands are ordered and contiguous, and the last one ends at 100.
# --------------------------------------------------------------------------

#: stage key -> (start %, end %, what to tell the user)
SEGMENTATION_STAGES = {
    "loading": (0, 4, "Opening the segmentation mask"),
    "inspecting": (4, 10, "Checking the mask's dimensions"),
    "preparing": (10, 30, "Reading the mask"),
    "building": (30, 92, "Building the tiled pyramid"),
    "writing": (92, 99, "Writing the pyramid"),
}


def _staged_reporter(stages, on_change):
    """Turn stage names and per-stage fractions into one monotone percentage.

    `on_change(percent, stage, message)` is called only when the integer
    percent or the stage actually changes -- the tile loop reports once per
    written tile, which is thousands of calls on a large pyramid.

    Returns `(stage, report)`: `stage(key, detail=None)` enters a stage, and
    `report(done, total)` moves within the current one.
    """
    state = {"key": None, "percent": -1}

    def emit(percent, key, detail=None):
        percent = max(0, min(99, int(percent)))
        # Monotone: a stage that reports fewer tiles than the last tick, or a
        # band entered late, must never walk the bar backwards.
        percent = max(percent, state["percent"])
        if percent == state["percent"] and key == state["key"]:
            return
        state["percent"], state["key"] = percent, key
        label = stages.get(key, (0, 0, key))[2]
        on_change(percent, key, f"{label} ({percent}%)" if detail is None else detail)

    def stage(key, detail=None):
        start = stages.get(key, (0, 0, ""))[0]
        emit(start, key, detail)

    def report(done, total):
        key = state["key"] or next(iter(stages))
        start, end, _ = stages.get(key, (0, 100, ""))
        fraction = (done / total) if total else 0.0
        emit(start + (end - start) * max(0.0, min(1.0, fraction)), key)

    return stage, report



#: The phases of preparing a project's cell table, as bands of one bar. Same
#: shape and same reasoning as SEGMENTATION_STAGES: `metadata` walks a
#: multi-image table's obs and `preparing` reads the marker matrix a block at a
#: time, and neither used to say anything at all -- the form simply sat there,
#: which is what made "Still reading the data file" the only feedback a user
#: ever got out of this path.
TABLE_STAGES = {
    "opening": (0, 5, "Opening the data file"),
    "metadata": (5, 25, "Reading cell metadata"),
    "preparing": (25, 80, "Loading marker values"),
    "loading": (80, 95, "Indexing cells"),
    "finalizing": (95, 99, "Finishing up"),
}

_table_jobs = {}


def table_progress(datasource_name):
    """A (stage, report) pair that writes into `datasource_name`'s job record.

    The record is polled by the browser WHILE the request that is doing the
    work is still outstanding -- the load stays synchronous, so the routes keep
    validating a user's answer and restoring the previous project exactly as
    they did. All that is added is somewhere for the work to say where it has
    got to, and waitress is multi-threaded, so the poll is answered while the
    save is still running.
    """
    def on_change(percent, key, message):
        _table_jobs[datasource_name] = {
            "status": "pending",
            "progress": percent,
            "stage": key,
            "stage_label": TABLE_STAGES.get(key, (0, 0, key))[2],
            "message": message,
            "error": None,
        }

    return _staged_reporter(TABLE_STAGES, on_change)


def finish_table_job(datasource_name, error=None):
    _table_jobs[datasource_name] = {
        "status": "error" if error else "ready",
        "progress": 100,
        "stage": "failed" if error else "ready",
        "stage_label": "Failed" if error else "Ready",
        "message": str(error) if error else "Cell table ready",
        "error": str(error) if error else None,
    }


def get_table_job_status(datasource_name):
    """Where the table load has got to, or "ready" when nothing is running.

    "ready" for an unknown datasource on purpose: the browser starts polling
    the moment it posts, and may well ask before the server thread doing the
    work has reached its first stage. An absent record means "nothing to
    report", never "something went wrong".
    """
    return _table_jobs.get(datasource_name) or {
        "status": "ready", "progress": 100, "stage": "ready",
        "stage_label": "Ready", "message": None, "error": None,
    }


def start_segmentation_job(datasource_name, label_file, data_directory,
                           mode=segmentation_pyramid.DEFAULT_MODE):
    """Convert a label mask into the layer the viewer draws, on a background
    thread, so the request that triggers it never blocks on the conversion.

    That conversion is tens of seconds on a large mask, so both import flows
    start it the moment their first form page is submitted and then report
    progress out of get_segmentation_job_status() while the user works through
    the second page.
    """
    lock = _segmentation_job_lock_for(datasource_name)
    if not lock.acquire(blocking=False):
        return  # already running for this datasource
    # Resolved NOW, on the request's thread, while the data root is still the
    # one this project was opened from. See _patch_config_segmentation.
    config_file = Project.config_path_for(datasource_name)
    # Reading this costs a header parse, and it is the only chance to explain
    # *why* a mask the user thinks is ready is being converted anyway.
    work = describe_segmentation_work(label_file, mode)
    _segmentation_jobs[datasource_name] = {
        "status": "pending",
        "error": None,
        "progress": 0,
        "stage": "loading",
        "stage_label": SEGMENTATION_STAGES["loading"][2],
        # `work` says WHY a mask the user thinks is ready is being converted
        # anyway, so it leads; the stage says what is happening right now.
        "message": work,
    }

    def _run():
        def on_change(percent, key, message):
            record = _segmentation_jobs.get(datasource_name)
            if record is None:
                return
            record["progress"] = percent
            record["stage"] = key
            record["stage_label"] = SEGMENTATION_STAGES.get(key, (0, 0, key))[2]
            record["message"] = message

        stage, report = _staged_reporter(SEGMENTATION_STAGES, on_change)

        try:
            stage("loading")
            result = convertOmeTiff(
                label_file,
                dataDirectory=data_directory,
                isLabelImg=True,
                progress_callback=report,
                stage_callback=stage,
                segmentation_mode_=mode,
            )
            _segmentation_jobs[datasource_name] = {
                "status": "ready",
                "error": None,
                "segmentation": result["segmentation"],
                "progress": 100,
                "stage": "ready",
                "stage_label": "Ready",
                "message": "Segmentation mask ready",
            }
            _patch_config_segmentation(
                datasource_name, result["segmentation"], "ready",
                segmentation_source=label_file, config_file=config_file,
            )
            # Reloaded only if this is still the project being viewed AND the
            # registry it was started against is still the one in force. This
            # job outlives a great deal -- a delete, a switch to another
            # project, a change of data directory -- and a background thread
            # that reloads "the project called X" against whatever registry
            # happens to be current is a thread that can adopt somebody else's.
            if (source == datasource_name
                    and Project.config_path_for(datasource_name) == config_file
                    and Project.find(datasource_name) is not None):
                load_datasource(datasource_name, reload=True)
        except Exception as exc:
            _segmentation_jobs[datasource_name] = {
                "status": "error",
                "error": str(exc),
                "progress": 0,
                "stage": "failed",
                "stage_label": "Failed",
                "message": "Segmentation mask failed",
            }
            _patch_config_segmentation(
                datasource_name, None, "error", segmentation_source=label_file,
                config_file=config_file,
            )
        finally:
            lock.release()

    threading.Thread(target=_run, daemon=True).start()


def get_segmentation_job_status(datasource_name):
    if datasource_name in _segmentation_jobs:
        return _segmentation_jobs[datasource_name]
    # Fall back to config.json's persisted status (server restarted mid-job,
    # or this process never ran the job -- e.g. multi-worker deployment).
    entry = get_config().get(datasource_name, {})
    status = entry.get("segmentation_status", "ready")
    return {
        "status": status,
        "error": None,
        "progress": 100 if status == "ready" else 0,
        # A restarted server has no record of which phase the job was in, so it
        # says so rather than guessing: the client shows an unnamed stage
        # instead of asserting one that may be minutes out of date.
        "stage": "ready" if status == "ready" else None,
        "stage_label": "Ready" if status == "ready" else None,
        "message": None,
        # Reported here as well as in the in-memory record above, because the
        # viewer takes the finished mask on without reloading the page and needs
        # the path to do it. This branch is the one a restarted server serves,
        # and a viewer that got no path here would have no way to pick the mask
        # up short of the reload this replaced.
        "segmentation": entry.get("segmentation"),
    }


def logTransform(csvPath, skip_columns=[]):
    df = pl.read_csv(csvPath)
    transform_cols = [c for c in df.columns if c not in skip_columns]
    df = df.with_columns([pl.col(c).log1p().alias(c) for c in transform_cols])
    df.write_csv(csvPath)

